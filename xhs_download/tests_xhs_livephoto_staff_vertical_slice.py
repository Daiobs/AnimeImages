import json
import re
import threading
import time
from pathlib import Path
from unittest.mock import Mock, patch

from bs4 import BeautifulSoup
from django.contrib.auth.models import AnonymousUser, User
from django.template.loader import render_to_string
from django.test import RequestFactory, SimpleTestCase

from .services.assets import MediaAsset, ParseResult
from .services.downloads import provider_manifest_allows_public_download
from .services.platforms.xiaohongshu import XiaohongshuAdapter
from .services.platforms.xhs_livephoto_staff_builder import (
    XHS_LIVEPHOTO_STAFF_MAX_PROBE_BYTES,
    XHS_LIVEPHOTO_STAFF_PROVIDER,
    XhsLivePhotoProbeEvidenceV1,
    XhsLivePhotoStaffPreviewError,
    build_xhs_livephoto_staff_preview_context,
    build_xhs_livephoto_staff_result,
)
from .services.platforms.xhs_page_state import (
    XHS_PRIVATE_SELECTED_NOTE_SNAPSHOT_SCHEMA_VERSION,
    XhsPrivateSelectedNoteSnapshotV1,
    extract_xhs_page_state,
)
from .services.registry import get_adapter_for_url
from .services.batch_parse.results import build_batch_result_row
from .views import (
    build_result_display_context,
    build_search_results_context,
    download_items_have_invalid_provider_manifest,
    run_provider_health_test,
)


NOTE_ID = "64a1b2c3d4e5f6789012abcd"
NOTE_URL = f"https://www.xiaohongshu.com/explore/{NOTE_ID}"
JPEG = b"\xff\xd8\xff\xe0" + (b"J" * 20)
WEBP = b"RIFF\x10\x00\x00\x00WEBP" + (b"W" * 12)
MP4 = b"\x00\x00\x00\x18ftypisom" + (b"M" * 12)


def still_url(index):
    return f"https://sns-webpic-qc.xhscdn.com/notes_pre_post/still-{index}?sig=fixture"


def motion_url(index, host="sns-video-v7.xhscdn.com"):
    return f"https://{host}/stream/motion-{index}.mp4?sig=fixture"


def regular_item(index):
    value = still_url(index)
    return {
        "livePhoto": False,
        "urlDefault": value,
        "infoList": [{"imageScene": "WB_DFT", "url": value}],
        "stream": {},
    }


def livephoto_item(index, host="sns-video-v7.xhscdn.com"):
    item = regular_item(index)
    item["livePhoto"] = True
    item["stream"] = {
        "h264": [
            {
                "masterUrl": motion_url(index, host),
                "backupUrls": [],
            }
        ]
    }
    return item


def page_html(items, *, note_id=NOTE_ID, note_type="normal"):
    payload = {
        "note": {
            "noteDetailMap": {
                note_id: {
                    "note": {
                        "noteId": note_id,
                        "type": note_type,
                        "title": "脱敏测试标题",
                        "user": {"nickname": "脱敏测试作者"},
                        "imageList": items,
                    }
                }
            }
        }
    }
    serialized = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    return f"<script>window.__INITIAL_STATE__={serialized};</script>"


def selected_snapshot(items):
    state = extract_xhs_page_state(
        BeautifulSoup(page_html(items), "html.parser"),
        source_url=NOTE_URL,
    )
    return state.private_selected_note_snapshot


def flat_result(items):
    return ParseResult(
        platform="xiaohongshu",
        post_type="image",
        base_filename="xhs_fixture",
        assets=tuple(
            MediaAsset(
                media_type="image",
                url=still_url(index),
                platform="xiaohongshu",
            )
            for index, _item in enumerate(items)
        ),
        metadata={"image_source": "structured"},
    )


def fake_probe(records, *, deadline_monotonic, webp_ordinals=frozenset()):
    del deadline_monotonic
    evidence = {}
    for record in records:
        is_still = record.locator.member_role == "still"
        prefix = (
            WEBP
            if is_still and record.locator.source_item_ordinal in webp_ordinals
            else JPEG
            if is_still
            else MP4
        )
        content_type = "image/webp" if prefix is WEBP else "application/octet-stream"
        evidence[record.https_candidate.value] = (
            XhsLivePhotoProbeEvidenceV1(
                content_type=content_type,
                bytes_read=len(prefix),
                prefix=prefix,
            )
        )
    return evidence


def enhanced_result(items, *, webp_ordinals=frozenset()):
    snapshot = selected_snapshot(items)
    outcome = build_xhs_livephoto_staff_result(
        flat_result=flat_result(items),
        snapshot=snapshot,
        probe_candidates=lambda records, deadline_monotonic: fake_probe(
            records,
            deadline_monotonic=deadline_monotonic,
            webp_ordinals=webp_ordinals,
        ),
    )
    return outcome.result


def result_region_context(result, *, staff_preview=False):
    display = build_result_display_context(
        result,
        probe_missing_video_sizes=False,
    )
    return {
        "error_message": "",
        "platform_notice": "",
        "provider_notice": "",
        "image_items": display["image_items"],
        "video_items": display["video_items"],
        "platform_name": display["platform_name"],
        "post_type": display["post_type"],
        "base_filename": display["base_filename"],
        "display_title": "脱敏测试标题",
        "display_author": "",
        "result_theme": "xiaohongshu",
        "show_internal_diagnostics": staff_preview,
        "show_xhs_livephoto_staff_preview": staff_preview,
        "xhs_livephoto_staff_preview": (
            build_xhs_livephoto_staff_preview_context(result)
            if staff_preview
            else {}
        ),
        "is_mobile_request": False,
    }


class XhsPrivateSelectedNoteSnapshotTests(SimpleTestCase):
    def test_snapshot_cannot_be_forged_outside_exact_note_selector(self):
        with self.assertRaisesMessage(
            ValueError,
            "XHS_PRIVATE_SELECTED_NOTE_SNAPSHOT_INVALID",
        ):
            XhsPrivateSelectedNoteSnapshotV1(
                schema_version=XHS_PRIVATE_SELECTED_NOTE_SNAPSHOT_SCHEMA_VERSION,
                note_id=NOTE_ID,
                image_list=(regular_item(0),),
            )

    def test_snapshot_repr_and_page_state_repr_hide_identity_and_candidates(self):
        state = extract_xhs_page_state(
            BeautifulSoup(page_html([livephoto_item(0)]), "html.parser"),
            source_url=NOTE_URL,
        )

        snapshot_repr = repr(state.private_selected_note_snapshot)
        self.assertNotIn(NOTE_ID, snapshot_repr)
        self.assertNotIn(still_url(0), snapshot_repr)
        self.assertNotIn(motion_url(0), snapshot_repr)
        self.assertNotIn("private_selected_note_snapshot", repr(state))


class XhsLivePhotoStaffBuilderTests(SimpleTestCase):
    def test_fourteen_livephotos_emit_ordered_assets_and_groups(self):
        result = enhanced_result([livephoto_item(index) for index in range(14)])

        self.assertEqual(result.post_type, "live_photo")
        self.assertEqual(len(result.assets), 28)
        self.assertEqual(len(result.rich_media.groups), 14)
        self.assertEqual(
            [descriptor.role for descriptor in result.rich_media.assets[:4]],
            ["still", "motion", "still", "motion"],
        )

    def test_mixed_source_order_and_exact_formats_are_preserved(self):
        items = []
        live_ordinals = set(range(0, 12))
        for index in range(17):
            items.append(
                livephoto_item(index)
                if index in live_ordinals
                else regular_item(index)
            )

        result = enhanced_result(items, webp_ordinals=frozenset({12}))
        context = build_xhs_livephoto_staff_preview_context(result)

        self.assertEqual(result.post_type, "multimedia")
        self.assertEqual(len(result.assets), 29)
        self.assertEqual(len(result.rich_media.groups), 12)
        self.assertEqual(context["source_item_count"], 17)
        self.assertEqual(context["livephoto_count"], 12)
        self.assertEqual(context["regular_count"], 5)
        self.assertEqual(
            [item["kind"] for item in context["items"]],
            (["live_photo"] * 12) + (["regular_image"] * 5),
        )
        regular_binding = result.rich_media.assets[24]
        self.assertEqual(regular_binding.extension, "webp")
        self.assertEqual(result.assets[24].url, still_url(12))

    def test_preview_assets_have_provenance_but_no_download_authority(self):
        result = enhanced_result([livephoto_item(0), regular_item(1)])

        self.assertTrue(result.assets)
        for asset in result.assets:
            self.assertEqual(asset.provider, XHS_LIVEPHOTO_STAFF_PROVIDER)
            self.assertEqual(asset.provider_manifest_token, "")
            self.assertEqual(asset.headers, {})
            self.assertFalse(provider_manifest_allows_public_download(asset.to_download_item()))
            self.assertTrue(download_items_have_invalid_provider_manifest([asset.to_download_item()]))
        self.assertEqual(
            [asset.media_type for asset in result.assets],
            ["image", "video", "image"],
        )
        self.assertRegex(result.assets[0].filename, r"_01_still\.jpg$")
        self.assertRegex(result.assets[1].filename, r"_01_motion\.mp4$")
        self.assertEqual(result.metadata["xhs_livephoto_staff_full_media_bytes"], 0)

    def test_ordinary_gallery_is_not_enhanced(self):
        items = [regular_item(0), regular_item(1)]
        outcome = build_xhs_livephoto_staff_result(
            flat_result=flat_result(items),
            snapshot=selected_snapshot(items),
            probe_candidates=lambda *_args, **_kwargs: self.fail("probe called"),
        )

        self.assertEqual(outcome.status, "not_applicable")
        self.assertIsNone(outcome.result)

    def test_cross_contract_limit_rejects_before_any_network_probe(self):
        items = [livephoto_item(index) for index in range(33)]

        with self.assertRaises(Exception) as raised:
            build_xhs_livephoto_staff_result(
                flat_result=flat_result(items),
                snapshot=selected_snapshot(items),
                probe_candidates=lambda *_args, **_kwargs: self.fail("probe called"),
            )

        self.assertEqual(
            getattr(raised.exception, "code", ""),
            "XHS_LIVEPHOTO_ASSET_SPEC_GROUP_LIMIT",
        )

    def test_bounded_probe_reads_only_prefixes_and_sends_no_cookie(self):
        items = [livephoto_item(0)]
        responses = []

        def request_get(url, **kwargs):
            is_motion = "sns-video" in url
            prefix = MP4 if is_motion else JPEG
            response = Mock()
            response.status_code = 206
            response.url = url
            response.headers = {"Content-Type": "application/octet-stream"}
            response.iter_content.return_value = iter(
                [prefix + (b"x" * XHS_LIVEPHOTO_STAFF_MAX_PROBE_BYTES)]
            )
            responses.append((response, kwargs))
            return response

        with patch(
            "xhs_download.services.platforms.xhs_livephoto_staff_builder.requests.get",
            side_effect=request_get,
        ):
            outcome = build_xhs_livephoto_staff_result(
                flat_result=flat_result(items),
                snapshot=selected_snapshot(items),
            )

        self.assertEqual(len(responses), 2)
        self.assertEqual(outcome.bounded_probe_bytes, 2 * XHS_LIVEPHOTO_STAFF_MAX_PROBE_BYTES)
        for response, kwargs in responses:
            self.assertTrue(kwargs["stream"])
            self.assertLessEqual(kwargs["timeout"], 5)
            self.assertFalse(kwargs["allow_redirects"])
            self.assertEqual(kwargs["headers"]["Range"], "bytes=0-4095")
            self.assertNotIn("Cookie", kwargs["headers"])
            self.assertNotIn("cookies", kwargs)
            response.close.assert_called_once()

    def test_probe_rejects_unverified_redirect_before_following_it(self):
        items = [livephoto_item(0)]
        requested_urls = []

        def request_get(url, **_kwargs):
            requested_urls.append(url)
            response = Mock()
            response.url = url
            if "sns-webpic" in url:
                response.status_code = 302
                response.headers = {
                    "Location": "https://unverified.example/private-target"
                }
                response.iter_content.return_value = iter(())
            else:
                response.status_code = 206
                response.headers = {"Content-Type": "video/mp4"}
                response.iter_content.return_value = iter([MP4])
            return response

        with patch(
            "xhs_download.services.platforms.xhs_livephoto_staff_builder.requests.get",
            side_effect=request_get,
        ):
            with self.assertRaises(Exception):
                build_xhs_livephoto_staff_result(
                    flat_result=flat_result(items),
                    snapshot=selected_snapshot(items),
                )

        self.assertFalse(
            any("unverified.example" in value for value in requested_urls)
        )

    def test_probe_post_deadline_residue_is_bounded_and_closed(self):
        adapter = XiaohongshuAdapter()
        items = [livephoto_item(0), livephoto_item(1)]
        lock = threading.Lock()
        active_responses = 0
        started_responses = 0

        class BlockingResponse:
            def __init__(self, url):
                nonlocal active_responses, started_responses
                self.status_code = 206
                self.url = url
                self.headers = {"Content-Type": "application/octet-stream"}
                self.closed = False
                with lock:
                    active_responses += 1
                    started_responses += 1

            def iter_content(self, *, chunk_size):
                self.assert_chunk_size = chunk_size
                time.sleep(0.25)
                yield MP4 if "sns-video" in self.url else JPEG

            def close(self):
                nonlocal active_responses
                with lock:
                    if not self.closed:
                        self.closed = True
                        active_responses -= 1

        started_at = time.monotonic()
        with patch(
            "xhs_download.services.platforms.xhs_livephoto_staff_builder.requests.get",
            side_effect=lambda url, **_kwargs: BlockingResponse(url),
        ):
            result = adapter.parse_html(
                page_html(items),
                source_url=NOTE_URL,
                enable_xhs_livephoto_staff_preview=True,
                deadline_monotonic=time.monotonic() + 0.03,
            )
        foreground_duration = time.monotonic() - started_at

        self.assertLess(foreground_duration, 0.15)
        self.assertIsNone(result.rich_media)
        self.assertEqual(
            result.metadata["xhs_livephoto_staff_preview_status"],
            "fallback",
        )
        self.assertEqual(
            result.metadata["xhs_livephoto_staff_preview_error_code"],
            "XHS_LIVEPHOTO_STAFF_PREVIEW_DEADLINE",
        )
        with lock:
            self.assertGreaterEqual(started_responses, 2)

        residue_deadline = time.monotonic() + 0.8
        while time.monotonic() < residue_deadline:
            with lock:
                response_count = active_responses
            owned_threads = [
                thread
                for thread in threading.enumerate()
                if thread.name.startswith("xhs-livephoto-probe")
            ]
            if response_count == 0 and not owned_threads:
                break
            time.sleep(0.01)

        with lock:
            self.assertEqual(active_responses, 0)
        self.assertFalse(
            any(
                thread.name.startswith("xhs-livephoto-probe")
                for thread in threading.enumerate()
            )
        )

    def test_unverified_motion_host_falls_back_without_partial_result(self):
        adapter = XiaohongshuAdapter()
        items = [livephoto_item(0, host="sns-video-zl.xhscdn.com")]

        result = adapter.parse_html(
            page_html(items),
            source_url=NOTE_URL,
            enable_xhs_livephoto_staff_preview=True,
        )

        self.assertIsNone(result.rich_media)
        self.assertEqual(len(result.assets), 1)
        self.assertEqual(
            result.metadata["xhs_livephoto_staff_preview_status"],
            "fallback",
        )
        self.assertIn(
            "HOST_CLASS_UNVERIFIED",
            result.metadata["xhs_livephoto_staff_preview_error_code"],
        )

    def test_format_probe_failure_falls_back_and_logs_no_candidate(self):
        adapter = XiaohongshuAdapter()
        items = [livephoto_item(0)]
        unsafe_error = XhsLivePhotoStaffPreviewError(
            "XHS_LIVEPHOTO_STAFF_PREVIEW_PROBE_FAILED"
        )

        with patch(
            "xhs_download.services.platforms.xiaohongshu.build_xhs_livephoto_staff_result",
            side_effect=unsafe_error,
        ), patch(
            "xhs_download.services.platforms.xiaohongshu.logger.warning"
        ) as warning:
            result = adapter.parse_html(
                page_html(items),
                source_url=NOTE_URL,
                enable_xhs_livephoto_staff_preview=True,
            )

        self.assertIsNone(result.rich_media)
        self.assertEqual(len(result.assets), 1)
        logged = repr(warning.call_args)
        self.assertNotIn(still_url(0), logged)
        self.assertNotIn(motion_url(0), logged)

    def test_public_adapter_path_never_runs_staff_builder(self):
        adapter = XiaohongshuAdapter()
        with patch(
            "xhs_download.services.platforms.xiaohongshu.build_xhs_livephoto_staff_result"
        ) as builder:
            result = adapter.parse_html(
                page_html([livephoto_item(0)]),
                source_url=NOTE_URL,
            )

        builder.assert_not_called()
        self.assertIsNone(result.rich_media)
        self.assertEqual(len(result.assets), 1)


class XhsLivePhotoStaffGateTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.result = flat_result([regular_item(0)])

    def parse_options_for(self, user, query_suffix=""):
        request = self.factory.get(
            f"/?q=https://xhslink.cn/o/synthetic{query_suffix}"
        )
        request.user = user
        with patch(
            "xhs_download.views.parse_media_page",
            return_value=self.result,
        ) as parser, patch("xhs_download.views.log_parse_request_event"):
            _context, status = build_search_results_context(request)
        self.assertEqual(status, 200)
        return parser.call_args.kwargs

    def test_only_active_staff_request_enables_internal_parse_option(self):
        staff = User(username="staff", is_staff=True, is_active=True)
        public = AnonymousUser()
        inactive_staff = User(username="inactive", is_staff=True, is_active=False)

        self.assertTrue(
            self.parse_options_for(staff)["enable_xhs_livephoto_staff_preview"]
        )
        self.assertNotIn(
            "enable_xhs_livephoto_staff_preview",
            self.parse_options_for(public, "&xhs_livephoto=1"),
        )
        self.assertNotIn(
            "enable_xhs_livephoto_staff_preview",
            self.parse_options_for(inactive_staff, "&xhs_livephoto=1"),
        )

    def test_current_short_link_host_is_classified_as_xhs(self):
        adapter = get_adapter_for_url("https://xhslink.cn/o/synthetic")
        self.assertEqual(adapter.platform_id, "xiaohongshu")

    def test_batch_parser_never_receives_staff_preview_option(self):
        captured = {}

        def parser(_source, **kwargs):
            captured.update(kwargs)
            return self.result

        request = self.factory.post("/batch/")
        request.user = User(username="staff", is_staff=True, is_active=True)
        row = build_batch_result_row(
            request,
            NOTE_URL,
            prefer_ultra_hd=False,
            prefer_youtube_merge=False,
            per_link_timeout_seconds=5,
            parser=parser,
            display_context_builder=lambda result, platform_key: {
                "image_items": [{"url": result.assets[0].url}],
                "video_items": [],
            },
            query_uses_third_party_provider_func=lambda _value: False,
        )

        self.assertEqual(row["status"], "success")
        self.assertNotIn("enable_xhs_livephoto_staff_preview", captured)

    def test_provider_health_parser_never_receives_staff_preview_option(self):
        item = {"key": "douyin_native_parse", "provider": "native"}
        request = self.factory.post("/admin/provider-health-tests/")
        request.user = User(username="staff", is_staff=True, is_active=True)

        with patch("xhs_download.views.ProviderRuntimeSetting.load"), patch(
            "xhs_download.views.ProviderHealthTestSetting.load"
        ), patch(
            "xhs_download.views.provider_test_runtime_info",
            return_value={"runtime_mode": "staff", "hard_enabled": True},
        ), patch(
            "xhs_download.views.provider_test_url_for",
            return_value="https://www.douyin.com/video/synthetic",
        ), patch(
            "xhs_download.views.provider_health_test_record",
            return_value=Mock(),
        ), patch(
            "xhs_download.views.parse_media_page",
            return_value=self.result,
        ) as parser:
            run_provider_health_test(request, item)

        self.assertNotIn(
            "enable_xhs_livephoto_staff_preview",
            parser.call_args.kwargs,
        )


class XhsLivePhotoStaffPresentationTests(SimpleTestCase):
    def setUp(self):
        self.request = RequestFactory().get("/")
        self.request.user = User(username="staff", is_staff=True, is_active=True)
        self.result = enhanced_result(
            [livephoto_item(0), regular_item(1), livephoto_item(2)]
        )

    def test_staff_template_groups_source_items_and_hides_download_controls(self):
        html = render_to_string(
            "partials/xhs_results_region.html",
            result_region_context(self.result, staff_preview=True),
            request=self.request,
        )
        soup = BeautifulSoup(html, "html.parser")

        self.assertEqual(len(soup.select("[data-livephoto-card]")), 2)
        self.assertEqual(len(soup.select(".xhs-livephoto-preview-card")), 3)
        self.assertEqual(len(soup.select(".xhs-livephoto-badge")), 2)
        self.assertEqual(
            len(soup.select(".xhs-livephoto-preview-media > [data-livephoto-toggle]")),
            2,
        )
        self.assertFalse(
            soup.select(".xhs-livephoto-preview-footer [data-livephoto-toggle]")
        )
        self.assertEqual(
            len(soup.select(".xhs-livephoto-preview-media > [data-livephoto-error]")),
            2,
        )
        self.assertTrue(
            all(
                card.get("data-livephoto-state") == "idle"
                for card in soup.select("[data-livephoto-card]")
            )
        )
        preview_root = soup.select_one("[data-xhs-livephoto-staff-preview]")
        self.assertRegex(
            preview_root.get("data-preview-script-url", ""),
            r"/static/js/xhs_livephoto_staff_preview\.js",
        )
        self.assertIn("不受项目设置控制", html)
        self.assertIsNone(soup.select_one("#download-btn"))
        self.assertIsNone(soup.select_one("#select-all-btn"))
        self.assertFalse(soup.select(".asset-card"))
        self.assertNotIn("download-progress-wrap", html)
        self.assertNotIn("proxy-image", html)
        for video in soup.select("[data-livephoto-motion]"):
            self.assertFalse(video.has_attr("src"))
            self.assertFalse(video.has_attr("loop"))
            self.assertEqual(video.get("preload"), "none")
            self.assertEqual(video.get("crossorigin"), "anonymous")
            self.assertEqual(video.get("referrerpolicy"), "no-referrer")

    def test_public_template_contains_no_livephoto_data_or_staff_diagnostic(self):
        public_result = flat_result([regular_item(0), regular_item(1)])
        public_request = RequestFactory().get("/")
        public_request.user = AnonymousUser()
        html = render_to_string(
            "partials/xhs_results_region.html",
            result_region_context(public_result, staff_preview=False),
            request=public_request,
        )

        self.assertNotIn("实况", html)
        self.assertNotIn("LivePhoto", html)
        self.assertNotIn("data-motion-url", html)
        self.assertNotIn("xhs_livephoto_staff_preview.js", html)
        self.assertNotIn(motion_url(0), html)
        self.assertIn('id="download-btn"', html)

    def test_public_shell_does_not_load_staff_preview_javascript(self):
        public_request = RequestFactory().get("/xhs/")
        public_request.user = AnonymousUser()
        html = render_to_string(
            "xhs_download.html",
            {
                "static_asset_version": "test",
                "image_items": [],
                "video_items": [],
                "error_message": "",
                "platform_notice": "",
                "provider_notice": "",
            },
            request=public_request,
        )

        self.assertNotIn("xhs_livephoto_staff_preview.js", html)
        self.assertNotIn("data-livephoto-staff-loader", html)

    def test_staff_shell_loads_preview_javascript(self):
        html = render_to_string(
            "xhs_download.html",
            {
                "static_asset_version": "test",
                "image_items": [],
                "video_items": [],
                "error_message": "",
                "platform_notice": "",
                "provider_notice": "",
            },
            request=self.request,
        )

        self.assertIn("xhs_livephoto_staff_preview.js", html)
        self.assertIn("data-livephoto-staff-loader", html)

    def test_staff_context_uses_contract_groups_not_asset_id_parsing(self):
        context = build_xhs_livephoto_staff_preview_context(self.result)

        self.assertEqual(
            [item["kind"] for item in context["items"]],
            ["live_photo", "regular_image", "live_photo"],
        )
        self.assertEqual(context["livephoto_count"], 2)
        self.assertEqual(context["regular_count"], 1)


class XhsLivePhotoStaffStaticBoundaryTests(SimpleTestCase):
    def test_staff_javascript_has_no_download_or_persistence_surface(self):
        source = (
            Path(__file__).resolve().parents[1]
            / "static"
            / "js"
            / "xhs_livephoto_staff_preview.js"
        ).read_text(encoding="utf-8")

        for forbidden in (
            "localStorage",
            "download-prepare",
            "download-stream",
            "download-status",
            "serviceWorker",
            "Blob(",
            "createObjectURL",
        ):
            self.assertNotIn(forbidden, source)
        self.assertNotRegex(source, re.compile(r"console\.(?:log|error|warn)"))

    def test_layout_is_viewport_bounded_and_controls_are_in_media(self):
        css = (
            Path(__file__).resolve().parents[1] / "static" / "css" / "app.css"
        ).read_text(encoding="utf-8")
        template = (
            Path(__file__).resolve().parents[1]
            / "templates"
            / "partials"
            / "xhs_livephoto_staff_preview.html"
        ).read_text(encoding="utf-8")

        self.assertIn("repeat(auto-fit, minmax(280px, 380px))", css)
        self.assertIn("height: clamp(320px, 56vh, 560px)", css)
        self.assertNotIn("aspect-ratio: 3 / 4", css)
        self.assertIn("xhs-livephoto-overlay-control", css)
        self.assertLess(
            template.index("data-livephoto-toggle"),
            template.index("xhs-livephoto-preview-footer"),
        )

    def test_async_staff_result_has_same_origin_lazy_script_bootstrap(self):
        source = (
            Path(__file__).resolve().parents[1]
            / "static"
            / "js"
            / "xhs_download.js"
        ).read_text(encoding="utf-8")

        self.assertIn("ensureLivePhotoStaffPreview", source)
        self.assertIn("scriptUrl.origin !== window.location.origin", source)
        self.assertIn(
            'scriptUrl.pathname.endsWith("/js/xhs_livephoto_staff_preview.js")',
            source,
        )
        self.assertRegex(
            source,
            re.compile(
                r"resultsRegion\.innerHTML = payload\.html \|\| \"\";\s+"
                r"void ensureLivePhotoStaffPreview\(\);"
            ),
        )
