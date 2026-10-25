"""Real service normalization, Native merge and result templates; no network."""

from contextlib import ExitStack
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bs4 import BeautifulSoup
from django.core.cache import cache
from django.template.loader import render_to_string
from django.test import RequestFactory, SimpleTestCase, override_settings

from .models import ProviderAccessMode, ProviderRuntimeSetting
from .services.assets import MediaAsset, ParseResult
from .services.platforms import xhs_independent as subject
from .services.quality_execution import execute_provider
from .services.upstream_candidates import resolve_candidate_record
from .templatetags.ytdlp_picker import remaining_picker_videos, ytdlp_presentation
from .views import build_result_display_context


NOTE_ID = "64b000000000000000000001"
SOURCE = f"https://www.xiaohongshu.com/explore/{NOTE_ID}"
COVER = "https://ci.xiaohongshu.com/notes_pre_post/current-work-cover.jpg"
PREVIEW = COVER + "?imageView2/2/w/640/format/webp"


@override_settings(
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache",
                        "LOCATION": "xhs-display-ownership"}},
    THIRD_PARTY_PROVIDER_ENABLED=True,
)
class XhsDisplayOwnershipTests(SimpleTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        cache.clear()
        self.addCleanup(cache.clear)
        self.network = [self.stack.enter_context(patch(target,
            side_effect=AssertionError("Network forbidden in XHS display regression")))
            for target in ("socket.getaddrinfo", "socket.create_connection",
                           "requests.sessions.Session.request")]
        self.addCleanup(self.assert_no_network)
        for target in ("xhs_download.services.upstream_candidates.validate_public_download_url",
                       "xhs_download.services.upstream_delivery.validate_public_download_url"):
            self.stack.enter_context(patch(target, side_effect=lambda url, **kwargs: url))
        self.stack.enter_context(patch("xhs_download.services.quality_execution._python_for"))
        self.stack.enter_context(patch.object(subject, "service_configured", return_value=True))

    def assert_no_network(self):
        for guard in self.network:
            guard.assert_not_called()

    def service(self, provider="kukutool_generic", payload=None, source=SOURCE):
        name = ("40b6b3302653089b0ed07351" if provider == "tiantianjiexi_generic"
                else "1d723e3f60f5f7b7634cd84a")
        if payload is None:
            fixture = json.loads((Path(__file__).parent / "fixtures/native_replay" / (name + ".json")).read_text())
            payload = fixture["records"][0]
            if provider == "tiantianjiexi_generic":
                payload = payload["data"]
        module, factory = (("tiantianjiexi_generic", "TianTianJieXiGenericProvider")
                           if provider == "tiantianjiexi_generic" else
                           ("kukutool_generic", "KukutoolGenericProvider"))
        with patch(f"xhs_download.services.platforms.{module}.{factory}.parse", return_value=payload) as boundary:
            result = execute_provider(provider, source, service_authorized=True)
        boundary.assert_called_once()
        self.assertEqual(result.assets, ())
        return result

    def video_rows(self, service):
        return [row for row in service.metadata["upstream_candidates"] if row["media_type"] == "video"]

    def resource_url(self, row):
        return resolve_candidate_record(row["resource_token"], row["candidate_id"])["url"]

    def native(self, urls, **metadata):
        return ParseResult(platform="xiaohongshu", post_type="video", base_filename="Current work",
            assets=tuple(MediaAsset(media_type="video", platform="xiaohongshu", url=url,
                cover_url=COVER, cover_preview_url=PREVIEW, ext="mp4", size_bytes=9000)
                for url in urls),
            metadata={"source_provider": "native", "source_identity_verified": True,
                      "note_id": NOTE_ID, "title": "Current work", **metadata})

    def merge(self, native, service, source=SOURCE):
        with patch.object(subject, "execute_provider", return_value=service):
            return subject.parse_independent(source, native_parse=lambda deadline: native,
                provider_ids=(service.metadata["source_provider"],), timeout_seconds=2)

    def presentation(self, result):
        context = build_result_display_context(result, probe_missing_video_sizes=False)
        picker = ytdlp_presentation(context["upstream_candidates"], legacy_videos=context["video_items"])
        return picker, context

    def html(self, context, *, staff=False, batch=False):
        data = {**context, "proxy_image_url": "/image/", "show_internal_diagnostics": staff,
                "batch_download_allowed": staff}
        if batch:
            data["row"] = {**context, "status": "success", "platform_name": "小红书"}
        return BeautifulSoup(render_to_string("partials/batch_result_card.html" if batch else
                                             "partials/xhs_results_region.html", data), "html.parser")

    def test_exact_resource_is_one_group_without_borrowing_native_authority(self):
        service = self.service()
        row = self.video_rows(service)[-1]
        record_before = deepcopy(resolve_candidate_record(row["resource_token"], row["candidate_id"]))
        native = self.native([self.resource_url(row)])
        candidates_before = deepcopy(service.metadata["upstream_candidates"])
        result = self.merge(native, service)
        picker, context = self.presentation(result)
        self.assertEqual(picker["video_resource_count"], 1)
        self.assertEqual(remaining_picker_videos(picker, context["video_items"]), [])
        self.assertIsNone(picker["legacy_video"])
        self.assertEqual(picker["cover_video"]["cover_url"], COVER)
        self.assertIs(result.assets[0], native.assets[0])
        self.assertEqual(service.metadata["upstream_candidates"], candidates_before)
        self.assertEqual(resolve_candidate_record(row["resource_token"], row["candidate_id"]), record_before)
        self.assertTrue(all(not item.get("legacy_asset_id") and not item.get("legacy_delivery")
                            for item in result.metadata["upstream_candidates"]))
        self.assertTrue(all(not item["can_legacy_download"] for item in picker["videos"]))
        for staff in (False, True):
            for batch in (False, True):
                with self.subTest(staff=staff, batch=batch):
                    soup = self.html(context, staff=staff, batch=batch)
                    self.assertEqual(len(soup.select("[data-ytdlp-picker]")), 1)
                    self.assertEqual(len(soup.select("[data-picker-radio]")), 7)
                    self.assertFalse(soup.select(".asset-card[data-type=video], [data-batch-download-item][data-type=video]"))
                    image = soup.select_one("[data-picker-legacy-cover]")
                    self.assertIn("/image/?u=", image["src"])
                    self.assertFalse(image.has_attr("data-cover-available"))
                    self.assertEqual(bool(soup.select("[data-upstream-candidates]")), staff)

    def test_saved_tiantian_http_formats_keep_native_download_and_reuse_cover(self):
        service = self.service("tiantianjiexi_generic")
        native = self.native([self.resource_url(self.video_rows(service)[-1])])
        picker, context = self.presentation(self.merge(native, service))
        self.assertEqual(picker["video_resource_count"], 1)
        self.assertFalse(any(item["can_download"] for item in picker["videos"]))
        self.assertEqual(remaining_picker_videos(picker, context["video_items"]), context["video_items"])
        self.assertEqual(picker["cover_video"]["cover_url"], COVER)
        self.assertTrue(self.html(context).select(".asset-card[data-type=video]"))

    def test_same_work_without_exact_resource_only_shares_cover_keeps_native_operation(self):
        for provider in subject.SERVICE_ORDER:
            with self.subTest(provider=provider):
                service = self.service(provider, payload={"note_id": NOTE_ID,
                    "videos": [{"url": "https://media.example/service-only.mp4", "quality": "720P"}]})
                cache.set("same-work-service-result", service, 30)
                service = cache.get("same-work-service-result")
                self.assertEqual(service.metadata["_source_identity_signals"], (NOTE_ID,))
                native = self.native(["https://sns-video-bd.xhscdn.com/native-only.mp4"])
                picker, context = self.presentation(self.merge(native, service))
                self.assertEqual(picker["video_resource_count"], 1)
                self.assertEqual(picker["cover_video"]["cover_url"], COVER)
                self.assertEqual(remaining_picker_videos(picker, context["video_items"]), context["video_items"])

    def test_service_without_work_identity_keeps_nonexact_native_independent(self):
        for provider in subject.SERVICE_ORDER:
            with self.subTest(provider=provider):
                service = self.service(provider)
                native = self.native(["https://sns-video-bd.xhscdn.com/native-only.mp4"])
                picker, context = self.presentation(self.merge(native, service))
                self.assertEqual(picker["video_resource_count"], 2)
                self.assertIsNone(picker["cover_video"])
                self.assertEqual(remaining_picker_videos(picker, context["video_items"]), context["video_items"])
                for staff in (False, True):
                    for batch in (False, True):
                        soup = self.html(context, staff=staff, batch=batch)
                        self.assertFalse(soup.select("[data-picker-legacy-cover]"))
                        self.assertTrue(soup.select(".asset-card[data-type=video], [data-batch-download-item][data-type=video]"))

    def test_explicit_other_work_never_borrows_cover_even_for_exact_resource(self):
        other_note = NOTE_ID[:-1] + "2"
        url = "https://media.example/service-video.mp4"
        for provider in subject.SERVICE_ORDER:
            for native_url in (url, "https://media.example/native-only.mp4"):
                for identity in ({"note_id": other_note},
                                 {"note_id": NOTE_ID, "canonical_url": SOURCE.replace(NOTE_ID, other_note)}):
                    with self.subTest(provider=provider, exact=native_url == url, identity=identity):
                        service = self.service(provider, payload={**identity,
                            "videos": [{"url": url, "quality": "720P"}]})
                        candidates = deepcopy(service.metadata["upstream_candidates"])
                        records = [deepcopy(resolve_candidate_record(row["resource_token"], row["candidate_id"]))
                                   for row in candidates]
                        picker, context = self.presentation(self.merge(self.native([native_url]), service))
                        self.assertEqual(picker["video_resource_count"], 2)
                        self.assertIsNone(picker["cover_video"])
                        self.assertEqual(context["upstream_candidates"], candidates)
                        self.assertEqual(remaining_picker_videos(picker, context["video_items"]), context["video_items"])
                        self.assertEqual([resolve_candidate_record(row["resource_token"], row["candidate_id"])
                                          for row in candidates], records)
                        for staff in (False, True):
                            for batch in (False, True):
                                soup = self.html(context, staff=staff, batch=batch)
                                self.assertFalse(soup.select("[data-picker-legacy-cover]"))
                                self.assertTrue(soup.select(".asset-card[data-type=video], [data-batch-download-item][data-type=video]"))

    def test_restricted_service_and_old_cached_candidates_keep_native_control(self):
        url = "https://media.example/video.mp4"
        restricted = self.service(payload={"videos": [{"url": url, "quality": "720P",
                                                       "requires_cookie": True}]})
        regular = self.service(payload={"videos": [{"url": url, "quality": "720P"}]})
        older = replace(regular, metadata={**regular.metadata, "upstream_candidates": [
            {key: value for key, value in row.items() if key != "resource_alias_key"}
            for row in regular.metadata["upstream_candidates"]]})
        for service in (restricted, older):
            with self.subTest(restricted=service is restricted):
                picker, context = self.presentation(self.merge(self.native([url]), service))
                self.assertEqual(picker["video_resource_count"], 1 if service is restricted else 2)
                self.assertIsNone(picker["legacy_video"])
                self.assertEqual(remaining_picker_videos(picker, context["video_items"]), context["video_items"])
                if service is restricted:
                    self.assertEqual(picker["cover_video"]["cover_url"], COVER)
                else:
                    self.assertIsNone(picker["cover_video"])

    def test_title_size_or_unknown_identity_cannot_establish_ownership(self):
        service = self.service()
        native = self.native([self.resource_url(self.video_rows(service)[0])], source_identity_verified=False)
        picker, context = self.presentation(self.merge(native, service))
        self.assertEqual(picker["video_resource_count"], 2)
        self.assertIsNone(picker["cover_video"])
        self.assertEqual(remaining_picker_videos(picker, context["video_items"]), context["video_items"])

    def test_different_source_or_note_id_never_reuses_native_cover(self):
        service = self.service()
        url = self.resource_url(self.video_rows(service)[0])
        for changed in (replace(service, metadata={**service.metadata,
                            "canonical_source_url": SOURCE.replace(NOTE_ID, "64b000000000000000000002")}),
                        replace(service, metadata={**service.metadata, "note_id": NOTE_ID[:-1] + "2"})):
            with self.subTest(metadata=changed.metadata["canonical_source_url"]):
                picker, _ = self.presentation(self.merge(self.native([url]), changed))
                self.assertEqual(picker["video_resource_count"], 2)
                self.assertIsNone(picker["cover_video"])

    def test_independent_videos_keep_their_own_groups_and_covers(self):
        urls = [f"https://media.example/video-{i}.mp4" for i in (1, 2)]
        service = self.service(payload={"title": "Collection", "videos": [
            {"url": url, "quality": "720P"} for url in urls]})
        native = self.native(urls)
        native = replace(native, assets=(native.assets[0], replace(native.assets[1], cover_url=COVER + "-two")))
        picker, context = self.presentation(self.merge(native, service))
        self.assertEqual(picker["video_resource_count"], 2)
        self.assertEqual([group["cover_video"]["cover_url"] for group in picker["groups"]], [COVER, COVER + "-two"])
        self.assertFalse(remaining_picker_videos(picker, context["video_items"]))

    def test_shared_url_in_multiple_work_groups_is_ambiguous(self):
        url = "https://media.example/shared.mp4"
        service = self.service(payload={"videos": [{"url": url, "quality": "720P"},
                                                   {"url": url, "quality": "1080P"}]})
        picker, context = self.presentation(self.merge(self.native([url]), service))
        self.assertEqual(picker["video_resource_count"], 3)
        self.assertTrue(all(group["cover_video"] is None for group in picker["groups"]))
        self.assertEqual(remaining_picker_videos(picker, context["video_items"]), context["video_items"])

    def test_native_plus_single_result_and_livephoto_remain_unchanged(self):
        service = self.service()
        native = self.native([self.resource_url(self.video_rows(service)[0])])
        context = build_result_display_context(native, probe_missing_video_sizes=False)
        self.assertIsNone(ytdlp_presentation([], legacy_videos=context["video_items"]))
        self.assertEqual(len(self.html(context).select(".asset-card[data-type=video]")), 1)
        rich_native = SimpleNamespace(platform="xiaohongshu", metadata=native.metadata, rich_media=object())
        self.assertIs(subject._native_display_candidates(rich_native, service, SOURCE),
                      service.metadata["upstream_candidates"])

    def test_cached_result_reuses_display_facts_without_altering_candidate_or_asset(self):
        service = self.service()
        row = self.video_rows(service)[0]
        native = self.native([self.resource_url(row)])
        result = self.merge(native, service)
        cache.set("display-result", result, 30)
        cached = cache.get("display-result")
        picker, context = self.presentation(cached)
        self.assertEqual(picker["video_resource_count"], 1)
        self.assertFalse(remaining_picker_videos(picker, context["video_items"]))
        self.assertEqual(cached.assets, native.assets)
        self.assertEqual(resolve_candidate_record(row["resource_token"], row["candidate_id"])["url"], native.assets[0].url)

    def test_current_request_admission_controls_both_staff_and_anonymous_paths(self):
        config = ProviderRuntimeSetting(third_party_mode=ProviderAccessMode.PUBLIC,
            third_party_allowed_public_platforms="upstream:kukutool_generic",
            third_party_blocked_platforms="upstream:tiantianjiexi_generic")
        service = self.service()
        native = self.native([self.resource_url(self.video_rows(service)[0])])
        with patch("xhs_download.services.provider_access.load_runtime_setting", return_value=config):
            for staff in (False, True):
                request = RequestFactory().get("/", REMOTE_ADDR="192.0.2.1")
                request.user = SimpleNamespace(is_active=staff, is_authenticated=staff, is_staff=staff, pk=1 if staff else None)
                providers = subject.authorized_services(request, SOURCE, reserve_limits=False)
                self.assertEqual(providers, ("kukutool_generic",))
                with patch.object(subject, "execute_provider", return_value=service):
                    result = subject.parse_independent(SOURCE, native_parse=lambda deadline: native,
                                                      provider_ids=providers, timeout_seconds=2)
                picker, _ = self.presentation(result)
                self.assertEqual(picker["video_resource_count"], 1)
            config.third_party_allowed_public_platforms = ""
            request.user = SimpleNamespace(is_active=False, is_authenticated=False, is_staff=False)
            self.assertEqual(subject.authorized_services(request, SOURCE, reserve_limits=False), ())
