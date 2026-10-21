from contextlib import ExitStack, contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests
from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from .models import ProviderRuntimeSetting
from .services.assets import ParseResult
from .services.common import XhsDownloadError, XhsParseError, XhsSecurityError, request_get_with_retry
from .services.platforms import bilibili_fallback as subject
from .services.registry import parse_media_page

URL = "https://www.bilibili.com/video/BV1J4JX6PEMV/"
PROVIDER = subject.PROVIDER
HTTP_ERROR = lambda: XhsDownloadError("HTTP 412", http_status=412)


class BilibiliFallbackControlTests(SimpleTestCase):
    def test_actual_request_helper_does_not_repeat_explicit_412(self):
        response = Mock(status_code=412, headers={})
        with patch("xhs_download.services.common.socket.getaddrinfo",
                   return_value=[(None, None, None, "", ("8.8.8.8", 443))]), patch(
                       "xhs_download.services.common._SingleHopSession.request",
                       return_value=response) as send, patch(
                           "xhs_download.services.common.time.sleep") as sleep:
            with self.assertRaises(XhsDownloadError) as raised:
                request_get_with_retry(
                    "https://api.bilibili.com/x/web-interface/view",
                    retries=2, non_retry_statuses=(412, 429))
        self.assertEqual(raised.exception.http_status, 412)
        send.assert_called_once()
        sleep.assert_not_called()
        response.close.assert_called_once()

    def test_native_success_does_not_authorize_or_start_service(self):
        native = ParseResult(platform="bilibili", post_type="video",
                             base_filename="native", assets=())
        authorize = Mock()
        with patch.object(subject, "TianTianJieXiGenericProvider") as service:
            result = subject.parse_fallback(URL, native_parse=Mock(return_value=native),
                                            authorize=authorize)
        self.assertIs(result, native)
        authorize.assert_not_called()
        service.assert_not_called()

    def test_native_keeps_original_deadline_without_reserving_service_time(self):
        native = Mock(return_value=object())
        with patch.object(subject.time, "monotonic", return_value=100):
            subject.parse_fallback(URL, native_parse=native, authorize=Mock())
            native.assert_called_once_with(None)
            native.reset_mock()
            subject.parse_fallback(URL, native_parse=native, authorize=Mock(),
                                   timeout_seconds=30, deadline_monotonic=125)
            native.assert_called_once_with(125)

    def test_only_availability_errors_are_eligible(self):
        for error in (HTTP_ERROR(), XhsDownloadError("HTTP 503", http_status=503),
                      requests.Timeout(), requests.ConnectionError()):
            self.assertTrue(subject.eligible_failure(error))
        for error in (XhsSecurityError("blocked address"), XhsParseError("login required"),
                      requests.exceptions.SSLError("certificate rejected"),
                      XhsDownloadError("HTTP 404", http_status=404),
                      XhsDownloadError("HTTP 403", http_status=403), ValueError()):
            self.assertFalse(subject.eligible_failure(error))

    def test_no_authority_no_fallback_and_original_error_preserved(self):
        error = HTTP_ERROR()
        with patch.object(subject, "TianTianJieXiGenericProvider") as service:
            with self.assertRaises(XhsDownloadError) as raised:
                subject.parse_fallback(URL, native_parse=Mock(side_effect=error),
                                       authorize=lambda: ())
        self.assertIs(raised.exception, error)
        service.assert_not_called()

    def test_total_deadline_exhaustion_does_not_start_service(self):
        authorize = Mock(return_value=(PROVIDER,))
        with patch.object(subject.time, "monotonic", side_effect=[0, 31]), patch.object(
                subject, "TianTianJieXiGenericProvider") as service:
            with self.assertRaises(XhsDownloadError):
                subject.parse_fallback(URL, native_parse=Mock(side_effect=HTTP_ERROR()),
                                       authorize=authorize, timeout_seconds=30)
        authorize.assert_not_called()
        service.assert_not_called()

    def test_scope_does_not_assume_services_support_parts_or_other_content(self):
        self.assertEqual(subject.canonical_video_source(URL + "?trackid=example"), URL)
        self.assertEqual(subject.canonical_video_source(URL + "?p=1"), URL)
        for source in (URL + "?p=2", URL + "?p=1&p=2",
                       "https://b23.tv/example", "https://www.bilibili.com/opus/123",
                       "https://www.bilibili.com:invalid/video/BV1J4JX6PEMV/",
                       "https://example.com/video/BV1J4JX6PEMV/"):
            self.assertFalse(subject.canonical_video_source(source))


@override_settings(THIRD_PARTY_RATE_LIMIT_PER_MINUTE=10,
                   THIRD_PARTY_GLOBAL_REQUEST_LIMIT_PER_MINUTE=100)
class BilibiliFallbackAccessTests(SimpleTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.config = ProviderRuntimeSetting(third_party_allowed_public_platforms="",
                                            third_party_blocked_platforms="")
        self.request = SimpleNamespace(user=SimpleNamespace(
            is_active=False, is_authenticated=False, is_staff=False))
        self.stack.enter_context(patch("xhs_download.services.provider_access.load_runtime_setting",
                                       return_value=self.config))
        self.access = self.stack.enter_context(patch(
            "xhs_download.services.provider_access.provider_access_summary",
            return_value={"request_allowed": True}))
        self.stack.enter_context(patch.object(subject, "provider_configured", return_value=True))
        self.limit = self.stack.enter_context(patch(
            "xhs_download.services.rate_limits.consume_fixed_window",
            return_value=SimpleNamespace(allowed=True)))
        self.stack.enter_context(patch("xhs_download.services.rate_limits.get_client_identity",
                                       return_value="isolated-client"))

    def test_native_source_permission_does_not_grant_third_party_permission(self):
        self.assertEqual(subject.authorized_services(self.request, URL), ())
        self.config.third_party_allowed_public_platforms = "upstream:tiantianjiexi_generic"
        self.assertEqual(subject.authorized_services(self.request, URL), (PROVIDER,))
        self.config.third_party_blocked_platforms = "bilibili"
        self.assertEqual(subject.authorized_services(self.request, URL), ())

    def test_action_recheck_does_not_reserve_parse_limits_and_revocation_is_effective(self):
        self.config.third_party_allowed_public_platforms = "upstream:tiantianjiexi_generic"
        self.assertEqual(subject.authorized_services(self.request, URL, reserve_limits=False),
                         (PROVIDER,))
        self.limit.assert_not_called()
        self.access.return_value = {"request_allowed": False}
        self.assertEqual(subject.authorized_services(self.request, URL, reserve_limits=False), ())

    def test_scoped_public_rule_only_allows_bilibili_first_part_fallback(self):
        from .services.provider_access import upstream_match_is_public_allowed

        self.config.third_party_allowed_public_platforms = subject.PUBLIC_FALLBACK_RULE
        self.assertEqual(subject.authorized_services(self.request, URL), (PROVIDER,))
        for source in (URL + "?p=2", "https://b23.tv/example",
                       "https://www.douyin.com/video/123/",
                       "https://www.bilibili.com/opus/123"):
            with self.subTest(source=source):
                self.assertEqual(subject.authorized_services(self.request, source), ())
        # The fallback rule is not a generic Provider or host-wide grant.
        for provider in (PROVIDER, "kukutool_generic", "yt_dlp_generic"):
            for host in ("www.bilibili.com", "www.douyin.com"):
                self.assertFalse(upstream_match_is_public_allowed(
                    {"provider_id": provider, "extractor_id": provider}, host, self.config))
        self.config.third_party_allowed_public_platforms = ""
        self.assertEqual(subject.authorized_services(self.request, URL, reserve_limits=False), ())

    def test_scoped_rule_obeys_existing_gates_and_explicit_blocks(self):
        self.config.third_party_allowed_public_platforms = subject.PUBLIC_FALLBACK_RULE
        for blocked in ("bilibili", "upstream", f"upstream:{PROVIDER}",
                        subject.PUBLIC_FALLBACK_RULE):
            with self.subTest(blocked=blocked):
                self.config.third_party_blocked_platforms = blocked
                self.assertEqual(subject.authorized_services(self.request, URL), ())
        self.config.third_party_blocked_platforms = ""
        self.config.third_party_blocked_hosts = "bilibili.com"
        self.assertEqual(subject.authorized_services(self.request, URL), ())
        self.config.third_party_blocked_hosts = ""
        self.access.return_value = {"request_allowed": False}
        self.assertEqual(subject.authorized_services(self.request, URL), ())
        self.access.return_value = {"request_allowed": True}
        with patch.object(subject, "provider_configured", return_value=False):
            self.assertEqual(subject.authorized_services(self.request, URL), ())
        self.limit.return_value = SimpleNamespace(allowed=False)
        self.assertEqual(subject.authorized_services(self.request, URL), ())

    def test_staff_obeys_runtime_blocklist_and_limits(self):
        self.request.user = SimpleNamespace(is_active=True, is_authenticated=True, is_staff=True)
        self.assertEqual(subject.authorized_services(self.request, URL), (PROVIDER,))
        self.limit.return_value = SimpleNamespace(allowed=False)
        self.assertEqual(subject.authorized_services(self.request, URL), ())
        self.config.third_party_blocked_platforms = "upstream:tiantianjiexi_generic"
        self.assertEqual(subject.authorized_services(self.request, URL, reserve_limits=False), ())


@override_settings(THIRD_PARTY_PROVIDER_ENABLED=True, TIANTIANJIEXI_PROVIDER_ENABLED=True,
                   TIANTIANJIEXI_PHONE="isolated-fixture", TIANTIANJIEXI_PASSWORD="fixture",
                   TIANTIANJIEXI_API_BASE_URL="https://service.example.invalid/parse",
                   CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
class BilibiliFallbackPipelineTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.directory = self.stack.enter_context(TemporaryDirectory())
        self.stack.enter_context(patch("xhs_download.services.platforms.tiantian_serial.lock_root",
                                       return_value=Path(self.directory)))
        self.stack.enter_context(patch("socket.getaddrinfo",
                                       return_value=[(2, 1, 6, "", ("8.8.8.8", 443))]))
        self.stack.enter_context(patch("requests.sessions.Session.request",
                                       side_effect=AssertionError("unexpected HTTP")))
        response = Mock()
        response.json.return_value = {"code": 200, "data": {
            "desc": "A Bilibili fixture", "cover": "https://i0.hdslb.com/cover.jpg",
            "videos": [{"url": "https://upos-sz-mirrorcos.bilivideo.com/video.mp4",
                        "size": 12345, "quality": "720p"}],
        }}
        self.post = self.stack.enter_context(patch(
            "xhs_download.services.platforms.tiantianjiexi_generic.requests.post",
            return_value=response))

    def test_real_registry_and_provider_to_result_after_native_failure(self):
        with patch("xhs_download.services.platforms.bilibili.request_get_with_retry",
                   side_effect=HTTP_ERROR()) as native:
            result = parse_media_page(
                URL, bilibili_service_authorizer=lambda: (PROVIDER,))
        self.assertEqual(result.platform, "bilibili")
        self.assertEqual(result.metadata["selected_provider"], PROVIDER)
        self.assertEqual(result.metadata["provider_fallback_count"], 1)
        self.assertTrue(any(a.media_type == "video" for a in result.assets))
        self.assertTrue(result.metadata.get("service_format_picker") or
                        result.metadata.get("upstream_candidates"))
        self.post.assert_called_once()
        self.assertEqual(self.post.call_args.kwargs["data"]["link"], URL)
        self.assertEqual(native.call_args.kwargs["non_retry_statuses"], (412, 429))

    def test_parse_deadline_bounds_the_single_size_probe(self):
        from .services.quality import QualityProbeResult
        self.post.return_value.json.return_value = {"code": 200, "data": {
            "desc": "A Bilibili fixture", "images": ["http://i0.hdslb.com/cover.jpg"],
            "videos": [{"url": "http://upos-sz-mirrorcos.bilivideo.com/video.mp4", "streams": []}],
        }}
        with patch("xhs_download.services.platforms.bilibili.request_get_with_retry",
                   side_effect=HTTP_ERROR()), patch(
                       "xhs_download.services.platforms.bilibili_service_result.QualityProbe") as probe:
            probe.return_value.probe.return_value = QualityProbeResult(
                url="", reachable=True, size_bytes=4096)
            result = parse_media_page(URL, bilibili_service_authorizer=lambda: (PROVIDER,),
                                      timeout_seconds=1)
        asset = next(a for a in result.assets if a.media_type == "video")
        self.assertEqual((asset.size_bytes, asset.cover_url), (4096, "https://i0.hdslb.com/cover.jpg"))
        probe.return_value.probe.assert_called_once()
        self.assertLessEqual(probe.return_value.probe.call_args.kwargs["timeout"], 1)

    def test_service_failure_preserves_native_error_without_second_service(self):
        error = HTTP_ERROR()
        self.post.side_effect = requests.Timeout("fixture")
        with patch("xhs_download.services.platforms.bilibili.request_get_with_retry",
                   side_effect=error):
            with self.assertRaises(XhsDownloadError) as raised:
                parse_media_page(URL, bilibili_service_authorizer=lambda: (PROVIDER,))
        self.assertIs(raised.exception, error)
        self.post.assert_called_once()

    def test_no_authorizer_uses_original_native_only_path(self):
        with patch("xhs_download.services.platforms.bilibili.request_get_with_retry",
                   side_effect=HTTP_ERROR()):
            with self.assertRaises(XhsDownloadError):
                parse_media_page(URL)
        self.post.assert_not_called()

    def test_native_api_certificate_failure_does_not_authorize_service(self):
        authorize = Mock(return_value=(PROVIDER,))
        with patch("xhs_download.services.platforms.bilibili.request_get_with_retry",
                   side_effect=requests.exceptions.SSLError("certificate rejected")):
            with self.assertRaises(requests.exceptions.SSLError):
                parse_media_page(URL, bilibili_service_authorizer=authorize)
        authorize.assert_not_called()
        self.post.assert_not_called()

    def test_account_queue_consumes_overall_deadline_without_starting_http(self):
        @contextmanager
        def exhausted_queue(wait_seconds):
            self.assertEqual(wait_seconds, 30)
            yield 30000

        with patch.object(subject.time, "monotonic", return_value=100), \
             patch("xhs_download.services.platforms.tiantian_serial.clock", side_effect=[100, 131]), \
             patch("xhs_download.services.platforms.tiantian_serial.account_slot", exhausted_queue), \
             patch("xhs_download.services.platforms.bilibili.request_get_with_retry",
                   side_effect=HTTP_ERROR()):
            with self.assertRaises(XhsDownloadError):
                parse_media_page(URL, timeout_seconds=30,
                                 bilibili_service_authorizer=lambda: (PROVIDER,))
        self.post.assert_not_called()

    def test_real_action_and_download_guard_recheck_current_service_access(self):
        from .views import check_bilibili_service_download_access
        from .services.service_format_selection import selected_legacy_descriptor
        from .services.upstream_candidates import resolve_candidate_record
        with patch("xhs_download.services.platforms.bilibili.request_get_with_retry",
                   side_effect=HTTP_ERROR()):
            result = parse_media_page(URL, bilibili_service_authorizer=lambda: (PROVIDER,))
        row = result.metadata["upstream_candidates"][0]
        body = {key: row[key] for key in ("candidate_id", "resource_token")}
        record = resolve_candidate_record(body["resource_token"], body["candidate_id"])
        item = selected_legacy_descriptor(record)
        config = ProviderRuntimeSetting(third_party_allowed_public_platforms="upstream:" + PROVIDER)
        with patch("xhs_download.services.provider_access.load_runtime_setting", return_value=config), \
             patch("xhs_download.services.provider_access.provider_access_summary",
                   return_value={"request_allowed": True}) as access, \
             patch("xhs_download.views.get_download_runtime_setting",
                   return_value=SimpleNamespace(visitor_video_download_enabled=True,
                                                single_video_bytes_limit=0,
                                                single_archive_bytes_limit=0)), \
             patch("xhs_download.views.check_download_request_limits", return_value=("test", None)), \
             patch("xhs_download.services.upstream_delivery.validate_public_download_url",
                   side_effect=lambda url: url):
            response = self.client.post(reverse("service_selected_format"),
                                        {**body, "action": "legacy"}, content_type="application/json")
            self.assertEqual(response.status_code, 200, response.content)
            self.assertEqual(response.json()["item"]["url"], item["url"])
            request = SimpleNamespace(user=SimpleNamespace(is_authenticated=False, is_staff=False))
            self.assertIsNone(check_bilibili_service_download_access(request, [item]))
            access.return_value = {"request_allowed": False}
            response = self.client.post(reverse("service_selected_format"),
                                        {**body, "action": "legacy"}, content_type="application/json")
            self.assertEqual(response.status_code, 403)
            self.assertEqual(check_bilibili_service_download_access(request, [item])[2], 403)
            self.assertIsNone(check_bilibili_service_download_access(
                request, [{"type": "video", "asset_id": "bilibili:native"}]))
        self.post.assert_called_once()


@override_settings(
    THIRD_PARTY_PROVIDER_ENABLED=True, TIANTIANJIEXI_PROVIDER_ENABLED=True,
    TIANTIANJIEXI_PHONE="isolated-fixture", TIANTIANJIEXI_PASSWORD="fixture",
    TIANTIANJIEXI_API_BASE_URL="https://provider.contract.invalid/parse",
    TIANTIANJIEXI_RETRIES=2, TIANTIANJIEXI_HEALTH_QUERY_ACCOUNT_STATUS_ENABLED=False,
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
)
class BilibiliFallbackDiagnosticHTTPTests(TestCase):
    def test_public_failure_retains_only_safe_service_diagnostic_in_log(self):
        from contracts.network import offline_requests
        from contracts.test_backend_contracts import OfflineTransport, SERVICE, json_response

        ProviderRuntimeSetting.objects.create(
            pk=1, third_party_mode="public",
            third_party_allowed_public_platforms=subject.PUBLIC_FALLBACK_RULE,
        )
        for kind, code, expected in (("http", 502, "SERVICE_HTTP_503"),
                                     ("json", 502, "SERVICE_INVALID_JSON"),
                                     ("business", 502, "SERVICE_BUSINESS_CODE_502"),
                                     ("business", 1001, "SERVICE_BUSINESS_CODE_1001"),
                                     ("business", -99999, "SERVICE_BUSINESS_CODE_-99999")):
            with self.subTest(kind=kind), TemporaryDirectory() as directory:
                cache.clear()
                transport = OfflineTransport()
                service_calls = []

                def send(request, **kwargs):
                    if request.url != SERVICE:
                        return transport.send(request, **kwargs)
                    service_calls.append(request.method)
                    response = json_response(request, {
                        "code": code, "message": "private-response token=never-log-this",
                    }, status=503 if kind == "http" else 200)
                    if kind == "json":
                        response._content = b"<html>private-response token=never-log-this</html>"
                    return response

                with offline_requests(send), patch(
                    "xhs_download.services.platforms.tiantian_serial.lock_root",
                    return_value=Path(directory),
                ), self.assertLogs(subject.logger, level="INFO") as captured:
                    response = self.client.get(reverse("bilibili_results"), {"q": URL})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(service_calls, ["POST", "POST"])
                self.assertEqual(transport.events, ["native"])
                text = "\n".join(captured.output)
                self.assertIn("error_type=TianTianJieXiApiError", text)
                self.assertIn("diagnostic_detail=" + expected, text)
                self.assertNotIn("private-response", text)
                self.assertNotIn("never-log-this", text)
                self.assertNotIn(URL, text)
                self.assertNotIn(expected.encode(), response.content)
                self.assertNotIn(b"private-response", response.content)
        cache.clear()

    def test_unrecognized_diagnostic_and_exception_message_are_not_logged(self):
        from .services.platforms.tiantianjiexi_generic import TianTianJieXiApiError

        service_error = TianTianJieXiApiError(
            "secret raw response", diagnostic_detail="https://secret.invalid/?token=private")
        native_error = HTTP_ERROR()
        with patch.object(subject, "TianTianJieXiGenericProvider") as service, \
                self.assertLogs(subject.logger, level="INFO") as captured:
            service.return_value.parse.side_effect = service_error
            with self.assertRaises(XhsDownloadError) as raised:
                subject.parse_fallback(URL, native_parse=Mock(side_effect=native_error),
                                       authorize=lambda: (PROVIDER,))
        self.assertIs(raised.exception, native_error)
        text = "\n".join(captured.output)
        self.assertIn("diagnostic_detail=UNAVAILABLE", text)
        self.assertNotIn("secret", text)
        self.assertNotIn("token", text)
