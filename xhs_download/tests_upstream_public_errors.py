from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import RequestFactory, SimpleTestCase

from . import views
from .services.common import XhsParseError
from .services.batch_parse.results import build_batch_result_row
from .services.upstream_public_errors import UPSTREAM_PUBLIC_MESSAGES, public_upstream_error_message


class UpstreamPublicErrorTests(SimpleTestCase):
    def test_known_codes_and_exact_legacy_messages_are_safe(self):
        for code, message in UPSTREAM_PUBLIC_MESSAGES.items():
            with self.subTest(code=code):
                error = XhsParseError("secret https://private.example/?token=secret")
                error.error_code = code
                self.assertEqual(public_upstream_error_message(error, is_upstream=True, fallback="generic"), "扩展解析失败：" + message)
                legacy = XhsParseError("扩展解析失败：" + message)
                self.assertEqual(public_upstream_error_message(legacy, is_upstream=True, fallback="generic"), "扩展解析失败：" + message)

    def test_prefix_or_unknown_code_cannot_expose_arbitrary_text(self):
        error = XhsParseError("扩展解析失败：https://private.example/?token=secret")
        error.error_code = "UPSTREAM_UNTRUSTED"
        self.assertEqual(public_upstream_error_message(error, is_upstream=True, fallback="generic"), "扩展解析失败：当前上游不可用，请稍后重试。")

    def test_mature_error_redaction_is_unchanged(self):
        error = XhsParseError("扩展解析失败：" + UPSTREAM_PUBLIC_MESSAGES["UPSTREAM_UNSUPPORTED_RESULT"])
        error.error_code = "UPSTREAM_UNSUPPORTED_RESULT"
        self.assertEqual(public_upstream_error_message(error, is_upstream=False, fallback="generic"), "generic")

    def test_homepage_exposes_known_reason_without_raw_backend_text(self):
        request = RequestFactory().get("/results/?q=https://example.org/post/1")
        request.user = SimpleNamespace(is_authenticated=False, is_active=False, is_staff=False)
        gate = {"allowed": True, "platform_id": "upstream", "upstream_provider_ids": (), "upstream_matches": ()}
        error = XhsParseError("private backend diagnostic")
        error.error_code = "UPSTREAM_UNSUPPORTED_RESULT"
        overrides = {
            "get_adapter_for_url": Mock(return_value=SimpleNamespace(platform_id="third_party")),
            "get_download_settings_context": Mock(return_value={"show_internal_diagnostics": False}),
            "third_party_provider_is_available": Mock(return_value=True),
            "third_party_public_access_summary": Mock(return_value=gate),
            "consume_fixed_window": Mock(return_value=SimpleNamespace(allowed=True)),
            "parse_media_page": Mock(side_effect=error),
            "log_parse_request_event": Mock(),
        }
        with ExitStack() as stack:
            for name, replacement in overrides.items():
                stack.enter_context(patch.object(views, name, replacement))
            context, _ = views.build_search_results_context(request)
        self.assertIn("分离音视频或流媒体", context["error_message"])
        self.assertNotIn("private", context["error_message"])

    def test_batch_uses_same_safe_reason(self):
        error = XhsParseError("private backend diagnostic")
        error.error_code = "UPSTREAM_ACCESS_DENIED"
        gate = {"allowed": True, "platform_id": "upstream", "upstream_provider_ids": (), "upstream_matches": ()}
        with patch("xhs_download.services.batch_parse.results.third_party_public_access_summary", return_value=gate):
            row = build_batch_result_row(
                RequestFactory().get("/"), "https://example.org/post/1",
                prefer_ultra_hd=False, prefer_youtube_merge=False, per_link_timeout_seconds=30,
                query_uses_third_party_provider_func=lambda _: True,
                third_party_provider_is_available_func=lambda _: True,
                parser=Mock(side_effect=error),
            )
        self.assertIn("上游拒绝访问", row["public_error_message"])
        self.assertNotIn("private", row["public_error_message"])
