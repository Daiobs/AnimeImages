import json
import os
import re
import threading
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from bs4 import BeautifulSoup
from django.contrib.auth.models import AnonymousUser, User
from django.template.loader import render_to_string
from django.test import RequestFactory, SimpleTestCase

from djangoProject.context_processors import (
    STATIC_VERSION_FILES,
    source_static_asset_version,
)

from .services.assets import MediaAsset, ParseResult
from .services.downloads import provider_manifest_allows_public_download
from .services.platforms.xiaohongshu import XiaohongshuAdapter
from .services.platforms import xhs_livephoto_builder
from .services.platforms.xhs_livephoto_staff_builder import (
    XHS_LIVEPHOTO_STAFF_MAX_PROBE_BYTES,
    XHS_LIVEPHOTO_STAFF_PROVIDER,
    XhsLivePhotoProbeEvidenceV1,
    XhsLivePhotoStaffPreviewError,
    build_xhs_livephoto_staff_preview_context,
    build_xhs_livephoto_staff_result,
    probe_xhs_livephoto_candidates,
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
    merge_xhs_livephoto_items_into_image_cards,
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


def result_region_context(result, *, staff_preview=False, public_preview=False):
    del public_preview
    display = build_result_display_context(
        result,
        probe_missing_video_sizes=False,
    )
    show_preview = bool(staff_preview)
    preview_context = (
        build_xhs_livephoto_staff_preview_context(
            result,
            client_identity="test-client",
        )
        if show_preview
        else {}
    )
    image_items = display["image_items"]
    video_items = display["video_items"]
    if show_preview:
        image_items = merge_xhs_livephoto_items_into_image_cards(
            image_items,
            preview_context,
        )
        video_items = []
    return {
        "error_message": "",
        "platform_notice": "",
        "provider_notice": "",
        "image_items": image_items,
        "video_items": video_items,
        "platform_name": display["platform_name"],
        "post_type": display["post_type"],
        "base_filename": display["base_filename"],
        "display_title": "脱敏测试标题",
        "display_author": "",
        "result_theme": "xiaohongshu",
        "show_internal_diagnostics": staff_preview,
        "show_xhs_livephoto_preview": show_preview,
        "xhs_livephoto_preview": preview_context,
        "show_xhs_livephoto_staff_preview": staff_preview,
        "xhs_livephoto_staff_preview": preview_context if staff_preview else {},
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

    def test_preview_assets_have_bound_manifest_but_no_ordinary_download_authority(self):
        result = enhanced_result([livephoto_item(0), regular_item(1)])

        self.assertTrue(result.assets)
        for asset in result.assets:
            self.assertEqual(asset.provider, XHS_LIVEPHOTO_STAFF_PROVIDER)
            self.assertTrue(asset.provider_manifest_token)
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

    def test_exported_probe_callback_returns_url_keyed_evidence(self):
        items = [livephoto_item(0)]

        def probe_exact(record, *, overall_deadline_monotonic):
            del overall_deadline_monotonic
            prefix = JPEG if record.locator.member_role == "still" else MP4
            return XhsLivePhotoProbeEvidenceV1(
                content_type="application/octet-stream",
                bytes_read=len(prefix),
                prefix=prefix,
            )

        with patch(
            "xhs_download.services.platforms.xhs_livephoto_builder."
            "_probe_exact_candidate",
            side_effect=probe_exact,
        ):
            outcome = build_xhs_livephoto_staff_result(
                flat_result=flat_result(items),
                snapshot=selected_snapshot(items),
                probe_candidates=probe_xhs_livephoto_candidates,
            )

        self.assertEqual(outcome.status, "enhanced")
        self.assertEqual(outcome.result.metadata["xhs_livephoto_delivery_ready_count"], 1)

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

    def test_probe_network_slots_fail_closed_without_starting_more_upstream_reads(self):
        acquired = 0
        try:
            for _index in range(xhs_livephoto_builder.XHS_LIVEPHOTO_MAX_PROBE_WORKERS):
                self.assertTrue(
                    xhs_livephoto_builder._XHS_LIVEPHOTO_PROBE_NETWORK_SLOTS.acquire(
                        blocking=False
                    )
                )
                acquired += 1

            with patch(
                "xhs_download.services.platforms.xhs_livephoto_staff_builder.requests.get"
            ) as request_get:
                result = XiaohongshuAdapter().parse_html(
                    page_html([livephoto_item(0)]),
                    source_url=NOTE_URL,
                    enable_xhs_livephoto_staff_preview=True,
                    deadline_monotonic=time.monotonic() + 0.5,
                )

            request_get.assert_not_called()
            self.assertIsNone(result.rich_media)
            self.assertEqual(
                result.metadata["xhs_livephoto_staff_preview_status"],
                "fallback",
            )
        finally:
            for _index in range(acquired):
                xhs_livephoto_builder._XHS_LIVEPHOTO_PROBE_NETWORK_SLOTS.release()

    def test_unverified_motion_host_keeps_typed_static_livephoto_item(self):
        items = [livephoto_item(0, host="sns-video-zl.xhscdn.com")]
        outcome = build_xhs_livephoto_staff_result(
            flat_result=flat_result(items),
            snapshot=selected_snapshot(items),
            probe_candidates=lambda records, deadline_monotonic: fake_probe(
                records,
                deadline_monotonic=deadline_monotonic,
            ),
        )
        result = outcome.result

        self.assertIsNotNone(result.rich_media)
        self.assertEqual(len(result.assets), 1)
        self.assertEqual(
            result.metadata["xhs_livephoto_staff_preview_status"],
            "enhanced",
        )
        self.assertEqual(result.metadata["xhs_livephoto_delivery_ready_count"], 0)
        self.assertEqual(
            result.metadata["xhs_livephoto_items"][0]["delivery_status"],
            "unavailable",
        )

    def test_one_unavailable_item_does_not_remove_other_ready_group(self):
        items = [
            livephoto_item(0, host="sns-video-zl.xhscdn.com"),
            livephoto_item(1),
        ]
        outcome = build_xhs_livephoto_staff_result(
            flat_result=flat_result(items),
            snapshot=selected_snapshot(items),
            probe_candidates=lambda records, deadline_monotonic: fake_probe(
                records,
                deadline_monotonic=deadline_monotonic,
            ),
        )
        result = outcome.result

        self.assertEqual(result.metadata["xhs_livephoto_delivery_ready_count"], 1)
        self.assertEqual(result.metadata["xhs_livephoto_delivery_unavailable_count"], 1)
        self.assertEqual(
            [item["delivery_status"] for item in result.metadata["xhs_livephoto_items"]],
            ["unavailable", "ready"],
        )
        self.assertEqual(len(result.rich_media.groups), 1)
        self.assertEqual(result.rich_media.groups[0].members[0].asset_id.endswith("i1:still"), True)
        self.assertEqual(result.rich_media.groups[0].members[1].asset_id.endswith("i1:motion"), True)
        self.assertEqual([asset.media_type for asset in result.assets], ["image", "image", "video"])

    def test_invalid_master_uses_same_item_backup_candidate(self):
        items = [livephoto_item(0)]
        invalid_backup_url = "https://sns-bak-v2.xhscdn.com/stream/backup-0.mp4?sig=fixture"
        backup_url = "https://sns-bak-v3.xhscdn.com/stream/backup-1.mp4?sig=fixture"
        items[0]["stream"]["h264"][0]["backupUrls"] = [
            invalid_backup_url,
            backup_url,
        ]
        probed_waves = []

        def probe_wave(records, *, deadline_monotonic):
            del deadline_monotonic
            probed_waves.append(
                tuple(
                    (record.locator.source_kind, record.locator.backup_index)
                    for record in records
                )
            )
            evidence = {}
            for record in records:
                if record.locator.member_role == "still":
                    prefix = JPEG
                elif (
                    record.locator.source_kind == "master_url"
                    or record.locator.backup_index == 0
                ):
                    prefix = JPEG
                else:
                    prefix = MP4
                evidence[record.https_candidate.value] = XhsLivePhotoProbeEvidenceV1(
                    content_type="application/octet-stream",
                    bytes_read=len(prefix),
                    prefix=prefix,
                )
            return evidence

        with patch(
            "xhs_download.services.platforms.xhs_livephoto_builder."
            "_probe_xhs_livephoto_candidates_partial",
            side_effect=probe_wave,
        ):
            outcome = build_xhs_livephoto_staff_result(
                flat_result=flat_result(items),
                snapshot=selected_snapshot(items),
            )

        result = outcome.result
        self.assertEqual(
            probed_waves,
            [
                (("url_default", None), ("master_url", None)),
                (("backup_url", 0),),
                (("backup_url", 1),),
            ],
        )
        self.assertEqual(result.metadata["xhs_livephoto_delivery_ready_count"], 1)
        self.assertEqual(result.video_assets[0].url, backup_url)
        self.assertEqual(
            result.metadata["xhs_livephoto_items"][0]["motion_source_kind"],
            "backup_url",
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

    def test_flat_adapter_path_does_not_enable_preview_without_explicit_option(self):
        adapter = XiaohongshuAdapter()
        with patch(
            "xhs_download.services.platforms.xiaohongshu.build_xhs_livephoto_result"
        ) as generic_builder, patch(
            "xhs_download.services.platforms.xiaohongshu.build_xhs_livephoto_staff_result"
        ) as staff_builder, patch(
            "xhs_download.services.platforms.xiaohongshu."
            "enhance_xhs_images_with_parsehub",
            return_value=Mock(result=flat_result([livephoto_item(0)])),
        ) as still_enhancer:
            result = adapter.parse_html(
                page_html([livephoto_item(0)]),
                source_url=NOTE_URL,
            )

        generic_builder.assert_not_called()
        staff_builder.assert_not_called()
        still_enhancer.assert_called_once()
        self.assertIsNone(result.rich_media)
        self.assertEqual(len(result.assets), 1)

    def test_explicit_generic_preview_option_remains_dormant_adapter_capability(self):
        adapter = XiaohongshuAdapter()
        items = [livephoto_item(0)]
        enhanced = enhanced_result(items)
        with patch(
            "xhs_download.services.platforms.xiaohongshu.build_xhs_livephoto_result",
            return_value=Mock(result=enhanced),
        ) as generic_builder, patch(
            "xhs_download.services.platforms.xiaohongshu.build_xhs_livephoto_staff_result"
        ) as staff_builder:
            result = adapter.parse_html(
                page_html(items),
                source_url=NOTE_URL,
                enable_xhs_livephoto_preview=True,
            )

        generic_builder.assert_called_once()
        staff_builder.assert_not_called()
        self.assertIsNotNone(result.rich_media)


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

    def test_only_active_staff_xhs_single_requests_enable_livephoto(self):
        staff = User(pk=1, username="staff", is_staff=True, is_active=True)
        public = AnonymousUser()
        inactive_staff = User(
            pk=2,
            username="inactive",
            is_staff=True,
            is_active=False,
        )

        staff_options = self.parse_options_for(staff)
        public_options = self.parse_options_for(public, "&xhs_livephoto=1")
        inactive_options = self.parse_options_for(
            inactive_staff,
            "&xhs_livephoto=1",
        )

        self.assertTrue(staff_options["enable_xhs_livephoto_staff_preview"])
        self.assertNotIn("enable_xhs_livephoto_preview", staff_options)
        self.assertNotIn("enable_xhs_livephoto_preview", public_options)
        self.assertNotIn("enable_xhs_livephoto_staff_preview", public_options)
        self.assertNotIn("enable_xhs_livephoto_preview", inactive_options)
        self.assertNotIn("enable_xhs_livephoto_staff_preview", inactive_options)

    def test_anonymous_xhs_request_does_not_create_pair_identity(self):
        request = self.factory.get("/?q=https://xhslink.cn/o/synthetic")
        request.user = AnonymousUser()

        with patch(
            "xhs_download.views.parse_media_page",
            return_value=self.result,
        ), patch(
            "xhs_download.views.get_xhs_livephoto_pair_client_identity"
        ) as pair_identity, patch("xhs_download.views.log_parse_request_event"):
            _context, status = build_search_results_context(request)

        self.assertEqual(status, 200)
        pair_identity.assert_not_called()

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
        self.assertNotIn("enable_xhs_livephoto_preview", captured)
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
        self.assertNotIn(
            "enable_xhs_livephoto_preview",
            parser.call_args.kwargs,
        )


class XhsLivePhotoStaffPresentationTests(SimpleTestCase):
    def setUp(self):
        self.request = RequestFactory().get("/")
        self.request.user = User(username="staff", is_staff=True, is_active=True)
        self.result = enhanced_result(
            [livephoto_item(0), regular_item(1), livephoto_item(2)]
        )

    def test_staff_template_unifies_gallery_selection_and_livephoto_playback(self):
        html = render_to_string(
            "partials/xhs_results_region.html",
            result_region_context(self.result, staff_preview=True),
            request=self.request,
        )
        soup = BeautifulSoup(html, "html.parser")

        self.assertEqual(len(soup.select(".asset-card[data-type='image']")), 3)
        self.assertEqual(len(soup.select("[data-livephoto-card]")), 2)
        self.assertEqual(len(soup.select(".asset-card[data-asset-kind='livephoto']")), 2)
        self.assertEqual(len(soup.select(".xhs-livephoto-badge")), 2)
        self.assertEqual(
            len(soup.select(".xhs-livephoto-asset-media > [data-livephoto-toggle]")),
            2,
        )
        self.assertEqual(
            len(soup.select(".xhs-livephoto-asset-media > [data-livephoto-error]")),
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
            r"/static/js/xhs_livephoto_preview\.js",
        )
        self.assertIsNotNone(soup.select_one("#download-btn"))
        self.assertIsNotNone(soup.select_one("#select-all-btn"))
        self.assertIsNotNone(soup.select_one("#download-progress-wrap"))
        self.assertEqual(len(soup.select(".asset-check")), 3)
        self.assertEqual(len(soup.select("button[data-asset-select-toggle]")), 3)
        self.assertTrue(
            all(
                control.get("aria-pressed") == "false"
                for control in soup.select("button[data-asset-select-toggle]")
            )
        )
        self.assertEqual(len(soup.select("[data-livephoto-download-original]")), 0)
        self.assertEqual(len(soup.select("[data-livephoto-download-pair]")), 0)
        self.assertNotIn("查看原图", html)
        self.assertTrue(
            all(
                image.get("src", "").startswith("?p=")
                for image in soup.select(".asset-card > img, .asset-card .xhs-livephoto-asset-media > img")
            )
        )
        self.assertEqual(len(soup.select("[data-livephoto-pair-token]")), 2)
        self.assertNotIn(motion_url(0), html)
        for video in soup.select("[data-livephoto-motion]"):
            self.assertFalse(video.has_attr("src"))
            self.assertFalse(video.has_attr("loop"))
            self.assertEqual(video.get("preload"), "none")
            self.assertEqual(video.get("crossorigin"), "anonymous")
            self.assertEqual(video.get("referrerpolicy"), "no-referrer")

    def test_unavailable_livephoto_is_not_selectable_or_playable(self):
        context = result_region_context(self.result, staff_preview=True)
        livephoto_item_context = context["image_items"][0]
        livephoto_item_context["livephoto_delivery_status"] = "unavailable"
        livephoto_item_context["livephoto_playback_status"] = "unavailable"
        livephoto_item_context["livephoto_playback_error_code"] = (
            "XHS_LIVEPHOTO_MOTION_CANDIDATE_UNAVAILABLE"
        )
        livephoto_item_context["livephoto_motion_token"] = ""
        livephoto_item_context["livephoto_pair_download_status"] = "unavailable"
        livephoto_item_context["livephoto_pair_token"] = ""

        html = render_to_string(
            "partials/xhs_results_region.html",
            context,
            request=self.request,
        )
        soup = BeautifulSoup(html, "html.parser")
        card = soup.select_one("[data-livephoto-item]")

        self.assertEqual(card.get("data-download-selectable"), "false")
        self.assertFalse(card.has_attr("data-livephoto-card"))
        self.assertIsNone(card.select_one("[data-livephoto-toggle]"))

    def test_preview_merge_fails_closed_when_still_identity_does_not_match(self):
        display = build_result_display_context(
            self.result,
            probe_missing_video_sizes=False,
        )
        preview_context = build_xhs_livephoto_staff_preview_context(
            self.result,
            client_identity="test-client",
        )
        preview_context["items"][0]["still_asset_id"] = "different-asset"

        with self.assertRaisesMessage(
            ValueError,
            "XHS_LIVEPHOTO_STAFF_PREVIEW_RESULT_INVALID",
        ):
            merge_xhs_livephoto_items_into_image_cards(
                display["image_items"],
                preview_context,
            )

    def test_staff_diagnostic_distinguishes_still_pair_authority_from_motion(self):
        context = result_region_context(self.result, staff_preview=True)
        livephoto_item_context = context["image_items"][0]
        livephoto_item_context["livephoto_delivery_status"] = "unavailable"
        livephoto_item_context["livephoto_delivery_error_code"] = (
            "XHS_LIVEPHOTO_PAIR_STILL_REJECTED"
        )
        livephoto_item_context["livephoto_pair_download_status"] = "unavailable"
        livephoto_item_context["livephoto_pair_download_error_code"] = (
            "XHS_LIVEPHOTO_PAIR_STILL_REJECTED"
        )
        livephoto_item_context["livephoto_pair_token"] = ""

        html = render_to_string(
            "partials/xhs_results_region.html",
            context,
            request=self.request,
        )

        self.assertIn("当前静态原图未通过浏览器配对下载准入", html)
        self.assertNotIn("当前 motion 候选未通过", html)
        soup = BeautifulSoup(html, "html.parser")
        card = soup.select_one("[data-livephoto-item]")
        self.assertEqual(card.get("data-download-selectable"), "false")
        self.assertIsNotNone(card.select_one("[data-livephoto-toggle]"))
        self.assertTrue(card.has_attr("data-livephoto-motion-token"))
        self.assertFalse(card.has_attr("data-livephoto-pair-token"))

    def test_anonymous_template_uses_ordinary_flat_image_presentation(self):
        public_request = RequestFactory().get("/")
        public_request.user = AnonymousUser()
        public_result = flat_result(
            [regular_item(0), regular_item(1), regular_item(2)]
        )
        html = render_to_string(
            "partials/xhs_results_region.html",
            result_region_context(public_result),
            request=public_request,
        )
        soup = BeautifulSoup(html, "html.parser")

        self.assertEqual(len(soup.select(".asset-card[data-type='image']")), 3)
        self.assertIsNotNone(soup.select_one("#download-btn"))
        self.assertIsNotNone(soup.select_one("#select-all-btn"))
        self.assertEqual(len(soup.select("[data-xhs-livephoto-preview]")), 0)
        self.assertEqual(len(soup.select(".xhs-livephoto-badge")), 0)
        self.assertEqual(len(soup.select("[data-livephoto-card]")), 0)
        self.assertEqual(len(soup.select("video")), 0)
        self.assertEqual(len(soup.select("[data-livephoto-toggle]")), 0)
        self.assertEqual(len(soup.select("[data-livephoto-download-original]")), 0)
        self.assertEqual(len(soup.select("[data-livephoto-download-pair]")), 0)
        self.assertEqual(len(soup.select("[data-livephoto-pair-token]")), 0)
        self.assertEqual(len(soup.select("[data-asset-select-toggle]")), 0)
        self.assertNotIn("下载实况", html)
        self.assertNotIn("实况照片", html)
        self.assertNotIn("查看原图", html)
        self.assertNotIn("data-motion-url", html)
        self.assertNotIn(motion_url(0), html)
        self.assertNotIn("诊断：", html)
        self.assertIsNone(soup.select_one("[data-xhs-livephoto-staff-preview]"))

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

        self.assertNotIn("xhs_livephoto_preview.js", html)
        self.assertNotIn("data-livephoto-loader", html)

    def test_anonymous_shell_never_loads_pair_or_playback_javascript(self):
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
                "show_xhs_livephoto_preview": True,
            },
            request=public_request,
        )

        self.assertNotIn("xhs_livephoto_pair_download.js", html)
        self.assertNotIn("data-livephoto-pair-loader", html)
        self.assertNotIn("xhs_livephoto_preview.js", html)
        self.assertNotIn("data-livephoto-loader", html)

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
                "show_xhs_livephoto_preview": True,
                "show_xhs_livephoto_staff_preview": True,
            },
            request=self.request,
        )

        self.assertIn("xhs_livephoto_preview.js", html)
        self.assertIn("data-livephoto-loader", html)
        self.assertIn("xhs_livephoto_pair_download.js", html)

        shell = BeautifulSoup(html, "html.parser")
        shell_script = shell.select_one("script[data-livephoto-loader]")
        preview_html = render_to_string(
            "partials/xhs_results_region.html",
            result_region_context(self.result, staff_preview=True)
            | {"static_asset_version": "test"},
            request=self.request,
        )
        preview = BeautifulSoup(preview_html, "html.parser")
        preview_root = preview.select_one("[data-xhs-livephoto-preview]")

        self.assertEqual(
            shell_script.get("src"),
            preview_root.get("data-preview-script-url"),
        )
        self.assertTrue(shell_script.get("src", "").endswith("?v=test"))

    def test_staff_context_uses_contract_groups_not_asset_id_parsing(self):
        context = build_xhs_livephoto_staff_preview_context(self.result)

        self.assertEqual(
            [item["kind"] for item in context["items"]],
            ["live_photo", "regular_image", "live_photo"],
        )
        self.assertEqual(context["livephoto_count"], 2)
        self.assertEqual(context["regular_count"], 1)


class XhsLivePhotoStaffStaticBoundaryTests(SimpleTestCase):
    def test_staff_javascript_participates_in_static_cache_versioning(self):
        self.assertIn(
            "static/js/xhs_livephoto_preview.js",
            STATIC_VERSION_FILES,
        )
        self.assertIn(
            "static/js/xhs_livephoto_pair_download.js",
            STATIC_VERSION_FILES,
        )
        self.assertIn(
            "static/js/browser_photo_archive.js",
            STATIC_VERSION_FILES,
        )

    def test_controller_only_source_change_updates_static_version(self):
        relative_path = "static/js/xhs_livephoto_preview.js"
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            controller = root / relative_path
            controller.parent.mkdir(parents=True)
            controller.write_text("first", encoding="utf-8")
            os.utime(controller, (100, 100))

            with patch(
                "djangoProject.context_processors.settings.BASE_DIR",
                root,
            ), patch(
                "djangoProject.context_processors.STATIC_VERSION_FILES",
                (relative_path,),
            ):
                before = source_static_asset_version()
                os.utime(controller, (200, 200))
                after = source_static_asset_version()

        self.assertEqual(before, "100")
        self.assertEqual(after, "200")

    def test_staff_javascript_has_no_download_or_persistence_surface(self):
        source = (
            Path(__file__).resolve().parents[1]
            / "static"
            / "js"
            / "xhs_livephoto_preview.js"
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
            / "xhs_results_region.html"
        ).read_text(encoding="utf-8")

        self.assertIn("repeat(auto-fill, minmax(180px, 1fr))", css)
        self.assertIn(".xhs-livephoto-asset-media", css)
        self.assertIn("height: 220px", css)
        self.assertIn("xhs-livephoto-overlay-control", css)
        self.assertIn("class=\"asset-card", template)
        self.assertIn("data-livephoto-toggle", template)
        self.assertNotIn("data-livephoto-download-pair", template)

    def test_unified_download_preserves_source_order_and_locks_playback(self):
        source = (
            Path(__file__).resolve().parents[1]
            / "static"
            / "js"
            / "xhs_download.js"
        ).read_text(encoding="utf-8")

        self.assertIn(
            ".sort((left, right) => left.display_ordinal - right.display_ordinal)",
            source,
        )
        self.assertIn('card.querySelector("[data-livephoto-toggle]")', source)
        self.assertIn('playbackButton.setAttribute("aria-disabled", "true")', source)
        self.assertIn('event.target.closest("[data-livephoto-toggle]")', source)
        self.assertIn('event.target.closest("[data-asset-select-toggle]")', source)
        self.assertIn('selectionControl.setAttribute("aria-pressed"', source)

    def test_async_staff_result_has_same_origin_lazy_script_bootstrap(self):
        source = (
            Path(__file__).resolve().parents[1]
            / "static"
            / "js"
            / "xhs_download.js"
        ).read_text(encoding="utf-8")

        self.assertIn("ensureLivePhotoPreview", source)
        self.assertIn("scriptUrl.origin === window.location.origin", source)
        self.assertIn(
            '"/js/xhs_livephoto_preview.js"',
            source,
        )
        self.assertIn('"/js/xhs_livephoto_pair_download.js"', source)
        self.assertRegex(
            source,
            re.compile(
                r"resultsRegion\.innerHTML = payload\.html \|\| \"\";\s+"
                r"void ensureLivePhotoPreview\(\);"
            ),
        )
