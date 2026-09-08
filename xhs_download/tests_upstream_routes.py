"""Persistent Human routes consumed by the real ordinary and Batch adapters."""

from contextlib import ExitStack
from dataclasses import replace
import json
import sys
import time
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core import signing
from django.core.cache import cache
from django.test import Client, RequestFactory, TestCase, override_settings
from django.urls import reverse

from .models import UpstreamRoute, UpstreamRouteAudit
from .services.assets import ParseResult
from .services.common import XhsParseError
from .services.platforms.third_party import GenericThirdPartyAdapter
from .services.registry import PlatformRegistry
from .services.upstream_routes import (
    UpstreamRouteError, read_route, route_matches, source_scope, write_route,
)


SOURCE = "https://www.douban.com/group/topic/123/"
PIDS = ["yt_dlp_generic", "gallery_dl_generic", "parsehub_generic", "videodl_repo"]
MATCHES = tuple({"provider_id": pid, "extractor_id": "Example", "label": pid, "version": "fixed-v1"}
                for pid in PIDS)


class RouteFixtures:
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="route-admin", is_staff=True, is_superuser=True)

    def save(self, order=None, revision=0, **kwargs):
        return write_route(SOURCE, actor=self.user, expected_revision=revision,
                           provider_order=order or ["parsehub_generic"], **kwargs)


class RoutePersistenceTests(RouteFixtures, TestCase):
    def test_save_reload_revision_audit_and_reset_tombstone(self):
        self.assertEqual(read_route(SOURCE)["revision"], 0)
        saved = self.save(format_preferences={"max_height": 2160})
        self.assertEqual(saved["revision"], 1)
        self.assertEqual(read_route(SOURCE)["effective_provider_order"], ["parsehub_generic"])
        self.assertEqual(UpstreamRoute.objects.get().format_preferences, {"max_height": 2160})
        reset = write_route(SOURCE, actor=self.user, expected_revision=1, reset=True)
        self.assertEqual(reset["revision"], 2)
        self.assertEqual(reset["mode"], "default")
        self.assertFalse(UpstreamRoute.objects.get().active)
        self.assertEqual(UpstreamRouteAudit.objects.count(), 2)
        self.assertEqual(UpstreamRouteAudit.objects.first().before["revision"], 1)
        self.assertEqual(UpstreamRouteAudit.objects.first().actor_id, self.user.pk)
        for old in (0, 1):
            with self.assertRaises(UpstreamRouteError) as caught:
                self.save(revision=old)
            self.assertEqual(caught.exception.status, 409)
        self.assertEqual(UpstreamRouteAudit.objects.count(), 2)
        self.assertEqual(self.save(revision=2)["revision"], 3)

    def test_same_host_scope_not_cdn_or_shared_parser(self):
        self.save()
        self.assertEqual(read_route("https://www.douban.com/note/456")["mode"], "manual")
        for url in ("https://douban.com/note/456", "https://cdn.example.com/a.jpg",
                    "https://www.coolapk.com/feed/123"):
            self.assertEqual(read_route(url)["mode"], "default")
        stored = UpstreamRoute.objects.get()
        self.assertEqual(stored.source_host, "www.douban.com")
        self.assertNotIn("123", json.dumps(UpstreamRouteAudit.objects.get().after))

    def test_mature_routes_and_unsafe_sources_cannot_be_saved(self):
        for url in ("https://www.youtube.com/watch?v=123", "https://www.xiaohongshu.com/explore/123",
                    "https://www.douyin.com/video/123", "https://www.bilibili.com/video/BV123",
                    "https://tiktok.com/@a/video/1"):
            self.assertFalse(source_scope(url)["applicable"])
            with self.assertRaises(UpstreamRouteError):
                write_route(url, actor=self.user, expected_revision=0, provider_order=PIDS)
        for url in ("file:///tmp/a", "https://127.0.0.1/a", "https://localhost/a", "https://user:secret@example.com/a", ""):
            with self.assertRaises(UpstreamRouteError):
                source_scope(url)
        self.assertFalse(UpstreamRoute.objects.exists())

    def test_only_registered_choices_and_reusable_preferences(self):
        for order in (["os.system"], ["parsehub_generic"] * 2, [], "parsehub_generic"):
            with self.assertRaises(UpstreamRouteError):
                write_route(SOURCE, actor=self.user, expected_revision=0, provider_order=order)
        for prefs in ({"format_id": "signed-temporary"}, {"url": "https://cdn.example/a"},
                      {"max_height": True}, {"max_height": 99999}, {"preferred_container": "secret"}):
            with self.assertRaises(UpstreamRouteError):
                self.save(format_preferences=prefs)
        for options in ({"parsehub_generic": {"command": "cat"}},
                        {"videodl_repo": {"mode": "grabber"}},
                        {"videodl_repo": {"mode": "specific_backend", "backend_id": "ArbitraryClient"}}):
            with self.assertRaises(UpstreamRouteError):
                self.save(PIDS, provider_options=options)
        self.assertFalse(UpstreamRoute.objects.exists())

    def test_valid_dedicated_backend_options_are_retained(self):
        state = self.save(["videodl_repo"], provider_options={"videodl_repo": {"mode": "dedicated_only"}})
        self.assertEqual(state["provider_options"], {"videodl_repo": {"mode": "dedicated_only"}})

    def test_identity_changes_with_revision_execution_and_defaults(self):
        _, first = route_matches(SOURCE, MATCHES)
        self.save()
        _, second = route_matches(SOURCE, MATCHES)
        self.assertNotEqual(first["cache_identity"], second["cache_identity"])
        newer = tuple({**match, "version": "fixed-v2"} for match in MATCHES)
        self.assertNotEqual(route_matches(SOURCE, newer)[1]["cache_identity"], second["cache_identity"])
        write_route(SOURCE, actor=self.user, expected_revision=1, reset=True)
        self.assertNotEqual(first["cache_identity"], route_matches(SOURCE, MATCHES)[1]["cache_identity"])


class RouteApiTests(RouteFixtures, TestCase):
    def setUp(self):
        super().setUp()
        self.endpoint = reverse("upstream_routes")

    def post(self, data=None, **kwargs):
        return self.client.post(self.endpoint, json.dumps(data or {
            "source_url": SOURCE, "expected_revision": 0, "provider_order": ["parsehub_generic"],
        }), content_type="application/json", **kwargs)

    def test_api_read_save_conflict_and_reset(self):
        self.client.force_login(self.user)
        response = self.client.get(self.endpoint, {"source_url": SOURCE})
        self.assertEqual(response.json()["route"]["source_host"], "www.douban.com")
        self.assertEqual(self.post().status_code, 200)
        self.assertEqual(self.post().status_code, 409)
        response = self.client.delete(self.endpoint, json.dumps({"source_url": SOURCE, "expected_revision": 1}),
                                      content_type="application/json")
        self.assertEqual(response.json()["route"]["revision"], 2)
        self.assertEqual(response.json()["route"]["mode"], "default")

    def test_api_permissions_and_csrf(self):
        self.assertEqual(self.post().status_code, 403)
        staff = get_user_model().objects.create_user(username="route-reader", is_staff=True)
        self.client.force_login(staff)
        self.assertEqual(self.client.get(self.endpoint, {"source_url": SOURCE}).status_code, 200)
        self.assertEqual(self.post().status_code, 403)
        staff.user_permissions.add(Permission.objects.get(codename="change_upstreamroute"))
        self.assertEqual(self.post().status_code, 200)
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(self.user)
        self.assertEqual(strict.post(self.endpoint, "{}", content_type="application/json").status_code, 403)
        self.assertEqual(strict.delete(self.endpoint, "{}", content_type="application/json").status_code, 403)

    def test_api_does_not_accept_extra_secret_fields(self):
        self.client.force_login(self.user)
        response = self.post({"source_url": SOURCE, "expected_revision": 0,
                              "provider_order": PIDS, "token": "private"})
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("private", response.content.decode())


@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache",
                                       "LOCATION": "upstream-route-regressions"}})
class RouteConsumptionTests(RouteFixtures, TestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch("xhs_download.services.upstream_matching.match_upstream_url", return_value=MATCHES))
        self.stack.enter_context(patch("xhs_download.services.common.validate_public_download_url"))
        self.stack.enter_context(patch("xhs_download.services.platforms.third_party.runtime_bool", return_value=True))
        self.stack.enter_context(patch("xhs_download.services.platforms.third_party.provider_is_enabled", return_value=True))
        self.stack.enter_context(patch("xhs_download.services.upstream_routes.videodl_route_availability",
                                      return_value={"matches": [MATCHES[3]]}))
        self.candidate = {"candidate_id": "test:2160p", "media_type": "video", "height": 2160,
                          "delivery": "separate_tracks"}
        self.result = ParseResult(platform="third_party", post_type="video", base_filename="quality", assets=(),
                                  metadata={"source_platform": "upstream", "upstream_candidates": [self.candidate]})
        from .services.quality_execution import execute_provider
        self.real_execute = execute_provider
        self.execute = self.stack.enter_context(patch("xhs_download.services.quality_execution.execute_provider",
                                                       return_value=self.result))

    def parse(self):
        return PlatformRegistry().parse_url(SOURCE, upstream_provider_ids=PIDS, upstream_matches=MATCHES)

    def enable_real_cache(self):
        from .services.upstream_candidates import issue_candidate
        self.stack.enter_context(patch("xhs_download.services.quality_execution._python_for", return_value=sys.executable))
        self.stack.enter_context(patch("xhs_download.services.upstream_candidates.validate_public_download_url"))
        self.candidate = issue_candidate("https://media.example/video.mp4", {"height": 2160}, ordinal=1,
            provider="parsehub_generic", source_url=SOURCE, media_type="video", state="separate_tracks")
        self.result = replace(self.result, metadata={**self.result.metadata, "upstream_candidates": [self.candidate]})
        self.execute.return_value = self.result

    def test_actual_parse_cache_hit_and_save_reset_revision_invalidation(self):
        self.enable_real_cache()
        self.save()
        self.assertFalse(self.parse().metadata["upstream_route"]["cache_hit"])
        self.assertTrue(self.parse().metadata["upstream_route"]["cache_hit"])
        self.assertEqual(self.execute.call_count, 1)
        self.save(revision=1, format_preferences={"max_height": 2160})
        self.assertFalse(self.parse().metadata["upstream_route"]["cache_hit"])
        self.assertTrue(self.parse().metadata["upstream_route"]["cache_hit"])
        self.assertEqual(self.execute.call_count, 2)
        write_route(SOURCE, actor=self.user, expected_revision=2, reset=True)
        with patch("xhs_download.services.upstream_routes.default_provider_order", return_value=["parsehub_generic"]):
            self.assertFalse(self.parse().metadata["upstream_route"]["cache_hit"])
            self.assertTrue(self.parse().metadata["upstream_route"]["cache_hit"])
        self.assertEqual(self.execute.call_count, 3)

    def test_actual_cache_isolates_url_and_version_and_rejects_revoked_candidate(self):
        self.enable_real_cache()
        self.save()
        self.parse()
        other = SOURCE.replace("123", "456")
        PlatformRegistry().parse_url(other, upstream_provider_ids=PIDS, upstream_matches=MATCHES)
        self.assertEqual(self.execute.call_count, 2)
        newer = tuple({**match, "version": "fixed-v2"} for match in MATCHES)
        with patch("xhs_download.services.upstream_matching.match_upstream_url", return_value=newer):
            PlatformRegistry().parse_url(SOURCE, upstream_provider_ids=PIDS, upstream_matches=newer)
        self.assertEqual(self.execute.call_count, 3)
        self.assertTrue(self.parse().metadata["upstream_route"]["cache_hit"])
        from .services.upstream_candidates import SALT
        token = signing.loads(self.candidate["resource_token"], salt=SALT)
        bundle_key = "upstream:candidate-bundle:" + token["bundle"]
        records = cache.get(bundle_key)
        del records[self.candidate["candidate_id"]]
        cache.set(bundle_key, records, timeout=300)
        self.assertFalse(self.parse().metadata["upstream_route"]["cache_hit"])
        self.assertEqual(self.execute.call_count, 4)
        with patch("xhs_download.services.platforms.third_party.provider_is_enabled", return_value=False):
            with self.assertRaises(XhsParseError):
                self.parse()
        self.assertEqual(self.execute.call_count, 4)

    def test_cached_fallback_does_not_suppress_preferred_provider_retry(self):
        self.enable_real_cache()
        self.save(["yt_dlp_generic", "parsehub_generic"])
        self.execute.side_effect = [XhsParseError("temporary"), self.result, XhsParseError("temporary")]
        self.parse()
        self.assertTrue(self.parse().metadata["upstream_route"]["cache_hit"])
        self.assertEqual([call.args[0] for call in self.execute.call_args_list],
                         ["yt_dlp_generic", "parsehub_generic", "yt_dlp_generic"])

    def test_cache_hit_rejects_expired_and_tampered_candidate_authority(self):
        from .services.upstream_routes import parse_cache_key
        self.enable_real_cache()
        self.save()
        self.parse()
        self.assertTrue(self.parse().metadata["upstream_route"]["cache_hit"])
        with patch("django.core.signing.time", SimpleNamespace(time=lambda: time.time() + 301)):
            self.assertFalse(self.parse().metadata["upstream_route"]["cache_hit"])
        self.assertEqual(self.execute.call_count, 2)
        _, route = route_matches(SOURCE, MATCHES)
        key = parse_cache_key(SOURCE, route, "parsehub_generic")
        cached = cache.get(key)
        cached.metadata["upstream_candidates"][0]["resource_token"] += "tampered"
        cache.set(key, cached, timeout=60)
        self.assertFalse(self.parse().metadata["upstream_route"]["cache_hit"])
        self.assertEqual(self.execute.call_count, 3)

    def test_format_preferences_reorder_real_candidates_preserving_images_and_unknowns(self):
        self.save(format_preferences={"max_height": 1080, "preferred_video_codec": "h264",
                                      "preferred_container": "mp4"})
        candidates = [
            {"candidate_id": "4k", "media_type": "video", "height": 2160, "vcodec": "avc1", "extension": "mp4"},
            {"candidate_id": "image", "media_type": "image", "ordinal": 2},
            {"candidate_id": "unknown", "media_type": "video"},
            {"candidate_id": "1080", "media_type": "video", "height": 1080, "vcodec": "avc1.640028", "extension": "mp4"},
            {"candidate_id": "720", "media_type": "video", "height": 720, "vcodec": "h264", "extension": "mp4"},
        ]
        self.execute.return_value = replace(self.result, metadata={"upstream_candidates": candidates})
        result = self.parse()
        self.assertEqual([c["candidate_id"] for c in result.metadata["upstream_candidates"]],
                         ["1080", "image", "720", "4k", "unknown"])
        self.assertEqual(result.metadata["format_preference_selection"]["candidate_id"], "1080")
        self.assertEqual(candidates[0]["candidate_id"], "4k")
        self.assertCountEqual(result.metadata["upstream_candidates"], candidates)
        self.execute.assert_called_once()

    def prepare_tail_format_candidates(self, preferences, baseline_count=70):
        rows = [[3, f"https://media.example/video-{index}.mp4", {
            "media_type": "video", "extension": "mp4", "height": 720,
            "vcodec": "avc1.64001f", "acodec": "none", "format_note": f"baseline-{index}",
        }] for index in range(baseline_count)]
        for index in (0, 31, 65):
            rows.insert(index, [3, f"https://media.example/image-{index}.jpg", {
                "media_type": "image", "extension": "jpg", "format_note": f"image-{index}",
            }])
        rows.extend([
            [3, "https://media.example/tail-2160.mp4", {"media_type": "video", "extension": "mp4",
                "height": 2160, "vcodec": "avc1.640033", "acodec": "none", "format_note": "tail-2160"}],
            [3, "https://media.example/tail-av1.webm", {"media_type": "video", "extension": "webm",
                "height": 1080, "vcodec": "av01.0.08M.08", "acodec": "none", "format_note": "tail-av1"}],
        ])
        self.stack.enter_context(patch("xhs_download.services.quality_execution._python_for", return_value=sys.executable))
        for module in ("upstream_delivery", "upstream_candidates"):
            self.stack.enter_context(patch(f"xhs_download.services.{module}.validate_public_download_url"))
        self.transport = self.stack.enter_context(patch("xhs_download.services.quality_execution._execute_upstream",
                                                       return_value=(rows, MATCHES[2], {})))
        self.normalized_results = []

        def execute(*args, **kwargs):
            result = self.real_execute(*args, **kwargs)
            self.normalized_results.append(result)
            return result

        self.execute.side_effect = execute
        self.save(format_preferences=preferences)
        return self.parse()

    def assert_candidate_identity_and_image_order(self, result, expected_count=75):
        original = self.normalized_results[0].metadata["upstream_candidates"]
        ordered = result.metadata["upstream_candidates"]
        self.assertEqual(len(original), expected_count)
        self.assertEqual(len({candidate["candidate_id"] for candidate in original}), expected_count)
        self.assertCountEqual(ordered, original)
        self.assertEqual([(index, candidate) for index, candidate in enumerate(ordered) if candidate["media_type"] == "image"],
                         [(index, candidate) for index, candidate in enumerate(original) if candidate["media_type"] == "image"])
        self.assertEqual(result.metadata["candidates_truncated"], 0)

    def test_tail_2160_preference_uses_retained_candidates_beyond_64(self):
        result = self.prepare_tail_format_candidates({"max_height": 2160, "preferred_video_codec": "h264"})
        selection = result.metadata["format_preference_selection"]
        candidate = next(item for item in result.metadata["upstream_candidates"]
                         if item["candidate_id"] == selection["candidate_id"])
        self.assertEqual(candidate["format_label"], "tail-2160")
        self.assert_candidate_identity_and_image_order(result)
        self.transport.assert_called_once()

    def test_tail_alternative_codec_preference_preserves_every_candidate(self):
        result = self.prepare_tail_format_candidates({"max_height": 1080, "preferred_video_codec": "av1",
                                                      "preferred_container": "webm"})
        selection = result.metadata["format_preference_selection"]
        self.assertEqual(selection["status"], "matched")
        candidate = next(item for item in result.metadata["upstream_candidates"]
                         if item["candidate_id"] == selection["candidate_id"])
        self.assertEqual(candidate["format_label"], "tail-av1")
        self.assert_candidate_identity_and_image_order(result)
        self.transport.assert_called_once()

    def test_parse_cache_keeps_all_retained_candidates_and_stable_ids(self):
        from .services.upstream_routes import parse_cache_key
        first = self.prepare_tail_format_candidates({"max_height": 2160, "preferred_video_codec": "h264"})
        second = self.parse()
        self.assertTrue(second.metadata["upstream_route"]["cache_hit"])
        self.transport.assert_called_once()
        self.assertEqual(first.metadata["upstream_candidates"], second.metadata["upstream_candidates"])
        _, route = route_matches(SOURCE, MATCHES)
        stored = cache.get(parse_cache_key(SOURCE, route, "parsehub_generic"))
        self.assertEqual(len(stored.metadata["upstream_candidates"]), 75)
        self.assertEqual(stored.metadata["upstream_candidates"],
                         self.normalized_results[0].metadata["upstream_candidates"])
        self.assertEqual(stored.metadata["upstream_candidates"][-2]["format_label"], "tail-2160")
        self.assertEqual(stored.metadata["upstream_candidates"][-1]["format_label"], "tail-av1")
        self.assert_candidate_identity_and_image_order(second)

    def test_355_candidate_bundle_keeps_every_record_valid_with_default_cache_capacity(self):
        from .services.upstream_candidates import SALT, resolve_candidate_record
        self.assertEqual(cache._max_entries, 300)
        first = self.prepare_tail_format_candidates({"max_height": 2160, "preferred_video_codec": "h264"},
                                                     baseline_count=350)
        candidates = first.metadata["upstream_candidates"]
        self.assertEqual(len(candidates), 355)
        bundle_ids = {signing.loads(candidate["resource_token"], salt=SALT)["bundle"] for candidate in candidates}
        self.assertEqual(len(bundle_ids), 1)
        for candidate in candidates:
            with self.subTest(ordinal=candidate["ordinal"]):
                record = resolve_candidate_record(candidate["resource_token"], candidate["candidate_id"])
                self.assertEqual(record["public"], candidate)
                self.assertEqual(record["source_url"], SOURCE)
                self.assertEqual(record["provider_id"], "parsehub_generic")
        second = self.parse()
        self.assertTrue(second.metadata["upstream_route"]["cache_hit"])
        self.transport.assert_called_once()
        self.assertEqual(second.metadata["upstream_candidates"], candidates)
        selection = second.metadata["format_preference_selection"]
        selected = next(candidate for candidate in candidates if candidate["candidate_id"] == selection["candidate_id"])
        self.assertEqual(selected["format_label"], "tail-2160")
        self.assert_candidate_identity_and_image_order(second, expected_count=355)

    def test_unmatched_format_preferences_keep_success_and_do_not_fallback(self):
        self.save(["parsehub_generic", "yt_dlp_generic"], format_preferences={"max_height": 720})
        result = self.parse()
        self.assertEqual(result.metadata["format_preference_selection"]["status"], "unmatched")
        self.assertEqual(result.metadata["upstream_candidates"], [self.candidate])
        self.execute.assert_called_once()

    def test_preferred_candidate_only_result_is_success_no_fallback(self):
        self.save(["parsehub_generic", "yt_dlp_generic"], format_preferences={"max_height": 2160})
        result = self.parse()
        self.assertEqual(result.assets, ())
        self.assertEqual(result.metadata["upstream_candidates"], [self.candidate])
        self.execute.assert_called_once()
        self.assertEqual(self.execute.call_args.args[0], "parsehub_generic")
        self.assertEqual(self.execute.call_args.kwargs["expected_match"], MATCHES[2])
        self.assertEqual(result.metadata["upstream_route"]["format_preferences"], {"max_height": 2160})

    def test_shared_executor_contract_preserves_normalized_candidate_only_success(self):
        self.save(["parsehub_generic", "yt_dlp_generic"])
        self.execute.side_effect = self.real_execute
        payload = [
            [3, "https://media.example/video.mp4", {"media_type": "video", "extension": "mp4", "height": 2160}],
        ]
        with patch("xhs_download.services.quality_execution._python_for", return_value="/isolated/python"), \
                patch("xhs_download.services.quality_execution._execute_upstream", return_value=(payload, MATCHES[2], {})) as worker, \
                patch("xhs_download.services.upstream_delivery.validate_public_download_url") as validate_media:
            result = self.parse()
        self.assertEqual(result.assets, ())
        self.assertTrue(result.metadata["upstream_candidates"])
        self.assertEqual(result.metadata["selected_provider"], "parsehub_generic")
        self.execute.assert_called_once()
        worker.assert_called_once()
        self.assertEqual(worker.call_args.args[0], "parsehub_generic")
        validate_media.assert_any_call("https://media.example/video.mp4")
        self.assertEqual(self.execute.call_args.kwargs["expected_match"], MATCHES[2])

    def test_same_provider_passes_only_the_admitted_extractor_to_execution(self):
        self.save(["parsehub_generic"])
        blocked = {**MATCHES[2], "extractor_id": "BlockedExtractor"}
        admitted = {**MATCHES[2], "extractor_id": "AdmittedExtractor"}
        with patch("xhs_download.services.upstream_matching.match_upstream_url", return_value=(blocked, admitted)):
            PlatformRegistry().parse_url(SOURCE, upstream_provider_ids=["parsehub_generic"],
                                         upstream_matches=(admitted,))
        self.execute.assert_called_once()
        self.assertEqual(self.execute.call_args.kwargs["expected_match"], admitted)
        self.assertNotIn("backend", self.execute.call_args.kwargs)

    def test_new_ordinary_parse_reads_changed_route_and_reset(self):
        self.save(["parsehub_generic"])
        self.assertEqual(self.parse().metadata["selected_provider"], "parsehub_generic")
        self.save(["gallery_dl_generic"], revision=1)
        self.assertEqual(self.parse().metadata["selected_provider"], "gallery_dl_generic")
        write_route(SOURCE, actor=self.user, expected_revision=2, reset=True)
        self.assertEqual(self.parse().metadata["selected_provider"], "yt_dlp_generic")

    def test_explicit_failure_order_can_reach_fourth_provider(self):
        self.save(PIDS)
        self.execute.side_effect = [XhsParseError("one"), XhsParseError("two"), XhsParseError("three"), self.result]
        result = self.parse()
        self.assertEqual([call.args[0] for call in self.execute.call_args_list], PIDS)
        self.assertEqual(result.metadata["provider_fallback_count"], 3)
        self.assertEqual(result.metadata["upstream_route"]["selected_provider"], "videodl_repo")
        self.assertEqual(result.metadata["upstream_route"]["fallback_reason"],
                         "yt_dlp_generic: failed; gallery_dl_generic: failed; parsehub_generic: failed")

    def test_access_filtered_provider_is_never_restored_by_preference(self):
        self.save(["parsehub_generic", "gallery_dl_generic"])
        result = PlatformRegistry().parse_url(SOURCE, upstream_provider_ids=["gallery_dl_generic"],
                                              upstream_matches=[MATCHES[1]])
        self.assertEqual(result.metadata["selected_provider"], "gallery_dl_generic")
        self.assertEqual(result.metadata["provider_attempts"][0]["status"], "unavailable")
        self.assertEqual(result.metadata["upstream_route"]["fallback_reason"], "parsehub_generic: unavailable")
        self.assertEqual(self.execute.call_count, 1)

    def test_disabled_manual_route_stays_saved_and_no_default_fallback(self):
        self.save(["parsehub_generic"])
        with patch("xhs_download.services.platforms.third_party.provider_is_enabled", return_value=False):
            with self.assertRaisesMessage(XhsParseError, "设置已保留"):
                self.parse()
        self.execute.assert_not_called()
        self.assertTrue(UpstreamRoute.objects.get().active)

    def test_backend_options_are_passed_not_executed_as_arbitrary_code(self):
        self.save(["videodl_repo"], provider_options={"videodl_repo": {"mode": "dedicated_only"}})
        self.parse()
        self.assertEqual(self.execute.call_args.kwargs["backend"], MATCHES[3]["extractor_id"])
        self.assertNotIn("options", self.execute.call_args.kwargs)
        self.assertNotIn("expected_match", self.execute.call_args.kwargs)

    def test_batch_uses_same_saved_route_and_preserves_candidate_only_success(self):
        from .services.batch_parse.results import build_batch_result_row
        self.enable_real_cache()
        self.save(["parsehub_generic", "yt_dlp_generic"])
        self.parse()
        gate = {"allowed": True, "platform_id": "upstream", "upstream_provider_ids": PIDS, "upstream_matches": MATCHES}
        request = RequestFactory().get("/")
        request.user = self.user
        seen_results = []

        def display(result, **kwargs):
            seen_results.append(result)
            return {"upstream_candidates": result.metadata["upstream_candidates"]}

        with patch("xhs_download.services.batch_parse.results.third_party_public_access_summary", return_value=gate):
            row = build_batch_result_row(request, SOURCE, prefer_ultra_hd=False, prefer_youtube_merge=False,
                per_link_timeout_seconds=30, query_uses_third_party_provider_func=lambda _: True,
                third_party_provider_is_available_func=lambda _: True,
                display_context_builder=display)
        self.assertEqual(row["status"], "success", row)
        self.assertEqual(row["selected_provider"], "parsehub_generic")
        self.assertTrue(seen_results[0].metadata["upstream_route"]["cache_hit"])
        self.execute.assert_called_once()


@override_settings(THIRD_PARTY_PROVIDER_ENABLED=True,
                   CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache",
                                       "LOCATION": "saved-videodl-admission"}})
class SavedVideodlAdmissionTests(RouteFixtures, TestCase):
    source = "https://www.zhihu.com/question/123/answer/456"

    def setUp(self):
        super().setUp()
        cache.clear()
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch("xhs_download.services.quality_execution._python_for", return_value=sys.executable))
        self.stack.enter_context(patch("xhs_download.services.upstream_matching.match_upstream_url", return_value=()))
        from .services.provider_lab.videodl_policy import REVIEWED_DEDICATED_BACKEND_IDS, REVIEWED_COMMON_BACKEND_IDS
        self.inventory = [{"backend_id": backend, "backend_category": category,
                           "registry_available": True, "display_label": backend}
                          for category, ids in (("dedicated", REVIEWED_DEDICATED_BACKEND_IDS),
                                                ("common", REVIEWED_COMMON_BACKEND_IDS)) for backend in ids]
        self.matched = ["ZhihuVideoClient"]

        def worker(**kwargs):
            if kwargs["operation"] == "inventory":
                return {"inventory": self.inventory}
            self.assertEqual(kwargs["operation"], "match_dedicated")
            self.assertEqual(kwargs["source"], self.source)
            return {"backend_ids": self.matched}

        self.worker = self.stack.enter_context(patch(
            "xhs_download.services.provider_lab.runtime.execute_provider_worker", side_effect=worker))
        from .services.provider_lab.videodl_control import load_videodl_inventory
        self.stack.enter_context(patch("xhs_download.services.provider_lab.videodl_control.load_videodl_inventory",
            side_effect=lambda **kwargs: load_videodl_inventory(**kwargs, invoke=self.worker)))

    def save_videodl(self, options=None):
        return write_route(self.source, expected_revision=0, actor=self.user,
            provider_order=["videodl_repo"], provider_options={"videodl_repo": options or {"mode": "dedicated_only"}})

    def gate(self, **values):
        from .models import ProviderRuntimeSetting, ProviderAccessMode
        from .services.provider_access import third_party_public_access_summary
        runtime = ProviderRuntimeSetting(third_party_mode=ProviderAccessMode.PUBLIC,
            third_party_allowed_public_platforms="upstream:videodl_repo", **values)
        return third_party_public_access_summary(self.source, runtime_setting=runtime)

    def test_real_registry_discovery_admits_saved_route_and_executes_exact_backend(self):
        self.save_videodl()
        gate = self.gate()
        self.assertTrue(gate["allowed"], gate)
        self.assertEqual(gate["upstream_matches"][0]["extractor_id"], "ZhihuVideoClient")
        result = ParseResult(platform="third_party", post_type="video", base_filename="test", assets=(),
                            metadata={"upstream_candidates": [{"candidate_id": "retained"}]})
        with patch("xhs_download.services.common.validate_public_download_url"), \
                patch("xhs_download.services.quality_execution.execute_provider", return_value=result) as execute:
            parsed = PlatformRegistry().parse_url(self.source, upstream_provider_ids=gate["upstream_provider_ids"],
                                                  upstream_matches=gate["upstream_matches"])
        execute.assert_called_once()
        self.assertEqual(execute.call_args.kwargs["backend"], "ZhihuVideoClient")
        self.assertEqual(parsed.assets, ())
        self.assertEqual(parsed.metadata["selected_provider"], "videodl_repo")
        self.assertEqual(self.worker.call_count, 2)

    def test_batch_consumes_real_saved_videodl_admission(self):
        from .services.batch_parse.results import build_batch_result_row
        from .models import ProviderRuntimeSetting, ProviderAccessMode
        self.save_videodl()
        runtime = ProviderRuntimeSetting(third_party_mode=ProviderAccessMode.PUBLIC,
            third_party_allowed_public_platforms="upstream:videodl_repo")
        request = RequestFactory().get("/")
        request.user = self.user
        result = ParseResult(platform="third_party", post_type="video", base_filename="test", assets=(),
                            metadata={"upstream_candidates": [{"candidate_id": "retained"}]})
        with patch("xhs_download.services.provider_access.load_runtime_setting", return_value=runtime), \
                patch("xhs_download.services.common.validate_public_download_url"), \
                patch("xhs_download.services.quality_execution.execute_provider", return_value=result) as execute:
            row = build_batch_result_row(request, self.source, prefer_ultra_hd=False, prefer_youtube_merge=False,
                per_link_timeout_seconds=30, query_uses_third_party_provider_func=lambda _: True,
                third_party_provider_is_available_func=lambda _: True,
                display_context_builder=lambda parsed, **kwargs: {"upstream_candidates": parsed.metadata["upstream_candidates"]})
        self.assertEqual(row["status"], "success", row)
        self.assertEqual(row["selected_provider"], "videodl_repo")
        execute.assert_called_once()
        self.assertEqual(execute.call_args.kwargs["backend"], "ZhihuVideoClient")

    def test_no_saved_route_or_other_host_does_not_grant_discovery(self):
        from .services.upstream_routes import saved_videodl_matches
        self.assertEqual(saved_videodl_matches(self.source), ())
        self.save_videodl()
        self.assertEqual(saved_videodl_matches("https://zhihu.com/question/123/answer/456"), ())
        self.assertEqual(saved_videodl_matches("https://www.youtube.com/watch?v=123"), ())
        self.worker.assert_not_called()

    def test_saved_specific_backend_must_actually_match_source(self):
        self.save_videodl({"mode": "specific_backend", "backend_id": "ABCVideoClient"})
        self.assertFalse(self.gate()["allowed"])

    def test_runtime_inventory_and_policy_fail_closed(self):
        from .services.upstream_routes import saved_videodl_matches
        self.save_videodl()
        for matches in (["UnknownVideoClient"], ["HaokanVideoClient"], ["C56VideoClient"], ["GVVideoClient"], []):
            cache.clear()
            self.matched = matches
            self.assertEqual(saved_videodl_matches(self.source), ())
        self.matched = ["ZhihuVideoClient"]
        cache.clear()
        next(item for item in self.inventory if item["backend_id"] == "ZhihuVideoClient")["registry_available"] = False
        self.assertEqual(saved_videodl_matches(self.source), ())
        cache.clear()
        with patch("xhs_download.services.quality_execution.provider_available", return_value=False):
            self.assertEqual(saved_videodl_matches(self.source), ())

    def test_host_provider_and_extractor_blocks_still_win(self):
        self.save_videodl()
        for value in ("upstream", "upstream:videodl_repo", "upstream:extractor:zhihuvideoclient"):
            self.assertFalse(self.gate(third_party_blocked_platforms=value)["allowed"])
        self.assertFalse(self.gate(third_party_blocked_hosts="www.zhihu.com")["allowed"])

    def test_reset_removes_videodl_admission_despite_cached_match(self):
        self.save_videodl()
        self.assertTrue(self.gate()["allowed"])
        write_route(self.source, expected_revision=1, actor=self.user, reset=True)
        self.assertFalse(self.gate()["allowed"])

    def test_stale_backend_a_is_not_executed_under_new_backend_b_revision(self):
        self.matched = ["ZhihuVideoClient", "ABCVideoClient"]
        self.save_videodl({"mode": "specific_backend", "backend_id": "ZhihuVideoClient"})
        adapter = GenericThirdPartyAdapter()
        stale_rule = adapter.get_rule_for_url(self.source)
        self.assertEqual(stale_rule.upstream_matches[0]["extractor_id"], "ZhihuVideoClient")
        write_route(self.source, expected_revision=1, actor=self.user, provider_order=["videodl_repo"],
                    provider_options={"videodl_repo": {"mode": "specific_backend", "backend_id": "ABCVideoClient"}})
        matches, route = route_matches(self.source, stale_rule.upstream_matches)
        self.assertEqual(route["revision"], 2)
        self.assertEqual(matches, ())
        with patch("xhs_download.services.common.validate_public_download_url"), \
                patch("xhs_download.services.quality_execution.execute_provider") as execute, \
                patch("xhs_download.services.upstream_routes.cached_parse_result") as cached:
            with self.assertRaises(XhsParseError):
                adapter.parse_upstream_rule(self.source, stale_rule, 30, None)
        execute.assert_not_called()
        cached.assert_not_called()
        fresh = adapter.get_rule_for_url(self.source).upstream_matches
        self.assertEqual(route_matches(self.source, fresh)[0][0]["extractor_id"], "ABCVideoClient")
        self.assertEqual(route_matches(self.source, [{**fresh[0], "version": "stale-pin"}])[0], ())

    def test_stale_videodl_rule_cannot_execute_after_reset(self):
        self.save_videodl()
        adapter = GenericThirdPartyAdapter()
        stale_rule = adapter.get_rule_for_url(self.source)
        self.assertTrue(stale_rule.upstream_matches)
        write_route(self.source, expected_revision=1, actor=self.user, reset=True)
        matches, route = route_matches(self.source, stale_rule.upstream_matches)
        self.assertEqual(route["revision"], 2)
        self.assertFalse(route["active"])
        self.assertEqual(matches, ())
        with patch("xhs_download.services.common.validate_public_download_url"), \
                patch("xhs_download.services.quality_execution.execute_provider") as execute, \
                patch("xhs_download.services.upstream_routes.cached_parse_result") as cached:
            with self.assertRaises(XhsParseError):
                adapter.parse_upstream_rule(self.source, stale_rule, 30, None)
        execute.assert_not_called()
        cached.assert_not_called()

    def test_saved_unavailable_route_reports_status_without_erasing_preferences(self):
        self.client.force_login(self.user)
        with patch("xhs_download.services.quality_execution.provider_available", return_value=False):
            response = self.client.post(reverse("upstream_routes"), json.dumps({"source_url": self.source,
                "expected_revision": 0, "provider_order": ["videodl_repo"]}), content_type="application/json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["route"]["provider_availability"]["videodl_repo"],
                         {"status": "unavailable", "reason": "PROVIDER_NOT_CONFIGURED"})
        self.assertTrue(UpstreamRoute.objects.get().active)
