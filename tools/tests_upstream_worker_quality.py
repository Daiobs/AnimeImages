"""Offline real-registry and engine contracts; no live support claims."""

import http.client
import gzip
import io
import json
import socket
import time
import unittest
from unittest.mock import patch

from tools import upstream_match_worker as worker


class WorkerQualityTests(unittest.TestCase):
    def setUp(self):
        self.network = patch.object(socket.socket, "connect", side_effect=AssertionError("offline"))
        self.network.start()
        self.addCleanup(self.network.stop)

    def test_actual_installed_registry_examples(self):
        counts = {}
        for provider in worker.PROVIDERS:
            matched = set()
            for cls in worker.extractor_classes(provider):
                if not worker.eligible(provider, cls, allow_mature=True):
                    continue
                if provider == "yt_dlp_generic":
                    cls = getattr(cls, "real_class", cls)
                    cases = getattr(cls, "_TESTS", []) or [getattr(cls, "_TEST", {})]
                    examples = [case.get("url", "") for case in cases]
                else:
                    examples = [getattr(cls, "example", "")]
                for example in examples:
                    try:
                        worker.validate_url(example)
                        if worker.suitable(provider, cls, example):
                            matched.add(cls.__name__)
                            break
                    except (ValueError, TypeError):
                        pass
            self.assertGreaterEqual(len(matched), 100, provider)
            counts[provider] = len(matched)
        print("offline_registry_matching_classes=" + json.dumps(counts, sort_keys=True))

    def test_mature_opt_in_and_wrong_source(self):
        from yt_dlp.extractor.youtube import YoutubeIE, YoutubeTabIE
        from gallery_dl.extractor.wikiart import WikiartImageExtractor
        for cls in (YoutubeIE, YoutubeTabIE):
            self.assertFalse(worker.eligible("yt_dlp_generic", cls))
            self.assertTrue(worker.eligible("yt_dlp_generic", cls, allow_mature=True))
        self.assertFalse(worker.eligible("gallery_dl_generic", WikiartImageExtractor, allow_mature=True))

    def test_version_and_flag_fail_closed(self):
        with patch.object(worker, "package_identity", return_value={"version": "pinned"}):
            with self.assertRaisesRegex(ValueError, "version_changed"):
                worker.handle({"provider_id": "yt_dlp_generic", "action": "match", "version": "old"})
            with self.assertRaisesRegex(ValueError, "invalid_request"):
                worker.handle({"provider_id": "yt_dlp_generic", "action": "match",
                               "version": "pinned", "allow_mature": "true"})

    def test_normal_ytdlp_delegation_preserves_unknown_tracks(self):
        from yt_dlp import YoutubeDL
        from yt_dlp.extractor.common import InfoExtractor

        class ChildIE(InfoExtractor):
            _VALID_URL = r"https://example.org/child/(?P<id>\d+)"

            def _real_extract(self, url):
                return {"id": "1", "title": "fixture", "formats": [
                    {"url": "https://example.org/unknown.mp4", "ext": "mp4"},
                    {"url": "https://example.org/audio.m4a", "vcodec": "none"},
                    {"url": "https://example.org/video.mp4", "acodec": "none"},
                    {"url": "https://example.org/hls.m3u8", "protocol": "m3u8_native"},
                ]}

        class ParentIE(InfoExtractor):
            _VALID_URL = r"https://example.org/root/(?P<id>\d+)"

            def _real_extract(self, url):
                return self.url_result("https://example.org/child/1", ChildIE)

        with patch.object(YoutubeDL, "add_default_info_extractors",
                          lambda ydl: ydl.add_info_extractor(ChildIE(ydl))):
            result = worker.parse_ytdlp(ParentIE, "https://example.org/root/1", 3)
        self.assertEqual(len(result["formats"]), 4)
        unknown = next(item for item in result["formats"] if "unknown" in item["url"])
        self.assertIsNone(unknown["upstream_track_evidence"]["acodec"])
        self.assertNotIn("upstream_bmff_track_evidence", unknown)

    def test_ytdlp_cycle(self):
        from yt_dlp.extractor.common import InfoExtractor

        class CycleIE(InfoExtractor):
            _VALID_URL = r"https://example.org/(?P<id>\d+)"

            def _real_extract(self, url):
                return self.url_result(url, CycleIE)

        with self.assertRaisesRegex(ValueError, "cycle_detected"):
            worker.parse_ytdlp(CycleIE, "https://example.org/1", 3)

    def test_ytdlp_lazy_collection_stops_and_preserves_partial(self):
        from yt_dlp.extractor.common import InfoExtractor
        consumed = []

        class CollectionIE(InfoExtractor):
            _VALID_URL = r"https://example.org/(?P<id>\d+)"

            def _real_extract(self, url):
                def entries():
                    for index in range(1000):
                        consumed.append(index)
                        yield {"id": str(index), "title": "fixture",
                               "url": "https://example.org/file.mp4"}
                return self.playlist_result(entries(), "collection")

        with patch.object(worker, "MAX_ITEMS", 2):
            result = worker.parse_ytdlp(CollectionIE, "https://example.org/1", 3)
        self.assertEqual(consumed, [0, 1])
        self.assertEqual(len(result["entries"]), 2)
        self.assertEqual(result["upstream_scope"]["completion"], "unknown")

    def test_nested_collections_bound_all_nodes_and_depth(self):
        from yt_dlp.extractor.common import InfoExtractor
        consumed = []

        class TreeIE(InfoExtractor):
            _VALID_URL = r"https://example.org/(?P<id>\d+)"

            def _real_extract(self, url):
                def tree(depth):
                    def children():
                        for _ in range(2):
                            consumed.append(depth)
                            yield tree(depth + 1)
                    return {"_type": "playlist", "id": str(depth), "entries": children()}
                return tree(0)

        for depth, reason in ((2, "hop_limit"), (64, "node_limit")):
            consumed.clear()
            with patch.object(worker, "MAX_ITEMS", 2), patch.object(worker, "MAX_HOPS", depth):
                result = worker.parse_ytdlp(TreeIE, "https://example.org/1", 3)
            self.assertLessEqual(len(consumed), 2)
            self.assertEqual(result["upstream_scope"]["reason"], reason)

    def test_collection_metadata_charged_before_visiting_children(self):
        from yt_dlp.extractor.common import InfoExtractor
        visited = []

        class MetadataIE(InfoExtractor):
            _VALID_URL = r"https://example.org/(?P<id>\d+)"

            def _real_extract(self, url):
                def children():
                    visited.append(True)
                    yield {"id": "1", "url": "https://example.org/file.mp4"}
                return self.playlist_result([{
                    "_type": "playlist", "id": "child", "description": "x" * 5000,
                    "entries": children()}])

        with patch.object(worker, "MAX_OUTPUT", 20000):
            result = worker.parse_ytdlp(MetadataIE, "https://example.org/1", 3)
        self.assertEqual(visited, [])
        self.assertEqual(result["entries"], [])
        self.assertEqual(result["upstream_scope"]["reason"], "output_limit")

    def test_format_overflow_retains_candidates_with_scope(self):
        from yt_dlp.extractor.common import InfoExtractor

        class FormatsIE(InfoExtractor):
            _VALID_URL = r"https://example.org/(?P<id>\d+)"

            def _real_extract(self, url):
                return {"id": "1", "formats": [
                    {"url": "https://example.org/video.mp4", "format_id": str(i)}
                    for i in range(1001)]}

        result = worker.parse_ytdlp(FormatsIE, "https://example.org/1", 3)
        self.assertEqual(len(result["formats"]), 1000)
        self.assertEqual(result["upstream_scope"]["format_total"], 1001)
        self.assertEqual(result["upstream_scope"]["format_retained"], 1000)
        self.assertIsNone(result["formats"][0]["upstream_track_evidence"]["acodec"])

    def test_decoded_gzip_budget_actual_urllib_and_urllib3(self):
        from yt_dlp.networking._urllib import HTTPHandler
        from urllib3.response import HTTPResponse
        compressed = gzip.compress(b"x" * 1_000_000)
        self.assertLess(len(compressed), 2000)
        with patch.object(worker, "MAX_BODY", 2000), worker.runtime_budget(1):
            with self.assertRaisesRegex(ValueError, "body_limit"):
                HTTPHandler.gz(compressed)
        with patch.object(worker, "MAX_BODY", 2000), worker.runtime_budget(1):
            with self.assertRaisesRegex(ValueError, "body_limit"):
                HTTPResponse(body=io.BytesIO(compressed), headers={"content-encoding": "gzip"},
                             preload_content=False).read()

    def test_decoded_budget_streaming_and_success(self):
        from urllib3.response import HTTPResponse
        content = b"known metadata " * 10
        with patch.object(worker, "MAX_BODY", 2000), worker.runtime_budget(1) as state:
            response = HTTPResponse(body=io.BytesIO(gzip.compress(content)),
                                    headers={"content-encoding": "gzip"}, preload_content=False)
            self.assertEqual(b"".join(response.stream(7)), content)
            self.assertEqual(state["decoded_bytes"], len(content))
        with patch.object(worker, "MAX_BODY", 2000), worker.runtime_budget(1):
            response = HTTPResponse(body=io.BytesIO(gzip.compress(b"x" * 1_000_000)),
                                    headers={"content-encoding": "gzip"}, preload_content=False)
            with self.assertRaisesRegex(ValueError, "body_limit"):
                list(response.stream(7))

    def test_gallery_queue_snapshot_and_cycle_partial(self):
        from gallery_dl.extractor.message import Message

        class Child:
            category = "fixture"
            pattern = r"https://example.org/child"

            def __init__(self, match):
                pass

            def __iter__(self):
                metadata = {"extension": "jpg"}
                yield Message.Url, "https://example.org/a.jpg", metadata
                metadata["extension"] = "png"
                yield Message.Queue, "https://example.org/child", {}

        class Root(Child):
            pattern = r"https://example.org/root"

            def __iter__(self):
                yield Message.Queue, "https://example.org/child", {"_extractor": Child}

        with patch.object(worker, "extractor_classes", return_value=[Root, Child]):
            result = worker.parse_gallery(Root, "https://example.org/root", 3)
        self.assertEqual(result[0][2]["extension"], "jpg")
        self.assertEqual(result[0][2]["upstream_scope"]["reason"], "cycle_detected")

    def test_actual_http_request_and_cookie_boundary(self):
        with patch.object(http.client.HTTPConnection, "putrequest"), \
                patch.object(http.client.HTTPConnection, "putheader") as header, \
                patch.object(worker, "MAX_REQUESTS", 2):
            with worker.runtime_budget(1) as state:
                conn = http.client.HTTPConnection("example.org")
                conn.putrequest("GET", "/one")
                conn.putheader("Cookie", "private")
                conn.putheader("Authorization", "private")
                conn.putheader("Accept", "application/json")
                self.assertEqual(header.call_count, 1)
                conn.putrequest("GET", "/two")
                with self.assertRaisesRegex(ValueError, "request_limit"):
                    conn.putrequest("GET", "/three")
                self.assertEqual(state["requests"], 3)

    def test_actual_http_redirect_cycle(self):
        class Response:
            status = 302
            target = "/two"

            def getheader(self, name):
                return self.target

        response = Response()
        with patch.object(http.client.HTTPConnection, "putrequest"), \
                patch.object(http.client.HTTPConnection, "getresponse", return_value=response):
            with worker.runtime_budget(1):
                conn = http.client.HTTPConnection("example.org")
                conn.putrequest("GET", "/one")
                conn.getresponse()
                conn.putrequest("GET", "/two")
                response.target = "/one"
                with self.assertRaisesRegex(ValueError, "cycle_detected"):
                    conn.getresponse()

    def test_actual_http_body_budget(self):
        class Socket:
            def makefile(self, *args):
                return io.BytesIO(b"HTTP/1.1 200 OK\r\nContent-Length: 10\r\n\r\n0123456789")

        with patch.object(worker, "MAX_BODY", 4), worker.runtime_budget(1):
            response = http.client.HTTPResponse(Socket())
            response.begin()
            with self.assertRaisesRegex(ValueError, "body_limit"):
                response.read()

    def test_actual_http_readinto_budget(self):
        class Socket:
            def makefile(self, *args):
                return io.BytesIO(b"HTTP/1.1 200 OK\r\nContent-Length: 10\r\n\r\n0123456789")

        with patch.object(worker, "MAX_BODY", 4), worker.runtime_budget(1):
            response = http.client.HTTPResponse(Socket())
            response.begin()
            with self.assertRaisesRegex(ValueError, "body_limit"):
                response.readinto(bytearray(10))

    def test_redirect_private_and_hop_limits(self):
        class Response:
            status = 302

            def getheader(self, name):
                return "http://127.0.0.1/private"

        with patch.object(http.client.HTTPConnection, "putrequest"), \
                patch.object(http.client.HTTPConnection, "getresponse", return_value=Response()):
            with worker.runtime_budget(1):
                conn = http.client.HTTPConnection("example.org")
                conn.putrequest("GET", "/one")
                with self.assertRaisesRegex(ValueError, "network_denied"):
                    conn.getresponse()

    def test_ytdlp_hop_budget(self):
        from yt_dlp.extractor.common import InfoExtractor

        class HopIE(InfoExtractor):
            _VALID_URL = r"https://example.org/(?P<id>\d+)"

            def _real_extract(self, url):
                return self.url_result("https://example.org/" + str(int(self._match_id(url)) + 1), HopIE)

        with patch.object(worker, "MAX_HOPS", 2):
            with self.assertRaisesRegex(ValueError, "hop_limit"):
                worker.parse_ytdlp(HopIE, "https://example.org/1", 3)

    def test_wall_clock_budget(self):
        with self.assertRaises(TimeoutError):
            with worker.runtime_budget(0.01):
                time.sleep(0.1)


if __name__ == "__main__":
    unittest.main()
