"""P12: real public controller/old parser, only the final HTTP boundary replaced."""
from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
import threading
import time
from unittest.mock import patch

from django.conf import settings
from django.core.cache import cache
from django.test import TransactionTestCase, override_settings
from django.urls import reverse

from contracts.network import offline_requests
from contracts import test_inv01_douyin as douyin_contract
from contracts.test_backend_contracts import OfflineTransport, SOURCE, SERVICE, ADMISSION, LOCAL_CACHE
from xhs_download.models import (ProviderHealthTestSetting, ProviderRuntimeSetting,
                                ProviderAccessMode, PipelineShadowDailyCount)
from xhs_download.services.pipeline_shadow import wait_for_shadow
from xhs_download.services.provider_health_tests import (
    ProviderHealthExecutionContext, execute_provider_health_test, provider_health_test_by_key,
)
from xhs_download.services.provider_daily_validation import run_daily_provider_validation


@override_settings(
    ALLOWED_HOSTS=["testserver", "localhost", "127.0.0.1"], CACHES=LOCAL_CACHE,
    THIRD_PARTY_PROVIDER_ENABLED=True, TIANTIANJIEXI_PROVIDER_ENABLED=True,
    TIANTIANJIEXI_PHONE="shadow-contract", TIANTIANJIEXI_PASSWORD="shadow-contract",
    TIANTIANJIEXI_API_BASE_URL=SERVICE, TIANTIANJIEXI_HEALTH_QUERY_ACCOUNT_STATUS_ENABLED=False,
    TIANTIANJIEXI_RETRIES=1, TIANTIANJIEXI_REQUEST_LIMIT_PER_MINUTE=0,
    TIANTIANJIEXI_GLOBAL_MAX_CONCURRENCY=0, THIRD_PARTY_RATE_LIMIT_PER_MINUTE=1000,
    THIRD_PARTY_GLOBAL_REQUEST_LIMIT_PER_MINUTE=10000,
)
class PipelineShadowHTTPContracts(TransactionTestCase):
    def setUp(self):
        self.assertFalse((Path(settings.BASE_DIR) / ".env").exists())
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.addCleanup(wait_for_shadow)
        cache.clear()
        self.addCleanup(cache.clear)
        self.runtime = ProviderRuntimeSetting.objects.create(pk=1, third_party_mode=ProviderAccessMode.PUBLIC,
            third_party_allowed_public_platforms=ADMISSION, third_party_blocked_platforms="")
        self.setting = ProviderHealthTestSetting.objects.create(pk=1, bilibili_url=SOURCE)
        self.transport = OfflineTransport()
        self.transport.native_outcome = "success"
        self.requests = []
        def send(request, **kwargs):
            self.requests.append((request.method, request.url))
            return self.transport.send(request, **kwargs)
        self.stack.enter_context(offline_requests(send))
        directory = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch("xhs_download.services.platforms.tiantian_serial.lock_root",
                                      return_value=directory))

    def parse(self, enabled):
        self.setting.public_shadow_enabled = enabled
        self.setting.save(update_fields=["public_shadow_enabled"])
        cache.clear()
        self.requests.clear()
        response = self.client.get(reverse("bilibili_results"), {"q": SOURCE},
                                   HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertFalse(wait_for_shadow())
        return response, list(self.requests)

    def test_switch_changes_neither_public_response_nor_upstream_requests(self):
        with patch("time.time", return_value=1793050000):
            off, off_requests = self.parse(False)
            self.assertEqual(PipelineShadowDailyCount.objects.count(), 0)
            on, on_requests = self.parse(True)
        self.assertEqual(on.status_code, 200)
        self.assertEqual(on.status_code, off.status_code)
        self.assertEqual(on.content, off.content)
        self.assertEqual(on_requests, off_requests)
        codes = set(PipelineShadowDailyCount.objects.values_list("difference_code", flat=True))
        self.assertTrue(codes)
        self.assertNotIn("SHADOW_NO_RAW", codes)
        self.assertNotIn("SHADOW_ERROR", codes)

    def test_shadow_failure_is_fail_open_and_counted_once(self):
        with patch("xhs_download.services.pipeline_shadow_compare.compare_capture", side_effect=RuntimeError("shadow only")):
            response, requests = self.parse(True)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Contract native video")
        self.assertTrue(requests)
        self.assertEqual(list(PipelineShadowDailyCount.objects.values_list("difference_code", "count")),
                         [("SHADOW_ERROR", 1)])

    def test_public_switch_uses_existing_singleton_not_row_primary_key(self):
        self.setting.delete()
        self.setting = ProviderHealthTestSetting.objects.create(pk=7, bilibili_url=SOURCE)
        response, _ = self.parse(True)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(PipelineShadowDailyCount.objects.exists())

    def test_public_storage_has_only_safe_counter_dimensions(self):
        self.parse(True)
        rows = list(PipelineShadowDailyCount.objects.values())
        self.assertTrue(rows)
        self.assertEqual(set(rows[0]), {"id", "date", "platform", "provider", "difference_code", "count"})
        serialized = json.dumps(rows, default=str)
        for secret in (SOURCE, "BV1J4JX6PEMV", "Contract native video", "198.51.100", "http"):
            self.assertNotIn(secret, serialized)

    def test_health_details_include_differences_coverage_and_invariants(self):
        context = ProviderHealthExecutionContext(request_id="shadow-health", test_setting=self.setting,
                                                  runtime_setting=self.runtime)
        run = execute_provider_health_test(context, provider_health_test_by_key("bilibili_native_parse"))
        self.assertEqual(run.status, "success")
        self.assertFalse(wait_for_shadow())
        run.refresh_from_db()
        observations = run.shadow_details["observations"]
        self.assertTrue(observations)
        self.assertTrue(any(row["invariants"]["checked"] for row in observations), observations)
        for row in observations:
            self.assertIn("difference_codes", row)
            self.assertIn("field_coverage", row)
            self.assertIn("invariants", row)
        self.assertEqual(PipelineShadowDailyCount.objects.count(), 0)

    def test_empty_homepage_does_not_count_as_parse(self):
        self.setting.public_shadow_enabled = True
        self.setting.save(update_fields=["public_shadow_enabled"])
        response = self.client.get(reverse("bilibili_results"))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(wait_for_shadow())
        self.assertEqual(PipelineShadowDailyCount.objects.count(), 0)
        self.assertEqual(self.requests, [])

    def test_health_result_does_not_wait_for_shadow_comparison(self):
        released, entered = threading.Event(), threading.Event()
        def compare(*args, **kwargs):
            entered.set()
            released.wait(1)
            return {"observations": []}
        context = ProviderHealthExecutionContext(request_id="shadow-health-async", test_setting=self.setting,
                                                  runtime_setting=self.runtime)
        with patch("xhs_download.services.pipeline_shadow_compare.compare_capture", side_effect=compare):
            try:
                started = time.monotonic()
                run = execute_provider_health_test(context, provider_health_test_by_key("bilibili_native_parse"))
                self.assertLess(time.monotonic() - started, 0.75)
                self.assertEqual(run.status, "success")
                self.assertTrue(entered.wait(1))
                self.assertFalse(released.is_set())
            finally:
                released.set()
                self.assertFalse(wait_for_shadow())

    def test_daily_keeps_shadow_details_on_existing_health_run(self):
        outcome = run_daily_provider_validation(force=True, no_email=True,
                                               test_keys_override=["bilibili_native_parse"])
        self.assertFalse(wait_for_shadow())
        run = outcome.suite.test_runs.get()
        self.assertEqual(run.status, "success")
        self.assertTrue(run.shadow_details["observations"])
        self.assertTrue(run.shadow_details["observations"][0]["invariants"]["checked"])
        self.assertEqual(PipelineShadowDailyCount.objects.count(), 0)

    def test_cached_result_without_raw_is_counted_without_refetch(self):
        self.transport.native_outcome = 412
        self.parse(False)
        self.requests.clear()
        off = self.client.get(reverse("bilibili_results"), {"q": SOURCE},
                              HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        cached_requests = list(self.requests)
        self.setting.public_shadow_enabled = True
        self.setting.save(update_fields=["public_shadow_enabled"])
        self.requests.clear()
        response = self.client.get(reverse("bilibili_results"), {"q": SOURCE},
                                   HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(wait_for_shadow())
        self.assertEqual(response.status_code, off.status_code)
        self.assertEqual(self.requests, cached_requests)
        self.assertFalse(any(url == SERVICE for _, url in self.requests))
        self.assertTrue(PipelineShadowDailyCount.objects.filter(
            provider="tiantianjiexi_generic", difference_code="SHADOW_NO_RAW", count=1).exists())

    def test_public_response_does_not_wait_for_comparison(self):
        self.setting.public_shadow_enabled = True
        self.setting.save(update_fields=["public_shadow_enabled"])
        released, entered = threading.Event(), threading.Event()
        def compare(*args, **kwargs):
            entered.set()
            self.assertTrue(released.wait(5), "public response waited for shadow")
            return {"observations": []}
        with patch("xhs_download.services.pipeline_shadow_compare.compare_capture", side_effect=compare):
            try:
                response = self.client.get(reverse("bilibili_results"), {"q": SOURCE},
                                           HTTP_X_REQUESTED_WITH="XMLHttpRequest")
                self.assertEqual(response.status_code, 200)
                self.assertTrue(entered.wait(1))
                self.assertFalse(released.is_set())
            finally:
                released.set()
                self.assertFalse(wait_for_shadow())


@override_settings(**douyin_contract.DouyinUltraHTTPContracts._overridden_settings)
class PipelineShadowConcurrentHTTPContracts(TransactionTestCase):
    def setUp(self):
        douyin_contract.DouyinUltraHTTPContracts.setUp(self)
        self.setting = ProviderHealthTestSetting.objects.create(pk=1, public_shadow_enabled=True)
        self.addCleanup(wait_for_shadow)

    @override_settings(DOUYIN_ULTRA_PROVIDER_ORDER="tiantianjiexi,native")
    def test_native_and_late_ultra_are_captured_without_extending_budget(self):
        self.transport.ultra_waits = True
        try:
            body, elapsed = douyin_contract.DouyinUltraHTTPContracts.parse(self)
            self.assertLess(elapsed, douyin_contract.BUDGET + 0.4)
            self.assertLess(self.transport.asked_at("native"), 0.3)
            self.assertIn(douyin_contract.NATIVE_TITLE, body["html"])
            self.assertEqual(body["status"], "continuation_pending")
        finally:
            self.transport.release_ultra.set()
        settled = douyin_contract.DouyinUltraHTTPContracts.poll_until_settled(self, body["continuation_token"])
        self.assertIn(douyin_contract.ULTRA_TITLE, settled["html"])
        self.assertFalse(wait_for_shadow())
        rows = list(PipelineShadowDailyCount.objects.values_list("provider", "difference_code"))
        self.assertTrue(any(p == "douyin_native" for p, _ in rows), rows)
        self.assertTrue(any(p == "tiantianjiexi_generic" for p, _ in rows), rows)
        self.assertFalse(any(code in {"SHADOW_ERROR", "SHADOW_NO_RAW"} for _, code in rows), rows)
        self.assertEqual(self.transport.services(), ["tiantianjiexi"])
