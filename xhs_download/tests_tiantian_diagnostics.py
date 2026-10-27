from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
from unittest.mock import patch

import requests
from django.conf import settings
from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, override_settings

from contracts.network import offline_requests
from djangoProject.logging import RequestContextFormatter

from .models import (
    ProviderHealthTestSetting,
    ProviderHealthTestStatus,
    ProviderRuntimeSetting,
)
from .services.provider_health_tests import (
    ProviderHealthExecutionContext,
    execute_provider_health_test,
    provider_health_test_by_key,
)
from .services.provider_lab.contracts import ProviderLabError
from .services.provider_reliability import (
    ProviderFailureClass,
    ProviderOutcome,
    record_provider_attempt,
)


SAFE_DETAILS = ("SERVICE_BUSINESS_CODE_1", "SERVICE_EMPTY_RESULT")
UNSAFE_DETAILS = (
    "https://private.example/media?token=secret",
    "SERVICE_BUSINESS_CODE_1\nhttps://private.example/?token=secret",
    "SERVICE_EMPTY_RESULT\r\nforged_log_entry",
    "SERVICE_EMPTY_RESULT\n",
    "NOT_ALLOWLISTED",
    None,
    1,
    {"detail": "SERVICE_EMPTY_RESULT"},
)
SERVICE_URL = "https://tiantian.example/ajax/detail.php"
SOURCE_URL = "https://www.tiktok.com/@fixture/video/1234567890123456789"


class TianTianDiagnosticLoggingTests(SimpleTestCase):
    def render_attempt(self, **kwargs):
        with self.assertLogs("xhs_download.provider_reliability", level="INFO") as captured:
            fields = record_provider_attempt(
                provider="tiantianjiexi",
                outcome=ProviderOutcome.FAILURE_RESULT_INVALID,
                failure_class=ProviderFailureClass.RESULT_INVALID,
                **kwargs,
            )
        self.assertEqual(len(captured.records), 1)
        record = captured.records[0]
        formatter = RequestContextFormatter(
            settings.LOGGING["formatters"]["request_context"]["format"]
        )
        return fields, record.getMessage(), formatter.format(record)

    def test_absent_diagnostic_preserves_message_and_fields(self):
        fields, message, _rendered = self.render_attempt()
        self.assertEqual(message, "provider_attempt_completed")
        self.assertEqual(fields, {
            "event": "provider_attempt_completed",
            "provider": "tiantianjiexi",
            "outcome": "FAILURE_RESULT_INVALID",
            "external_call": False,
            "cache_hit": False,
            "cache_bypassed": False,
            "single_flight_wait": False,
            "continuation": False,
            "attempt_consumed": False,
            "elapsed_ms": 0,
            "payload_age_ms": 0,
            "retry_after_seconds": 0,
            "candidate_count": 0,
            "media_host": "unknown",
            "expected_bytes": 0,
            "failure_class": "RESULT_INVALID",
        })

    def test_safe_details_reach_existing_formatter_without_changing_fields(self):
        original_fields, _message, _rendered = self.render_attempt()
        for detail in SAFE_DETAILS:
            with self.subTest(detail=detail):
                fields, message, rendered = self.render_attempt(diagnostic_detail=detail)
                self.assertEqual(fields, original_fields)
                self.assertEqual(message, f"provider_attempt_completed diagnostic_detail={detail}")
                self.assertIn(f"message={message}", rendered)
                self.assertIn("outcome=FAILURE_RESULT_INVALID", rendered)
                self.assertIn("failure_class=RESULT_INVALID", rendered)

    def test_unsafe_details_are_not_logged_or_added_to_fields(self):
        original_fields, original_message, _rendered = self.render_attempt()
        for detail in UNSAFE_DETAILS:
            with self.subTest(detail=detail):
                fields, message, rendered = self.render_attempt(diagnostic_detail=detail)
                self.assertEqual(fields, original_fields)
                self.assertEqual(message, original_message)
                self.assertNotIn("diagnostic_detail=", rendered)
                self.assertNotIn("private.example", rendered)
                self.assertNotIn("secret", rendered)
                self.assertNotIn("forged_log_entry", rendered)
                self.assertNotIn("\n", rendered)
                self.assertNotIn("\r", rendered)


@override_settings(
    CACHES={"default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "tiantian-diagnostics-tests",
    }},
    THIRD_PARTY_PROVIDER_ENABLED=True,
    TIANTIANJIEXI_PROVIDER_ENABLED=True,
    TIANTIANJIEXI_PHONE="diagnostics-fixture",
    TIANTIANJIEXI_PASSWORD="diagnostics-fixture",
    TIANTIANJIEXI_API_BASE_URL=SERVICE_URL,
    TIANTIANJIEXI_RETRIES=1,
    TIANTIANJIEXI_REQUEST_LIMIT_PER_MINUTE=0,
    TIANTIANJIEXI_GLOBAL_MAX_CONCURRENCY=0,
    TIANTIANJIEXI_HEALTH_QUERY_ACCOUNT_STATUS_ENABLED=False,
)
class TianTianHealthDiagnosticTests(TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        cache.clear()
        self.addCleanup(cache.clear)
        root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch(
            "xhs_download.services.platforms.tiantian_serial.lock_root",
            return_value=root,
        ))
        self.requests = []
        self.reply = {"code": 1, "data": "", "msg": "private upstream reply"}
        self.transport_error = None
        self.stack.enter_context(offline_requests(self.send))
        self.context = ProviderHealthExecutionContext(
            request_id="tiantian-health-diagnostics",
            is_staff=True,
            test_setting=ProviderHealthTestSetting(third_party_url=SOURCE_URL),
            runtime_setting=ProviderRuntimeSetting.load(),
        )

    def send(self, request, **kwargs):
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.url, SERVICE_URL)
        self.requests.append(request)
        if self.transport_error is not None:
            raise self.transport_error
        response = requests.Response()
        response.status_code = 200
        response.headers["Content-Type"] = "application/json"
        response._content = json.dumps(self.reply).encode("utf-8")
        response.request = request
        response.url = request.url
        return response

    def execute(self):
        cache.clear()
        before = len(self.requests)
        run = execute_provider_health_test(
            self.context,
            provider_health_test_by_key("third_party_tiantianjiexi"),
        )
        self.assertEqual(len(self.requests), before + 1)
        run.refresh_from_db()
        self.assertEqual(run.status, ProviderHealthTestStatus.FAILED)
        return run

    def test_code_one_empty_data_records_business_detail_from_http_boundary(self):
        with self.assertLogs("xhs_download.provider_reliability", level="INFO") as captured:
            run = self.execute()
        self.assertEqual(run.error_message_short, "SERVICE_BUSINESS_CODE_1")
        self.assertEqual(run.error_type, "TianTianJieXiApiError")
        self.assertEqual(run.error_code, "TIAN_TIAN_JIE_XI_API_ERROR")
        self.assertNotIn("private upstream reply", run.error_message_short)
        self.assertTrue(any("diagnostic_detail=SERVICE_BUSINESS_CODE_1" in line
                            for line in captured.output))

    def test_empty_success_records_detail_without_changing_failure_class(self):
        self.reply = {"code": 200, "data": {"title": "No media"}}
        with self.assertLogs("xhs_download.provider_reliability", level="INFO") as captured:
            run = self.execute()
        self.assertEqual(run.error_message_short, "SERVICE_EMPTY_RESULT")
        self.assertEqual(run.error_type, "ProviderResultInvalidError")
        completed = [row for row in captured.records
                     if getattr(row, "event", "") == "provider_attempt_completed"]
        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0].outcome, "FAILURE_RESULT_INVALID")
        self.assertIn("diagnostic_detail=SERVICE_EMPTY_RESULT", completed[0].getMessage())

    def test_ordinary_exception_safe_detail_preserves_error_identity(self):
        for detail in SAFE_DETAILS:
            with self.subTest(detail=detail):
                self.transport_error = RuntimeError("original error message")
                self.transport_error.diagnostic_detail = detail
                run = self.execute()
                self.assertEqual(run.error_message_short, detail)
                self.assertEqual(run.error_type, "RuntimeError")
                self.assertEqual(run.error_code, "RUNTIME_ERROR")

    def test_ordinary_exception_rejected_detail_keeps_existing_message_behavior(self):
        message = "upstream failed https://private.example/?token=secret Cookie=private"
        self.transport_error = RuntimeError(message)
        baseline = self.execute()
        self.assertIn("upstream failed", baseline.error_message_short)
        self.assertIn("[url-redacted]", baseline.error_message_short)
        self.assertNotIn("private", baseline.error_message_short)
        self.assertNotIn("secret", baseline.error_message_short)
        for detail in UNSAFE_DETAILS:
            with self.subTest(detail=detail):
                self.transport_error = RuntimeError(message)
                self.transport_error.diagnostic_detail = detail
                run = self.execute()
                self.assertEqual(run.error_message_short, baseline.error_message_short)
                self.assertEqual(run.error_type, baseline.error_type)
                self.assertEqual(run.error_code, baseline.error_code)

    def test_provider_lab_error_safe_detail_preserves_error_identity(self):
        for detail in SAFE_DETAILS:
            with self.subTest(detail=detail):
                self.transport_error = ProviderLabError("PROVIDER_UPSTREAM_FAILED", detail=detail)
                run = self.execute()
                self.assertEqual(run.error_message_short, detail)
                self.assertEqual(run.error_type, "ProviderLabError")
                self.assertEqual(run.error_code, "PROVIDER_UPSTREAM_FAILED")

    def test_provider_lab_error_absent_or_unsafe_detail_stays_empty(self):
        for detail in ("", *UNSAFE_DETAILS):
            with self.subTest(detail=detail):
                self.transport_error = ProviderLabError("PROVIDER_UPSTREAM_FAILED")
                # Recheck at the recording boundary even if an error was mutated.
                self.transport_error.detail = detail
                run = self.execute()
                self.assertEqual(run.error_message_short, "")
                self.assertEqual(run.error_type, "ProviderLabError")
                self.assertEqual(run.error_code, "PROVIDER_UPSTREAM_FAILED")
