from __future__ import annotations

from io import StringIO
import json
from types import SimpleNamespace
import time
from unittest.mock import Mock, patch

from django.contrib.admin.sites import AdminSite
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core import mail
from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from .admin import ProviderHealthTestRunAdmin, ProviderValidationSuiteRunAdmin
from .checks import check_provider_validation_email_configuration
from .models import (
    ProviderHealthNotificationDeliveryStatus,
    ProviderHealthNotificationEvent,
    ProviderHealthTestRun,
    ProviderHealthTestSetting,
    ProviderHealthTestStatus,
    ProviderValidationNotificationStatus,
    ProviderValidationSuiteRun,
    ProviderValidationSuiteStatus,
    ProviderValidationSuiteTrigger,
)
from .services.provider_daily_validation import (
    DailyValidationConfigurationError,
    LOCK_KEY,
    NON_DELIVERY_EMAIL_BACKENDS,
    build_daily_validation_report,
    deliver_notifications,
    email_delivery_configuration,
    plan_notification_events,
    prune_validation_history,
    recover_stale_suites,
    run_daily_provider_validation,
)
from .services.provider_health_tests import (
    PROVIDER_HEALTH_TESTS,
    ProviderHealthExecutionContext,
    execute_provider_health_test,
    provider_health_test_by_key,
    provider_health_test_keys,
    provider_health_test_record,
    provider_result_fingerprint,
    validate_daily_test_keys,
    validate_daily_videodl_backend_id,
    validate_provider_health_sample_url,
)
from .services.common import XhsSecurityError
from .services.provider_lab.videodl_policy import (
    REVIEWED_COMMON_BACKEND_IDS,
    REVIEWED_DEDICATED_BACKEND_IDS,
)


ORIGINAL_HEALTH_TEST_KEYS = (
    "health_ready",
    "douyin_native_parse",
    "douyin_kukutool_ultra",
    "tiantianjiexi_account_status",
    "douyin_tiantianjiexi_ultra",
    "youtube_merge",
    "third_party_ytdlp",
    "third_party_tiantianjiexi",
    "gallery_dl",
)
NEW_HEALTH_TEST_KEYS = (
    "xhs_video_current",
    "xhs_gallery_current",
    "bilibili_native_parse",
    "videodl_policy_inventory",
    "videodl_specific_backend",
)
PUBLIC_DNS_RESULT = (
    (2, 1, 6, "", ("93.184.216.34", 443)),
)


def result(platform, media_type):
    return SimpleNamespace(
        platform=platform,
        assets=(SimpleNamespace(media_type=media_type),),
    )


def allowed_dedicated_backend():
    return next(
        backend_id
        for backend_id in REVIEWED_DEDICATED_BACKEND_IDS
        if validate_daily_videodl_backend_id(backend_id)
    )


class ProviderHealthRegistryV1Tests(TestCase):
    def setUp(self):
        self.setting = ProviderHealthTestSetting.load()

    def context(self):
        return ProviderHealthExecutionContext(
            request_id="daily-health-test",
            is_staff=True,
            test_setting=self.setting,
        )

    def execute(self, key):
        with patch(
            "xhs_download.services.common.socket.getaddrinfo",
            return_value=PUBLIC_DNS_RESULT,
        ):
            return execute_provider_health_test(
                self.context(),
                provider_health_test_by_key(key),
            )

    def test_registry_preserves_nine_checks_and_adds_five(self):
        self.assertEqual(len(PROVIDER_HEALTH_TESTS), 14)
        self.assertEqual(provider_health_test_keys()[:9], ORIGINAL_HEALTH_TEST_KEYS)
        self.assertEqual(provider_health_test_keys()[9:], NEW_HEALTH_TEST_KEYS)

    def test_daily_key_validation_is_bounded_ordered_and_fail_closed(self):
        self.assertEqual(
            validate_daily_test_keys(["xhs_gallery_current", "health_ready"]),
            ("health_ready", "xhs_gallery_current"),
        )
        for invalid in (["unknown"], ["health_ready", "health_ready"], "health_ready"):
            with self.assertRaises(ValueError):
                validate_daily_test_keys(invalid)

    @patch("xhs_download.services.provider_health_tests.parse_media_page")
    def test_xhs_video_requires_xhs_video_candidate(self, mock_parse):
        self.setting.xhs_video_url = "https://www.xiaohongshu.com/explore/safe-id"
        self.setting.save()
        mock_parse.return_value = result("xiaohongshu", "video")
        self.assertEqual(self.execute("xhs_video_current").status, ProviderHealthTestStatus.SUCCESS)
        mock_parse.return_value = result("xiaohongshu", "image")
        self.assertEqual(self.execute("xhs_video_current").status, ProviderHealthTestStatus.UNAVAILABLE)
        mock_parse.return_value = result("bilibili", "video")
        self.assertEqual(self.execute("xhs_video_current").error_code, "RESULT_PLATFORM_MISMATCH")

    @patch("xhs_download.services.provider_health_tests.parse_media_page")
    def test_xhs_gallery_requires_xhs_image_candidate(self, mock_parse):
        self.setting.xhs_gallery_url = "https://www.xiaohongshu.com/explore/safe-id"
        self.setting.save()
        mock_parse.return_value = result("xiaohongshu", "image")
        self.assertEqual(self.execute("xhs_gallery_current").status, ProviderHealthTestStatus.SUCCESS)
        mock_parse.return_value = SimpleNamespace(platform="xiaohongshu", assets=())
        self.assertEqual(self.execute("xhs_gallery_current").status, ProviderHealthTestStatus.UNAVAILABLE)
        mock_parse.return_value = result("bilibili", "image")
        self.assertEqual(self.execute("xhs_gallery_current").error_code, "RESULT_PLATFORM_MISMATCH")

    @patch("xhs_download.services.provider_health_tests.parse_media_page")
    def test_bilibili_native_requires_bilibili_video_without_lab_fanout(self, mock_parse):
        self.setting.bilibili_url = "https://www.bilibili.com/video/safe-id"
        self.setting.save()
        mock_parse.return_value = result("bilibili", "video")
        self.assertEqual(self.execute("bilibili_native_parse").status, ProviderHealthTestStatus.SUCCESS)
        mock_parse.return_value = result("bilibili", "image")
        self.assertEqual(self.execute("bilibili_native_parse").status, ProviderHealthTestStatus.UNAVAILABLE)
        self.assertEqual(mock_parse.call_count, 2)

    @patch("xhs_download.services.provider_health_tests.parse_media_page")
    def test_missing_sample_urls_skip_without_parse(self, mock_parse):
        for key in ("xhs_video_current", "xhs_gallery_current", "bilibili_native_parse"):
            run = self.execute(key)
            self.assertEqual(run.status, ProviderHealthTestStatus.SKIPPED)
            self.assertEqual(run.error_code, "TEST_URL_MISSING")
        mock_parse.assert_not_called()

    @patch(
        "xhs_download.services.provider_health_tests.parse_media_page",
        side_effect=RuntimeError(
            "failed https://media.example/a?token=secret Cookie=private /Users/name/private/file"
        ),
    )
    def test_health_error_and_sample_url_are_sanitized(self, _mock_parse):
        self.setting.xhs_video_url = "https://www.xiaohongshu.com/explore/id?token=secret"
        self.setting.save()
        run = self.execute("xhs_video_current")
        self.assertEqual(run.status, ProviderHealthTestStatus.FAILED)
        self.assertEqual(run.test_url_host, "www.xiaohongshu.com")
        self.assertNotIn("media.example", run.error_message_short)
        self.assertNotIn("token=secret", run.error_message_short)
        self.assertNotIn("private", run.error_message_short)
        self.assertNotIn("/Users", run.error_message_short)

    def test_videodl_backend_validator_allows_only_reviewed_dedicated(self):
        self.assertTrue(validate_daily_videodl_backend_id(""))
        self.assertTrue(validate_daily_videodl_backend_id(allowed_dedicated_backend()))
        self.assertFalse(validate_daily_videodl_backend_id(next(iter(REVIEWED_COMMON_BACKEND_IDS))))
        self.assertFalse(validate_daily_videodl_backend_id("UnknownBackend"))
        self.assertFalse(validate_daily_videodl_backend_id("bad-backend"))

    @patch("xhs_download.services.provider_health_tests.execute_controlled_videodl")
    @patch("xhs_download.services.provider_health_tests._videodl_runtime_ready", return_value=True)
    def test_videodl_specific_backend_uses_specific_dedicated_only(self, _ready, execute):
        self.setting.videodl_url = "https://example.com/public-sample"
        self.setting.videodl_backend_id = allowed_dedicated_backend()
        self.setting.save()
        execute.return_value = SimpleNamespace(resources=[{}], result={})
        run = self.execute("videodl_specific_backend")
        self.assertEqual(run.status, ProviderHealthTestStatus.SUCCESS)
        options = execute.call_args.kwargs["options"]
        self.assertEqual(options.mode, "specific_backend")
        self.assertEqual(options.backend_category, "dedicated")
        self.assertFalse(options.external_sharing_confirmed)
        self.assertEqual(options.backend_ids, (self.setting.videodl_backend_id,))

    @patch("xhs_download.services.provider_health_tests.execute_controlled_videodl")
    def test_videodl_specific_backend_skips_blank_and_reports_runtime_unavailable(self, execute):
        self.setting.videodl_url = "https://example.com/public-sample"
        self.setting.save()
        self.assertEqual(self.execute("videodl_specific_backend").status, ProviderHealthTestStatus.SKIPPED)
        self.setting.videodl_backend_id = allowed_dedicated_backend()
        self.setting.save()
        with patch("xhs_download.services.provider_health_tests._videodl_runtime_ready", return_value=False):
            run = self.execute("videodl_specific_backend")
        self.assertEqual(run.status, ProviderHealthTestStatus.UNAVAILABLE)
        self.assertEqual(run.error_code, "VIDEODL_RUNTIME_UNAVAILABLE")
        execute.assert_not_called()

    @patch("xhs_download.services.provider_health_tests.execute_controlled_videodl")
    @patch("xhs_download.services.provider_health_tests._videodl_runtime_ready", return_value=True)
    @patch("xhs_download.services.provider_health_tests.load_videodl_inventory")
    def test_videodl_inventory_checks_exact_policy_without_backend_parse(self, load_inventory, _ready, execute):
        load_inventory.return_value = tuple(
            SimpleNamespace(
                backend_id=backend_id,
                backend_category=category,
                browser_execution=False,
                controlled_available=True,
            )
            for category, ids in (
                ("dedicated", REVIEWED_DEDICATED_BACKEND_IDS),
                ("common", REVIEWED_COMMON_BACKEND_IDS),
            )
            for backend_id in ids
        )
        run = self.execute("videodl_policy_inventory")
        self.assertEqual(run.status, ProviderHealthTestStatus.SUCCESS)
        self.assertEqual(len(REVIEWED_DEDICATED_BACKEND_IDS), 70)
        self.assertEqual(len(REVIEWED_COMMON_BACKEND_IDS), 37)
        execute.assert_not_called()


class ProviderHealthSampleUrlPolicyTests(TestCase):
    def setUp(self):
        self.setting = ProviderHealthTestSetting.load()

    def context(self, request_id="sample-url-policy"):
        return ProviderHealthExecutionContext(
            request_id=request_id,
            is_staff=True,
            test_setting=self.setting,
        )

    def test_structural_policy_rejects_private_credentials_and_nonstandard_ports(self):
        rejected = (
            ("videodl_url", "https://localhost/sample"),
            ("videodl_url", "https://127.0.0.1/sample"),
            ("videodl_url", "https://[::1]/sample"),
            ("videodl_url", "https://10.1.2.3/sample"),
            ("videodl_url", "https://169.254.169.254/latest/meta-data"),
            ("videodl_url", "https://user:password@example.com/sample"),
            ("videodl_url", "https://example.com:8080/sample"),
        )
        for field_name, value in rejected:
            with self.subTest(value=value), self.assertRaises(XhsSecurityError):
                validate_provider_health_sample_url(value, field_name)

    def test_platform_field_binding_accepts_expected_hosts_and_rejects_cross_platform(self):
        accepted = (
            ("douyin_url", "https://www.douyin.com/video/1"),
            ("youtube_url", "https://youtu.be/example"),
            ("xhs_video_url", "https://www.xiaohongshu.com/explore/example"),
            ("xhs_gallery_url", "https://xhslink.com/example"),
            ("bilibili_url", "https://www.bilibili.com/video/example"),
            ("third_party_url", "https://www.youtube.com/watch?v=example"),
            ("gallerydl_url", "https://imgur.com/a/example"),
            ("videodl_url", "https://public.example/sample"),
        )
        for field_name, value in accepted:
            with self.subTest(field_name=field_name):
                self.assertEqual(validate_provider_health_sample_url(value, field_name), value)
        for field_name, value in (
            ("bilibili_url", "https://www.xiaohongshu.com/explore/example"),
            ("xhs_video_url", "https://www.bilibili.com/video/example"),
        ):
            with self.subTest(field_name=field_name), self.assertRaises(XhsSecurityError):
                validate_provider_health_sample_url(value, field_name)

    def test_execution_dns_policy_rejects_private_and_mixed_results(self):
        self.setting.xhs_video_url = "https://www.xiaohongshu.com/explore/example?token=hidden"
        self.setting.save()
        dns_results = (
            ((2, 1, 6, "", ("127.0.0.1", 443)),),
            ((2, 1, 6, "", ("10.0.0.8", 443)),),
            (
                (2, 1, 6, "", ("93.184.216.34", 443)),
                (2, 1, 6, "", ("192.168.1.8", 443)),
            ),
        )
        with patch("xhs_download.services.provider_health_tests.parse_media_page") as parse:
            for index, results in enumerate(dns_results):
                with self.subTest(results=results), patch(
                    "xhs_download.services.common.socket.getaddrinfo",
                    return_value=results,
                ):
                    run = execute_provider_health_test(
                        self.context(f"sample-url-policy-{index}"),
                        provider_health_test_by_key("xhs_video_current"),
                    )
                    self.assertEqual(run.status, ProviderHealthTestStatus.UNAVAILABLE)
                    self.assertEqual(run.error_code, "SAMPLE_URL_POLICY_REJECTED")
                    self.assertEqual(run.test_url_host, "www.xiaohongshu.com")
                    self.assertNotIn("token=hidden", run.error_message_short)
                    self.assertNotIn("127.0.0.1", run.error_message_short)
            parse.assert_not_called()

    @patch("xhs_download.services.provider_health_tests.parse_media_page")
    def test_public_dns_result_is_accepted(self, parse):
        self.setting.bilibili_url = "https://www.bilibili.com/video/example"
        self.setting.save()
        parse.return_value = result("bilibili", "video")
        with patch(
            "xhs_download.services.common.socket.getaddrinfo",
            return_value=PUBLIC_DNS_RESULT,
        ):
            run = execute_provider_health_test(
                self.context(),
                provider_health_test_by_key("bilibili_native_parse"),
            )
        self.assertEqual(run.status, ProviderHealthTestStatus.SUCCESS)
        parse.assert_called_once()

    @override_settings(
        THIRD_PARTY_PROVIDER_ENABLED=True,
        GALLERYDL_PROVIDER_ENABLED=True,
        VIDEODL_PROVIDER_LAB_ENABLED=True,
    )
    def test_policy_rejection_precedes_provider_and_worker_invocation(self):
        cases = (
            ("douyin_native_parse", "douyin_url", "https://www.douyin.com/video/1", "xhs_download.services.provider_health_tests.parse_media_page"),
            ("third_party_ytdlp", "third_party_url", "https://www.youtube.com/watch?v=1", "xhs_download.services.provider_health_tests.YtDlpGenericProvider.health"),
            ("gallery_dl", "gallerydl_url", "https://imgur.com/a/example", "xhs_download.services.provider_health_tests.GalleryDlGenericProvider.health"),
            ("videodl_specific_backend", "videodl_url", "https://public.example/sample", "xhs_download.services.provider_health_tests._videodl_runtime_ready"),
        )
        self.setting.videodl_backend_id = allowed_dedicated_backend()
        for index, (key, field_name, value, target) in enumerate(cases):
            setattr(self.setting, field_name, value)
            self.setting.save()
            with self.subTest(key=key), patch(
                "xhs_download.services.common.socket.getaddrinfo",
                return_value=((2, 1, 6, "", ("10.0.0.9", 443)),),
            ), patch(target) as external_call:
                run = execute_provider_health_test(
                    self.context(f"provider-policy-order-{index}"),
                    provider_health_test_by_key(key),
                )
                self.assertEqual(run.error_code, "SAMPLE_URL_POLICY_REJECTED")
                external_call.assert_not_called()


@override_settings(
    DJANGO_EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    DJANGO_DEFAULT_FROM_EMAIL="noreply@example.test",
    PROVIDER_VALIDATION_EMAIL_DELIVERY_ENABLED=True,
    PROVIDER_VALIDATION_EMAIL_RECIPIENTS=("maintainer@example.test",),
    REQUIRE_PRODUCTION_CONFIG=False,
)
class DailyValidationOrchestrationTests(TestCase):
    def setUp(self):
        cache.clear()
        self.setting = ProviderHealthTestSetting.load()
        self.setting.daily_enabled = True
        self.setting.daily_test_keys = ["health_ready"]
        self.setting.save()

    def executor(self, statuses, order=None):
        def execute(context, item):
            if order is not None:
                order.append(item["key"])
            status = statuses.get(item["key"], ProviderHealthTestStatus.SUCCESS)
            return provider_health_test_record(
                context,
                item,
                status=status,
                started_at=time.monotonic(),
                error_type="ProbeFailure" if status in {ProviderHealthTestStatus.FAILED, ProviderHealthTestStatus.UNAVAILABLE} else "",
                error_code="PROBE_FAILURE" if status in {ProviderHealthTestStatus.FAILED, ProviderHealthTestStatus.UNAVAILABLE} else "",
                test_url="https://sample.example/private?token=secret",
            )
        return execute

    def test_safe_defaults_and_daily_disabled_skip_external_calls(self):
        fresh = ProviderHealthTestSetting()
        self.assertFalse(fresh.daily_enabled)
        self.assertEqual(fresh.daily_test_keys, ["health_ready"])
        self.setting.daily_enabled = False
        self.setting.save()
        execute = Mock()
        outcome = run_daily_provider_validation(execute_test=execute)
        self.assertEqual(outcome.suite.status, ProviderValidationSuiteStatus.SKIPPED)
        self.assertEqual(outcome.suite.error_code, "DAILY_VALIDATION_DISABLED")
        execute.assert_not_called()

    @patch(
        "xhs_download.services.provider_health_tests.get_runtime_readiness",
        return_value={"ready": True, "components": {"database": True, "cache": True}},
    )
    @patch("xhs_download.services.provider_health_tests.execute_controlled_videodl")
    def test_unselected_invalid_videodl_backend_does_not_block_suite(self, worker, _readiness):
        self.setting.daily_test_keys = ["health_ready"]
        self.setting.videodl_backend_id = "UnknownBackend"
        self.setting.save()
        outcome = run_daily_provider_validation(no_email=True)
        self.assertEqual(outcome.suite.status, ProviderValidationSuiteStatus.SUCCESS)
        self.assertEqual(outcome.suite.success_count, 1)
        worker.assert_not_called()

    @patch("xhs_download.services.provider_health_tests.execute_controlled_videodl")
    def test_selected_invalid_videodl_backend_fails_closed_before_worker(self, worker):
        self.setting.daily_test_keys = ["videodl_specific_backend"]
        self.setting.videodl_backend_id = "UnknownBackend"
        self.setting.save()
        with self.assertRaises(DailyValidationConfigurationError) as captured:
            run_daily_provider_validation(no_email=True)
        self.assertEqual(str(captured.exception), "INVALID_VIDEODL_BACKEND")
        worker.assert_not_called()

    def test_daily_disabled_still_recovers_old_stale_suite(self):
        stale = ProviderValidationSuiteRun.objects.create(
            request_id="disabled-stale-suite",
            trigger=ProviderValidationSuiteTrigger.SCHEDULED,
            status=ProviderValidationSuiteStatus.RUNNING,
        )
        ProviderValidationSuiteRun.objects.filter(pk=stale.pk).update(
            started_at=timezone.now() - timezone.timedelta(minutes=20)
        )
        self.setting.daily_enabled = False
        self.setting.save()
        outcome = run_daily_provider_validation(execute_test=Mock())
        self.assertEqual(outcome.stale_recovered, 1)
        stale.refresh_from_db()
        self.assertEqual(stale.error_code, "STALE_SUITE_RECOVERED")

    def test_registry_order_selected_only_and_sequential_failure_isolation(self):
        self.setting.daily_test_keys = ["xhs_gallery_current", "health_ready", "xhs_video_current"]
        self.setting.save()
        order = []
        outcome = run_daily_provider_validation(
            execute_test=self.executor(
                {"xhs_video_current": ProviderHealthTestStatus.FAILED},
                order,
            ),
            no_email=True,
        )
        self.assertEqual(order, ["health_ready", "xhs_video_current", "xhs_gallery_current"])
        self.assertEqual(outcome.suite.total_count, 3)
        self.assertEqual(outcome.suite.success_count, 2)
        self.assertEqual(outcome.suite.failed_count, 1)
        self.assertEqual(outcome.suite.status, ProviderValidationSuiteStatus.DEGRADED)
        self.assertEqual({run.request_id for run in outcome.suite.test_runs.all()}, {outcome.suite.request_id})

    def test_success_unavailable_and_skipped_counts(self):
        self.setting.daily_test_keys = ["health_ready", "xhs_video_current", "xhs_gallery_current"]
        self.setting.save()
        outcome = run_daily_provider_validation(
            execute_test=self.executor(
                {
                    "xhs_video_current": ProviderHealthTestStatus.UNAVAILABLE,
                    "xhs_gallery_current": ProviderHealthTestStatus.SKIPPED,
                }
            ),
            no_email=True,
        )
        self.assertEqual(
            (outcome.suite.success_count, outcome.suite.failed_count, outcome.suite.skipped_count, outcome.suite.unavailable_count),
            (1, 0, 1, 1),
        )
        self.assertEqual(outcome.suite.status, ProviderValidationSuiteStatus.DEGRADED)

    def test_single_flight_prevents_execution(self):
        cache.set(LOCK_KEY, "other", timeout=60)
        execute = Mock()
        outcome = run_daily_provider_validation(execute_test=execute)
        self.assertEqual(outcome.suite.status, ProviderValidationSuiteStatus.SKIPPED)
        self.assertEqual(outcome.suite.error_code, "SINGLE_FLIGHT_ACTIVE")
        execute.assert_not_called()

    def test_lock_released_after_success_and_exception(self):
        run_daily_provider_validation(execute_test=self.executor({}), no_email=True)
        self.assertIsNone(cache.get(LOCK_KEY))
        with self.assertRaises(RuntimeError):
            run_daily_provider_validation(execute_test=Mock(side_effect=RuntimeError("boom")))
        self.assertIsNone(cache.get(LOCK_KEY))
        self.assertEqual(
            ProviderValidationSuiteRun.objects.filter(status=ProviderValidationSuiteStatus.FAILED).count(),
            1,
        )

    def test_stale_running_suite_is_recovered(self):
        stale = ProviderValidationSuiteRun.objects.create(
            request_id="stale-suite",
            trigger=ProviderValidationSuiteTrigger.SCHEDULED,
            status=ProviderValidationSuiteStatus.RUNNING,
            selected_test_keys=["health_ready"],
        )
        ProviderValidationSuiteRun.objects.filter(pk=stale.pk).update(
            started_at=timezone.now() - timezone.timedelta(minutes=20)
        )
        self.assertEqual(recover_stale_suites(timeout=300), 1)
        stale.refresh_from_db()
        self.assertEqual(stale.status, ProviderValidationSuiteStatus.FAILED)
        self.assertEqual(stale.error_code, "STALE_SUITE_RECOVERED")

    @override_settings(PROVIDER_DAILY_VALIDATION_TOTAL_TIMEOUT_SECONDS=30)
    def test_total_deadline_records_unexecuted_tests_as_skipped(self):
        self.setting.daily_test_keys = ["health_ready", "xhs_video_current"]
        self.setting.save()
        values = iter((0.0, 0.0, 31.0, 31.0))
        outcome = run_daily_provider_validation(
            execute_test=self.executor({}),
            no_email=True,
            clock=lambda: next(values),
        )
        self.assertEqual(outcome.suite.error_code, "SUITE_DEADLINE_EXCEEDED")
        self.assertEqual(outcome.suite.status, ProviderValidationSuiteStatus.DEGRADED)
        skipped = outcome.suite.test_runs.get(test_key="xhs_video_current")
        self.assertEqual(skipped.status, ProviderHealthTestStatus.SKIPPED)
        self.assertEqual(skipped.duration_ms, 0)
        self.assertEqual(skipped.error_code, "SUITE_DEADLINE_EXCEEDED")

    def test_retention_cascades_daily_runs_and_preserves_manual_rows(self):
        old = ProviderValidationSuiteRun.objects.create(
            request_id="old-suite",
            trigger=ProviderValidationSuiteTrigger.SCHEDULED,
            status=ProviderValidationSuiteStatus.SUCCESS,
        )
        ProviderValidationSuiteRun.objects.filter(pk=old.pk).update(
            created_at=timezone.now() - timezone.timedelta(days=120)
        )
        ProviderHealthTestRun.objects.create(
            request_id="old-suite",
            test_key="health_ready",
            status=ProviderHealthTestStatus.SUCCESS,
            suite_run=old,
        )
        manual = ProviderHealthTestRun.objects.create(
            request_id="manual",
            test_key="health_ready",
            status=ProviderHealthTestStatus.SUCCESS,
        )
        matched, deleted = prune_validation_history(self.setting)
        self.assertEqual((matched, deleted), (1, 1))
        self.assertFalse(ProviderHealthTestRun.objects.filter(request_id="old-suite").exists())
        self.assertTrue(ProviderHealthTestRun.objects.filter(pk=manual.pk).exists())

    def test_report_contains_selected_state_and_trends(self):
        run_daily_provider_validation(execute_test=self.executor({}), no_email=True)
        report = build_daily_validation_report(self.setting)
        self.assertEqual(report["selected_test_count"], 1)
        self.assertEqual(report["test_states"][0]["test_key"], "health_ready")
        self.assertEqual(report["trend_7"][ProviderValidationSuiteStatus.SUCCESS], 1)


@override_settings(
    DJANGO_EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    DJANGO_DEFAULT_FROM_EMAIL="noreply@example.test",
    PROVIDER_VALIDATION_EMAIL_DELIVERY_ENABLED=True,
    PROVIDER_VALIDATION_EMAIL_RECIPIENTS=("maintainer@example.test",),
    REQUIRE_PRODUCTION_CONFIG=False,
)
class DailyValidationNotificationTests(TestCase):
    def setUp(self):
        self.setting = ProviderHealthTestSetting.load()
        self.setting.email_notifications_enabled = True
        self.setting.failure_threshold = 2
        self.setting.notification_cooldown_hours = 24
        self.setting.notify_recovery = True
        self.setting.save()

    def make_run(self, *, key="health_ready", status=ProviderHealthTestStatus.FAILED, error_code="PROBE_FAILED"):
        suite = ProviderValidationSuiteRun.objects.create(
            request_id=f"suite-{ProviderValidationSuiteRun.objects.count() + 1}",
            trigger=ProviderValidationSuiteTrigger.SCHEDULED,
            status=ProviderValidationSuiteStatus.DEGRADED if status != ProviderHealthTestStatus.SUCCESS else ProviderValidationSuiteStatus.SUCCESS,
            total_count=1,
            failed_count=int(status == ProviderHealthTestStatus.FAILED),
            unavailable_count=int(status == ProviderHealthTestStatus.UNAVAILABLE),
            success_count=int(status == ProviderHealthTestStatus.SUCCESS),
        )
        run = ProviderHealthTestRun.objects.create(
            request_id=suite.request_id,
            suite_run=suite,
            test_key=key,
            status=status,
            provider="safe-provider",
            error_code="" if status == ProviderHealthTestStatus.SUCCESS else error_code,
            error_message_short="must not enter email https://private.example/?token=secret",
            result_fingerprint=provider_result_fingerprint(
                test_key=key,
                status=status,
                provider="safe-provider",
                error_code="" if status == ProviderHealthTestStatus.SUCCESS else error_code,
            ),
            test_url_host="sample.example",
        )
        return suite, run

    def mark_sent(self, run, event, *, notified_at=None):
        run.notification_event = event
        run.notification_delivery_status = ProviderHealthNotificationDeliveryStatus.SENT
        run.notified_at = notified_at or timezone.now()
        run.save(update_fields=("notification_event", "notification_delivery_status", "notified_at"))

    def test_threshold_dedup_cooldown_and_changed_fingerprint(self):
        first_suite, first = self.make_run()
        self.assertEqual(plan_notification_events(first_suite, [first], self.setting), ())
        second_suite, second = self.make_run()
        events = plan_notification_events(second_suite, [second], self.setting)
        self.assertEqual([event.event for event in events], [ProviderHealthNotificationEvent.FAILURE])
        self.assertEqual(deliver_notifications(second_suite, events, self.setting), ProviderValidationNotificationStatus.SENT)
        self.assertEqual(len(mail.outbox), 1)

        third_suite, third = self.make_run()
        self.assertEqual(plan_notification_events(third_suite, [third], self.setting), ())
        old = timezone.now() - timezone.timedelta(hours=25)
        ProviderHealthTestRun.objects.filter(pk=second.pk).update(notified_at=old)
        reminder_suite, reminder = self.make_run()
        reminder_events = plan_notification_events(reminder_suite, [reminder], self.setting)
        self.assertEqual(reminder_events[0].event, ProviderHealthNotificationEvent.REMINDER)

        self.mark_sent(reminder, ProviderHealthNotificationEvent.REMINDER)
        changed_suite, changed = self.make_run(error_code="DIFFERENT_FAILURE")
        changed_events = plan_notification_events(changed_suite, [changed], self.setting)
        self.assertEqual(changed_events[0].event, ProviderHealthNotificationEvent.FAILURE)

    def test_recovery_only_follows_successfully_sent_alert(self):
        _first_suite, _first = self.make_run()
        failure_suite, failure = self.make_run()
        failure_events = plan_notification_events(failure_suite, [failure], self.setting)
        deliver_notifications(failure_suite, failure_events, self.setting)
        recovery_suite, recovery = self.make_run(status=ProviderHealthTestStatus.SUCCESS)
        events = plan_notification_events(recovery_suite, [recovery], self.setting)
        self.assertEqual(events[0].event, ProviderHealthNotificationEvent.RECOVERY)
        self.assertEqual(deliver_notifications(recovery_suite, events, self.setting), ProviderValidationNotificationStatus.SENT)

        isolated_suite, isolated = self.make_run(key="xhs_video_current", status=ProviderHealthTestStatus.SUCCESS)
        self.assertEqual(plan_notification_events(isolated_suite, [isolated], self.setting), ())

    def test_skipped_is_neutral_for_failure_and_recovery(self):
        _first_suite, _first = self.make_run()
        second_suite, second = self.make_run()
        events = plan_notification_events(second_suite, [second], self.setting)
        deliver_notifications(second_suite, events, self.setting)
        skipped_suite, skipped = self.make_run(status=ProviderHealthTestStatus.SKIPPED)
        self.assertEqual(plan_notification_events(skipped_suite, [skipped], self.setting), ())
        recovery_suite, recovery = self.make_run(status=ProviderHealthTestStatus.SUCCESS)
        self.assertEqual(
            plan_notification_events(recovery_suite, [recovery], self.setting)[0].event,
            ProviderHealthNotificationEvent.RECOVERY,
        )

    def test_unavailable_is_unhealthy_and_multiple_events_use_one_email(self):
        self.make_run(key="health_ready", status=ProviderHealthTestStatus.UNAVAILABLE)
        self.make_run(key="xhs_video_current", status=ProviderHealthTestStatus.UNAVAILABLE)
        suite_a, run_a = self.make_run(key="health_ready", status=ProviderHealthTestStatus.UNAVAILABLE)
        _suite_b, run_b = self.make_run(key="xhs_video_current", status=ProviderHealthTestStatus.UNAVAILABLE)
        events = plan_notification_events(suite_a, [run_a, run_b], self.setting)
        self.assertEqual(len(events), 2)
        deliver_notifications(suite_a, events, self.setting)
        self.assertEqual(len(mail.outbox), 1)

    def test_email_gates_disable_delivery_without_losing_results(self):
        self.setting.email_notifications_enabled = False
        self.setting.save()
        _first_suite, _first = self.make_run()
        suite, run = self.make_run()
        events = plan_notification_events(suite, [run], self.setting)
        status = deliver_notifications(suite, events, self.setting)
        self.assertEqual(status, ProviderValidationNotificationStatus.DISABLED)
        run.refresh_from_db()
        self.assertEqual(run.notification_delivery_status, ProviderHealthNotificationDeliveryStatus.DISABLED)
        self.assertEqual(len(mail.outbox), 0)

    @override_settings(PROVIDER_VALIDATION_EMAIL_DELIVERY_ENABLED=False)
    def test_environment_gate_disables_delivery(self):
        self.make_run()
        suite, run = self.make_run()
        events = plan_notification_events(suite, [run], self.setting)
        self.assertEqual(
            deliver_notifications(suite, events, self.setting),
            ProviderValidationNotificationStatus.DISABLED,
        )
        self.assertEqual(len(mail.outbox), 0)

    @override_settings(PROVIDER_VALIDATION_EMAIL_RECIPIENTS=())
    def test_empty_recipient_list_disables_delivery(self):
        self.make_run()
        suite, run = self.make_run()
        events = plan_notification_events(suite, [run], self.setting)
        self.assertEqual(
            deliver_notifications(suite, events, self.setting),
            ProviderValidationNotificationStatus.DISABLED,
        )
        self.assertEqual(len(mail.outbox), 0)

    @override_settings(
        DJANGO_EMAIL_BACKEND="django.core.mail.backends.console.EmailBackend",
        REQUIRE_PRODUCTION_CONFIG=True,
    )
    def test_production_console_backend_disables_runtime_delivery(self):
        self.make_run()
        suite, run = self.make_run()
        events = plan_notification_events(suite, [run], self.setting)
        self.assertEqual(
            deliver_notifications(suite, events, self.setting),
            ProviderValidationNotificationStatus.DISABLED,
        )
        self.assertEqual(len(mail.outbox), 0)

    def test_production_non_delivery_backends_never_send_or_mark_sent(self):
        keys = (
            "health_ready",
            "xhs_video_current",
            "xhs_gallery_current",
            "bilibili_native_parse",
        )
        for backend, key in zip(sorted(NON_DELIVERY_EMAIL_BACKENDS), keys):
            self.make_run(key=key)
            suite, run = self.make_run(key=key)
            events = plan_notification_events(suite, [run], self.setting)
            self.assertEqual(len(events), 1)
            with self.subTest(backend=backend), self.settings(
                DJANGO_EMAIL_BACKEND=backend,
                REQUIRE_PRODUCTION_CONFIG=True,
            ), patch(
                "xhs_download.services.provider_daily_validation.EmailMessage.send"
            ) as send:
                self.assertFalse(email_delivery_configuration()["configured"])
                status = deliver_notifications(suite, events, self.setting)
                self.assertEqual(status, ProviderValidationNotificationStatus.DISABLED)
                send.assert_not_called()
            run.refresh_from_db()
            self.assertEqual(
                run.notification_delivery_status,
                ProviderHealthNotificationDeliveryStatus.DISABLED,
            )
            self.assertIsNone(run.notified_at)

    @patch("xhs_download.services.provider_daily_validation.EmailMessage.send", side_effect=RuntimeError("SMTP secret leak"))
    def test_mail_failure_is_safe_and_retryable(self, _send):
        self.make_run()
        suite, run = self.make_run()
        events = plan_notification_events(suite, [run], self.setting)
        self.assertEqual(deliver_notifications(suite, events, self.setting), ProviderValidationNotificationStatus.FAILED)
        run.refresh_from_db()
        self.assertEqual(run.notification_delivery_status, ProviderHealthNotificationDeliveryStatus.FAILED)
        retry_suite, retry = self.make_run()
        retry_events = plan_notification_events(retry_suite, [retry], self.setting)
        self.assertEqual(retry_events[0].event, ProviderHealthNotificationEvent.FAILURE)

    @patch("xhs_download.services.provider_daily_validation.EmailMessage.send", return_value=0)
    def test_unconfirmed_mail_delivery_is_failed(self, _send):
        self.make_run()
        suite, run = self.make_run()
        events = plan_notification_events(suite, [run], self.setting)
        self.assertEqual(
            deliver_notifications(suite, events, self.setting),
            ProviderValidationNotificationStatus.FAILED,
        )
        run.refresh_from_db()
        self.assertEqual(
            run.notification_delivery_status,
            ProviderHealthNotificationDeliveryStatus.FAILED,
        )

    @override_settings(PROVIDER_VALIDATION_ADMIN_REPORT_URL="https://admin.example.test/admin/provider-health-tests/")
    def test_email_body_is_bounded_and_contains_only_safe_fields(self):
        self.make_run()
        suite, run = self.make_run()
        events = plan_notification_events(suite, [run], self.setting)
        deliver_notifications(suite, events, self.setting)
        message = mail.outbox[0]
        self.assertLessEqual(len(message.subject), 180)
        self.assertLessEqual(len(message.body), 12000)
        self.assertIn("sample.example", message.body)
        self.assertIn("https://admin.example.test/admin/provider-health-tests/", message.body)
        for forbidden in ("private.example", "token=secret", "Cookie", "Authorization", "SMTP secret"):
            self.assertNotIn(forbidden, message.body)


class ProviderValidationSystemCheckTests(TestCase):
    def check_ids(self):
        return {issue.id for issue in check_provider_validation_email_configuration(None)}

    @override_settings(PROVIDER_VALIDATION_EMAIL_DELIVERY_ENABLED=False)
    def test_disabled_delivery_has_no_issues(self):
        self.assertEqual(self.check_ids(), set())

    @override_settings(
        PROVIDER_VALIDATION_EMAIL_DELIVERY_ENABLED=True,
        PROVIDER_VALIDATION_EMAIL_RECIPIENTS=(),
        DJANGO_DEFAULT_FROM_EMAIL="",
        DJANGO_EMAIL_BACKEND="django.core.mail.backends.smtp.EmailBackend",
        DJANGO_EMAIL_HOST="",
    )
    def test_missing_recipient_from_and_smtp_host_are_reported(self):
        self.assertTrue({"xhs_download.E013", "xhs_download.E014", "xhs_download.E016"} <= self.check_ids())

    @override_settings(
        PROVIDER_VALIDATION_EMAIL_DELIVERY_ENABLED=True,
        PROVIDER_VALIDATION_EMAIL_RECIPIENTS=("   ",),
        DJANGO_DEFAULT_FROM_EMAIL="sender@example.test",
        DJANGO_EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    )
    def test_blank_recipient_values_are_treated_as_unconfigured(self):
        self.assertIn("xhs_download.E013", self.check_ids())

    @override_settings(
        PROVIDER_VALIDATION_EMAIL_DELIVERY_ENABLED=True,
        PROVIDER_VALIDATION_EMAIL_RECIPIENTS=("secret-recipient@example.test",),
        DJANGO_DEFAULT_FROM_EMAIL="sender@example.test",
        DJANGO_EMAIL_BACKEND="django.core.mail.backends.smtp.EmailBackend",
        DJANGO_EMAIL_HOST="smtp.example.test",
        DJANGO_EMAIL_USE_TLS=True,
        DJANGO_EMAIL_USE_SSL=True,
    )
    def test_tls_ssl_conflict_is_safe(self):
        issues = check_provider_validation_email_configuration(None)
        self.assertIn("xhs_download.E015", {issue.id for issue in issues})
        rendered = " ".join(issue.msg for issue in issues)
        self.assertNotIn("secret-recipient", rendered)

    @override_settings(
        PROVIDER_VALIDATION_EMAIL_DELIVERY_ENABLED=True,
        PROVIDER_VALIDATION_EMAIL_RECIPIENTS=("maintainer@example.test",),
        DJANGO_DEFAULT_FROM_EMAIL="sender@example.test",
        DJANGO_EMAIL_BACKEND="django.core.mail.backends.console.EmailBackend",
        REQUIRE_PRODUCTION_CONFIG=True,
    )
    def test_production_console_backend_is_rejected(self):
        self.assertIn("xhs_download.E017", self.check_ids())

    def test_all_production_non_delivery_backends_are_rejected(self):
        for backend in NON_DELIVERY_EMAIL_BACKENDS:
            with self.subTest(backend=backend), self.settings(
                PROVIDER_VALIDATION_EMAIL_DELIVERY_ENABLED=True,
                PROVIDER_VALIDATION_EMAIL_RECIPIENTS=("maintainer@example.test",),
                DJANGO_DEFAULT_FROM_EMAIL="sender@example.test",
                DJANGO_EMAIL_BACKEND=backend,
                REQUIRE_PRODUCTION_CONFIG=True,
            ):
                self.assertIn("xhs_download.E017", self.check_ids())

    @override_settings(
        PROVIDER_VALIDATION_EMAIL_DELIVERY_ENABLED=True,
        PROVIDER_VALIDATION_EMAIL_RECIPIENTS=("secret-recipient@example.test",),
        DJANGO_DEFAULT_FROM_EMAIL="sender@example.test",
        DJANGO_EMAIL_BACKEND="invalid.private.Backend",
        DJANGO_EMAIL_HOST_PASSWORD="smtp-secret",
    )
    def test_unimportable_backend_is_rejected_without_sensitive_output(self):
        issues = check_provider_validation_email_configuration(None)
        self.assertIn("xhs_download.E019", {issue.id for issue in issues})
        self.assertFalse(email_delivery_configuration()["configured"])
        rendered = " ".join(issue.msg for issue in issues)
        self.assertNotIn("secret-recipient", rendered)
        self.assertNotIn("smtp-secret", rendered)

    @override_settings(
        PROVIDER_VALIDATION_EMAIL_DELIVERY_ENABLED=True,
        PROVIDER_VALIDATION_EMAIL_RECIPIENTS=("maintainer@example.test",),
        DJANGO_DEFAULT_FROM_EMAIL="sender@example.test",
        DJANGO_EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
        REQUIRE_PRODUCTION_CONFIG=False,
    )
    def test_nonproduction_locmem_backend_remains_allowed(self):
        self.assertEqual(self.check_ids(), set())
        self.assertTrue(email_delivery_configuration()["configured"])

    @override_settings(
        PROVIDER_VALIDATION_EMAIL_DELIVERY_ENABLED=True,
        PROVIDER_VALIDATION_EMAIL_RECIPIENTS=("maintainer@example.test",),
        DJANGO_DEFAULT_FROM_EMAIL="sender@example.test",
        DJANGO_EMAIL_BACKEND="django.core.mail.backends.smtp.EmailBackend",
        DJANGO_EMAIL_HOST="smtp.example.test",
        DJANGO_EMAIL_USE_TLS=True,
        DJANGO_EMAIL_USE_SSL=False,
        REQUIRE_PRODUCTION_CONFIG=True,
        PROVIDER_VALIDATION_ADMIN_REPORT_URL="https://admin.example.test/report",
    )
    def test_valid_smtp_configuration_passes(self):
        self.assertEqual(self.check_ids(), set())

    @override_settings(
        PROVIDER_VALIDATION_EMAIL_DELIVERY_ENABLED=True,
        PROVIDER_VALIDATION_EMAIL_RECIPIENTS=("maintainer@example.test",),
        DJANGO_DEFAULT_FROM_EMAIL="sender@example.test",
        DJANGO_EMAIL_BACKEND="django.core.mail.backends.smtp.EmailBackend",
        DJANGO_EMAIL_HOST="smtp.example.test",
        PROVIDER_VALIDATION_ADMIN_REPORT_URL="file:///private/report",
    )
    def test_unsafe_report_url_is_rejected_without_echo(self):
        issues = check_provider_validation_email_configuration(None)
        self.assertIn("xhs_download.E018", {issue.id for issue in issues})
        self.assertNotIn("/private/report", " ".join(issue.msg for issue in issues))

    @override_settings(
        PROVIDER_VALIDATION_EMAIL_DELIVERY_ENABLED=True,
        PROVIDER_VALIDATION_EMAIL_RECIPIENTS=("maintainer@example.test",),
        DJANGO_DEFAULT_FROM_EMAIL="sender@example.test",
        DJANGO_EMAIL_BACKEND="django.core.mail.backends.smtp.EmailBackend",
        DJANGO_EMAIL_HOST="smtp.example.test",
        PROVIDER_VALIDATION_ADMIN_REPORT_URL="https://admin.example.test/report?token=private",
    )
    def test_report_url_with_query_is_rejected_without_echo(self):
        issues = check_provider_validation_email_configuration(None)
        self.assertIn("xhs_download.E018", {issue.id for issue in issues})
        self.assertNotIn("token=private", " ".join(issue.msg for issue in issues))


@override_settings(
    DJANGO_EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    DJANGO_DEFAULT_FROM_EMAIL="noreply@example.test",
    PROVIDER_VALIDATION_EMAIL_DELIVERY_ENABLED=False,
)
class ProviderValidationCommandTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_dry_run_has_no_database_write_or_external_call(self):
        output = StringIO()
        with patch("xhs_download.services.provider_daily_validation.execute_provider_health_test") as execute:
            call_command("run_daily_provider_validation", "--dry-run", stdout=output)
        self.assertFalse(ProviderHealthTestSetting.objects.exists())
        self.assertFalse(ProviderValidationSuiteRun.objects.exists())
        execute.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())["status"], "dry_run")

    def test_disabled_command_safely_skips(self):
        output = StringIO()
        call_command("run_daily_provider_validation", stdout=output)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["status"], ProviderValidationSuiteStatus.SKIPPED)
        self.assertNotIn("http", output.getvalue())

    @patch("xhs_download.services.provider_health_tests.get_runtime_readiness", return_value={"ready": True, "components": {}})
    def test_force_known_key_executes_while_disabled(self, _readiness):
        output = StringIO()
        call_command(
            "run_daily_provider_validation",
            "--force",
            "--no-email",
            "--test-key",
            "health_ready",
            stdout=output,
        )
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["status"], ProviderValidationSuiteStatus.SUCCESS)
        self.assertEqual(payload["selected_test_keys"], ["health_ready"])

    def test_fail_on_degraded_uses_exit_code_four(self):
        setting = ProviderHealthTestSetting.load()
        setting.daily_enabled = True
        setting.daily_test_keys = ["videodl_policy_inventory"]
        setting.save()
        with self.assertRaises(CommandError) as raised:
            call_command("run_daily_provider_validation", "--fail-on-degraded")
        self.assertEqual(raised.exception.returncode, 4)

    def test_configuration_failure_uses_exit_code_two(self):
        with patch(
            "xhs_download.management.commands.run_daily_provider_validation.run_daily_provider_validation",
            side_effect=DailyValidationConfigurationError("INVALID_DAILY_TEST_KEYS"),
        ):
            with self.assertRaises(CommandError) as raised:
                call_command("run_daily_provider_validation", "--dry-run")
        self.assertEqual(raised.exception.returncode, 2)

    def test_mail_failure_uses_exit_code_three(self):
        suite = SimpleNamespace(
            notification_status=ProviderValidationNotificationStatus.FAILED,
            status=ProviderValidationSuiteStatus.DEGRADED,
        )
        outcome = SimpleNamespace(
            suite=suite,
            as_safe_dict=lambda: {
                "status": "degraded",
                "notification_status": "failed",
            },
        )
        with patch(
            "xhs_download.management.commands.run_daily_provider_validation.run_daily_provider_validation",
            return_value=outcome,
        ):
            with self.assertRaises(CommandError) as raised:
                call_command("run_daily_provider_validation")
        self.assertEqual(raised.exception.returncode, 3)

    def test_dry_run_does_not_prune_existing_history(self):
        old = ProviderValidationSuiteRun.objects.create(
            request_id="dry-run-retention",
            trigger=ProviderValidationSuiteTrigger.SCHEDULED,
            status=ProviderValidationSuiteStatus.SUCCESS,
        )
        ProviderValidationSuiteRun.objects.filter(pk=old.pk).update(
            created_at=timezone.now() - timezone.timedelta(days=365)
        )
        call_command("run_daily_provider_validation", "--dry-run", stdout=StringIO())
        self.assertTrue(ProviderValidationSuiteRun.objects.filter(pk=old.pk).exists())


class ProviderValidationAdminTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="daily-admin",
            password="password",
            is_staff=True,
        )
        self.client = Client()
        self.client.force_login(self.user)

    def grant(self, codename):
        permission = Permission.objects.get(
            content_type__app_label="xhs_download",
            codename=codename,
        )
        self.user.user_permissions.add(permission)
        for name in ("_perm_cache", "_user_perm_cache", "_group_perm_cache"):
            if hasattr(self.user, name):
                delattr(self.user, name)

    def test_permissions_remain_fail_closed(self):
        self.assertEqual(Client().get("/admin/provider-health-tests/").status_code, 404)
        self.assertEqual(self.client.get("/admin/provider-health-tests/").status_code, 404)
        self.grant("view_providerhealthtestrun")
        self.assertEqual(self.client.get("/admin/provider-health-tests/").status_code, 200)

    @override_settings(
        PROVIDER_VALIDATION_EMAIL_RECIPIENTS=("private-recipient@example.test",),
        DJANGO_EMAIL_HOST_PASSWORD="private-password",
    )
    def test_report_renders_daily_controls_warnings_and_no_email_secrets(self):
        self.grant("view_providerhealthtestrun")
        self.grant("change_providerhealthtestsetting")
        response = self.client.get("/admin/provider-health-tests/")
        self.assertContains(response, "每日测试项")
        self.assertContains(response, "可能消耗真实解析额度", count=4)
        self.assertContains(response, "仅限 noncommercial internal use")
        self.assertContains(response, "Asset 3 v1 不提供 Common backend")
        self.assertContains(response, "收件人数")
        self.assertContains(response, "投递配置完整")
        self.assertContains(response, "最近通知状态")
        self.assertNotContains(response, "private-recipient")
        self.assertNotContains(response, "private-password")

    def test_daily_configuration_saves_enabled_fields_and_urls(self):
        self.grant("view_providerhealthtestrun")
        self.grant("change_providerhealthtestsetting")
        response = self.client.post(
            "/admin/provider-health-tests/",
            data={
                "action": "save_test_urls",
                "daily_configuration_present": "1",
                "daily_enabled": "1",
                "daily_test_keys": ["health_ready", "xhs_video_current"],
                "email_notifications_enabled": "1",
                "notify_recovery": "1",
                "failure_threshold": "3",
                "notification_cooldown_hours": "48",
                "retention_days": "120",
                "xhs_video_url": "https://www.xiaohongshu.com/explore/safe-id",
                "videodl_backend_id": allowed_dedicated_backend(),
            },
        )
        self.assertEqual(response.status_code, 302)
        setting = ProviderHealthTestSetting.load()
        self.assertTrue(setting.daily_enabled)
        self.assertEqual(setting.daily_test_keys, ["health_ready", "xhs_video_current"])
        self.assertEqual(setting.failure_threshold, 3)
        self.assertEqual(setting.xhs_video_url, "https://www.xiaohongshu.com/explore/safe-id")

    def test_sample_url_save_rejects_unsafe_values_without_echoing_them(self):
        self.grant("view_providerhealthtestrun")
        self.grant("change_providerhealthtestsetting")
        secret_url = "https://private-user:private-password@www.douyin.com/video/1"
        response = self.client.post(
            "/admin/provider-health-tests/",
            data={
                "action": "save_test_urls",
                "douyin_url": secret_url,
                "youtube_url": "https://127.0.0.1/watch?v=1",
                "xhs_video_url": "https://www.bilibili.com/video/example",
                "bilibili_url": "https://www.xiaohongshu.com/explore/example",
                "videodl_url": "https://public.example:8080/sample",
                "third_party_url": "https://www.youtube.com/watch?v=valid",
            },
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        setting = ProviderHealthTestSetting.load()
        self.assertEqual(setting.douyin_url, "")
        self.assertEqual(setting.youtube_url, "")
        self.assertEqual(setting.xhs_video_url, "")
        self.assertEqual(setting.bilibili_url, "")
        self.assertEqual(setting.videodl_url, "")
        self.assertEqual(
            setting.third_party_url,
            "https://www.youtube.com/watch?v=valid",
        )
        self.assertNotContains(response, secret_url)
        self.assertNotContains(response, "private-password")
        self.assertContains(response, "未通过公网与平台准入校验")

    def test_invalid_key_and_backend_are_rejected(self):
        self.grant("view_providerhealthtestrun")
        self.grant("change_providerhealthtestsetting")
        for data in (
            {"daily_test_keys": ["unknown"], "videodl_backend_id": ""},
            {"daily_test_keys": ["health_ready"], "videodl_backend_id": "UnknownBackend"},
        ):
            response = self.client.post(
                "/admin/provider-health-tests/",
                data={
                    "action": "save_test_urls",
                    "daily_configuration_present": "1",
                    "failure_threshold": "2",
                    "notification_cooldown_hours": "24",
                    "retention_days": "90",
                    **data,
                },
            )
            self.assertEqual(response.status_code, 302)
            self.assertEqual(ProviderHealthTestSetting.load().daily_test_keys, ["health_ready"])

    def test_suite_history_and_per_test_state_show_host_not_full_url(self):
        self.grant("view_providerhealthtestrun")
        setting = ProviderHealthTestSetting.load()
        setting.daily_test_keys = ["health_ready"]
        setting.save()
        suite = ProviderValidationSuiteRun.objects.create(
            request_id="daily-report-safe-id",
            trigger=ProviderValidationSuiteTrigger.SCHEDULED,
            status=ProviderValidationSuiteStatus.DEGRADED,
            total_count=1,
            failed_count=1,
        )
        ProviderHealthTestRun.objects.create(
            request_id=suite.request_id,
            suite_run=suite,
            test_key="health_ready",
            status=ProviderHealthTestStatus.FAILED,
            error_code="PROBE_FAILED",
            test_url_host="sample.example",
        )
        response = self.client.get("/admin/provider-health-tests/")
        self.assertContains(response, "daily-report-safe-id")
        self.assertContains(response, "sample.example")
        self.assertNotContains(response, "token=secret")

    def test_suite_history_paginates_and_filters(self):
        self.grant("view_providerhealthtestrun")
        for index in range(26):
            ProviderValidationSuiteRun.objects.create(
                request_id=f"suite-page-{index}",
                trigger=ProviderValidationSuiteTrigger.COMMAND,
                status=(
                    ProviderValidationSuiteStatus.DEGRADED
                    if index == 25
                    else ProviderValidationSuiteStatus.SUCCESS
                ),
            )
        response = self.client.get(
            "/admin/provider-health-tests/?suite_status=degraded&suite_trigger=command"
        )
        self.assertEqual(response.context["suite_runs_page"].paginator.count, 1)
        response = self.client.get("/admin/provider-health-tests/?suite_page=2")
        self.assertEqual(response.context["suite_runs_page"].number, 2)

    @override_settings(
        DJANGO_EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
        DJANGO_DEFAULT_FROM_EMAIL="noreply@example.test",
        PROVIDER_VALIDATION_EMAIL_DELIVERY_ENABLED=True,
        PROVIDER_VALIDATION_EMAIL_RECIPIENTS=("maintainer@example.test",),
    )
    def test_manual_run_all_never_sends_daily_email(self):
        self.grant("view_providerhealthtestrun")
        self.grant("add_providerhealthtestrun")
        setting = ProviderHealthTestSetting.load()
        setting.email_notifications_enabled = True
        setting.save()
        response = self.client.post(
            "/admin/provider-health-tests/",
            data={"action": "run_all_tests"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(ProviderHealthTestRun.objects.count(), 14)
        self.assertEqual(len(mail.outbox), 0)

    def test_read_only_log_admins_cannot_forge_rows(self):
        request = SimpleNamespace(user=self.user)
        for admin_class, model in (
            (ProviderHealthTestRunAdmin, ProviderHealthTestRun),
            (ProviderValidationSuiteRunAdmin, ProviderValidationSuiteRun),
        ):
            model_admin = admin_class(model, AdminSite())
            self.assertFalse(model_admin.has_add_permission(request))
            self.assertFalse(model_admin.has_change_permission(request))
            self.assertFalse(model_admin.has_delete_permission(request))
        self.user.is_superuser = True
        self.assertTrue(ProviderValidationSuiteRunAdmin(ProviderValidationSuiteRun, AdminSite()).has_delete_permission(request))


class ProviderDailyValidationMigrationTests(TransactionTestCase):
    reset_sequences = True

    migrate_from = ("xhs_download", "0024_userfeedback")
    migrate_to = ("xhs_download", "0025_provider_daily_validation")

    def test_existing_reverse_and_reapply_migration(self):
        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_from])
        old_apps = executor.loader.project_state([self.migrate_from]).apps
        old_setting = old_apps.get_model("xhs_download", "ProviderHealthTestSetting")
        old_setting.objects.create(singleton_key=1, douyin_url="https://example.test/safe")

        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_to])
        new_apps = executor.loader.project_state([self.migrate_to]).apps
        new_setting = new_apps.get_model("xhs_download", "ProviderHealthTestSetting").objects.get(singleton_key=1)
        self.assertEqual(new_setting.daily_test_keys, ["health_ready"])
        self.assertFalse(new_setting.daily_enabled)
        self.assertTrue(new_apps.get_model("xhs_download", "ProviderValidationSuiteRun"))

        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_from])
        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_to])
        executor = MigrationExecutor(connection)
        self.assertIn(self.migrate_to, executor.loader.applied_migrations)
