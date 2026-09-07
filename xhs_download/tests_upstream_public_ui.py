import time
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.template.loader import render_to_string
from django.test import RequestFactory, SimpleTestCase, override_settings

from .models import ProviderAccessMode, ProviderRuntimeSetting
from .services.assets import MediaAsset, ParseResult
from .services.platforms.third_party import ThirdPartyPlatformRule
from .services.provider_access import (
    third_party_public_access_summary, update_upstream_bulk_allowlist,
)
from .services import upstream_coverage
from .services.batch_parse import results as batch_results
from .views import build_image_item, build_video_item, upstream_coverage_view


MATCHES = (
    {"provider_id": "yt_dlp_generic", "extractor_id": "ExampleVideo", "label": "Example", "version": "1"},
    {"provider_id": "gallery_dl_generic", "extractor_id": "ExampleImage", "label": "Example", "version": "2"},
)
RULE = ThirdPartyPlatformRule(
    platform_id="upstream", display_name="Example", host_suffixes=(),
    provider_ids=tuple(match["provider_id"] for match in MATCHES), upstream_matches=MATCHES,
)


@override_settings(THIRD_PARTY_PROVIDER_ENABLED=True, YTDLP_PROVIDER_ENABLED=True, GALLERYDL_PROVIDER_ENABLED=True)
class UpstreamPublicAccessTests(SimpleTestCase):
    def setUp(self):
        self.request = RequestFactory().get("/")
        self.request.user = SimpleNamespace(is_active=False, is_staff=False)
        self.runtime = ProviderRuntimeSetting(
            third_party_mode=ProviderAccessMode.PUBLIC,
            third_party_allowed_public_platforms="youtube\nupstream:yt_dlp_generic",
        )
        self.resolver = patch("xhs_download.services.platforms.third_party.GenericThirdPartyAdapter.get_rule_for_url", return_value=RULE)
        self.resolver.start()
        self.addCleanup(self.resolver.stop)

    def gate(self):
        return third_party_public_access_summary("https://example.org/post/1", self.request, self.runtime)

    def test_only_bulk_enabled_matched_executor_is_authorized(self):
        gate = self.gate()
        self.assertTrue(gate["allowed"])
        self.assertEqual(gate["upstream_matches"], MATCHES[:1])
        self.assertEqual(gate["upstream_provider_ids"], ("yt_dlp_generic",))

    def test_default_dynamic_is_closed_and_existing_tokens_are_not_flags(self):
        self.runtime.third_party_allowed_public_platforms = "youtube\nflickr"
        self.assertFalse(self.gate()["allowed"])

    def test_blocks_override_bulk_even_for_staff(self):
        self.request.user = SimpleNamespace(is_active=True, is_staff=True)
        for blocked in ("upstream", "upstream:yt_dlp_generic\nupstream:gallery_dl_generic",
                        "upstream:yt_dlp_generic:ExampleVideo\nupstream:extractor:ExampleImage"):
            with self.subTest(blocked=blocked):
                self.runtime.third_party_blocked_platforms = blocked
                self.assertFalse(self.gate()["allowed"])
        self.runtime.third_party_blocked_platforms = ""
        self.runtime.third_party_blocked_hosts = "example.org"
        self.assertFalse(self.gate()["allowed"])

    def test_staff_uses_current_mode_not_bulk_public_flag(self):
        self.request.user = SimpleNamespace(is_active=True, is_staff=True)
        self.runtime.third_party_mode = ProviderAccessMode.STAFF
        self.runtime.third_party_allowed_public_platforms = ""
        self.assertEqual(self.gate()["upstream_matches"], MATCHES)
        self.runtime.third_party_mode = ProviderAccessMode.DISABLED
        self.assertFalse(self.gate()["allowed"])

    @override_settings(YTDLP_PROVIDER_ENABLED=False)
    def test_disabled_executor_cannot_be_enabled_by_allowlist(self):
        self.assertFalse(self.gate()["allowed"])

    def test_unmatched_executor_is_never_authorized(self):
        with patch("xhs_download.services.platforms.third_party.GenericThirdPartyAdapter.get_rule_for_url", return_value=replace(RULE, provider_ids=("gallery_dl_generic",))):
            self.assertFalse(self.gate()["allowed"])

    def test_bulk_update_preserves_legacy_and_exception_tokens(self):
        value = "youtube\nflickr\nupstream:yt_dlp_generic\ncustom:exception"
        self.assertEqual(update_upstream_bulk_allowlist(value, ["gallery_dl_generic", "evil"]),
                         "youtube\nflickr\ncustom:exception\nupstream:gallery_dl_generic")

    def test_batch_denies_before_parser_and_passes_filtered_matches(self):
        parser = Mock(return_value=ParseResult("third_party", "video", "test"))
        with patch.object(batch_results, "third_party_public_access_summary", return_value={"allowed": False, "message": "blocked"}):
            row = batch_results.build_batch_result_row(
                self.request, "https://example.org/post/1", prefer_ultra_hd=False,
                prefer_youtube_merge=False, per_link_timeout_seconds=30, parser=parser,
                query_uses_third_party_provider_func=lambda _: True,
                third_party_provider_is_available_func=lambda _: True,
            )
        self.assertEqual(row["status"], "failed")
        parser.assert_not_called()
        with patch.object(batch_results, "third_party_public_access_summary", return_value=self.gate()):
            batch_results.build_batch_result_row(
                self.request, "https://example.org/post/1", prefer_ultra_hd=False,
                prefer_youtube_merge=False, per_link_timeout_seconds=30, parser=parser,
                query_uses_third_party_provider_func=lambda _: True,
                third_party_provider_is_available_func=lambda _: True,
                display_context_builder=lambda *a, **k: {"video_items": [{"url": "https://cdn.example/video"}]},
            )
        self.assertEqual(parser.call_args.kwargs["upstream_matches"], MATCHES[:1])

    def test_batch_gives_only_dynamic_items_thirty_seconds(self):
        received = {}
        def row_builder(request, source, **kwargs):
            received[source] = kwargs["per_link_timeout_seconds"]
            return {"status": "success", "source_text": source}
        started = time.monotonic()
        with patch.object(batch_results, "batch_source_is_upstream", side_effect=lambda source: source == "dynamic"), patch.object(batch_results, "batch_platform_for_source", return_value="third_party"):
            batch_results.build_batch_result_rows(
                self.request, ["mature", "dynamic"], prefer_ultra_hd=False,
                prefer_youtube_merge=False, deadline=started + 10,
                response_deadline=started + 4, parallel_workers=2,
                per_link_timeout_seconds=3, scheduling_timeout_seconds=10,
                max_response_timeout_seconds=4, row_builder=row_builder,
                upstream_sources=("dynamic",),
            )
        self.assertEqual(received, {"mature": 3.0, "dynamic": 30.0})


class UpstreamCoverageTests(SimpleTestCase):
    def test_inventory_is_bounded_and_cached(self):
        snapshot = [{"provider_id": "yt_dlp_generic", "version": "1", "entries": [
            {"extractor_id": str(index), "label": "Example"} for index in range(5000)
        ]}]
        with patch.object(upstream_coverage, "_inventory", ()), patch.object(upstream_coverage, "_expires_at", 0), patch.object(upstream_coverage, "load_coverage_snapshot", return_value=snapshot) as loader:
            self.assertEqual(len(upstream_coverage.cached_coverage_inventory()), 4096)
            upstream_coverage.cached_coverage_inventory()
            loader.assert_called_once()

    def test_coverage_is_searchable_paginated_escaped_and_private(self):
        rows = [{"provider_id": "yt_dlp_generic", "extractor_id": str(index),
                 "label": "<Example>", "version": "1", "allowed": True,
                 "status_label": "可尝试"} for index in range(70)]
        request = RequestFactory().get("/?coverage=1&search=Example&state=allowed&page=2")
        with patch.object(upstream_coverage, "coverage_rows_for_request", return_value=rows), patch.object(ProviderRuntimeSetting, "load", return_value=ProviderRuntimeSetting()):
            response = upstream_coverage_view(request)
        html = response.content.decode()
        self.assertEqual(html.count("<strong>&lt;Example&gt;</strong>"), 20)
        self.assertIn("可尝试", html)
        self.assertEqual(response["Cache-Control"], "private, no-store")


class UpstreamPreviewTests(SimpleTestCase):
    def test_normalized_unbound_upstream_video_exposes_authorization_action(self):
        from .services.upstream_delivery import normalize_upstream_result

        with patch("xhs_download.services.upstream_delivery.safe_resource_url", side_effect=lambda url: url):
            result = normalize_upstream_result(
                [[3, "https://cdn.example/video.webm", {"extension": "webm", "filename": "example"}]],
                match=MATCHES[1], source_url="https://example.org/post/1",
            )
        item = build_video_item(result.assets[0], result)
        self.assertFalse(item["browser_direct_fetch_safe"])
        self.assertTrue(item["upstream_browser_action"])
        item["direct_url"] = item["video_url"]
        context = {"row": {"status": "success", "platform": "third_party", "base_filename": "example",
                           "video_items": [item], "image_items": []}, "batch_download_allowed": True}
        html = render_to_string("partials/batch_result_card.html", context)
        self.assertIn("data-batch-direct-download", html)
        self.assertIn("data-batch-download-progress", html)
        self.assertIn('data-batch-browser-fetch-safe="false"', html)
        self.assertNotIn('browser-upstream-bound="true"', html)
        context["batch_download_allowed"] = False
        self.assertNotIn("data-batch-direct-download", render_to_string("partials/batch_result_card.html", context))
        context["batch_download_allowed"] = True
        item["requires_cookie"] = True
        self.assertNotIn("data-batch-direct-download", render_to_string("partials/batch_result_card.html", context))

    def test_mature_video_naming_fields_are_unchanged(self):
        for platform in ("xiaohongshu", "douyin", "bilibili", "third_party"):
            with self.subTest(platform=platform), patch("xhs_download.views.get_video_size_details", return_value=(0, False)):
                asset = MediaAsset("video", "https://cdn.example/video.webm", platform, ext="webm")
                item = build_video_item(asset, ParseResult(platform, "video", "legacy", (asset,)))
                self.assertNotIn("filename", item)
                self.assertNotIn("download_extension", item)

    def test_previews_preserve_source_identity_and_never_use_proxy(self):
        image = MediaAsset("image", "https://cdn.example/photo.webp", "third_party",
                           preview_url="https://cdn.example/photo.webp", ext="webp",
                           asset_id="upstream:job:image:1", source_platform="upstream")
        video = MediaAsset("video", "https://cdn.example/video.webm", "third_party", ext="webm",
                           cover_url="https://cdn.example/cover.webp",
                           asset_id="upstream:job:video:2", source_platform="upstream")
        result = ParseResult("third_party", "video", "example", (image, video))
        with patch("xhs_download.views.get_video_size_details", return_value=(0, False)) as probe:
            image_item = build_image_item(image, result)
            video_item = build_video_item(video, result)
        self.assertFalse(probe.call_args.kwargs["probe_missing"])
        self.assertEqual(image_item["source_platform"], "upstream")
        self.assertEqual(image_item["preview_provider_query"], "")
        self.assertEqual(video_item["filename"], "example.webm")
        self.assertEqual(video_item["cover_preview_provider_query"], "")
        context = {"image_items": [image_item], "video_items": [video_item],
                   "proxy_image_url": "/media/proxy-image/", "base_filename": "example"}
        html = render_to_string("partials/xhs_results_region.html", context)
        self.assertNotIn('src="/media/proxy-image/', html)
        self.assertIn('src="https://cdn.example/photo.webp"', html)
        self.assertIn('src="https://cdn.example/cover.webp"', html)
        video_item["direct_url"] = video.url
        context["row"] = {**context, "status": "success", "platform": "third_party"}
        context["batch_download_allowed"] = True
        html = render_to_string("partials/batch_result_card.html", context)
        self.assertNotIn('src="/media/proxy-image/', html)
        self.assertIn('data-download-filename="example_1.webm"', html)
        self.assertIn('data-lightbox-preview-src="https://cdn.example/photo.webp"', html)
