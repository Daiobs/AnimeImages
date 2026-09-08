"""Synthetic-only tests: no production URLs, credentials, or network calls."""

import importlib.metadata
from datetime import datetime
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase, override_settings

from tools import upstream_match_worker as worker
from xhs_download.services import upstream_matching as service
from xhs_download.services.platforms.external_tools import run_external_command


class MatchingServiceTests(SimpleTestCase):
    def setUp(self):
        service._CACHE.clear()
        service._IDENTITIES.clear()

    def test_specific_results_and_defensive_cache_copy(self):
        entry = {"provider_id": "yt_dlp_generic", "extractor_id": "Youtube",
                 "label": "youtube", "version": "1"}
        with mock.patch.object(service, "_context", return_value=(["python"], "1", "a")), \
                mock.patch.object(service, "_call", return_value=[entry]) as call:
            first = service._cached("yt_dlp_generic", "match", "https://example.com/item/1")
            first[0]["label"] = "modified"
            self.assertEqual(service._cached("yt_dlp_generic", "match",
                                             "https://example.com/item/1")[0]["label"], "youtube")
            self.assertEqual(call.call_count, 1)
            service._cached("yt_dlp_generic", "match", "https://example.com/item/2")
            self.assertEqual(call.call_count, 2)

    def test_cache_invalidates_executor_and_version(self):
        with mock.patch.object(service, "_context", side_effect=[
            (["a"], "1", "path-a-v1"), (["b"], "1", "path-b-v1"),
            (["b"], "2", "path-b-v2"),
        ]), mock.patch.object(service, "_call", return_value=[]) as call:
            for _ in range(3):
                service._cached("yt_dlp_generic", "match", "https://example.com/1")
            self.assertEqual(call.call_count, 3)

    def test_identity_cached_briefly_and_result_cache_bounded(self):
        with mock.patch.object(service, "_executor", return_value=(["python"], ["path"])), \
                mock.patch.object(service, "_call", side_effect=lambda command, request:
                                  {"version": "1", "package_path": "a"}
                                  if request["action"] == "identity" else []) as call, \
                mock.patch.object(service, "MAX_CACHE", 2):
            for path in ("1", "1", "2", "3"):
                service._cached("yt_dlp_generic", "match", "https://example.com/" + path)
            self.assertEqual(sum(c.args[1]["action"] == "identity" for c in call.call_args_list), 1)
            self.assertEqual(len(service._CACHE), 2)

    def test_identity_reprobe_on_ttl_and_watched_file_change(self):
        with tempfile.TemporaryDirectory() as directory:
            metadata = Path(directory) / "METADATA"
            metadata.write_text("Version: 1")
            with mock.patch.object(service, "_executor", return_value=(["python"], ["path"])), \
                    mock.patch.object(service, "_call", return_value={
                        "version": "1", "watch_paths": [str(metadata)]}) as call, \
                    mock.patch.object(service.time, "monotonic", return_value=0):
                first = service._context("yt_dlp_generic")
                self.assertEqual(service._context("yt_dlp_generic"), first)
                self.assertEqual(call.call_count, 1)
                metadata.write_text("Version: 2 - changed")
                self.assertNotEqual(service._context("yt_dlp_generic")[2], first[2])
                self.assertEqual(call.call_count, 2)
            with mock.patch.object(service, "_executor", return_value=(["python"], ["path"])), \
                    mock.patch.object(service, "_call", return_value={
                        "version": "2", "watch_paths": [str(metadata)]}) as call, \
                    mock.patch.object(service.time, "monotonic", return_value=4):
                self.assertEqual(service._context("yt_dlp_generic")[1], "2")
                self.assertEqual(call.call_count, 1)

    def test_invalid_urls_never_start_worker(self):
        with mock.patch.object(service, "_context") as context:
            for url in ("file:///a", "http://127.0.0.1/a", "http://[::1]/a",
                        "https://user:pass@example.com/a", "https://example.com:22/a",
                        "http://localhost/a", "https://example.com/\nfoo", "x" * 4097):
                self.assertEqual(service.match_upstream_url(url), ())
            context.assert_not_called()

    def test_unavailable_inventory_does_not_leak_details(self):
        with mock.patch.object(service, "_context", side_effect=OSError("sensitive-path")):
            rows = service.coverage_snapshot()
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(not row["available"] for row in rows))
        self.assertNotIn("sensitive", json.dumps(rows))

    def test_exact_module_executor_and_reject_extra_flags(self):
        with override_settings(YTDLP_BINARY=f"{sys.executable} -m yt_dlp"):
            args, _ = service._executor("yt_dlp_generic")
            self.assertEqual(args[0], sys.executable)
            self.assertEqual(args[1:3], ["-I", "-B"])
        with override_settings(YTDLP_BINARY=f"{sys.executable} -m yt_dlp --proxy bad"):
            with self.assertRaises(service.UpstreamMatchingError):
                service._executor("yt_dlp_generic")

    def test_no_sibling_interpreter_fallback_or_modified_wrapper(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "yt-dlp"
            script.write_text("#!/missing/python\nimport sys\nfrom yt_dlp import main\n"
                              "if __name__ == '__main__':\n"
                              "    sys.argv[0] = sys.argv[0].removesuffix('.exe')\n"
                              "    sys.exit(main())\n")
            script.chmod(0o700)
            with override_settings(YTDLP_BINARY=str(script)):
                with self.assertRaises(service.UpstreamMatchingError):
                    service._executor("yt_dlp_generic")
            script.write_text(script.read_text().replace("/missing/python", sys.executable)
                              + "print('modified wrapper')\n")
            with override_settings(YTDLP_BINARY=str(script)):
                with self.assertRaises(service.UpstreamMatchingError):
                    service._executor("yt_dlp_generic")

    def test_parse_version_and_bounded_request(self):
        match = {"provider_id": "yt_dlp_generic", "extractor_id": "Youtube", "version": "1"}
        with mock.patch.object(service, "_context", return_value=(["exact"], "1", "a")), \
                mock.patch.object(service, "_call", return_value={"formats": []}) as call:
            self.assertEqual(service.parse_upstream_url(match, "https://example.com/item", 90),
                             {"formats": []})
            self.assertEqual(call.call_args.args[2], 30)
            self.assertEqual(call.call_args.args[1]["extractor_id"], "Youtube")
        with mock.patch.object(service, "_context", return_value=(["exact"], "2", "b")):
            with self.assertRaisesMessage(service.UpstreamMatchingError, "version_changed"):
                service.parse_upstream_url(match, "https://example.com/item")

    def test_timeout_output_limit_and_error_redaction(self):
        with mock.patch.object(service, "run_external_command",
                               side_effect=subprocess.TimeoutExpired("secret-url", 1)) as run:
            with self.assertRaisesMessage(service.UpstreamMatchingError, "execution_timeout"):
                service._call(["exact"], {"action": "match"})
            self.assertEqual(run.call_args.kwargs["max_stdout_bytes"], worker.MAX_OUTPUT)
            self.assertEqual(run.call_args.kwargs["max_stderr_bytes"], 65536)

    def test_worker_error_codes_preserve_safe_stage_and_class(self):
        for code in ("auth_required", "access_denied", "network_denied", "execution_guard", "no_formats",
                     "playlist_denied", "network_timeout", "unavailable", "worker_error"):
            response = {"ok": False, "error": code, "stage": "ytdlp_extract", "error_class": "ValueError"}
            with mock.patch.object(service, "run_external_command", return_value=SimpleNamespace(
                returncode=0, stdout=json.dumps(response), stderr="private error text")):
                with self.assertRaises(service.UpstreamMatchingError) as captured:
                    service._call(["exact"], {})
                self.assertEqual(captured.exception.code, code)
                self.assertEqual(captured.exception.stage, "ytdlp_extract")
                self.assertEqual(captured.exception.error_class, "ValueError")
                self.assertNotIn("private", str(captured.exception))


class WorkerContractTests(SimpleTestCase):
    def test_wikiart_search_backed_image_is_not_a_single_item_match(self):
        from gallery_dl.extractor.wikiart import WikiartImageExtractor

        url = "https://www.wikiart.org/en/vincent-van-gogh/the-starry-night-1889"
        self.assertTrue(worker.suitable("gallery_dl_generic", WikiartImageExtractor, url))
        self.assertFalse(worker.eligible("gallery_dl_generic", WikiartImageExtractor))
        request = {"provider_id": "gallery_dl_generic", "version": "1", "url": url}
        with mock.patch.object(worker, "package_identity", return_value={"version": "1"}), \
                mock.patch.object(worker, "extractor_classes", return_value=[WikiartImageExtractor]), \
                mock.patch.object(worker, "parse_gallery") as parse:
            self.assertEqual(worker.handle(dict(request, action="match")), [])
            self.assertEqual(worker.handle(dict(request, action="coverage"))["entries"], [])
            with self.assertRaisesMessage(ValueError, "extractor_mismatch"):
                worker.handle(dict(request, action="parse", extractor_id="WikiartImageExtractor"))
            parse.assert_not_called()

    def test_extractor_key_error_is_not_misreported_as_network_failure(self):
        response = worker.safe_error(KeyError("private upstream field"), "ytdlp_extract")
        self.assertEqual(response, {"ok": False, "error": "worker_error",
                                   "stage": "ytdlp_extract", "error_class": "KeyError"})
        with mock.patch.object(service, "run_external_command", return_value=SimpleNamespace(
                returncode=0, stdout=json.dumps(response), stderr="")):
            with self.assertRaises(service.UpstreamMatchingError) as captured:
                service._call(["exact"], {})
            self.assertEqual(captured.exception.code, "worker_error")
            self.assertEqual(captured.exception.stage, "ytdlp_extract")
            self.assertEqual(captured.exception.error_class, "KeyError")

    def test_access_denied_status_survives_worker_parent_boundary(self):
        for status in (401, 403):
            error = RuntimeError("private upstream details")
            error.status = status
            response = worker.safe_error(error, "gallery_extract")
            self.assertEqual(response["error"], "access_denied")
            with mock.patch.object(service, "run_external_command", return_value=SimpleNamespace(
                    returncode=0, stdout=json.dumps(response), stderr="")):
                with self.assertRaises(service.UpstreamMatchingError) as captured:
                    service._call(["exact"], {})
                self.assertEqual(captured.exception.code, "access_denied")
                self.assertEqual(captured.exception.stage, "gallery_extract")
                self.assertNotIn("private", str(captured.exception))

    def test_comic_collection_is_rejected_before_instantiation(self):
        from gallery_dl.extractor.keenspot import KeenspotComicExtractor
        from gallery_dl.extractor.naverwebtoon import NaverWebtoonComicExtractor
        from gallery_dl.extractor.readcomiconline import ReadcomiconlineComicExtractor
        from gallery_dl.extractor.webtoons import WebtoonsComicExtractor

        classes = [KeenspotComicExtractor, NaverWebtoonComicExtractor,
                   ReadcomiconlineComicExtractor, WebtoonsComicExtractor]
        for cls in classes:
            self.assertEqual(cls.subcategory, "comic")
            self.assertFalse(worker.eligible("gallery_dl_generic", cls))
        # Keenspot follows pages internally, so rejecting Queue alone is insufficient.
        with mock.patch.object(worker, "package_identity", return_value={"version": "1"}), \
                mock.patch.object(worker, "extractor_classes", return_value=classes), \
                mock.patch.object(worker, "parse_gallery") as parse:
            with self.assertRaisesMessage(ValueError, "extractor_mismatch"):
                worker.handle({"action": "parse", "provider_id": "gallery_dl_generic",
                               "version": "1", "extractor_id": "KeenspotComicExtractor",
                               "url": "https://synthetic.keenspot.com/2026/01/01/"})
            parse.assert_not_called()

    def test_safe_errors_do_not_expose_messages(self):
        for error, code in ((ValueError("network_denied"), "network_denied"),
                            (ImportError("private path"), "unavailable"),
                            (TimeoutError("private url"), "network_timeout"),
                            (RuntimeError("private path"), "worker_error")):
            result = worker.safe_error(error, "ytdlp_import")
            self.assertEqual(result["error"], code)
            self.assertNotIn("private", json.dumps(result))

    def test_collection_classes_not_admitted(self):
        for key in ("Generic", "YoutubeTab", "YoutubePlaylist", "InstagramUser", "TestURL"):
            self.assertFalse(worker.eligible("yt_dlp_generic", SimpleNamespace(
                ie_key=lambda: key, _VALID_URL="https://example.com/item")))
        self.assertTrue(worker.eligible("yt_dlp_generic", SimpleNamespace(
            ie_key=lambda: "NewDedicatedProvider", _VALID_URL="https://example.com/item")))
        self.assertFalse(worker.eligible("yt_dlp_generic", SimpleNamespace(
            ie_key=lambda: "UnobviousCollection", _RETURN_TYPE="playlist", _VALID_URL="https://example.com/item")))
        for subcategory in ("user", "album", "gallery", "search", "collection", "timeline"):
            self.assertFalse(worker.eligible("gallery_dl_generic", SimpleNamespace(
                __name__="SyntheticExtractor", subcategory=subcategory, category="example")))

    def test_hidden_and_history_helpers_are_not_dynamic_coverage(self):
        self.assertFalse(worker.eligible("yt_dlp_generic", SimpleNamespace(
            ie_key=lambda: "HiddenHelper", IE_DESC=False, _VALID_URL="https://example.com/item")))
        for suffix in ("History", "Recommended", "Subscriptions", "WatchLater",
                       "Notifications", "TruncatedID", "TruncatedURL"):
            self.assertFalse(worker.eligible("yt_dlp_generic", SimpleNamespace(
                ie_key=lambda: "Synthetic" + suffix, _VALID_URL="https://example.com/item")))

    def test_established_route_prefixes_excluded_for_both_providers(self):
        for prefix in ("Youtube", "Bilibili", "BiliBili", "XiaoHongShu", "Douyin", "TikTok",
                       "Kwai", "Kuaishou", "Instagram", "Facebook", "Twitter", "Imgur",
                       "Reddit", "Tumblr", "Flickr", "Deviantart"):
            self.assertFalse(worker.eligible("yt_dlp_generic", SimpleNamespace(
                ie_key=lambda: prefix + "Video", _VALID_URL="https://example.com/item")))
            self.assertFalse(worker.eligible("gallery_dl_generic", SimpleNamespace(
                __name__=prefix + "ImageExtractor", subcategory="image", category=prefix.lower())))

    def test_ytdlp_complete_formats_and_no_delegation(self):
        class Fake:
            payload = {"id": "synthetic", "formats": [{"url": "https://example.com/v.mp4"}]}

            def __init__(self, ydl):
                self.ydl = ydl

            def extract(self, url):
                return self.payload

        with mock.patch('tools.upstream_metadata_probe.probe_in_guarded_child', return_value={"audio": "unknown"}):
            processed = worker.parse_ytdlp(Fake, "https://example.com/item", 1)["formats"][0]
        self.assertEqual(processed["url"], "https://example.com/v.mp4")
        self.assertEqual(processed["ext"], "mp4")
        self.assertEqual(processed["protocol"], "https")
        self.assertEqual(processed["format_id"], "0")
        self.assertEqual(processed["upstream_track_evidence"], {
            "acodec": None, "vcodec": None, "audio_ext": None, "video_ext": None})
        for payload in ({"_type": "url", "url": "https://example.com/next"},
                        {"_type": "playlist", "entries": []}, {"entries": []},
                        {"_type": "url_transparent"}):
            Fake.payload = payload
            with self.assertRaises(ValueError):
                worker.parse_ytdlp(Fake, "https://example.com/item", 1)

        class Delegating(Fake):
            def extract(self, url):
                return self.ydl.extract_info(url)

        with self.assertRaisesMessage(ValueError, "delegation_denied"):
            worker.parse_ytdlp(Delegating, "https://example.com/item", 1)

        class Collection(Fake):
            def extract(self, url):
                def items():
                    raise AssertionError("entries must not be consumed")
                    yield
                return self.playlist_result(items())

        with self.assertRaisesMessage(ValueError, "collection_denied"):
            worker.parse_ytdlp(Collection, "https://example.com/item", 1)

        class URLResult(Fake):
            def extract(self, url):
                return self.url_result(url)

        with self.assertRaisesMessage(ValueError, "delegation_denied"):
            worker.parse_ytdlp(URLResult, "https://example.com/item", 1)

    def test_gallery_exact_messages_queue_and_item_limits(self):
        class Fake:
            pattern = r"https://example.com/item"
            messages = [(2, {"title": "synthetic", "date": datetime(2026, 1, 1)}),
                        (3, "https://example.com/a.jpg", {"extension": "jpg"})]

            def __init__(self, match):
                assert match

            def __iter__(self):
                yield from self.messages

        self.assertEqual(worker.parse_gallery(Fake, "https://example.com/item", 1)[1][0], 3)
        for messages in ([(6, "https://example.com/next", {})], [(5, {})],
                         [(2, {})] * (worker.MAX_ITEMS + 1)):
            Fake.messages = messages
            with self.assertRaises(ValueError):
                worker.parse_gallery(Fake, "https://example.com/item", 1)

    def test_forged_match_rejected_before_parse(self):
        with mock.patch.object(worker, "package_identity", return_value={"version": "1"}), \
                mock.patch.object(worker, "extractor_classes", return_value=[]), \
                mock.patch.object(worker, "parse_ytdlp") as parse:
            with self.assertRaisesMessage(ValueError, "extractor_mismatch"):
                worker.handle({"action": "parse", "provider_id": "yt_dlp_generic",
                               "version": "1", "extractor_id": "Generic",
                               "url": "https://example.com/item"})
            parse.assert_not_called()


class IsolatedGuardTests(SimpleTestCase):
    def child(self, source):
        prelude = ("import importlib.util\n"
                   f"spec=importlib.util.spec_from_file_location('worker', {str(service.WORKER)!r})\n"
                   "w=importlib.util.module_from_spec(spec); spec.loader.exec_module(w)\n")
        completed = run_external_command([sys.executable, "-I", "-B", "-c", prelude + source],
                                         timeout_seconds=10, max_stdout_bytes=65536,
                                         max_stderr_bytes=65536)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return completed.stdout

    def test_offline_denies_socket_and_dns_without_touching_parent(self):
        import socket
        before = socket.socket.connect
        self.child("""
import socket
old_dns = socket.getaddrinfo
w.install_network_guard(offline=True)
for operation in (lambda: socket.getaddrinfo('example.com',443),
                  lambda: old_dns('example.com',443),
                  lambda: socket.socket().connect(('8.8.8.8',443)),
                  lambda: socket.socket().connect_ex(('8.8.8.8',443)),
                  lambda: socket.create_connection(('example.com',443))):
    try: operation()
    except ValueError: pass
    else: raise AssertionError('network escaped')
""")
        self.assertIs(socket.socket.connect, before)

    def test_dns_rebinding_private_redirect_and_ports_without_real_network(self):
        self.child("""
import socket
answers = ['8.8.8.8']
calls = []
socket.getaddrinfo = lambda host, port, *a, **k: [
    (socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, port)) for ip in answers]
socket.socket.connect = lambda sock, addr: calls.append(addr)
socket.socket.connect_ex = lambda sock, addr: calls.append(addr) or 0
w.install_network_guard(offline=False)
s = socket.socket()
s.connect(('example.com',443))
assert calls == [('8.8.8.8',443)]
answers[:] = ['127.0.0.1']
for method in (s.connect, s.connect_ex):
    try: method(('example.com',443))
    except ValueError: pass
    else: raise AssertionError('rebinding escaped')
answers[:] = ['8.8.8.8','10.0.0.1']
try: socket.create_connection(('example.com',443))
except ValueError: pass
else: raise AssertionError('mixed DNS escaped')
answers[:] = ['8.8.8.8']
try: s.connect(('example.com',22))
except ValueError: pass
else: raise AssertionError('port escaped')
assert len(calls) == 1
""")

    def test_actual_connect_audit_rejects_private_address_and_subprocess(self):
        self.child("""
import socket, subprocess
original_connect = socket.socket.connect
w.install_network_guard(offline=False)
for address in ('127.0.0.1', '10.0.0.1', '169.254.169.254', '0.0.0.0'):
    try: original_connect(socket.socket(), (address,443))
    except ValueError: pass
    else: raise AssertionError('original connect escaped')
try: subprocess.run(['/does-not-exist'])
except ValueError: pass
else: raise AssertionError('subprocess escaped')
""")

    def test_ssl_and_official_library_initialization_under_guard(self):
        self.child("""
w.install_network_guard(offline=True)
import ssl
ctx = ssl.create_default_context()
assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname
from yt_dlp import YoutubeDL
with YoutubeDL({'quiet':True, 'cachedir':False}, auto_init=False) as ydl:
    assert not ydl.params.get('nocheckcertificate')
from gallery_dl.extractor.instagram import InstagramPostExtractor
instance = InstagramPostExtractor.from_url('https://www.instagram.com/p/ABCsynthetic/')
instance._init_options()
instance._init_session()
assert instance._verify is True
""")

    def test_lazy_extractor_resolves_before_runtime_guards(self):
        self.child("""
w.install_network_guard(offline=True)
from yt_dlp.extractor import get_info_extractor
proxy = get_info_extractor('Vimeo')
concrete = getattr(proxy, 'real_class', proxy)
concrete.initialize = lambda self: None
concrete._real_extract = lambda self, url: self.playlist_result(iter(()))
try: w.parse_ytdlp(proxy, 'https://vimeo.com/12345678',1)
except ValueError as error: assert str(error) == 'collection_denied'
else: raise AssertionError('lazy proxy bypassed guards')
concrete._real_extract = lambda self, url: {'id':'synthetic','formats':[
    {'url':'https://example.com/video.mp4'}]}
assert len(w.parse_ytdlp(proxy, 'https://vimeo.com/12345678',1)['formats']) == 1
""")

    def test_real_installed_matching_is_offline_and_specific(self):
        with override_settings(YTDLP_BINARY=f"{sys.executable} -m yt_dlp",
                               GALLERYDL_BINARY=f"{sys.executable} -m gallery_dl"):
            inventory = service.coverage_snapshot()
            self.assertTrue(all(row["available"] for row in inventory[:2]))
            self.assertFalse(inventory[2]["available"])
            self.assertEqual(inventory[0]["version"], importlib.metadata.version("yt-dlp"))
            self.assertGreater(inventory[0]["counts"]["eligible"], 1000)
            self.assertEqual(inventory[1]["version"], importlib.metadata.version("gallery-dl"))
            self.assertEqual(service.match_upstream_url("https://example.com/item"), ())
            self.assertEqual(service.match_upstream_url("https://www.youtube.com/@synthetic/videos"), ())
            for url in ("https://www.youtube.com/watch?v=abcdefghijk",
                        "https://www.instagram.com/p/ABCsynthetic/",
                        "https://www.tiktok.com/@synthetic/video/1234567890123456789",
                        "https://twitter.com/synthetic/status/1234567890123456789"):
                self.assertEqual(service.match_upstream_url(url), ())
            results = service.match_upstream_url("https://vimeo.com/12345678")
            self.assertIn("Vimeo", [entry["extractor_id"] for entry in results])
            results = service.match_upstream_url("https://www.artstation.com/artwork/ABC123")
            self.assertIn("ArtstationImageExtractor", [entry["extractor_id"] for entry in results])
            for row in inventory:
                counts = row["counts"]
                self.assertEqual(counts["total"], counts["eligible"] + counts["excluded"])
                self.assertEqual(counts["excluded"], counts["already_routed"] + counts["other_excluded"])
                self.assertEqual(counts["eligible"], len(row["entries"]))
                if row['provider_id'] != 'parsehub_generic':
                    self.assertGreater(counts["already_routed"], 0)
                for entry in row["entries"]:
                    self.assertFalse(entry["extractor_id"].lower().startswith(worker._ROUTED_PREFIXES))
