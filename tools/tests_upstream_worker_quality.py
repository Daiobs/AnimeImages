"""Offline real-registry and engine contracts; no live support claims."""

import http.client
import gzip
import io
import json
import socket
import signal
import time
import unittest
from unittest.mock import Mock, patch

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

    def test_worker_restores_one_bounded_bmff_probe(self):
        from yt_dlp.extractor.common import InfoExtractor

        class FixtureIE(InfoExtractor):
            _VALID_URL = r"https://example.org/(?P<id>1)"

            def _real_extract(self, url):
                return {"id": "1", "title": "fixture", "formats": [
                    {"url": "https://example.org/low.mp4", "height": 360},
                    {"url": "https://example.org/high.mp4", "height": 1080},
                    {"url": "https://example.org/known.mp4", "height": 2160,
                     "vcodec": "h264", "acodec": "aac"},
                    {"url": "https://example.org/split.mp4", "height": 4320,
                     "vcodec": "h264", "acodec": "none"},
                ]}

        evidence = {"schema": "iso_bmff_tracks_v1", "complete_moov": True,
                    "audio": "present", "video": "present", "reason": "complete_track_table"}
        request = {"provider_id": "yt_dlp_generic", "version": "fixture", "action": "parse",
                   "url": "https://example.org/1", "extractor_id": FixtureIE.ie_key(),
                   "timeout_seconds": 3}
        started = time.monotonic()
        with patch.object(worker, "package_identity", return_value={"version": "fixture"}), \
                patch.object(worker, "extractor_classes", return_value=[FixtureIE]), \
                patch("tools.upstream_metadata_probe.probe_in_guarded_child", return_value=evidence) as probe:
            result = worker.handle(request)
        probe.assert_called_once()
        self.assertEqual(probe.call_args.args, ("https://example.org/high.mp4",))
        self.assertLessEqual(probe.call_args.kwargs["deadline_monotonic"], started + 3.1)
        self.assertEqual(len(result["formats"]), 4)
        enriched = [fmt for fmt in result["formats"] if "upstream_bmff_track_evidence" in fmt]
        self.assertEqual(len(enriched), 1)
        self.assertEqual(enriched[0]["upstream_bmff_track_evidence"], evidence)
        self.assertIsNone(enriched[0]["upstream_track_evidence"]["acodec"])

    def test_bmff_probe_skips_collections_known_tracks_and_exhausted_budgets(self):
        def candidate(**fields):
            return {"url": "https://example.org/movie.mp4", "ext": "mp4", "protocol": "https",
                    "upstream_track_evidence": {"acodec": None, "vcodec": None}, **fields}

        budget = {"requests": 0, "bytes": 0, "decoded_bytes": 0}
        cases = [
            ({"_type": "playlist", "entries": [{"formats": [candidate()]}]}, budget, 3),
            ({"formats": [candidate(upstream_track_evidence={"acodec": "aac", "vcodec": "h264"})]}, budget, 3),
            ({"formats": [candidate(upstream_track_evidence={"acodec": "none", "vcodec": "h264"})]}, budget, 3),
            ({"formats": [candidate(protocol="m3u8_native")]}, budget, 3),
            ({"formats": [candidate(url="http://example.org/movie.mp4", protocol="http")]}, budget, 3),
            ({"formats": [candidate()]}, budget, -1),
            ({"formats": [candidate()]}, {**budget, "requests": worker.MAX_REQUESTS - 1}, 3),
            ({"formats": [candidate()]}, {**budget, "bytes": worker.MAX_BODY - 1}, 3),
            ({"formats": [candidate()]}, {**budget, "decoded_bytes": worker.MAX_BODY - 1}, 3),
        ]
        for payload, remaining, seconds in cases:
            with self.subTest(payload=payload, remaining=remaining, seconds=seconds), \
                    patch("tools.upstream_metadata_probe.probe_in_guarded_child") as probe:
                before = json.dumps(payload, sort_keys=True)
                worker.probe_ytdlp_result(payload, deadline=time.monotonic() + seconds, budget=remaining)
                probe.assert_not_called()
                self.assertEqual(json.dumps(payload, sort_keys=True), before)

    def test_worker_real_bmff_probe_shares_two_ranges_and_original_deadline(self):
        from tools import upstream_metadata_probe as bmff
        from yt_dlp.extractor.common import InfoExtractor

        class FixtureIE(InfoExtractor):
            _VALID_URL = r"https://example.org/(?P<id>1)"

            def _real_extract(self, url):
                return {"id": "1", "title": "fixture", "formats": [
                    {"url": "https://example.org/movie.mp4"},
                    {"url": "https://example.org/other.mp4"},
                ]}

        def box(kind, body):
            return (len(body) + 8).to_bytes(4, "big") + kind + body

        moov = box(b"moov", b"".join(box(b"trak", box(b"mdia", box(
            b"hdlr", bytes(8) + handler + bytes(12)))) for handler in (b"vide", b"soun")))
        data = box(b"mdat", bytes(bmff.MAX_BYTES - 8)) + moov
        requests = []

        def open_range(request, timeout):
            requests.append((request, timeout))
            start, end = map(int, request.get_header("Range").removeprefix("bytes=").split("-"))
            end = min(end, len(data) - 1)
            response = io.BytesIO(data[start:end + 1])
            response.status = 206
            response.headers = {"Content-Range": f"bytes {start}-{end}/{len(data)}"}
            response.geturl = lambda: request.full_url
            return response

        def guarded_connection(*args, **kwargs):
            raise AssertionError("offline")

        guarded_connection.__qualname__ = "install_network_guard.<locals>.connect"
        real_probe = bmff.probe_in_guarded_child
        deadlines = []

        def probe(url, *, deadline_monotonic):
            self.assertEqual(signal.getitimer(signal.ITIMER_REAL)[0], 0)
            deadlines.append(deadline_monotonic)
            return real_probe(url, deadline_monotonic=deadline_monotonic)

        request = {"provider_id": "yt_dlp_generic", "version": "fixture", "action": "parse",
                   "url": "https://example.org/1", "extractor_id": FixtureIE.ie_key(),
                   "timeout_seconds": 10}
        started = time.monotonic()
        with patch.object(worker, "package_identity", return_value={"version": "fixture"}), \
                patch.object(worker, "extractor_classes", return_value=[FixtureIE]), \
                patch.object(socket, "create_connection", guarded_connection), \
                patch("urllib.request.build_opener", return_value=Mock(open=open_range)), \
                patch.object(bmff, "probe_in_guarded_child", side_effect=probe):
            result = worker.handle(request)
        self.assertEqual(len(deadlines), 1)
        self.assertLessEqual(deadlines[0], started + 10.1)
        self.assertEqual(len(requests), 2)
        self.assertEqual({req.full_url for req, _ in requests}, {"https://example.org/movie.mp4"})
        self.assertTrue(all(0 < timeout <= 5 for _, timeout in requests))
        self.assertTrue(all(req.get_header("Accept-encoding") == "identity" for req, _ in requests))
        evidence = result["formats"][0]["upstream_bmff_track_evidence"]
        self.assertTrue(evidence["complete_moov"])
        self.assertEqual((evidence["audio"], evidence["video"], evidence["requests"]), ("present", "present", 2))
        self.assertLessEqual(evidence["bytes_read"], 2 * bmff.MAX_BYTES)
        self.assertNotIn("upstream_bmff_track_evidence", result["formats"][1])
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL)[0], 0)

    def test_bmff_request_failure_preserves_candidates_without_retrying_another(self):
        from tools.upstream_metadata_probe import probe_iso_bmff_tracks
        payload = {"formats": [{"url": f"https://example.org/{i}.mp4", "ext": "mp4",
                                "protocol": "https", "upstream_track_evidence": {}}
                               for i in range(3)]}
        reader = Mock(side_effect=OSError("offline fixture"))
        def probe(url, *, deadline_monotonic):
            return probe_iso_bmff_tracks(url, guarded_range_reader=reader,
                                         deadline_monotonic=deadline_monotonic)
        with patch("tools.upstream_metadata_probe.probe_in_guarded_child", side_effect=probe) as call:
            worker.probe_ytdlp_result(payload, deadline=time.monotonic() + 3,
                                     budget={"requests": 0, "bytes": 0, "decoded_bytes": 0})
        call.assert_called_once()
        reader.assert_called_once()
        self.assertEqual(len(payload["formats"]), 3)
        evidence = payload["formats"][0]["upstream_bmff_track_evidence"]
        self.assertFalse(evidence["complete_moov"])
        self.assertEqual(evidence["reason"], "request_failed")

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
                    "_type": "playlist", "id": "child", "http_headers": {str(i): "x" * 4000 for i in range(3)},
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

    def test_gallery_retention_is_not_old_hundred_message_cap(self):
        class Gallery:
            category = 'fixture'
            pattern = r'https://example.org/item'

            def __init__(self, match):
                pass

            def __iter__(self):
                yield (2, {'title': 'fixture'})
                for i in range(1005):
                    yield (3, f'https://example.org/{i}.jpg', {'extension': 'jpg', 'width': i + 1})

        result = worker.parse_gallery(Gallery, 'https://example.org/item', 3)
        media = [row for row in result if row[0] == 3]
        self.assertEqual(len(media), 1000)
        self.assertEqual(media[-1][2]['width'], 1000)
        self.assertIsNone(media[0][2]['upstream_scope']['unexpanded_count'])
        self.assertLess(len(json.dumps(result).encode()), worker.MAX_OUTPUT)

    def test_parsehub_compact_retention_preserves_tail_and_counts(self):
        import sys
        from types import SimpleNamespace
        from tools import upstream_parsehub_worker as parsehub

        class MediaRef:
            pass

        class ImageRef(MediaRef):
            def __init__(self, i):
                self.url = f'https://example.org/{i}.jpg'
                self.ext = 'jpg'
                self.width = i + 1

        class VideoRef(MediaRef):
            pass

        class ParseResult:
            raw_url = 'https://example.org/post/1'
            title = 'fixture'
            content = 'x' * 16000
            media = [ImageRef(i) for i in range(1005)]

        types = SimpleNamespace(MediaRef=MediaRef, ImageRef=ImageRef, VideoRef=VideoRef,
                                AniRef=type('AniRef', (MediaRef,), {}),
                                LivePhotoRef=type('LivePhotoRef', (MediaRef,), {}))
        with patch.dict(sys.modules, {'parsehub.types.media_ref': types,
                                      'parsehub.types.result': SimpleNamespace(ParseResult=ParseResult)}):
            result = parsehub.normalize_result(ParseResult(), ParseResult.raw_url)
            with patch.object(parsehub, 'MAX_BYTES', 20000):
                small = parsehub.normalize_result(ParseResult(), ParseResult.raw_url)
        self.assertEqual(len(result), 1000)
        self.assertEqual(result[-1][2]['width'], 1000)
        self.assertEqual(result[0][2]['upstream_scope']['unexpanded_count'], 5)
        self.assertLess(len(json.dumps(result).encode()), parsehub.MAX_BYTES)
        self.assertLess(len(small), 1000)
        self.assertLess(len(json.dumps(small).encode()), 20000)
        self.assertEqual(small[0][2]['upstream_scope']['unexpanded_count'], 1005 - len(small))

    def test_compact_descriptors_preserve_explicit_relationship_facts(self):
        from tools import upstream_parsehub_worker as parsehub
        facts = {'requires_merge': False, 'audio_asset_present': True, 'role': 'live_photo_video',
                 'work_id': 'work1', 'upstream_asset_id': 'motion1',
                 'relationships': [{'relationship_type': 'live_photo',
                                    'source_asset_id': 'still1', 'target_asset_id': 'motion1'}]}
        self.assertEqual({key: worker.compact_descriptor(facts)[key] for key in facts}, facts)
        self.assertEqual({key: parsehub.scalar_fields(facts)[key] for key in facts}, facts)

    def test_explicit_track_states_and_upstream_role_survive_compaction(self):
        from types import SimpleNamespace
        from tools import upstream_parsehub_worker as parsehub
        for state in ('present', 'absent', 'unknown'):
            fields = {'audio_tracks': state, 'video_tracks': state,
                      'upstream_role': 'live_photo_video'}
            self.assertEqual(worker.compact_descriptor(fields), fields)
            self.assertEqual(parsehub.scalar_fields(fields), fields)
            self.assertEqual(parsehub.scalar_fields(SimpleNamespace(**fields)), fields)
        for compact in (worker.compact_descriptor, parsehub.scalar_fields):
            self.assertNotIn('audio_tracks', compact({}))
            self.assertNotIn('video_tracks', compact({}))

    def test_compaction_preserves_exact_long_resource_identity(self):
        url = 'https://example.org/media?signature=' + 'x' * 5000
        fields = {'url': url, 'manifest_url': url, 'format_id': 'f' * 400,
                  'work_id': 'w' * 400, 'upstream_asset_id': 'a' * 400,
                  'http_headers': {'Referer': url},
                  'relationships': [{'relationship_type': 'live_photo',
                                     'source_asset_id': 'a' * 400, 'target_asset_id': 'b' * 400}]}
        self.assertEqual(worker.compact_descriptor(fields), {**fields, 'relationships': []})
        from yt_dlp.extractor.common import InfoExtractor

        class LongURLIE(InfoExtractor):
            _VALID_URL = r'https://example.org/(?P<id>\d+)'

            def _real_extract(self, source):
                return {'id': '1', 'formats': [dict(fields)]}

        result = worker.parse_ytdlp(LongURLIE, 'https://example.org/1', 3)
        self.assertEqual(result['formats'][0]['url'], url)
        self.assertEqual(result['formats'][0]['http_headers']['Referer'], url)

    def test_overlong_relationship_endpoints_never_alias_valid_prefix(self):
        from tools import upstream_parsehub_worker as parsehub
        prefix = 'a' * 120
        valid = {'relationship_type': 'live_photo', 'source_asset_id': prefix,
                 'target_asset_id': 'motion'}
        forged = {**valid, 'source_asset_id': prefix + 'suffix'}
        metadata = {'work_id': 'w' * 121, 'upstream_asset_id': prefix + 'suffix',
                    'relationships': [valid, forged]}
        for compact in (worker.compact_descriptor, parsehub.scalar_fields):
            result = compact(metadata)
            self.assertEqual(result['relationships'], [valid])
            self.assertEqual(result['work_id'], metadata['work_id'])
            self.assertEqual(result['upstream_asset_id'], metadata['upstream_asset_id'])

    def test_media_guard_remains_independent_of_source_validator(self):
        with patch.object(worker, 'validate_url', side_effect=lambda value: value):
            for url in ('http://127.0.0.1/media', 'https://user:pass@example.org/media',
                        'https://example.org:22/media', 'https://example.org/' + 'x' * 8192):
                with self.assertRaises(ValueError):
                    worker.validate_media_url(url)
            exact = 'https://example.org/media?signature=' + 'x' * 5000
            self.assertEqual(worker.validate_media_url(exact), exact)

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
