"""Offline bridge contract tests; synthetic identities, no live source samples."""

import asyncio
import json
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from tools import upstream_parsehub_worker as worker
from xhs_download.services import upstream_parsehub as bridge

SOURCE = "https://tieba.baidu.com/p/123"

# These registered test doubles model isinstance identity, not package compatibility.
# Real package classes are exercised in tests_upstream_parsehub_real_types only.
RESULT_TYPES = {
    name: type("Mock" + name, (SimpleNamespace,), {})
    for name in ("ImageParseResult", "VideoParseResult", "MultimediaParseResult", "RichTextParseResult")
}
REF_TYPES = {
    name: type("Mock" + name, (SimpleNamespace,), {})
    for name in ("ImageRef", "AniRef", "VideoRef", "LivePhotoRef")
}


class MockParseHubTypesMixin:
    def setUp(self):
        super().setUp()
        modules = {name: ModuleType(name) for name in (
            "parsehub", "parsehub.types", "parsehub.types.result", "parsehub.types.media_ref",
        )}
        modules["parsehub"].__path__ = []
        modules["parsehub.types"].__path__ = []
        modules["parsehub"].types = modules["parsehub.types"]
        modules["parsehub.types"].result = modules["parsehub.types.result"]
        modules["parsehub.types"].media_ref = modules["parsehub.types.media_ref"]
        modules["parsehub.types.result"].__dict__.update(RESULT_TYPES)
        modules["parsehub.types.media_ref"].__dict__.update(REF_TYPES)
        registry = patch.dict("sys.modules", modules)
        registry.start()
        self.addCleanup(registry.stop)


def ref(kind, url=None):
    video = kind == "VideoRef"
    return REF_TYPES[kind](
        url=url or ("https://media.example/video.mp4" if video else "https://media.example/image.jpg"),
        ext="mp4" if video else "jpg",
    )


def result(media, raw_url=SOURCE):
    return RESULT_TYPES["ImageParseResult"](media=media, raw_url=raw_url, title="test")


class WorkerTests(MockParseHubTypesMixin, unittest.TestCase):
    def test_same_named_unregistered_types_are_rejected(self):
        forged = type("ImageParseResult", (), {
            "media": [ref("ImageRef")], "raw_url": SOURCE, "title": "fake",
        })()
        with self.assertRaisesRegex(ValueError, "^unsupported_result$"):
            worker.normalize_result(forged, SOURCE)
        forged_ref = type("ImageRef", (), {"url": "https://media.example/a.jpg", "ext": "jpg"})()
        with self.assertRaisesRegex(ValueError, "^unsupported_result$"):
            worker.normalize_result(result([forged_ref]), SOURCE)

    def test_exact_public_paths(self):
        for url, parser in [(SOURCE, "TieBaParser"),
                            ("https://h5.pipix.com/ppx/item/123", "PipixParser"),
                            ("https://share.xiaochuankeji.cn/hybrid/share/post?pid=123", "ZuiYouParser")]:
            with self.subTest(parser=parser):
                self.assertEqual(worker.match_url(url)[0]["extractor_id"], parser)

    def test_rejects_other_routes_credentials_and_tracking(self):
        for url in [SOURCE + "?pn=2", SOURCE + "#reply", SOURCE + "/search",
                    "https://tieba.baidu.com.evil.example/p/123",
                    "https://user:password@tieba.baidu.com/p/123",
                    "https://tieba.baidu.com:443/p/123", "http://tieba.baidu.com/p/123",
                    "https://127.0.0.1/p/123", "https://www.xiaohongshu.com/explore/123",
                    "https://h5.pipix.com/s/abc", "https://tieba.baidu.com/f?kw=test",
                    "https://share.xiaochuankeji.cn/hybrid/share/post?pid=123&pid=456"]:
            with self.subTest(url=url):
                self.assertEqual(worker.match_url(url), ())

    def test_pin_and_package_must_both_match(self):
        for version, pin, good in [(worker.VERSION, worker.PIN, True),
                                   ("2.2.1", worker.PIN, False),
                                   (worker.VERSION, "wrong", False)]:
            dist = SimpleNamespace(version=version, read_text=lambda _: json.dumps({"vcs_info": {"commit_id": pin}}))
            with patch.object(worker.metadata, "distribution", return_value=dist):
                if good:
                    worker.verify_install()
                else:
                    with self.assertRaisesRegex(ValueError, "version_changed"):
                        worker.verify_install()

    def test_images_and_unknown_video_are_explicit(self):
        rows = worker.normalize_result(result([ref("ImageRef"), ref("VideoRef")]), SOURCE)
        self.assertEqual(rows[0][0], 3)
        self.assertEqual(rows[0][2]["media_completeness"], "image_candidate")
        self.assertEqual(rows[1][2]["media_completeness"], "unknown")
        self.assertTrue(rows[1][2]["requires_completeness_check"])
        self.assertNotIn("acodec", rows[1][2])

    def test_video_without_extensions_keeps_video_type_hint(self):
        from xhs_download.services.platforms.gallerydl_generic import media_type_from_metadata
        item = ref("VideoRef", "https://media.example/resource")
        item.ext = None
        row = worker.normalize_result(result([item]), SOURCE)[0]
        self.assertEqual(row[2]["extension"], "")
        self.assertEqual(row[2]["mime_type"], "video/unknown")
        self.assertTrue(row[2]["requires_completeness_check"])
        self.assertEqual(media_type_from_metadata(row[2], row[1]), "video")

    def test_video_cannot_be_disguised_as_image_by_extension(self):
        item = ref("VideoRef", "https://media.example/image.jpg")
        with self.assertRaisesRegex(ValueError, "media_type_conflict"):
            worker.normalize_result(result([item]), SOURCE)
        item.url = "https://media.example/resource"
        item.ext = "png"
        with self.assertRaisesRegex(ValueError, "media_type_conflict"):
            worker.normalize_result(result([item]), SOURCE)

    def test_rejects_wrong_identity_empty_excess_and_rich_media(self):
        cases = [result([]), result([ref("ImageRef")] * 31),
                 result([ref("LivePhotoRef")]), result([ref("ImageRef")], SOURCE + "4"),
                 result([ref("ImageRef", "http://media.example/a.jpg")]),
                 result([ref("ImageRef", "https://127.0.0.1/a.jpg")])]
        for value in cases:
            with self.assertRaises(ValueError):
                worker.normalize_result(value, SOURCE)

    def test_response_id_not_echoed_request_is_authority(self):
        for value in [None, "456"]:
            with self.assertRaisesRegex(ValueError, "identity_mismatch"):
                worker.verify_response_identity("TieBaParser", "123", SimpleNamespace(json=lambda: {"thread": {"id": value}}))
        worker.verify_response_identity("TieBaParser", "123", SimpleNamespace(json=lambda: {"thread": {"id": "123"}}))
        worker.verify_response_identity("ZuiYouParser", "123", SimpleNamespace(json=lambda: {"data": {"post": {"id": 123}}}))

    def test_bad_budgets(self):
        for value in [float("nan"), float("inf"), 31, 0, True]:
            with self.assertRaises(ValueError):
                worker.timeout_value(value)

    def test_pipix_uses_exact_string_not_rounded_numeric_id(self):
        node = SimpleNamespace(text=json.dumps({"ppxItemDetail": {"item": {
            "item_id": 120, "item_id_str": "123"}}}))
        soup = SimpleNamespace(find=lambda *a, **kw: node)
        with patch.dict("sys.modules", {"bs4": SimpleNamespace(BeautifulSoup=lambda *a: soup)}):
            worker.verify_response_identity("PipixParser", "123", SimpleNamespace(text="synthetic"))
            with self.assertRaisesRegex(ValueError, "identity_mismatch"):
                worker.verify_response_identity("PipixParser", "120", SimpleNamespace(text="synthetic"))

    def test_wrapped_identity_failure_retains_safe_stage(self):
        outer = RuntimeError("sensitive exception details")
        outer.__cause__ = ValueError("identity_mismatch")
        summary = worker.failure_summary(outer, "parse")
        self.assertEqual(summary["error"], "identity_mismatch")
        self.assertNotIn("sensitive", json.dumps(summary))

    def test_http_boundary_does_not_fetch_media_or_delegate(self):
        class HTTPClient:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
        with patch.dict("sys.modules", {"httpx": SimpleNamespace(AsyncClient=HTTPClient)}):
            _, client, _ = worker.bounded_http("PipixParser", "123", "https://h5.pipix.com/ppx/item/123", 1)
            instance = client()
            self.assertFalse(instance.kwargs["trust_env"])
            self.assertIsNone(instance.kwargs["cookies"])
            async def check():
                for url in ["https://media.example/video.mp4", "https://other.example/page"]:
                    with self.assertRaisesRegex(ValueError, "network_denied"):
                        await instance.send(SimpleNamespace(method="GET", url=url))
            asyncio.run(check())

    def test_http_strips_credentials_bounds_body_and_closes(self):
        class Response:
            closed = False
            def raise_for_status(self):
                pass
            async def aiter_bytes(self):
                yield b"x" * 12
            async def aclose(self):
                self.closed = True
        response = Response()
        class HTTPClient:
            def __init__(self, **kwargs):
                pass
            async def send(self, request, **kwargs):
                self.headers = dict(request.headers)
                return response
        with patch.dict("sys.modules", {"httpx": SimpleNamespace(AsyncClient=HTTPClient)}), patch.object(worker, "MAX_BYTES", 8):
            _, client, _ = worker.bounded_http("PipixParser", "123", "https://h5.pipix.com/ppx/item/123", 1)
            instance = client()
            request = SimpleNamespace(method="GET", url="https://h5.pipix.com/ppx/item/123",
                                      headers={"cookie": "synthetic", "authorization": "synthetic"})
            with self.assertRaisesRegex(ValueError, "output_limit"):
                asyncio.run(instance.send(request))
            self.assertEqual(instance.headers, {})
            self.assertTrue(response.closed)

    def test_http_request_budget(self):
        class HTTPClient:
            def __init__(self, **kwargs):
                pass
        with patch.dict("sys.modules", {"httpx": SimpleNamespace(AsyncClient=HTTPClient)}):
            _, client, state = worker.bounded_http("TieBaParser", "123", SOURCE, 1)
            state["requests"] = worker.MAX_ASSETS + 2
            with self.assertRaisesRegex(ValueError, "request_limit"):
                asyncio.run(client().send(SimpleNamespace()))


@override_settings(PARSEHUB_UPSTREAM_ENABLED=True, PARSEHUB_UPSTREAM_PYTHON="/isolated/python")
class ServiceTests(MockParseHubTypesMixin, SimpleTestCase):
    @override_settings(PARSEHUB_UPSTREAM_ENABLED=False)
    def test_disabled_does_not_execute(self):
        with patch.object(bridge, "run_external_command") as run:
            self.assertEqual(bridge.match_upstream_url(SOURCE), ())
            run.assert_not_called()

    def test_match_tuple_and_version_check(self):
        with patch.object(bridge, "_call", return_value={"provider_id": worker.PROVIDER_ID, "version": worker.PROVIDER_VERSION}):
            self.assertEqual(bridge.match_upstream_url(SOURCE), worker.match_url(SOURCE))
        with patch.object(bridge, "_call", return_value={"version": "wrong"}):
            self.assertEqual(bridge.match_upstream_url(SOURCE), ())

    def test_coverage_valid_runtime_catalog(self):
        with patch.object(bridge, "_call", return_value={"provider_id": worker.PROVIDER_ID, "version": worker.PROVIDER_VERSION}) as call:
            snapshot = bridge.coverage_snapshot()
        call.assert_called_once_with({"action": "identity"}, 5)
        self.assertTrue(snapshot["available"])
        self.assertEqual({r["extractor_id"] for r in snapshot["entries"]}, set(worker.PARSERS))
        self.assertEqual(snapshot["counts"]["eligible"], len(worker.PARSERS))
        self.assertEqual(snapshot["version"], worker.PROVIDER_VERSION)

    def test_coverage_invalid_or_failed_runtime_is_empty(self):
        for identity in [None, {}, {"provider_id": worker.PROVIDER_ID, "version": "wrong"}]:
            with patch.object(bridge, "_call", return_value=identity):
                snapshot = bridge.coverage_snapshot()
                self.assertFalse(snapshot["available"])
                self.assertEqual(snapshot["entries"], [])
        with patch.object(bridge, "_call", side_effect=bridge.ParseHubBridgeError("unavailable")):
            self.assertEqual(bridge.coverage_snapshot()["counts"]["total"], 0)

    @override_settings(PARSEHUB_UPSTREAM_ENABLED=False)
    def test_coverage_disabled_does_not_spawn(self):
        with patch.object(bridge, "run_external_command") as run:
            self.assertFalse(bridge.coverage_snapshot()["available"])
            run.assert_not_called()

    def test_parse_rejects_forged_match_before_subprocess(self):
        match = {**worker.match_url(SOURCE)[0], "provider_id": "parsehub_repo"}
        with patch.object(bridge, "_call") as run:
            with self.assertRaisesRegex(bridge.ParseHubBridgeError, "invalid_parse_request"):
                bridge.parse_upstream_url(match, SOURCE)
            run.assert_not_called()

    def test_parse_preserves_completeness(self):
        rows = worker.normalize_result(result([ref("VideoRef")]), SOURCE)
        with patch.object(bridge, "_call", return_value=rows):
            self.assertEqual(bridge.parse_upstream_url(worker.match_url(SOURCE)[0], SOURCE), rows)

    def test_safe_error_does_not_propagate_worker_details(self):
        completed = SimpleNamespace(returncode=0, stdout=json.dumps({"ok": False, "error": "private-url", "stage": "private-path"}))
        with patch.object(bridge.Path, "is_file", return_value=True), patch.object(bridge, "run_external_command", return_value=completed):
            with self.assertRaises(bridge.ParseHubBridgeError) as caught:
                bridge._call({"action": "identity"}, 1)
            self.assertEqual(str(caught.exception), "worker_failed")
            self.assertEqual(caught.exception.stage, "worker")
