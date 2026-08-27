import asyncio
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
import sys
import threading
import time
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

from bs4 import BeautifulSoup
from django.core.cache import cache
from django.test import RequestFactory, SimpleTestCase
from django.template.loader import render_to_string

from .services.assets import MediaAsset, ParseResult
from .services.download_routing import (
    ROUTE_BLOCKED,
    ROUTE_DIRECT,
    decide_download_route,
)
from .services.downloads import provider_manifest_allows_public_download
from .services.platforms import xhs_parsehub_images as parsehub_images
from .services.platforms.xhs_livephoto_builder import (
    XhsLivePhotoProbeEvidenceV1,
    bind_xhs_livephoto_still_result,
    build_xhs_livephoto_preview_context,
    build_xhs_livephoto_result,
)
from .services.platforms.xhs_page_state import extract_xhs_page_state
from .services.platforms.xhs_tiantian_hybrid import enhance_xhs_ordinary_video
from .services.platforms.xiaohongshu import XiaohongshuAdapter
from .services.batch_parse.results import build_batch_result_row
from .views import build_result_display_context


NOTE_ID = "64a1b2c3d4e5f6789012abcd"
NOTE_URL = f"https://www.xiaohongshu.com/explore/{NOTE_ID}"
TRACE_PREFIX = "0123456789abcdef0123456789abcdef"
RAW_HOST = parsehub_images.PARSEHUB_RAW_IMAGE_HOST
MP4_PREFIX = b"\x00\x00\x00\x18ftypisom" + (b"M" * 12)


def source_resource(index):
    return f"notes_pre_post/resource-{index}"


def native_source_url(index):
    return (
        f"https://sns-webpic-qc.xhscdn.com/{TRACE_PREFIX}/"
        f"{source_resource(index)}!nd_dft_wlteh_webp_3"
    )


def raw_url(index):
    return f"https://{RAW_HOST}/{source_resource(index)}"


def native_item(index, *, livephoto=False):
    value = native_source_url(index)
    item = {
        "livePhoto": bool(livephoto),
        "urlDefault": value,
        "infoList": [{"imageScene": "WB_DFT", "url": value}],
        "stream": {},
    }
    if livephoto:
        item["stream"] = {
            "h264": [
                {
                    "masterUrl": (
                        "https://sns-video-v7.xhscdn.com/stream/"
                        f"motion-{index}.mp4"
                    ),
                    "backupUrls": [],
                }
            ]
        }
    return item


def snapshot_for(items):
    payload = {
        "note": {
            "noteDetailMap": {
                NOTE_ID: {
                    "note": {
                        "noteId": NOTE_ID,
                        "type": "normal",
                        "title": "fixture",
                        "imageList": items,
                    }
                }
            }
        }
    }
    html = (
        "<script>window.__INITIAL_STATE__="
        f"{json.dumps(payload, separators=(',', ':'))};</script>"
    )
    state = extract_xhs_page_state(
        BeautifulSoup(html, "html.parser"),
        source_url=NOTE_URL,
    )
    return state.private_selected_note_snapshot


def page_html_for(items, *, note_type="normal"):
    payload = {
        "note": {
            "noteDetailMap": {
                NOTE_ID: {
                    "note": {
                        "noteId": NOTE_ID,
                        "type": note_type,
                        "title": "fixture",
                        "user": {"nickname": "fixture-author"},
                        "imageList": items,
                    }
                }
            }
        }
    }
    return (
        "<script>window.__INITIAL_STATE__="
        f"{json.dumps(payload, separators=(',', ':'))};</script>"
    )


def native_result(items, *, identity_verified=True, post_type="image"):
    return ParseResult(
        platform="xiaohongshu",
        post_type=post_type,
        base_filename="xhs_fixture",
        assets=tuple(
            MediaAsset(
                media_type="image" if post_type == "image" else "video",
                url=native_source_url(index),
                platform="xiaohongshu",
                preview_url=native_source_url(index),
                filename=f"xhs_fixture_{index + 1:02d}.jpg",
                ext="jpg",
                source_platform="xiaohongshu",
            )
            for index, _item in enumerate(items)
        ),
        metadata={
            "note_id": NOTE_ID,
            "source_identity_verified": identity_verified,
            "page_state": "available",
        },
    )


def worker_payload(items):
    return {
        "schema_version": parsehub_images.PARSEHUB_SCHEMA_VERSION,
        "provider": parsehub_images.PARSEHUB_XHS_PROVIDER_ID,
        "provider_version": parsehub_images.PARSEHUB_PROVIDER_VERSION,
        "claimed_platform": "xiaohongshu",
        "claimed_note_id": NOTE_ID,
        "post_type": "image",
        "items": [
            {
                "source_ordinal": index,
                "source_kind": parsehub_images.xhs_image_source_kind(item),
                "source_fingerprint": (
                    parsehub_images.xhs_image_source_fingerprint(item)
                ),
                "raw_https_url": raw_url(index),
                "declared_extension": "jpg",
            }
            for index, item in enumerate(items)
        ],
    }


def probe_for(index, *, extension="jpg", bytes_read=64):
    mime_types = {
        "jpg": "image/jpeg",
        "png": "image/png",
        "gif": "image/gif",
        "webp": "image/webp",
        "avif": "image/avif",
        "heic": "image/heic",
    }
    return parsehub_images.XhsParseHubProbeV1(
        final_url=raw_url(index),
        extension=extension,
        mime_type=mime_types[extension],
        size_bytes=1000 + index,
        width=2400 + index,
        height=3200 + index,
        bytes_read=bytes_read,
    )


def jpeg_prefix(width=640, height=480):
    return (
        b"\xff\xd8\xff\xc0\x00\x11\x08"
        + height.to_bytes(2, "big")
        + width.to_bytes(2, "big")
        + (b"\x00" * 10)
    )


def png_prefix(width=640, height=480):
    return (
        b"\x89PNG\r\n\x1a\n"
        + b"\x00\x00\x00\x0dIHDR"
        + width.to_bytes(4, "big")
        + height.to_bytes(4, "big")
        + b"\x08\x06\x00\x00\x00"
    )


def gif_prefix(width=640, height=480):
    return b"GIF89a" + width.to_bytes(2, "little") + height.to_bytes(2, "little")


def webp_prefix(width=640, height=480):
    return (
        b"RIFF\x16\x00\x00\x00WEBPVP8X"
        + b"\x0a\x00\x00\x00"
        + b"\x00\x00\x00\x00"
        + (width - 1).to_bytes(3, "little")
        + (height - 1).to_bytes(3, "little")
    )


def bmff_prefix(brand, width=640, height=480):
    return (
        b"\x00\x00\x00\x18ftyp"
        + brand
        + b"\x00\x00\x00\x00"
        + brand
        + b"mif1"
        + b"\x00\x00\x00\x14ispe"
        + b"\x00\x00\x00\x00"
        + width.to_bytes(4, "big")
        + height.to_bytes(4, "big")
    )


class FakeResponse:
    def __init__(self, body, *, url, status=206, content_type="application/octet-stream", headers=None):
        self.status_code = status
        self.url = url
        self.headers = {
            "Content-Type": content_type,
            "Content-Range": f"bytes 0-{max(0, len(body) - 1)}/{len(body) + 100}",
            **(headers or {}),
        }
        self.body = body
        self.closed = False

    def iter_content(self, chunk_size):
        for index in range(0, len(self.body), max(1, chunk_size)):
            yield self.body[index:index + chunk_size]

    def close(self):
        self.closed = True


class ParseHubRawUrlPolicyTests(SimpleTestCase):
    def test_exact_https_host_and_resource_path_are_accepted(self):
        self.assertEqual(
            parsehub_images.validate_xhs_parsehub_raw_url(raw_url(0)),
            raw_url(0),
        )

    def test_untrusted_url_shapes_are_rejected(self):
        rejected = (
            raw_url(0).replace("https://", "http://"),
            "https://sns-img-hw.xhscdn.com.evil.example/resource",
            "https://127.0.0.1/resource",
            "https://sns-img-hw.xhscdn.com./resource",
            "https://user@sns-img-hw.xhscdn.com/resource",
            "https://sns-img-hw.xhscdn.com:444/resource",
            "https://sns-img-hw.xhscdn.com/",
            "https://sns-img-hw.xhscdn.com/resource?token=forbidden",
            "https://sns-img-hw.xhscdn.com/resource#fragment",
            "https://sns-img-hw.xhscdn.com/../resource",
        )
        for value in rejected:
            with self.subTest(value=value), self.assertRaises(
                parsehub_images.XhsParseHubImageError
            ):
                parsehub_images.validate_xhs_parsehub_raw_url(value)

    def test_source_fingerprint_uses_the_exact_item_resource_key(self):
        first = native_item(0)
        equivalent = dict(first, urlDefault=f"{native_source_url(0)}?ignored=1")
        different = native_item(1)

        self.assertEqual(
            parsehub_images.xhs_image_source_fingerprint(first),
            parsehub_images.xhs_image_source_fingerprint(equivalent),
        )
        self.assertNotEqual(
            parsehub_images.xhs_image_source_fingerprint(first),
            parsehub_images.xhs_image_source_fingerprint(different),
        )


class ParseHubRawProbeTests(SimpleTestCase):
    def test_magic_and_dimensions_cover_all_admitted_formats(self):
        cases = (
            (jpeg_prefix(), "jpg", "image/jpeg"),
            (png_prefix(), "png", "image/png"),
            (gif_prefix(), "gif", "image/gif"),
            (webp_prefix(), "webp", "image/webp"),
            (bmff_prefix(b"avif"), "avif", "image/avif"),
            (bmff_prefix(b"heic"), "heic", "image/heic"),
        )
        for body, extension, mime_type in cases:
            with self.subTest(extension=extension):
                response = FakeResponse(body, url=raw_url(0))
                requester = Mock(return_value=response)

                result = parsehub_images.probe_xhs_parsehub_raw_image(
                    raw_url(0),
                    requester=requester,
                )

                self.assertEqual(result.extension, extension)
                self.assertEqual(result.mime_type, mime_type)
                self.assertEqual((result.width, result.height), (640, 480))
                self.assertLessEqual(result.bytes_read, 4096)
                self.assertEqual(result.size_bytes, len(body) + 100)
                self.assertEqual(
                    requester.call_args.kwargs["headers"]["Range"],
                    "bytes=0-4095",
                )
                self.assertNotIn("Cookie", requester.call_args.kwargs["headers"])
                self.assertNotIn("cookies", requester.call_args.kwargs)
                self.assertTrue(response.closed)

    def test_invalid_document_and_unknown_binary_are_rejected(self):
        bodies = (
            b"<!doctype html><html></html>",
            b'{"error":true}',
            b"<?xml version='1.0'?><error/>",
            b"<svg xmlns='http://www.w3.org/2000/svg'/>",
            b"\x01\x02\x03\x04",
        )
        for body in bodies:
            with self.subTest(body=body[:12]):
                response = FakeResponse(body, url=raw_url(0))
                with self.assertRaises(parsehub_images.XhsParseHubImageError):
                    parsehub_images.probe_xhs_parsehub_raw_image(
                        raw_url(0),
                        requester=Mock(return_value=response),
                    )
                self.assertTrue(response.closed)

    def test_truncated_bmff_dimension_box_is_rejected(self):
        body = (
            b"\x00\x00\x00\x18ftypavif\x00\x00\x00\x00avifmif1"
            b"\x00\x00\x00\x14ispe"
            b"\x00\x00\x00\x00"
            b"\x00\x02\x80"
        )
        response = FakeResponse(body, url=raw_url(0))

        with self.assertRaises(parsehub_images.XhsParseHubImageError):
            parsehub_images.probe_xhs_parsehub_raw_image(
                raw_url(0),
                requester=Mock(return_value=response),
            )
        self.assertTrue(response.closed)

    def test_declared_image_type_without_magic_is_rejected(self):
        response = FakeResponse(
            b"not-an-image",
            url=raw_url(0),
            content_type="image/jpeg",
        )
        with self.assertRaises(parsehub_images.XhsParseHubImageError):
            parsehub_images.probe_xhs_parsehub_raw_image(
                raw_url(0), requester=Mock(return_value=response)
            )

    def test_redirect_to_unknown_host_is_rejected_before_following(self):
        response = FakeResponse(
            b"",
            url=raw_url(0),
            status=302,
            headers={"Location": "https://untrusted.example/image"},
        )
        requester = Mock(return_value=response)

        with self.assertRaises(parsehub_images.XhsParseHubImageError):
            parsehub_images.probe_xhs_parsehub_raw_image(
                raw_url(0), requester=requester
            )

        self.assertEqual(requester.call_count, 1)

    def test_bounded_prefix_discards_body_after_4096_bytes(self):
        body = png_prefix() + (b"x" * 8192)
        result = parsehub_images.probe_xhs_parsehub_raw_image(
            raw_url(0),
            requester=Mock(return_value=FakeResponse(body, url=raw_url(0))),
        )

        self.assertEqual(result.bytes_read, 4096)


class ParseHubCorrespondenceTests(SimpleTestCase):
    def setUp(self):
        self.items = [native_item(0), native_item(1, livephoto=True)]
        self.snapshot = snapshot_for(self.items)

    def test_exact_identity_count_order_fingerprint_and_kind_pass(self):
        candidates = parsehub_images.validate_parsehub_correspondence(
            worker_payload(self.items),
            snapshot=self.snapshot,
        )

        self.assertEqual([item.source_ordinal for item in candidates], [0, 1])
        self.assertEqual(
            [item.source_kind for item in candidates],
            ["regular_image", "livephoto_still"],
        )

    def test_count_fingerprint_order_and_kind_mismatches_fail_closed(self):
        mutations = []
        count = worker_payload(self.items)
        count["items"] = count["items"][:1]
        mutations.append(count)
        fingerprint = worker_payload(self.items)
        fingerprint["items"][0]["source_fingerprint"] = "0" * 64
        mutations.append(fingerprint)
        order = worker_payload(self.items)
        order["items"] = list(reversed(order["items"]))
        mutations.append(order)
        kind = worker_payload(self.items)
        kind["items"][0]["source_kind"] = "livephoto_still"
        mutations.append(kind)

        for payload in mutations:
            with self.subTest(payload=payload), self.assertRaises(
                parsehub_images.XhsParseHubImageError
            ):
                parsehub_images.validate_parsehub_correspondence(
                    payload,
                    snapshot=self.snapshot,
                )

    def test_claimed_identity_mismatch_is_rejected_before_correspondence(self):
        payload = worker_payload(self.items)
        payload["claimed_note_id"] = "74a1b2c3d4e5f6789012abcd"

        with self.assertRaises(parsehub_images.XhsParseHubImageError):
            parsehub_images._parse_worker_payload(
                json.dumps(payload),
                note_id=NOTE_ID,
            )

    def test_logical_cache_identity_does_not_depend_on_url_form(self):
        self.assertEqual(
            parsehub_images._source_cache_key(NOTE_ID),
            parsehub_images._source_cache_key(NOTE_ID),
        )
        self.assertNotIn(NOTE_ID, parsehub_images._source_cache_key(NOTE_ID))


class ParseHubEnhancementTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.items = [native_item(0), native_item(1), native_item(2)]
        self.snapshot = snapshot_for(self.items)
        self.native = native_result(self.items)
        self.enabled = patch.object(
            parsehub_images,
            "parsehub_provider_enabled",
            return_value=True,
        )
        self.enabled.start()

    def tearDown(self):
        self.enabled.stop()
        cache.clear()

    def enhance(self, *, payload=None, probe=None, result=None, snapshot=None):
        payload_loader = Mock(return_value=payload or worker_payload(self.items))
        probe_candidate = probe or (
            lambda url, **_kwargs: probe_for(int(url.rsplit("-", 1)[-1]))
        )
        outcome = parsehub_images.enhance_xhs_images_with_parsehub(
            native_result=result or self.native,
            snapshot=self.snapshot if snapshot is None else snapshot,
            source_url=NOTE_URL,
            payload_loader=payload_loader,
            probe_candidate=probe_candidate,
        )
        return outcome, payload_loader

    def test_raw_candidates_are_selected_in_native_order_with_separate_preview(self):
        outcome, loader = self.enhance()

        self.assertEqual(outcome.worker_status, "enhanced")
        self.assertEqual(outcome.selected_count, 3)
        self.assertEqual(outcome.fallback_count, 0)
        self.assertEqual([asset.url for asset in outcome.result.assets], [
            raw_url(0), raw_url(1), raw_url(2)
        ])
        self.assertTrue(all(asset.url != asset.preview_url for asset in outcome.result.assets))
        self.assertTrue(all("/w/1280/" in asset.preview_url for asset in outcome.result.assets))
        self.assertTrue(all(asset.ext == "jpg" for asset in outcome.result.assets))
        self.assertEqual(loader.call_count, 1)

    def test_single_probe_failure_uses_native_fallback_only_for_that_item(self):
        def probe(url, **_kwargs):
            index = int(url.rsplit("-", 1)[-1])
            if index == 1:
                raise parsehub_images.XhsParseHubImageError(
                    "PARSEHUB_RESULT_INVALID"
                )
            return probe_for(index, extension="png" if index == 2 else "jpg")

        outcome, _loader = self.enhance(probe=probe)

        self.assertEqual(outcome.selected_count, 2)
        self.assertEqual(outcome.fallback_count, 1)
        self.assertEqual(outcome.result.assets[0].provider, "parsehub_xhs_images")
        self.assertEqual(outcome.result.assets[1].provider, "")
        self.assertEqual(outcome.result.assets[1].ext, "jpg")
        self.assertEqual(outcome.result.assets[2].ext, "png")
        self.assertTrue(outcome.result.assets[2].filename.endswith(".png"))

    def test_correspondence_failure_rejects_the_complete_enhancement(self):
        mismatched = worker_payload(self.items)
        mismatched["items"][1]["source_fingerprint"] = "0" * 64
        probe = Mock()

        outcome, _loader = self.enhance(payload=mismatched, probe=probe)

        self.assertEqual(outcome.worker_status, "fallback")
        self.assertEqual(outcome.selected_count, 0)
        self.assertEqual(outcome.fallback_count, 3)
        self.assertTrue(all(not asset.provider for asset in outcome.result.assets))
        probe.assert_not_called()

    def test_disabled_ineligible_video_and_unverified_identity_do_not_start_worker(self):
        cases = (
            (self.native, self.snapshot, False),
            (native_result(self.items, post_type="video"), self.snapshot, True),
            (native_result(self.items, identity_verified=False), self.snapshot, True),
            (self.native, None, True),
        )
        for result, snapshot, enabled in cases:
            with self.subTest(post_type=result.post_type, enabled=enabled), patch.object(
                parsehub_images,
                "parsehub_provider_enabled",
                return_value=enabled,
            ):
                loader = Mock()
                parsehub_images.enhance_xhs_images_with_parsehub(
                    native_result=result,
                    snapshot=snapshot,
                    source_url=NOTE_URL,
                    payload_loader=loader,
                    probe_candidate=Mock(),
                )
                loader.assert_not_called()

    def test_ordinary_heic_is_retained_but_livephoto_heic_uses_compatible_still(self):
        ordinary = [native_item(0)]
        ordinary_outcome = parsehub_images.enhance_xhs_images_with_parsehub(
            native_result=native_result(ordinary),
            snapshot=snapshot_for(ordinary),
            source_url=NOTE_URL,
            payload_loader=Mock(return_value=worker_payload(ordinary)),
            probe_candidate=lambda *_args, **_kwargs: probe_for(0, extension="heic"),
        )
        live = [native_item(0, livephoto=True)]
        live_outcome = parsehub_images.enhance_xhs_images_with_parsehub(
            native_result=native_result(live),
            snapshot=snapshot_for(live),
            source_url=NOTE_URL,
            payload_loader=Mock(return_value=worker_payload(live)),
            probe_candidate=lambda *_args, **_kwargs: probe_for(0, extension="heic"),
        )

        self.assertEqual(ordinary_outcome.result.assets[0].ext, "heic")
        self.assertEqual(ordinary_outcome.result.assets[0].provider, "parsehub_xhs_images")
        self.assertEqual(live_outcome.result.assets[0].ext, "jpg")
        self.assertEqual(live_outcome.result.assets[0].provider, "")

    def test_internal_enhancement_deadline_is_bounded_to_eight_seconds(self):
        observed = []

        def loader(**kwargs):
            observed.append(kwargs["deadline_monotonic"] - time.monotonic())
            return worker_payload(self.items)

        parsehub_images.enhance_xhs_images_with_parsehub(
            native_result=self.native,
            snapshot=self.snapshot,
            source_url=NOTE_URL,
            payload_loader=loader,
            probe_candidate=lambda url, **_kwargs: probe_for(
                int(url.rsplit("-", 1)[-1])
            ),
        )

        self.assertEqual(len(observed), 1)
        self.assertGreater(observed[0], 0)
        self.assertLessEqual(observed[0], 8.0)


class ParseHubRuntimeIntegrationTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.items = [native_item(0), native_item(1)]

    def tearDown(self):
        cache.clear()

    def adapter_enhancer(self, **kwargs):
        return parsehub_images.enhance_xhs_images_with_parsehub(
            **kwargs,
            payload_loader=Mock(return_value=worker_payload(self.items)),
            probe_candidate=lambda url, **_probe_kwargs: probe_for(
                int(url.rsplit("-", 1)[-1]),
                extension=("jpg", "png")[int(url.rsplit("-", 1)[-1])],
            ),
        )

    def test_single_and_batch_share_raw_urls_extensions_order_and_preview(self):
        with patch.object(
            parsehub_images,
            "parsehub_provider_enabled",
            return_value=True,
        ), patch(
            "xhs_download.services.platforms.xiaohongshu.enhance_xhs_images_with_parsehub",
            side_effect=self.adapter_enhancer,
        ):
            result = XiaohongshuAdapter().parse_html(
                page_html_for(self.items),
                source_url=NOTE_URL,
            )

        single = build_result_display_context(
            result,
            probe_missing_video_sizes=False,
        )
        request = RequestFactory().post("/batch/")
        row = build_batch_result_row(
            request,
            NOTE_URL,
            prefer_ultra_hd=False,
            prefer_youtube_merge=False,
            per_link_timeout_seconds=5,
            parser=Mock(return_value=result),
            display_context_builder=lambda parsed, platform_key: (
                build_result_display_context(
                    parsed,
                    platform_key=platform_key,
                    probe_missing_video_sizes=False,
                )
            ),
            query_uses_third_party_provider_func=lambda _value: False,
        )

        expected_urls = [raw_url(0), raw_url(1)]
        expected_extensions = ["jpg", "png"]
        self.assertEqual(
            [item["url"] for item in single["image_items"]],
            expected_urls,
        )
        self.assertEqual(
            [item["download_extension"] for item in single["image_items"]],
            expected_extensions,
        )
        self.assertEqual(
            [item["url"] for item in row["image_items"]],
            expected_urls,
        )
        self.assertEqual(
            [item["download_extension"] for item in row["image_items"]],
            expected_extensions,
        )
        self.assertTrue(
            all(
                item["url"] != item["card_preview_url"]
                and "/w/1280/" in item["card_preview_url"]
                for item in single["image_items"]
            )
        )
        self.assertTrue(
            all(item["provider"] == "parsehub_xhs_images" for item in row["image_items"])
        )

        single_html = render_to_string(
            "partials/xhs_results_region.html",
            {
                **single,
                "base_filename": "fixture",
                "proxy_image_url": "/proxy-image/",
            },
        )
        self.assertIn('data-download-filename-stem="fixture_01"', single_html)
        self.assertIn('data-download-extension-hint="jpg"', single_html)
        self.assertIn('data-download-filename-stem="fixture_02"', single_html)
        self.assertIn('data-download-extension-hint="png"', single_html)

    def test_livephoto_uses_parsehub_still_native_motion_and_public_safe_metadata(self):
        items = [native_item(0, livephoto=True), native_item(1)]
        with patch.object(
            parsehub_images,
            "parsehub_provider_enabled",
            return_value=True,
        ):
            enhanced = parsehub_images.enhance_xhs_images_with_parsehub(
                native_result=native_result(items),
                snapshot=snapshot_for(items),
                source_url=NOTE_URL,
                payload_loader=Mock(return_value=worker_payload(items)),
                probe_candidate=lambda url, **_kwargs: probe_for(
                    int(url.rsplit("-", 1)[-1]),
                    extension=("jpg", "png")[int(url.rsplit("-", 1)[-1])],
                ),
            ).result

        def livephoto_probe(records, *, deadline_monotonic):
            self.assertGreater(deadline_monotonic, time.monotonic())
            return {
                record.https_candidate.value: XhsLivePhotoProbeEvidenceV1(
                    content_type="application/octet-stream",
                    prefix=(
                        jpeg_prefix()
                        if record.locator.member_role == "still"
                        else MP4_PREFIX
                    ),
                    bytes_read=len(
                        jpeg_prefix()
                        if record.locator.member_role == "still"
                        else MP4_PREFIX
                    ),
                )
                for record in records
            }

        outcome = build_xhs_livephoto_result(
            flat_result=enhanced,
            snapshot=snapshot_for(items),
            probe_candidates=livephoto_probe,
        )
        result = outcome.result
        context = build_xhs_livephoto_preview_context(result)

        self.assertIsNotNone(result)
        self.assertEqual(
            [asset.url for asset in result.assets],
            [
                raw_url(0),
                native_item(0, livephoto=True)["stream"]["h264"][0]["masterUrl"],
                raw_url(1),
            ],
        )
        self.assertEqual(len(result.rich_media.groups), 1)
        self.assertEqual(
            [item["kind"] for item in context["items"]],
            ["live_photo", "regular_image"],
        )
        self.assertFalse(
            any(key.startswith("xhs_livephoto_staff_") for key in result.metadata)
        )
        self.assertTrue(
            all(not asset.provider_manifest_token for asset in result.assets)
        )

    def test_livephoto_parallelizes_parsehub_stills_and_native_motion_evidence(self):
        items = [native_item(0, livephoto=True), native_item(1)]
        rendezvous = threading.Barrier(2)

        def parallel_parsehub(**kwargs):
            rendezvous.wait(timeout=1)
            return parsehub_images.enhance_xhs_images_with_parsehub(
                **kwargs,
                payload_loader=Mock(return_value=worker_payload(items)),
                probe_candidate=lambda url, **_probe_kwargs: probe_for(
                    int(url.rsplit("-", 1)[-1]),
                    extension=("jpg", "png")[int(url.rsplit("-", 1)[-1])],
                ),
            )

        def parallel_livephoto(**kwargs):
            rendezvous.wait(timeout=1)
            return build_xhs_livephoto_result(
                **kwargs,
                probe_candidates=lambda records, **_probe_kwargs: {
                    record.https_candidate.value: XhsLivePhotoProbeEvidenceV1(
                        content_type="application/octet-stream",
                        prefix=(
                            jpeg_prefix()
                            if record.locator.member_role == "still"
                            else MP4_PREFIX
                        ),
                        bytes_read=len(
                            jpeg_prefix()
                            if record.locator.member_role == "still"
                            else MP4_PREFIX
                        ),
                    )
                    for record in records
                },
            )

        with patch.object(
            parsehub_images,
            "parsehub_provider_enabled",
            return_value=True,
        ), patch(
            "xhs_download.services.platforms.xiaohongshu.enhance_xhs_images_with_parsehub",
            side_effect=parallel_parsehub,
        ), patch(
            "xhs_download.services.platforms.xiaohongshu.build_xhs_livephoto_result",
            side_effect=parallel_livephoto,
        ):
            result = XiaohongshuAdapter().parse_html(
                page_html_for(items),
                source_url=NOTE_URL,
                enable_xhs_livephoto_preview=True,
                deadline_monotonic=time.monotonic() + 2,
            )

        self.assertEqual(
            [asset.url for asset in result.image_assets],
            [raw_url(0), raw_url(1)],
        )
        self.assertEqual(len(result.rich_media.groups), 1)

    def test_rebinding_updates_still_urls_and_rich_media_extensions(self):
        items = [native_item(0, livephoto=True), native_item(1)]

        def livephoto_probe(records, **_kwargs):
            return {
                record.https_candidate.value: XhsLivePhotoProbeEvidenceV1(
                    content_type="application/octet-stream",
                    prefix=(
                        jpeg_prefix()
                        if record.locator.member_role == "still"
                        else MP4_PREFIX
                    ),
                    bytes_read=len(
                        jpeg_prefix()
                        if record.locator.member_role == "still"
                        else MP4_PREFIX
                    ),
                )
                for record in records
            }

        native_livephoto = build_xhs_livephoto_result(
            flat_result=native_result(items),
            snapshot=snapshot_for(items),
            probe_candidates=livephoto_probe,
        ).result
        with patch.object(
            parsehub_images,
            "parsehub_provider_enabled",
            return_value=True,
        ):
            selected_stills = parsehub_images.enhance_xhs_images_with_parsehub(
                native_result=native_result(items),
                snapshot=snapshot_for(items),
                source_url=NOTE_URL,
                payload_loader=Mock(return_value=worker_payload(items)),
                probe_candidate=lambda url, **_probe_kwargs: probe_for(
                    int(url.rsplit("-", 1)[-1]),
                    extension=("jpg", "png")[int(url.rsplit("-", 1)[-1])],
                ),
            ).result

        rebound = bind_xhs_livephoto_still_result(
            native_livephoto,
            selected_stills,
        )

        self.assertEqual(
            [asset.url for asset in rebound.image_assets],
            [raw_url(0), raw_url(1)],
        )
        self.assertEqual(
            [
                descriptor.extension
                for descriptor in rebound.rich_media.assets
                if descriptor.media_type == "image"
            ],
            ["jpg", "png"],
        )

    def test_empty_terminal_result_does_not_start_parsehub_or_tiantian(self):
        with patch(
            "xhs_download.services.platforms.xiaohongshu.enhance_xhs_images_with_parsehub"
        ) as parsehub_enhancer:
            result = XiaohongshuAdapter().parse_html(
                page_html_for([], note_type="normal"),
                source_url=NOTE_URL,
            )

        parsehub_enhancer.assert_not_called()
        self.assertFalse(result.assets)
        provider = Mock()
        self.assertIs(
            enhance_xhs_ordinary_video(result, provider=provider),
            result,
        )
        provider.parse.assert_not_called()

    def test_success_cache_reuses_identity_across_url_forms(self):
        payload = worker_payload(self.items)
        with patch.object(
            parsehub_images,
            "_run_worker",
            return_value=payload,
        ) as runner:
            first = parsehub_images.load_parsehub_worker_payload(
                source_url=NOTE_URL,
                note_id=NOTE_ID,
                deadline_monotonic=time.monotonic() + 2,
            )
            second = parsehub_images.load_parsehub_worker_payload(
                source_url="https://xhslink.cn/o/opaque-fixture",
                note_id=NOTE_ID,
                deadline_monotonic=time.monotonic() + 2,
            )

        self.assertEqual(first, second)
        runner.assert_called_once()

    def test_same_identity_requests_share_one_inflight_worker(self):
        payload = worker_payload(self.items)
        worker_started = threading.Event()
        release_worker = threading.Event()

        def run_worker(*_args, **_kwargs):
            worker_started.set()
            release_worker.wait(timeout=1)
            return payload

        with patch.object(parsehub_images, "_run_worker", side_effect=run_worker) as runner:
            with ThreadPoolExecutor(max_workers=2) as executor:
                first = executor.submit(
                    parsehub_images.load_parsehub_worker_payload,
                    source_url=NOTE_URL,
                    note_id=NOTE_ID,
                    deadline_monotonic=time.monotonic() + 2,
                )
                self.assertTrue(worker_started.wait(timeout=1))
                second = executor.submit(
                    parsehub_images.load_parsehub_worker_payload,
                    source_url="https://xhslink.cn/o/opaque-fixture",
                    note_id=NOTE_ID,
                    deadline_monotonic=time.monotonic() + 2,
                )
                time.sleep(0.05)
                release_worker.set()
                self.assertEqual(first.result(timeout=1), payload)
                self.assertEqual(second.result(timeout=1), payload)

        runner.assert_called_once()


class ParseHubManifestAndRouteTests(SimpleTestCase):
    def setUp(self):
        items = [native_item(0)]
        with patch.object(
            parsehub_images,
            "parsehub_provider_enabled",
            return_value=True,
        ):
            outcome = parsehub_images.enhance_xhs_images_with_parsehub(
                native_result=native_result(items),
                snapshot=snapshot_for(items),
                source_url=NOTE_URL,
                payload_loader=Mock(return_value=worker_payload(items)),
                probe_candidate=lambda *_args, **_kwargs: probe_for(0),
            )
        self.item = outcome.result.assets[0].to_download_item()
        self.factory = RequestFactory()
        self.runtime = SimpleNamespace(
            visitor_video_download_enabled=True,
            staff_large_download_exempt=False,
            single_video_bytes_limit=0,
            single_archive_bytes_limit=0,
        )

    def test_manifest_exact_binding_is_valid(self):
        self.assertTrue(provider_manifest_allows_public_download(self.item))

    def test_manifest_tampering_is_rejected(self):
        mutations = (
            {"url": raw_url(1)},
            {"asset_id": "xhs:bad:image:0:parsehub"},
            {"provider_cache_key": "xiaohongshu:parsehub:bad"},
            {"source_platform": "douyin"},
            {"provider_resource_id": "bad"},
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                self.assertFalse(
                    provider_manifest_allows_public_download(
                        {**self.item, **mutation}
                    )
                )

    def route(self, mode, items=None):
        with patch(
            "xhs_download.services.download_routing.DownloadRuntimeSetting.load",
            return_value=self.runtime,
        ):
            return decide_download_route(
                self.factory.post("/media/download-route/"),
                items or [self.item],
                requested_mode=mode,
            )

    def test_browser_route_is_direct_and_server_routes_are_blocked(self):
        direct = self.route("browser")
        self.assertEqual(direct["route_type"], ROUTE_DIRECT)
        self.assertEqual(direct["server_egress_estimated_bytes"], 0)
        for mode in ("proxy", "server_single", "zip"):
            with self.subTest(mode=mode):
                blocked = self.route(mode)
                self.assertEqual(blocked["route_type"], ROUTE_BLOCKED)
                self.assertEqual(
                    blocked["error_code"],
                    "XHS_IMAGE_BROWSER_DOWNLOAD_REQUIRED",
                )

    def test_mixed_selection_cannot_smuggle_xhs_image_into_server_route(self):
        generic_video = {
            "type": "video",
            "url": "https://media.example/video.mp4",
        }
        for mode in ("", "browser", "zip", "proxy"):
            with self.subTest(mode=mode):
                result = self.route(mode, [self.item, generic_video])
                self.assertEqual(result["route_type"], ROUTE_BLOCKED)


def load_worker_module():
    path = (
        Path(__file__).resolve().parents[1]
        / "tools"
        / "parsehub_provider"
        / "parsehub_xhs_image_worker.py"
    )
    spec = importlib.util.spec_from_file_location("parsehub_xhs_image_worker_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ParseHubWorkerProtocolTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.worker = load_worker_module()

    def fake_modules(self, api_class):
        parsehub = ModuleType("parsehub")
        provider_api = ModuleType("parsehub.provider_api")
        xhs = ModuleType("parsehub.provider_api.xhs")
        xhs.XHSAPI = api_class
        return {
            "parsehub": parsehub,
            "parsehub.provider_api": provider_api,
            "parsehub.provider_api.xhs": xhs,
        }

    def test_exact_pin_is_required(self):
        class FakeApi:
            pass

        distribution = Mock()
        distribution.read_text.return_value = json.dumps(
            {"vcs_info": {"commit_id": self.worker.PARSEHUB_PIN}}
        )
        with patch.dict(sys.modules, self.fake_modules(FakeApi)), patch.object(
            self.worker.metadata,
            "version",
            return_value="2.2.0",
        ), patch.object(
            self.worker.metadata,
            "distribution",
            return_value=distribution,
        ):
            self.worker._verify_parsehub_install()

        distribution.read_text.return_value = json.dumps(
            {"vcs_info": {"commit_id": "0" * 40}}
        )
        with patch.dict(sys.modules, self.fake_modules(FakeApi)), patch.object(
            self.worker.metadata,
            "version",
            return_value="2.2.0",
        ), patch.object(
            self.worker.metadata,
            "distribution",
            return_value=distribution,
        ), self.assertRaises(self.worker.WorkerError):
            self.worker._verify_parsehub_install()

    def test_worker_is_anonymous_parse_only_and_emits_no_motion(self):
        init_values = []

        class FakeApi:
            def __init__(self, *, proxy, cookie):
                init_values.append((proxy, cookie))

            async def _XHSAPI__fetch_html(self, source_url):
                self.source_url = source_url
                return "html"

            async def _XHSAPI__extract_data(self, html):
                self.html = html
                return {
                    "note": {
                        "noteDetailMap": {
                            NOTE_ID: {
                                "note": {
                                    "noteId": NOTE_ID,
                                    "type": "normal",
                                    "imageList": [native_item(0, livephoto=True)],
                                }
                            }
                        }
                    }
                }

            @staticmethod
            def get_trace_id(_value):
                return source_resource(0)

        request = {
            "request_id": "opaque-request",
            "source_url": NOTE_URL,
            "expected_note_id": NOTE_ID,
            "max_items": 64,
        }
        with patch.dict(sys.modules, self.fake_modules(FakeApi)):
            result = asyncio.run(self.worker._extract(request))

        self.assertEqual(init_values, [(None, None)])
        self.assertEqual(result["claimed_note_id"], NOTE_ID)
        self.assertEqual(result["items"][0]["source_kind"], "livephoto_still")
        self.assertEqual(result["items"][0]["raw_https_url"], raw_url(0))
        self.assertNotIn("motion", json.dumps(result).lower())

    def test_invalid_json_and_extra_fields_are_rejected(self):
        for payload in (
            b"not-json",
            json.dumps({"source_url": NOTE_URL}).encode(),
            json.dumps(
                {
                    "schema_version": "1.0",
                    "request_id": "opaque-request",
                    "source_url": NOTE_URL,
                    "expected_note_id": NOTE_ID,
                    "capability": "xhs_images",
                    "max_items": 64,
                    "unexpected": True,
                }
            ).encode(),
        ):
            with self.subTest(payload=payload[:20]), self.assertRaises(
                self.worker.WorkerError
            ):
                self.worker._parse_request(payload)

    def test_worker_independently_rejects_untrusted_source_urls(self):
        base = {
            "schema_version": self.worker.SCHEMA_VERSION,
            "request_id": "12345678",
            "source_url": NOTE_URL,
            "expected_note_id": NOTE_ID,
            "capability": "xhs_images",
            "max_items": 4,
        }
        rejected = (
            "http://www.xiaohongshu.com/explore/fixture",
            "https://evil.example/explore/fixture",
            "https://www.xiaohongshu.com.evil.example/explore/fixture",
            "https://user@www.xiaohongshu.com/explore/fixture",
            "https://www.xiaohongshu.com:444/explore/fixture",
            " https://www.xiaohongshu.com/explore/fixture",
        )
        for source_url in rejected:
            with self.subTest(source_url=source_url), self.assertRaises(
                self.worker.WorkerError
            ):
                self.worker._parse_request(
                    json.dumps({**base, "source_url": source_url}).encode()
                )

        parsed = self.worker._parse_request(json.dumps(base).encode())
        self.assertEqual(parsed["source_url"], NOTE_URL)

    def test_runtime_worker_errors_map_to_fixed_codes(self):
        with patch.object(
            parsehub_images,
            "runtime_str",
            return_value=sys.executable,
        ), patch.object(
            parsehub_images,
            "run_external_command",
            side_effect=parsehub_images.subprocess.TimeoutExpired("worker", 1),
        ), self.assertRaises(parsehub_images.XhsParseHubImageError) as timeout:
            parsehub_images._run_worker(
                NOTE_URL,
                NOTE_ID,
                deadline_monotonic=time.monotonic() + 1,
            )
        self.assertEqual(timeout.exception.code, "PARSEHUB_TIMEOUT")

        with patch.object(
            parsehub_images,
            "runtime_str",
            return_value=sys.executable,
        ), patch.object(
            parsehub_images,
            "run_external_command",
            side_effect=parsehub_images.ExternalCommandOutputTooLarge(),
        ), self.assertRaises(parsehub_images.XhsParseHubImageError) as overflow:
            parsehub_images._run_worker(
                NOTE_URL,
                NOTE_ID,
                deadline_monotonic=time.monotonic() + 1,
            )
        self.assertEqual(overflow.exception.code, "PARSEHUB_OUTPUT_TOO_LARGE")

    def test_worker_source_does_not_import_downloader_or_django(self):
        source = Path(self.worker.__file__).read_text(encoding="utf-8")
        self.assertNotIn("import django", source.lower())
        self.assertNotRegex(source, r"(?m)^\s*(?:from|import)\s+.*downloader")
