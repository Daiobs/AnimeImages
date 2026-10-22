from copy import deepcopy
from unittest.mock import patch

from django.core import mail
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.template.loader import render_to_string
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from contracts.network import offline_requests

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
    NotificationEvent,
    _notification_body,
    build_daily_validation_report,
    deliver_notifications,
    download_smoke_report_lines,
    plan_notification_events,
)
from .services.provider_health_tests import provider_result_fingerprint


def smoke_details(platform="bilibili", **observation):
    return {
        "schema_version": 1,
        "platform": platform,
        "provider": "native",
        "fields": {"title": True, "cover": True, "quality": False, "size": False},
        "download": {
            "status": "success",
            "error_code": "",
            "http_status": 206,
            "final_host": "media.example.test",
            "final_port": 443,
            "redirect_count": 0,
            "referer_lost": False,
            "content_type": "video/mp4",
            "range_supported": True,
            "bytes_received": 1024,
            "request_count": 1,
            **observation,
        },
    }


@override_settings(
    DJANGO_EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    DJANGO_DEFAULT_FROM_EMAIL="noreply@example.test",
    PROVIDER_VALIDATION_EMAIL_DELIVERY_ENABLED=True,
    PROVIDER_VALIDATION_EMAIL_RECIPIENTS=("maintainer@example.test",),
    PROVIDER_VALIDATION_ADMIN_REPORT_URL="",
    REQUIRE_PRODUCTION_CONFIG=False,
)
class DownloadSmokeReportingTests(TestCase):
    def setUp(self):
        self.enterContext(offline_requests(self.unexpected_request))
        self.setting = ProviderHealthTestSetting.load()
        self.setting.email_notifications_enabled = True
        self.setting.failure_threshold = 2
        self.setting.notification_cooldown_hours = 24
        self.setting.notify_recovery = True
        self.setting.save()

    def unexpected_request(self, *args, **kwargs):
        self.fail("Reporting must not make an upstream request")

    def make_run(self, details=None, *, key=None, status=None, error_code=None):
        download = details["download"] if details else {}
        key = key or "download_smoke_" + (details["platform"] if details else "bilibili")
        status = status or download.get("status", ProviderHealthTestStatus.SUCCESS)
        error_code = error_code if error_code is not None else download.get("error_code", "")
        suite = ProviderValidationSuiteRun.objects.create(
            request_id=f"smoke-{ProviderValidationSuiteRun.objects.count() + 1}",
            trigger=ProviderValidationSuiteTrigger.SCHEDULED,
            status=(ProviderValidationSuiteStatus.SUCCESS if status == ProviderHealthTestStatus.SUCCESS
                    else ProviderValidationSuiteStatus.DEGRADED),
            total_count=1,
            success_count=int(status == ProviderHealthTestStatus.SUCCESS),
            failed_count=int(status == ProviderHealthTestStatus.FAILED),
        )
        return ProviderHealthTestRun.objects.create(
            request_id=suite.request_id,
            suite_run=suite,
            test_key=key,
            provider="native",
            status=status,
            error_code=error_code,
            test_url_host="sample.example.test",
            result_fingerprint=provider_result_fingerprint(
                test_key=key, status=status, provider="native", error_code=error_code,
            ),
            download_smoke_details=details,
        )

    def report_for(self, runs):
        keys = tuple(run.test_key for run in runs)
        # Isolate reporting from the health runner's separately owned registry.
        with patch("xhs_download.services.provider_daily_validation.selected_test_keys", return_value=keys), \
                patch("xhs_download.services.provider_daily_validation.provider_health_test_keys", return_value=keys):
            return build_daily_validation_report(self.setting)

    def render_report(self, runs, *, surface="daily"):
        context = {"test_setting": self.setting, "admin_time_zone": "UTC"}
        if surface == "daily":
            context["daily_report"] = self.report_for(runs)
        elif surface == "history":
            context["recent_runs"] = runs
        else:
            context["test_cards"] = [{"key": run.test_key, "name": run.test_key, "last_run": run} for run in runs]
        return render_to_string("admin/provider_health_tests.html", context)

    def deliver(self, runs):
        events = tuple(
            NotificationEvent(
                run, ProviderHealthNotificationEvent.RECOVERY if run.status == ProviderHealthTestStatus.SUCCESS
                else ProviderHealthNotificationEvent.FAILURE, 2,
            )
            for run in runs
        )
        self.assertEqual(
            deliver_notifications(runs[0].suite_run, events, self.setting),
            ProviderValidationNotificationStatus.SENT,
        )
        return mail.outbox[-1].body

    def test_nullable_storage_round_trips_without_changing_daily_defaults(self):
        run = self.make_run()
        run.refresh_from_db()
        self.assertIsNone(run.download_smoke_details)
        self.assertIsNone(ProviderHealthTestRun().download_smoke_details)
        details = smoke_details()
        run.download_smoke_details = details
        run.save(update_fields=("download_smoke_details",))
        run.refresh_from_db()
        self.assertEqual(run.download_smoke_details, details)
        self.assertEqual(ProviderHealthTestSetting().daily_test_keys, ["health_ready"])
        self.assertFalse(ProviderHealthTestSetting().daily_enabled)

    def test_four_platform_fields_and_bounded_observation_in_report_and_email(self):
        runs = [self.make_run(smoke_details(platform)) for platform in (
            "xhs_gallery", "xhs_video", "douyin", "bilibili",
        )]
        report = self.report_for(runs)
        self.assertEqual(report["selected_test_count"], 4)
        self.assertEqual(report["active_failing_count"], 0)
        html = self.render_report(runs)
        body = self.deliver(runs)
        for output in (html, body):
            for run in runs:
                self.assertIn(f"platform={run.download_smoke_details['platform']} provider=native", output)
            self.assertIn("fields: title=yes cover=yes quality=no size=no", output)
            self.assertIn("status=success error_code=- http_status=206", output)
            self.assertIn("final_host=media.example.test final_port=443", output)
            self.assertIn("redirect_count=0 referer_lost=no", output)
            self.assertIn("content_type=video/mp4 range_supported=yes bytes_received=1024 request_count=1", output)
        self.assertEqual(len(mail.outbox), 1)

    def test_port_policy_denial_is_visible_in_all_report_surfaces_and_email(self):
        run = self.make_run(smoke_details(
            status="failed", error_code="PORT_POLICY_DENIED", http_status=None,
            final_port=8443, bytes_received=0, request_count=0, range_supported=None,
        ))
        for output in [self.render_report([run], surface=surface) for surface in ("daily", "history", "card")] + [self.deliver([run])]:
            self.assertIn("status=failed error_code=PORT_POLICY_DENIED http_status=-", output)
            self.assertIn("final_port=8443", output)
            self.assertIn("bytes_received=0 request_count=0", output)
        self.assertEqual(self.report_for([run])["active_failing_count"], 1)

    def test_referer_lost_and_403_are_visible_without_referer_value(self):
        details = smoke_details(
            status="failed", error_code="MEDIA_HTTP_403", http_status=403,
            redirect_count=1, referer_lost=True, bytes_received=0, request_count=2,
            range_supported=False, content_type="text/html",
            referer="https://private.example.test/?token=hidden-referer",
        )
        run = self.make_run(details)
        for output in (self.render_report([run]), self.deliver([run])):
            self.assertIn("status=failed error_code=MEDIA_HTTP_403 http_status=403", output)
            self.assertIn("redirect_count=1 referer_lost=yes", output)
            self.assertIn("range_supported=no bytes_received=0 request_count=2", output)
            self.assertNotIn("hidden-referer", output)
            self.assertNotIn("private.example.test", output)

    def test_untrusted_json_never_leaks_urls_tokens_or_free_text(self):
        secret = "https://private.example.test/path?token=do-not-display"
        details = smoke_details()
        details.update(platform=secret, provider=secret, raw_response=secret,
                       access_scope=secret, error_type=secret, stage=secret, media_type=secret,
                       continuation={"status": secret, "error_code": secret, "poll_count": secret})
        details["fields"] = dict.fromkeys(("title", "cover", "quality", "size"), secret)
        details["download"] = dict.fromkeys(details["download"], secret)
        details["download"]["error_message"] = secret
        run = self.make_run(smoke_details())
        run.download_smoke_details = details
        run.save(update_fields=("download_smoke_details",))
        before = deepcopy(details)
        for output in (self.render_report([run]), self.deliver([run])):
            self.assertNotIn("private.example.test", output)
            self.assertNotIn("do-not-display", output)
            self.assertNotIn("token=", output)
            self.assertIn("fields: title=- cover=- quality=- size=-", output)
            self.assertIn("status=- error_code=- http_status=-", output)
        self.assertEqual(details, before)

    def test_actual_resource_kind_and_continuation_are_reported_without_payloads(self):
        details = smoke_details()
        details.update(media_type="image", continuation={
            "status": "failed", "error_code": "DOUYIN_CONTINUATION_FAILED", "poll_count": 1,
        })
        run = self.make_run(details)
        for output in (self.render_report([run]), self.deliver([run])):
            self.assertIn("media_type=image", output)
            self.assertIn("continuation: status=failed error_code=DOUYIN_CONTINUATION_FAILED poll_count=1", output)
            self.assertIn("download: status=success", output)

    def test_browser_only_probe_is_explicitly_unverified_and_never_recovery(self):
        previous = self.make_run(smoke_details(status="failed", error_code="MEDIA_HTTP_403"))
        self.deliver([previous])
        details = smoke_details(status="not_run", error_code="SMOKE_BROWSER_ONLY_RESOURCE",
                                request_count=0, bytes_received=0, http_status=None)
        details["media_type"] = "video"
        skipped = self.make_run(details, status=ProviderHealthTestStatus.SKIPPED)
        self.assertEqual(plan_notification_events(skipped.suite_run, [skipped], self.setting), ())
        self.assertEqual(len(mail.outbox), 1)
        for surface in ("daily", "history", "card"):
            output = self.render_report([skipped], surface=surface)
            self.assertIn("status=not_run error_code=SMOKE_BROWSER_ONLY_RESOURCE", output)
            self.assertIn("bytes_received=0 request_count=0", output)
            self.assertNotIn("download: status=success", output)

    def test_parse_failure_and_optional_scope_use_only_recorded_safe_values(self):
        details = {
            "schema_version": 1, "platform": "bilibili", "provider": "", "fields": None,
            "access_scope": "anonymous_policy", "error_type": "XhsParseError",
            "download": {"status": "not_run", "error_code": "SMOKE_PARSE_OR_SELECTION_FAILED",
                         "request_count": 0, "bytes_received": 0},
        }
        run = self.make_run(details, status=ProviderHealthTestStatus.FAILED)
        run.is_staff = True
        run.save(update_fields=("is_staff",))
        for output in (self.render_report([run]), self.deliver([run])):
            self.assertIn("provider=- access_scope=anonymous_policy", output)
            self.assertNotIn("access_scope=staff", output)
            self.assertIn("fields: title=- cover=- quality=- size=-", output)
            self.assertIn("status=not_run error_code=SMOKE_PARSE_OR_SELECTION_FAILED http_status=- error_type=XhsParseError", output)
            self.assertIn("final_host=- final_port=- redirect_count=- referer_lost=-", output)
            self.assertIn("bytes_received=0 request_count=0", output)
        details["access_scope"] = "staff"
        self.assertIn("access_scope=staff", "\n".join(download_smoke_report_lines(details)))
        details.pop("fields")
        details["download"] = None
        self.assertIn("fields: title=- cover=- quality=- size=-", download_smoke_report_lines(details))

    def test_failure_stage_is_allowlisted_and_preserves_parsed_fields(self):
        details = smoke_details(status="not_run", error_code="SMOKE_SELECTION_NOT_ADMITTED",
                                http_status=None, request_count=0, bytes_received=0)
        details["stage"] = "selection"
        run = self.make_run(details, status=ProviderHealthTestStatus.FAILED)
        for output in (self.render_report([run]), self.deliver([run])):
            self.assertIn("stage=selection", output)
            self.assertIn("fields: title=yes cover=yes quality=no size=no", output)
            self.assertIn("status=not_run error_code=SMOKE_SELECTION_NOT_ADMITTED", output)
        for stage in ("parse", "selection", "admission", "download"):
            details["stage"] = stage
            self.assertIn(f"stage={stage}", "\n".join(download_smoke_report_lines(details)))
        details["stage"] = "unknown"
        self.assertIn("stage=-", "\n".join(download_smoke_report_lines(details)))

    def test_malformed_unsupported_and_missing_details_are_safe(self):
        for details in (None, [], "invalid", {}, {"schema_version": True}, {"schema_version": 2}):
            with self.subTest(details=details):
                self.assertEqual(download_smoke_report_lines(details), ())
        lines = download_smoke_report_lines({"schema_version": 1, "fields": [], "download": []})
        self.assertIn("fields: title=- cover=- quality=- size=-", lines)
        details = smoke_details(bytes_received=10**100, http_status=True, final_port=65536,
                                redirect_count=-1, request_count="1", content_type="x" * 10000)
        rendered = "\n".join(download_smoke_report_lines(details))
        self.assertLess(len(rendered), 1000)
        for name in ("bytes_received", "http_status", "final_port", "redirect_count", "request_count", "content_type"):
            self.assertIn(f"{name}=-", rendered)

    def test_hostname_accepts_dns_and_ipv6_but_rejects_url_parts(self):
        for host in ("media.example.test", "2001:db8::1", "203.0.113.8"):
            self.assertIn(f"final_host={host} ", "\n".join(download_smoke_report_lines(smoke_details(final_host=host))))
        for host in ("media.example.test:443", "user:secret@media.example.test", "media.example.test/?token=secret", "media.example.test\nsecret", "fe80::1%secret"):
            rendered = "\n".join(download_smoke_report_lines(smoke_details(final_host=host)))
            self.assertIn("final_host=- ", rendered)
            self.assertNotIn("secret", rendered)

    def test_old_null_runs_have_no_new_report_content_and_identical_email(self):
        run = self.make_run(key="health_ready")
        self.assertEqual(run.download_smoke_report_lines, ())
        for surface in ("daily", "history", "card"):
            html = self.render_report([run], surface=surface)
            self.assertNotIn("download_smoke:", html)
            self.assertNotIn("fields: title=", html)
            self.assertIn("health_ready", html)
        event = NotificationEvent(run, ProviderHealthNotificationEvent.RECOVERY, 0)
        suite = run.suite_run
        from .services.provider_health_tests import provider_health_test_by_key
        expected = "\n".join((
            "AnimeImages Provider validation",
            f"request_id: {suite.request_id}",
            f"started_at: {suite.started_at.isoformat()}",
            "suite_status: success",
            "counts: success=1 failed=0 skipped=0 unavailable=0",
            "",
            "test_key: health_ready",
            f"display_name: {provider_health_test_by_key('health_ready')['name']}",
            "provider: native", "status: success", "error_code: -",
            "consecutive_unhealthy: 0", "event: recovery", "test_host: sample.example.test", "",
        ))
        self.assertEqual(_notification_body(suite, [event]), expected)

    def test_notification_threshold_dedup_cooldown_and_recovery_are_unchanged(self):
        details = smoke_details(status="failed", error_code="PORT_POLICY_DENIED", final_port=8443)
        first = self.make_run(details)
        self.assertEqual(plan_notification_events(first.suite_run, [first], self.setting), ())
        second = self.make_run(details)
        events = plan_notification_events(second.suite_run, [second], self.setting)
        self.assertEqual([event.event for event in events], [ProviderHealthNotificationEvent.FAILURE])
        deliver_notifications(second.suite_run, events, self.setting)
        changed_observation = deepcopy(details)
        changed_observation["download"].update(final_port=8080, final_host="other.example.test")
        third = self.make_run(changed_observation)
        self.assertEqual(second.result_fingerprint, third.result_fingerprint)
        self.assertEqual(plan_notification_events(third.suite_run, [third], self.setting), ())
        ProviderHealthTestRun.objects.filter(pk=second.pk).update(
            notified_at=timezone.now() - timezone.timedelta(hours=25),
        )
        fourth = self.make_run(details)
        events = plan_notification_events(fourth.suite_run, [fourth], self.setting)
        self.assertEqual(events[0].event, ProviderHealthNotificationEvent.REMINDER)
        deliver_notifications(fourth.suite_run, events, self.setting)
        changed = self.make_run(smoke_details(status="failed", error_code="MEDIA_HTTP_403", http_status=403, referer_lost=True))
        events = plan_notification_events(changed.suite_run, [changed], self.setting)
        self.assertEqual(events[0].event, ProviderHealthNotificationEvent.FAILURE)
        deliver_notifications(changed.suite_run, events, self.setting)
        skipped = self.make_run(status=ProviderHealthTestStatus.SKIPPED)
        self.assertEqual(plan_notification_events(skipped.suite_run, [skipped], self.setting), ())
        recovered = self.make_run(smoke_details())
        events = plan_notification_events(recovered.suite_run, [recovered], self.setting)
        self.assertEqual(events[0].event, ProviderHealthNotificationEvent.RECOVERY)
        deliver_notifications(recovered.suite_run, events, self.setting)
        self.assertEqual(len(mail.outbox), 4)
        self.assertIn("recovered", mail.outbox[-1].subject)

    def test_existing_delivery_gates_still_apply_with_smoke_details(self):
        run = self.make_run(smoke_details(status="failed", error_code="PORT_POLICY_DENIED"))
        events = (NotificationEvent(run, ProviderHealthNotificationEvent.FAILURE, 2),)
        for no_email, setting_enabled in ((True, True), (False, False)):
            with self.subTest(no_email=no_email, setting_enabled=setting_enabled):
                self.setting.email_notifications_enabled = setting_enabled
                self.assertEqual(
                    deliver_notifications(run.suite_run, events, self.setting, no_email=no_email),
                    ProviderValidationNotificationStatus.DISABLED,
                )
                run.refresh_from_db()
                self.assertEqual(run.notification_delivery_status, ProviderHealthNotificationDeliveryStatus.DISABLED)
                self.assertEqual(run.download_smoke_details["download"]["error_code"], "PORT_POLICY_DENIED")
        self.assertEqual(len(mail.outbox), 0)


class DownloadSmokeStorageMigrationTests(TransactionTestCase):
    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()

    def test_existing_runs_remain_null_and_settings_are_untouched(self):
        previous = ("xhs_download", "0028_evaluation_history")
        target = ("xhs_download", "0029_providerhealthtestrun_download_smoke_details")
        executor = MigrationExecutor(connection)
        executor.migrate([previous])
        apps = executor.loader.project_state([previous]).apps
        run = apps.get_model("xhs_download", "ProviderHealthTestRun").objects.create(
            request_id="old-run", test_key="health_ready", status="success",
        )
        apps.get_model("xhs_download", "ProviderHealthTestSetting").objects.create(
            singleton_key=1, daily_enabled=True, daily_test_keys=["health_ready", "douyin_native_parse"],
        )
        executor = MigrationExecutor(connection)
        executor.migrate([target])
        apps = executor.loader.project_state([target]).apps
        migrated = apps.get_model("xhs_download", "ProviderHealthTestRun").objects.get(pk=run.pk)
        self.assertIsNone(migrated.download_smoke_details)
        setting = apps.get_model("xhs_download", "ProviderHealthTestSetting").objects.get(singleton_key=1)
        self.assertTrue(setting.daily_enabled)
        self.assertEqual(setting.daily_test_keys, ["health_ready", "douyin_native_parse"])
        executor.migrate([previous])
        executor = MigrationExecutor(connection)
        executor.migrate([target])
        self.assertTrue(ProviderHealthTestRun.objects.filter(pk=run.pk, download_smoke_details__isnull=True).exists())
