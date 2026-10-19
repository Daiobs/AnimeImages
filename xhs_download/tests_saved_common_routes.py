"""Saved Common scope, revocation and normal route API boundaries (offline)."""

from copy import deepcopy
from dataclasses import replace
import json
import sys
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from .models import UpstreamRoute, UpstreamRouteAudit
from .services.provider_lab.contracts import ProviderLabError
from .services.provider_lab.videodl_policy import backend_policy
from .services.saved_videodl import (
    authorized_common_options, confirmation_scope, normalize_saved_options, saved_backend_descriptor,
)
from .services.upstream_routes import read_route, source_scope, route_cache_identity, videodl_route_availability

HOST = "media.public.example.com"
SOURCE = f"https://{HOST}/work/one"
OPTIONS = {"mode": "specific_backend", "backend_category": "common", "backend_id": "GVVideoClient"}


@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache",
                                       "LOCATION": "saved-common-contract"}})
class SavedCommonRouteTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = get_user_model().objects.create_user("common-admin", is_staff=True, is_superuser=True)
        self.client.force_login(self.user)
        self.endpoint = reverse("upstream_routes")
        self.runtime = patch("xhs_download.services.saved_videodl.check_saved_runtime").start()
        self.addCleanup(patch.stopall)

    def payload(self, options=None, host=HOST, revision=0):
        options = deepcopy(OPTIONS if options is None else options)
        return {"source_host": host, "expected_revision": revision, "provider_order": ["videodl_repo"],
                "provider_options": {"videodl_repo": options},
                "sharing_confirmation": confirmation_scope(host, options)}

    def post(self, payload=None):
        return self.client.post(self.endpoint, json.dumps(self.payload() if payload is None else payload),
                                content_type="application/json")

    def test_save_binds_grant_and_audit_then_other_work_reads_it(self):
        result = self.post()
        self.assertEqual(result.status_code, 200, result.content)
        route = read_route(SOURCE.replace("one", "two"))
        grant = route["provider_options"]["videodl_repo"]["sharing_authorization"]
        self.assertEqual(grant["source_host"], HOST)
        self.assertEqual(grant["recipients"], ["greenvideo.cc"])
        self.assertEqual(grant["actor_id"], self.user.pk)
        self.assertEqual(grant["revision"], 1)
        self.assertEqual(UpstreamRouteAudit.objects.get().after["provider_options"], route["provider_options"])
        options = authorized_common_options(SOURCE.replace("one", "two"), 1, "GVVideoClient")
        self.assertEqual(options.backend_category, "common")
        self.assertEqual(options.backend_ids, ("GVVideoClient",))
        self.assertTrue(options.external_sharing_confirmed)
        self.runtime.assert_called_once()

    def test_confirmation_is_explicit_exact_scope_not_lab_or_history(self):
        base = self.payload()
        for confirmation in (None, True, {"external_sharing_confirmed": True},
                             {**base["sharing_confirmation"], "source_host": "other.example.com"},
                             {**base["sharing_confirmation"], "backend_id": "AnyFetcherVideoClient"},
                             {**base["sharing_confirmation"], "recipients": ["unreviewed.example.com"]},
                             {**base["sharing_confirmation"], "runtime_version": "old"}):
            with self.subTest(confirmation=confirmation):
                response = self.post({**base, "sharing_confirmation": confirmation})
                self.assertEqual(response.status_code, 403)
        injected = self.payload()
        injected["provider_options"]["videodl_repo"]["sharing_authorization"] = base["sharing_confirmation"]
        self.assertEqual(self.post(injected).status_code, 400)
        self.assertFalse(UpstreamRoute.objects.exists())
        self.runtime.assert_not_called()

    def test_wrong_category_blocked_unreviewed_and_comparison_only_rejected(self):
        for backend, category in (("GVVideoClient", "dedicated"), ("AcFunVideoClient", "common"),
                ("APICXVideoClient", "common"), ("UnreviewedClient", "common"), ("RayVideoClient", "common")):
            body = self.payload()
            body["provider_options"]["videodl_repo"].update(backend_id=backend, backend_category=category)
            self.assertEqual(self.post(body).status_code, 400)
        self.assertFalse(UpstreamRoute.objects.exists())

    def test_runtime_failure_never_saves_and_reader_has_no_write_permission(self):
        self.runtime.side_effect = ProviderLabError("PROVIDER_SCHEMA_INVALID")
        self.assertEqual(self.post().status_code, 422)
        self.assertFalse(UpstreamRoute.objects.exists())
        self.user.is_superuser = False
        self.user.save(update_fields=["is_superuser"])
        self.assertEqual(self.post().status_code, 403)
        self.client.logout()
        self.assertEqual(self.post().status_code, 403)

    def test_reset_backend_change_and_host_change_cannot_reuse_grant(self):
        self.assertEqual(self.post().status_code, 200)
        old = read_route(SOURCE)
        with self.assertRaises(ProviderLabError):
            authorized_common_options(SOURCE.replace(HOST, "sub." + HOST), 1, "GVVideoClient")
        second = self.payload({**OPTIONS, "backend_id": "AnyFetcherVideoClient"}, revision=1)
        stale = {**second, "sharing_confirmation": self.payload()["sharing_confirmation"]}
        self.assertEqual(self.post(stale).status_code, 403)
        self.assertEqual(self.post(second).status_code, 200)
        with self.assertRaises(ProviderLabError):
            authorized_common_options(SOURCE, 1, "GVVideoClient")
        self.assertEqual(authorized_common_options(SOURCE, 2, "AnyFetcherVideoClient").backend_ids,
                         ("AnyFetcherVideoClient",))
        self.assertNotEqual(route_cache_identity(old, ()), route_cache_identity(read_route(SOURCE), ()))
        reset = self.client.delete(self.endpoint, json.dumps({"source_host": HOST, "expected_revision": 2}),
                                   content_type="application/json")
        self.assertEqual(reset.status_code, 200)
        self.assertEqual(read_route(SOURCE)["provider_options"], {})
        with self.assertRaises(ProviderLabError):
            authorized_common_options(SOURCE, 2, "AnyFetcherVideoClient")
        self.assertEqual(UpstreamRouteAudit.objects.count(), 3)

    def test_revision_conflict_and_saved_grant_never_silently_renewed(self):
        self.assertEqual(self.post().status_code, 200)
        self.assertEqual(self.post().status_code, 409)
        payload = self.payload(revision=1)
        del payload["sharing_confirmation"]
        self.assertEqual(self.post(payload).status_code, 403)
        self.assertEqual(UpstreamRouteAudit.objects.count(), 1)

    def test_current_policy_receiver_and_version_rechecked(self):
        self.post()
        blocked = replace(backend_policy("GVVideoClient", "common"), controlled_state="blocked")
        with patch("xhs_download.services.saved_videodl.backend_policy", return_value=blocked):
            with self.assertRaises(ProviderLabError):
                authorized_common_options(SOURCE, 1, "GVVideoClient")
        with patch("xhs_download.services.saved_videodl.COMMON_ROUTE_RECEIVERS",
                   {"GVVideoClient": ("changed.example.com",)}):
            with self.assertRaises(ProviderLabError):
                authorized_common_options(SOURCE, 1, "GVVideoClient")
        with patch("xhs_download.services.saved_videodl.RUNTIME_VERSION", "changed"):
            with self.assertRaises(ProviderLabError):
                authorized_common_options(SOURCE, 1, "GVVideoClient")

    def test_saved_common_matching_is_not_fictitious_dedicated_match(self):
        from .services.quality_execution import match_saved_videodl
        self.post()
        with patch("xhs_download.services.provider_lab.runtime.execute_provider_worker") as worker:
            match = match_saved_videodl(SOURCE, OPTIONS, route_revision=1)[0]
        worker.assert_not_called()  # Runtime inventory is separately replaced by this unit test.
        self.assertEqual(match["extractor_id"], "GVVideoClient")
        self.assertEqual(match["match_basis"], "saved_exact_host_authorization")
        self.assertEqual(match["backend_category"], "common")
        with self.assertRaises(ProviderLabError):
            match_saved_videodl(SOURCE, OPTIONS)

    def test_cached_match_cannot_survive_revocation_or_runtime_check_failure(self):
        self.post()
        with patch("xhs_download.services.quality_execution.provider_available", return_value=True), \
                patch("xhs_download.services.quality_execution._python_for", return_value=sys.executable):
            self.assertEqual(videodl_route_availability(SOURCE, read_route(SOURCE))["status"], "available")
            self.runtime.side_effect = ProviderLabError("PROVIDER_SCHEMA_INVALID")
            self.assertEqual(videodl_route_availability(SOURCE, read_route(SOURCE))["status"], "unavailable")
        grant = UpstreamRoute.objects.get().provider_options
        grant["videodl_repo"].pop("sharing_authorization")
        UpstreamRoute.objects.update(provider_options=grant)
        with self.assertRaises(ProviderLabError):
            authorized_common_options(SOURCE, 1, "GVVideoClient")

    def test_legacy_dedicated_is_still_dedicated_and_mature_protection_unchanged(self):
        from .services.saved_videodl import execution_options
        old = {"mode": "specific_backend", "backend_id": "ArteTVVideoClient"}
        self.assertEqual(normalize_saved_options(old), old)
        self.assertEqual(execution_options(old).backend_category, "dedicated")
        self.assertFalse(execution_options(old).external_sharing_confirmed)
        for host in ("www.youtube.com", "www.bilibili.com", "www.douyin.com", "www.xiaohongshu.com"):
            self.assertFalse(source_scope(source_host=host)["applicable"])
            self.assertEqual(self.post(self.payload(host=host)).status_code, 422)
        self.assertFalse(UpstreamRoute.objects.exists())

    def test_descriptors_expose_specific_receiver_not_all_common(self):
        self.assertTrue(saved_backend_descriptor("GVVideoClient", "common")["saved_route_selectable"])
        self.assertEqual(saved_backend_descriptor("AnyFetcherVideoClient", "common")["saved_route_recipients"],
                         ["anyfetcher.com"])
        self.assertFalse(saved_backend_descriptor("RayVideoClient", "common")["saved_route_selectable"])

    @override_settings(THIRD_PARTY_PROVIDER_ENABLED=True)
    def test_common_order_uses_only_explicit_fallback_and_persisted_revision(self):
        from contextlib import ExitStack
        from .services.assets import ParseResult
        from .services.common import XhsParseError
        from .services.registry import PlatformRegistry
        from .services.saved_videodl import RUNTIME_VERSION
        matches = [
            {"provider_id": "videodl_repo", "extractor_id": "GVVideoClient", "version": RUNTIME_VERSION, "label": "Common fixture",
             "backend_category": "common", "match_basis": "saved_exact_host_authorization"},
            {"provider_id": "yt_dlp_generic", "extractor_id": "Fixture", "version": "2026.6.9", "label": "fixture"},
        ]
        result = ParseResult(platform="third_party", post_type="video", base_filename="offline", assets=(),
            metadata={"upstream_candidates": [{"candidate_id": "offline", "media_type": "video"}]})
        with ExitStack() as stack:
            stack.enter_context(patch("xhs_download.services.common.validate_public_download_url"))
            stack.enter_context(patch("xhs_download.services.platforms.third_party.provider_is_enabled", return_value=True))
            stack.enter_context(patch("xhs_download.services.platforms.third_party.runtime_bool", return_value=True))
            stack.enter_context(patch("xhs_download.services.quality_execution.provider_available", return_value=True))
            stack.enter_context(patch("xhs_download.services.upstream_matching.match_upstream_url", return_value=matches))
            execute = stack.enter_context(patch("xhs_download.services.quality_execution.execute_provider"))
            for revision, order in enumerate((
                    ["videodl_repo", "yt_dlp_generic"], ["yt_dlp_generic", "videodl_repo"], ["videodl_repo"])):
                body = self.payload(revision=revision)
                body["provider_order"] = order
                self.assertEqual(self.post(body).status_code, 200)
                execute.reset_mock()
                execute.side_effect = [ProviderLabError("PROVIDER_NO_MEDIA"), result]
                def parse():
                    return PlatformRegistry().parse_url(SOURCE, upstream_provider_ids=[m["provider_id"] for m in matches],
                                                        upstream_matches=matches)
                if len(order) == 1:
                    with self.assertRaises(XhsParseError):
                        parse()
                else:
                    parsed = parse()
                    self.assertEqual(parsed.metadata["selected_provider"], order[1])
                self.assertEqual([call.args[0] for call in execute.call_args_list], order)
                common = next(call for call in execute.call_args_list if call.args[0] == "videodl_repo")
                self.assertEqual(common.kwargs["backend"], "GVVideoClient")
                self.assertEqual(common.kwargs["saved_route_revision"], revision + 1)
