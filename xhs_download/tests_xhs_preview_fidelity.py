from io import BytesIO
from unittest.mock import patch

import requests
from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, override_settings

from .services.assets import MediaAsset, ParseResult
from .services.common import XhsDownloadError
from .services.platforms.xhs_preview import (
    XhsPreviewCandidateV1,
    bind_xhs_preview_sources,
    build_xhs_item_preview_sources,
    issue_xhs_preview_descriptor,
    load_xhs_preview_descriptor,
    resolve_xhs_preview_candidates,
    xhs_preview_final_url_is_allowed,
)
from .views import _read_xhs_preview_content, build_result_display_context


CI_KNOWN = "https://ci.xiaohongshu.com/notes_pre_post/fixture-a"
CI_UNKNOWN = "https://ci.xiaohongshu.com/abcdefghijklmnopqrstuv123"
NATIVE_DEFAULT = (
    "https://sns-webpic-qc.xhscdn.com/fixture/nested/default"
    "?imageView2/2/w/1080/format/jpg"
)
NATIVE_PREVIEW = (
    "https://sns-webpic-qc.xhscdn.com/fixture/nested/preview"
    "?imageView2/2/w/720/format/jpg"
)
NATIVE_FALLBACK = (
    "https://sns-webpic-qc.xhscdn.com/fixture/nested/fallback"
    "?imageView2/2/w/1440/format/jpg"
)
PARSEHUB_JPEG = "https://sns-img-hw.xhscdn.com/fixture-original.jpg"
PARSEHUB_PNG = "https://sns-img-hw.xhscdn.com/fixture-original.png"
PARSEHUB_HEIC = "https://sns-img-hw.xhscdn.com/fixture-original.heic"


def snapshot_item(*, default=NATIVE_DEFAULT, preview=NATIVE_PREVIEW, fallback=NATIVE_FALLBACK):
    return {
        "urlDefault": default,
        "infoList": (
            {"imageScene": "WB_PRV", "url": preview},
            {"imageScene": "WB_DFT", "url": fallback},
        ),
    }


def image_asset(
    *,
    url=CI_UNKNOWN,
    preview_url=NATIVE_DEFAULT,
    ext="jpg",
    provider="xhs_livephoto_preview_v1",
):
    return MediaAsset(
        media_type="image",
        url=url,
        preview_url=preview_url,
        platform="xiaohongshu",
        ext=ext,
        provider=provider,
    )


class FakeStreamingResponse:
    def __init__(self, content, *, url=NATIVE_DEFAULT, content_type="image/jpeg"):
        self.content = content
        self.url = url
        self.headers = {
            "Content-Length": str(len(content)),
            "Content-Type": content_type,
        }
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def iter_content(self, chunk_size):
        source = BytesIO(self.content)
        while True:
            chunk = source.read(chunk_size)
            if not chunk:
                break
            yield chunk

    def close(self):
        self.closed = True


JPEG = b"\xff\xd8\xff\xe0preview-data"
WEBP = b"RIFF\x0c\x00\x00\x00WEBPVP8 "


class XhsPreviewResolverTests(SimpleTestCase):
    def test_fixed_native_http_item_resource_is_normalized_to_https(self):
        raw_http = "http://sns-webpic-qc.xhscdn.com/item/resource-value"

        candidates = build_xhs_item_preview_sources(
            snapshot_item(default=raw_http, preview=raw_http, fallback=raw_http)
        )

        self.assertEqual(len(candidates), 1)
        self.assertEqual(
            candidates[0].value,
            "https://sns-webpic-qc.xhscdn.com/item/resource-value",
        )

    def test_snapshot_candidates_preserve_exact_item_order(self):
        candidates = build_xhs_item_preview_sources(snapshot_item())

        self.assertEqual(
            [candidate.value for candidate in candidates],
            [NATIVE_DEFAULT, NATIVE_PREVIEW, NATIVE_FALLBACK],
        )

    def test_existing_compatible_preview_is_not_double_normalized(self):
        candidates = resolve_xhs_preview_candidates(
            image_asset(),
            item_sources=build_xhs_item_preview_sources(snapshot_item()),
            width=1280,
        )

        self.assertEqual(candidates[0].value, NATIVE_DEFAULT)
        self.assertNotIn("ci.xiaohongshu.com", candidates[0].value)

    def test_resolution_is_idempotent_for_existing_preview(self):
        first = resolve_xhs_preview_candidates(image_asset(), width=1280)
        rebound = image_asset(preview_url=first[0].value)
        second = resolve_xhs_preview_candidates(rebound, width=1280)

        self.assertEqual(first[0].value, second[0].value)

    def test_known_resource_path_can_derive_reviewed_preview(self):
        candidates = resolve_xhs_preview_candidates(
            image_asset(url=CI_KNOWN, preview_url=""),
            width=1600,
        )

        self.assertIn("/w/1600/format/webp", candidates[0].value)

    def test_reviewed_derived_preview_uses_requested_card_and_lightbox_widths(self):
        card = resolve_xhs_preview_candidates(
            image_asset(url=CI_KNOWN, preview_url=""),
            width=1280,
        )
        lightbox = resolve_xhs_preview_candidates(
            image_asset(url=CI_KNOWN, preview_url=""),
            width=1600,
        )

        self.assertIn("/w/1280/format/webp", card[0].value)
        self.assertIn("/w/1600/format/webp", lightbox[0].value)

    def test_unknown_ci_resource_is_not_derived(self):
        candidates = resolve_xhs_preview_candidates(
            image_asset(url=CI_UNKNOWN, preview_url=""),
            width=1280,
        )

        self.assertFalse(
            any("imageView2" in candidate.value for candidate in candidates)
        )

    def test_parsehub_raw_is_not_rewritten_to_ci(self):
        candidates = resolve_xhs_preview_candidates(
            image_asset(
                url=PARSEHUB_JPEG,
                preview_url=NATIVE_DEFAULT,
                provider="parsehub_xhs_images",
            ),
            item_sources=build_xhs_item_preview_sources(snapshot_item()),
            width=1280,
        )

        self.assertIn(PARSEHUB_JPEG, [candidate.value for candidate in candidates])
        self.assertNotIn(
            "https://ci.xiaohongshu.com/fixture-original.jpg",
            [candidate.value for candidate in candidates],
        )

    def test_parsehub_heic_uses_native_preview_without_raw_img_candidate(self):
        candidates = resolve_xhs_preview_candidates(
            image_asset(
                url=PARSEHUB_HEIC,
                preview_url=NATIVE_DEFAULT,
                ext="heic",
                provider="parsehub_xhs_images",
            ),
            item_sources=build_xhs_item_preview_sources(snapshot_item()),
            width=1280,
        )

        self.assertEqual(candidates[0].value, NATIVE_DEFAULT)
        self.assertNotIn(PARSEHUB_HEIC, [candidate.value for candidate in candidates])

    def test_browser_raster_parsehub_original_is_safe_last_resort(self):
        for raw_url, extension in ((PARSEHUB_JPEG, "jpg"), (PARSEHUB_PNG, "png")):
            with self.subTest(extension=extension):
                candidates = resolve_xhs_preview_candidates(
                    image_asset(
                        url=raw_url,
                        preview_url=NATIVE_DEFAULT,
                        ext=extension,
                        provider="parsehub_xhs_images",
                    ),
                    item_sources=(
                        build_xhs_item_preview_sources(snapshot_item())[0],
                    ),
                    width=1280,
                )
                self.assertEqual(candidates[-1].value, raw_url)

    def test_candidate_count_is_bounded_to_three(self):
        sources = build_xhs_item_preview_sources(snapshot_item())
        candidates = resolve_xhs_preview_candidates(
            image_asset(url=PARSEHUB_JPEG, provider="parsehub_xhs_images"),
            item_sources=sources,
            width=1280,
        )

        self.assertLessEqual(len(candidates), 3)

    def test_item_candidates_never_include_another_ordinal(self):
        first = build_xhs_item_preview_sources(snapshot_item())
        other_url = NATIVE_FALLBACK.replace("fallback", "other-item")
        second = build_xhs_item_preview_sources(
            snapshot_item(default=other_url, preview=other_url, fallback=other_url)
        )
        candidates = resolve_xhs_preview_candidates(
            image_asset(),
            item_sources=first,
            width=1280,
        )

        self.assertNotIn(other_url, [candidate.value for candidate in candidates])
        self.assertNotEqual(first, second)

    def test_single_staff_and_batch_use_the_same_candidate_classes(self):
        sources = build_xhs_item_preview_sources(snapshot_item())
        asset = image_asset(
            preview_url=bind_xhs_preview_sources(NATIVE_DEFAULT, sources)
        )
        result = ParseResult(
            platform="xiaohongshu",
            post_type="image",
            base_filename="fixture",
            assets=(asset,),
            metadata={"safe_fact": True},
        )

        single = build_result_display_context(result, preview_surface="single")
        staff = build_result_display_context(result, preview_surface="staff_single")
        batch = build_result_display_context(result, preview_surface="batch")

        expected = single["image_items"][0]["xhs_preview_candidate_classes"]
        self.assertEqual(staff["image_items"][0]["xhs_preview_candidate_classes"], expected)
        self.assertEqual(batch["image_items"][0]["xhs_preview_candidate_classes"], expected)
        self.assertEqual(single["metadata"], {"safe_fact": True})

    def test_shared_display_context_keeps_original_download_fields_unchanged(self):
        sources = build_xhs_item_preview_sources(snapshot_item())
        asset = image_asset(
            url=PARSEHUB_JPEG,
            preview_url=bind_xhs_preview_sources(NATIVE_DEFAULT, sources),
            provider="parsehub_xhs_images",
        )
        result = ParseResult(
            platform="xiaohongshu",
            post_type="image",
            base_filename="fixture",
            assets=(asset,),
        )

        item = build_result_display_context(result)["image_items"][0]

        self.assertEqual(item["url"], PARSEHUB_JPEG)
        self.assertEqual(item["provider"], "parsehub_xhs_images")
        self.assertEqual(item["download_extension"], "jpg")
        self.assertTrue(item["preview_proxy_query"].startswith("p="))

    def test_final_redirect_must_keep_the_exact_reviewed_host(self):
        candidate = XhsPreviewCandidateV1(
            value=NATIVE_DEFAULT,
            candidate_class="existing_compatible_preview",
            authority_class="native_explicit_preview",
        )

        self.assertTrue(
            xhs_preview_final_url_is_allowed(candidate, NATIVE_PREVIEW)
        )
        self.assertFalse(
            xhs_preview_final_url_is_allowed(
                candidate,
                "https://sns-webpic-hw.xhscdn.com/fixture/nested/preview",
            )
        )

    def test_candidate_class_must_match_recomputed_url_authority(self):
        forged_parsehub = XhsPreviewCandidateV1(
            value=NATIVE_DEFAULT,
            candidate_class="parsehub_raw_raster",
            authority_class="parsehub_raw",
        )
        forged_native = XhsPreviewCandidateV1(
            value=PARSEHUB_JPEG,
            candidate_class="existing_compatible_preview",
            authority_class="native_raster",
        )

        self.assertEqual(
            issue_xhs_preview_descriptor(
                (forged_parsehub,),
                surface="single",
                source_ordinal=0,
            ),
            "",
        )
        self.assertEqual(
            issue_xhs_preview_descriptor(
                (forged_native,),
                surface="single",
                source_ordinal=0,
            ),
            "",
        )

    @patch("xhs_download.views.time.monotonic", side_effect=(0.0, 9.0))
    def test_response_body_read_obeys_preview_deadline(self, _monotonic):
        response = FakeStreamingResponse(JPEG)

        with self.assertRaises(requests.Timeout):
            _read_xhs_preview_content(response, deadline_monotonic=8.0)


@override_settings(
    PREVIEW_RATE_LIMIT_PER_MINUTE=0,
    PREVIEW_DAILY_REQUEST_LIMIT_PER_IP=0,
    PREVIEW_GLOBAL_DAILY_REQUEST_LIMIT=0,
    PREVIEW_DAILY_BYTES_LIMIT_PER_IP=0,
    PREVIEW_GLOBAL_DAILY_BYTES_LIMIT=0,
)
class XhsPreviewProxyFallbackTests(TestCase):
    def setUp(self):
        cache.clear()
        self.candidates = resolve_xhs_preview_candidates(
            image_asset(),
            item_sources=build_xhs_item_preview_sources(snapshot_item()),
            width=1280,
        )

    def descriptor(self, candidates=None):
        return issue_xhs_preview_descriptor(
            candidates or self.candidates,
            surface="batch",
            source_ordinal=0,
        )

    def test_descriptor_is_opaque_and_round_trips_bounded_candidates(self):
        token = self.descriptor()
        descriptor = load_xhs_preview_descriptor(token)

        self.assertNotIn("xhscdn", token)
        self.assertEqual(len(descriptor.candidates), 3)
        self.assertEqual(descriptor.surface, "batch")

    @patch("xhs_download.views.fetch_media_response")
    def test_primary_http_failure_falls_back_to_second_candidate(self, mock_fetch):
        mock_fetch.side_effect = (
            XhsDownloadError("HTTP 404 secret-query=blocked"),
            FakeStreamingResponse(JPEG, url=NATIVE_PREVIEW),
        )

        response = self.client.get("/media/proxy-image/", {"p": self.descriptor()})

        self.assertEqual(response["Content-Type"], "image/jpeg")
        self.assertEqual(response.content, JPEG)
        self.assertEqual(mock_fetch.call_count, 2)

    @patch("xhs_download.views.fetch_media_response")
    def test_invalid_magic_falls_back_to_second_candidate(self, mock_fetch):
        mock_fetch.side_effect = (
            FakeStreamingResponse(b"<html>blocked</html>", url=NATIVE_DEFAULT),
            FakeStreamingResponse(WEBP, url=NATIVE_PREVIEW, content_type="image/jpeg"),
        )

        response = self.client.get("/media/proxy-image/", {"p": self.descriptor()})

        self.assertEqual(response["Content-Type"], "image/webp")
        self.assertEqual(mock_fetch.call_count, 2)

    @patch("xhs_download.views.fetch_media_response")
    def test_two_failures_fall_back_to_third_candidate(self, mock_fetch):
        mock_fetch.side_effect = (
            XhsDownloadError("HTTP 404"),
            FakeStreamingResponse(b"{}", url=NATIVE_PREVIEW),
            FakeStreamingResponse(JPEG, url=NATIVE_FALLBACK),
        )

        response = self.client.get("/media/proxy-image/", {"p": self.descriptor()})

        self.assertEqual(response["Content-Type"], "image/jpeg")
        self.assertEqual(mock_fetch.call_count, 3)

    @patch("xhs_download.views.fetch_media_response")
    def test_all_failures_return_placeholder(self, mock_fetch):
        mock_fetch.side_effect = XhsDownloadError("HTTP 404")

        response = self.client.get("/media/proxy-image/", {"p": self.descriptor()})

        self.assertEqual(response["Content-Type"], "image/svg+xml")
        self.assertEqual(mock_fetch.call_count, 3)

    @patch("xhs_download.views.fetch_media_response")
    def test_unreviewed_final_host_is_rejected_before_body_admission(self, mock_fetch):
        mock_fetch.side_effect = (
            FakeStreamingResponse(JPEG, url="https://unreviewed.xhscdn.com/image.jpg"),
            FakeStreamingResponse(JPEG, url=NATIVE_PREVIEW),
        )

        response = self.client.get("/media/proxy-image/", {"p": self.descriptor()})

        self.assertEqual(response["Content-Type"], "image/jpeg")
        self.assertEqual(mock_fetch.call_count, 2)

    @patch("xhs_download.views.fetch_media_response")
    def test_candidate_fetches_share_one_bounded_deadline(self, mock_fetch):
        mock_fetch.side_effect = (
            XhsDownloadError("HTTP 404"),
            FakeStreamingResponse(JPEG, url=NATIVE_PREVIEW),
        )

        self.client.get("/media/proxy-image/", {"p": self.descriptor()})

        first_deadline = mock_fetch.call_args_list[0].kwargs["deadline_monotonic"]
        second_deadline = mock_fetch.call_args_list[1].kwargs["deadline_monotonic"]
        self.assertEqual(first_deadline, second_deadline)
        self.assertEqual(mock_fetch.call_args_list[0].kwargs["retries"], 1)

    @patch("xhs_download.views.fetch_media_response")
    def test_structured_logs_do_not_contain_urls_or_queries(self, mock_fetch):
        mock_fetch.side_effect = XhsDownloadError(
            f"HTTP 404 source={NATIVE_DEFAULT}&token=secret"
        )

        with patch("xhs_download.views.logger.warning") as warning:
            self.client.get("/media/proxy-image/", {"p": self.descriptor()})

        logged = repr(warning.call_args_list)
        self.assertNotIn("xhscdn.com", logged)
        self.assertNotIn("token=secret", logged)
        self.assertNotIn(NATIVE_DEFAULT, logged)

    def test_missing_or_tampered_descriptor_fails_closed(self):
        response = self.client.get("/media/proxy-image/", {"p": "tampered"})

        self.assertEqual(response["Content-Type"], "image/svg+xml")
