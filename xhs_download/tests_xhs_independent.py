"""Offline authorization and orchestration checks; never contact providers."""
from contextlib import ExitStack
import json
from dataclasses import dataclass, field
from threading import Event
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import RequestFactory, SimpleTestCase, override_settings

from .models import ProviderRuntimeSetting
from .services.assets import MediaAsset, ParseResult

from .services.common import XhsParseError
from .services.platforms import xhs_independent as subject


URL = "https://www.xiaohongshu.com/explore/64b000000000000000000001"


@dataclass(frozen=True)
class Result:
    assets: tuple = ("synthetic",)
    metadata: dict = field(default_factory=dict)
    rich_media: object = None


class IndependentExecutionTests(SimpleTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(subject, "service_configured", return_value=True))
        self.execute = self.stack.enter_context(patch.object(subject, "execute_provider"))

    def parse(self, native, providers=("kukutool_generic",), **kwargs):
        return subject.parse_independent(URL, native_parse=native,
                                         provider_ids=providers, **kwargs)

    def test_native_login_failure_does_not_discard_service_result(self):
        self.execute.return_value = Result(metadata={"signed_binding": "retained"})
        result = self.parse(Mock(side_effect=XhsParseError("login required")))
        self.assertEqual(result.metadata["xhs_independent_source"], "service")
        self.assertEqual(result.metadata["signed_binding"], "retained")
        self.assertEqual(result.metadata["xhs_native_status"], "failed")
        self.assertTrue(self.execute.call_args.kwargs["service_authorized"])

    def test_service_exception_falls_back_without_exposing_exception(self):
        self.execute.side_effect = RuntimeError("synthetic-private-detail")
        result = self.parse(lambda deadline: Result())
        self.assertEqual(result.metadata["xhs_independent_source"], "native")
        self.assertNotIn("synthetic-private-detail", repr(result))

    def test_services_run_in_fixed_order_after_failure(self):
        self.execute.side_effect = [RuntimeError("failed"), Result()]
        self.parse(lambda deadline: Result(), tuple(reversed(subject.SERVICE_ORDER)))
        self.assertEqual([c.args[0] for c in self.execute.call_args_list],
                         list(subject.SERVICE_ORDER))

    def test_native_and_service_start_independently(self):
        native_started, service_started = Event(), Event()
        def native(deadline):
            native_started.set()
            self.assertTrue(service_started.wait(1))
            return Result()
        def service(*args, **kwargs):
            service_started.set()
            self.assertTrue(native_started.wait(1))
            return Result()
        self.execute.side_effect = service
        self.parse(native, timeout_seconds=2)
        self.assertTrue(native_started.is_set() and service_started.is_set())

    def test_native_rich_relationship_is_preserved_over_service(self):
        relationship = object()
        candidates = [{"candidate_id": "synthetic-rich-candidate"}]
        self.execute.return_value = Result(metadata={"upstream_candidates": candidates,
                                                   "source_provider": "service-only"})
        native = Result(assets=("native-still", "native-motion"),
                        metadata={"source_provider": "native"}, rich_media=relationship)
        result = self.parse(lambda deadline: native)
        self.assertIs(result.rich_media, relationship)
        self.assertEqual(result.assets, native.assets)
        self.assertEqual(result.metadata["source_provider"], "native")
        self.assertEqual(result.metadata["upstream_candidates"], candidates)
        self.assertEqual(result.metadata["xhs_independent_source"], "native")

    def test_candidate_only_service_preserves_native_assets_and_authority(self):
        candidates = [{"candidate_id": "synthetic-%s" % i} for i in range(4)]
        for media_type in ("image", "video"):
            with self.subTest(media_type=media_type):
                asset = MediaAsset(media_type=media_type,
                    url="https://media.example.invalid/native", platform="xiaohongshu",
                    asset_id="native-synthetic", provider_manifest_token="native-binding")
                native = ParseResult(platform="xiaohongshu", post_type=media_type,
                    base_filename="native", assets=(asset,), metadata={
                        "source_provider": "native", "source_identity_verified": True,
                        "note_id": "synthetic-note", "title": "native-title"})
                service = ParseResult(platform="third_party", post_type="image",
                    base_filename="service", assets=(), metadata={
                        "upstream_candidates": candidates, "source_provider": "kukutool_generic",
                        "source_platform": "upstream", "selected_provider": "kukutool_generic",
                        "source_identity_verified": False, "note_id": "other-note"})
                self.execute.return_value = service
                result = self.parse(lambda deadline: native)
                self.assertEqual(result.assets, native.assets)
                self.assertIs(result.assets[0], asset)
                self.assertEqual((result.platform, result.post_type, result.base_filename),
                                 (native.platform, native.post_type, native.base_filename))
                for key, value in native.metadata.items():
                    self.assertEqual(result.metadata[key], value)
                self.assertNotIn("source_platform", result.metadata)
                self.assertNotIn("selected_provider", result.metadata)
                self.assertEqual(result.metadata["upstream_candidates"], candidates)
                self.assertEqual(result.metadata["xhs_independent_source"], "native")
                self.assertNotIn("upstream_candidates", native.metadata)
                self.assertNotIn("xhs_independent_source", service.metadata)

    def test_native_failure_retains_candidate_only_service_without_assets(self):
        candidates = [{"candidate_id": "synthetic-candidate"}]
        self.execute.return_value = ParseResult(platform="third_party", post_type="image",
            base_filename="service", assets=(), metadata={"upstream_candidates": candidates})
        result = self.parse(Mock(side_effect=XhsParseError("login required")))
        self.assertEqual(result.assets, ())
        self.assertIsNone(result.rich_media)
        self.assertEqual(result.metadata["upstream_candidates"], candidates)
        self.assertEqual(result.metadata["xhs_independent_source"], "service")

    def test_no_authorized_or_unsupported_provider_never_executes_service(self):
        for providers in ((), ("unsupported",)):
            with self.subTest(providers=providers):
                native = Mock(return_value=Result())
                self.parse(native, providers)
                native.assert_called_once()
        self.execute.assert_not_called()

    def test_all_failed_preserves_native_error(self):
        error = XhsParseError("synthetic login failure")
        self.execute.side_effect = RuntimeError("private-service-error")
        with self.assertRaises(XhsParseError) as caught:
            self.parse(Mock(side_effect=error))
        self.assertIs(caught.exception, error)

    def test_deadline_returns_available_service_without_waiting_for_native(self):
        release, finished = Event(), Event()
        def native(deadline):
            try:
                release.wait(2)
                return Result()
            finally:
                finished.set()
        self.execute.return_value = Result()
        try:
            start = time.monotonic()
            result = self.parse(native, timeout_seconds=.1)
            self.assertLess(time.monotonic() - start, 1)
            self.assertEqual(result.metadata["xhs_native_status"], "deadline")
        finally:
            release.set()
            self.assertTrue(finished.wait(2))

    def test_expired_deadline_does_not_start_either_branch(self):
        native = Mock(return_value=Result())
        with self.assertRaises(XhsParseError):
            self.parse(native, deadline_monotonic=time.monotonic() - 1)
        native.assert_not_called()
        self.execute.assert_not_called()


@override_settings(THIRD_PARTY_RATE_LIMIT_PER_MINUTE=10,
                   THIRD_PARTY_GLOBAL_REQUEST_LIMIT_PER_MINUTE=100)
class IndependentAuthorizationTests(SimpleTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.config = ProviderRuntimeSetting(third_party_allowed_public_platforms="",
                                            third_party_blocked_platforms="")
        self.request = SimpleNamespace(user=SimpleNamespace(is_active=False,
            is_authenticated=False, is_staff=False))
        self.stack.enter_context(patch("xhs_download.services.provider_access.load_runtime_setting",
                                       return_value=self.config))
        self.access = self.stack.enter_context(patch(
            "xhs_download.services.provider_access.provider_access_summary",
            return_value={"request_allowed": True}))
        self.configured = self.stack.enter_context(patch.object(subject, "service_configured",
                                                               return_value=True))
        self.limit = self.stack.enter_context(patch(
            "xhs_download.services.rate_limits.consume_fixed_window",
            return_value=SimpleNamespace(allowed=True)))
        self.stack.enter_context(patch("xhs_download.services.rate_limits.get_client_identity",
                                       return_value="synthetic-client"))

    def test_denied_mode_blocks_even_configured_services(self):
        self.access.return_value = {"request_allowed": False}
        self.assertEqual(subject.authorized_services(self.request, URL), ())
        self.configured.assert_not_called()
        self.limit.assert_not_called()

    def test_anonymous_requires_allowlist(self):
        self.assertEqual(subject.authorized_services(self.request, URL), ())
        self.config.third_party_allowed_public_platforms = "upstream:kukutool_generic"
        self.assertEqual(subject.authorized_services(self.request, URL), ("kukutool_generic",))

    def test_staff_still_obeys_blocklist_and_runtime_gate(self):
        self.request.user = SimpleNamespace(is_active=True, is_authenticated=True, is_staff=True)
        self.config.third_party_blocked_platforms = "upstream:tiantianjiexi_generic"
        self.assertEqual(subject.authorized_services(self.request, URL), ("kukutool_generic",))
        self.configured.return_value = False
        self.assertEqual(subject.authorized_services(self.request, URL), ())

    def test_limit_denial_or_failure_is_fail_closed(self):
        self.config.third_party_allowed_public_platforms = "upstream:kukutool_generic"
        self.limit.return_value = SimpleNamespace(allowed=False)
        self.assertEqual(subject.authorized_services(self.request, URL), ())
        self.limit.side_effect = RuntimeError("cache unavailable")
        self.assertEqual(subject.authorized_services(self.request, URL), ())

    def test_non_xhs_source_does_not_authorize_services(self):
        self.assertEqual(subject.authorized_services(self.request, "https://example.invalid/"), ())
        self.access.assert_not_called()

    def test_recheck_does_not_reserve_parse_budget(self):
        self.config.third_party_allowed_public_platforms = "upstream:kukutool_generic"
        self.assertEqual(subject.authorized_services(self.request, URL, reserve_limits=False),
                         ("kukutool_generic",))
        self.limit.assert_not_called()

    def candidate_request(self, extra=None):
        from .views import upstream_candidate_view
        body = {"candidate_id": "synthetic-id", "resource_token": "synthetic-token"}
        body.update(extra or {})
        request = RequestFactory().post("/candidate/", json.dumps(body),
                                        content_type="application/json")
        request.user = self.request.user
        request._dont_enforce_csrf_checks = True
        return upstream_candidate_view(request)

    def candidate_record(self):
        return {"source_url": URL, "provider_id": "kukutool_generic",
                "match": {"provider_id": "kukutool_generic", "extractor_id": "kukutool_generic"},
                "url": "https://media.example.invalid/synthetic.mp4",
                "public": {"media_type": "video", "delivery_state": "unknown"}}

    def test_candidate_current_allowlist_then_revocation(self):
        self.config.third_party_allowed_public_platforms = "upstream:kukutool_generic"
        with patch("xhs_download.services.upstream_candidates.resolve_candidate_record",
                   return_value=self.candidate_record()) as resolve, patch(
                       "xhs_download.services.platforms.third_party.provider_is_enabled", return_value=True):
            response = self.candidate_request()
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response["Cache-Control"], "private, no-store")
            resolve.assert_called_with("synthetic-token", "synthetic-id")
            self.config.third_party_allowed_public_platforms = ""
            self.assertEqual(self.candidate_request().status_code, 403)
            self.config.third_party_allowed_public_platforms = "upstream:kukutool_generic"
            self.access.return_value = {"request_allowed": False}
            self.assertEqual(self.candidate_request().status_code, 403)
        self.limit.assert_not_called()

    def test_candidate_uses_record_source_not_client_override(self):
        self.config.third_party_allowed_public_platforms = "upstream:kukutool_generic"
        with patch("xhs_download.services.upstream_candidates.resolve_candidate_record",
                   return_value=self.candidate_record()) as resolve, patch(
                       "xhs_download.services.platforms.third_party.provider_is_enabled", return_value=True), patch.object(
                           subject, "authorized_services", wraps=subject.authorized_services) as gate:
            self.assertEqual(self.candidate_request().status_code, 200)
            self.assertEqual(gate.call_args.args[1], URL)
            self.assertEqual(gate.call_args.kwargs, {"reserve_limits": False})
            resolve.reset_mock()
            self.assertEqual(self.candidate_request({"source_url": "https://example.invalid/"}).status_code, 403)
            resolve.assert_not_called()

    def test_candidate_invalid_binding_is_denied_before_gate(self):
        with patch("xhs_download.services.upstream_candidates.resolve_candidate_record",
                   side_effect=XhsParseError("invalid binding")):
            self.assertEqual(self.candidate_request().status_code, 403)
        self.limit.assert_not_called()
