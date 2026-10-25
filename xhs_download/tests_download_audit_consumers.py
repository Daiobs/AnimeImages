"""Audit dispatch compatibility rows are not independent download outcomes."""

from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone

from download_console.models import DownloadRuntimeSetting

from .models import DownloadAuditEvent, DownloadRequestLog, ProviderRuntimeSetting, RoutingDecisionLog
from .policy.adaptive import build_adaptive_weight_recommendation, build_policy_feedback_summary
from .policy.learning import build_policy_learning_snapshot
from .services.admin_console import build_admin_console_context
from .services.control_plane import build_core_metrics, build_download_cost_status, provider_log_stats
from .services.download_audit import legacy_download_logs, operational_download_logs
from .services.feedback_loop import build_feedback, build_insights


class DownloadAuditConsumerBoundaryTests(TestCase):
    def setUp(self):
        cache.clear()
        DownloadRuntimeSetting.load()
        ProviderRuntimeSetting.load()

    def business_log(self, **fields):
        return DownloadRequestLog.objects.create(**{
            "request_id": "dispatch", "status": "success", "provider": "tiantianjiexi",
            "route_type": "proxy", "final_policy_route": "proxy", **fields,
        })

    def audit(self, phase, **fields):
        return DownloadAuditEvent.objects.create(**{
            "attempt_id": "attempt", "phase": phase, "evidence_source": "server_observed",
            "provider": "tiantianjiexi", "status": "pending", **fields,
        })

    def assert_no_download_learning(self):
        learning = build_policy_learning_snapshot()
        self.assertEqual(learning["download_count"], 0)
        self.assertEqual(learning["sample_count"], 0)
        self.assertEqual(learning["route_distribution"], {})
        self.assertEqual(learning["shadow_recommendation"]["reason"], "insufficient_data")
        self.assertEqual(learning["confidence"], 0)
        adaptive = build_policy_feedback_summary()
        self.assertEqual(adaptive["policy_feedback_count"], 0)
        self.assertEqual(adaptive["by_provider"], {})
        self.assertIsNone(adaptive["success_rate"])
        self.assertEqual(build_adaptive_weight_recommendation()["confidence"], 0)

    def test_linked_dispatch_is_excluded_even_when_its_event_is_outside_window(self):
        old_business = self.business_log(request_id="old", actual_bytes=512)
        dispatch = self.business_log()
        event = self.audit("prepared", legacy_log_id=dispatch.pk)
        DownloadAuditEvent.objects.filter(pk=event.pk).update(
            created_at=timezone.now() - timezone.timedelta(days=3),
        )
        self.audit("prepared", attempt_id="unlinked")
        rows = DownloadRequestLog.objects.filter(created_at__gte=timezone.now() - timezone.timedelta(days=1))
        for reader in (legacy_download_logs, operational_download_logs):
            with self.subTest(reader=reader.__name__):
                self.assertEqual(list(reader(rows).values_list("pk", flat=True)), [old_business.pk])
        self.assertEqual(DownloadRequestLog.objects.count(), 2)
        self.assertEqual(DownloadAuditEvent.objects.count(), 2)

    def test_prepared_dispatch_does_not_supply_download_or_policy_samples(self):
        dispatch = self.business_log()
        self.audit("prepared", legacy_log_id=dispatch.pk)
        self.assert_no_download_learning()
        self.assertEqual(build_feedback()["signals"]["download_count"], 0)
        self.assertEqual(provider_log_stats(("tiantianjiexi",))["count"], 0)
        self.assertEqual(build_core_metrics()["today_download_count"], 0)
        self.assertEqual(build_download_cost_status()["actual_bytes"], 0)

    def test_client_completed_does_not_promote_dispatch_into_trusted_learning(self):
        dispatch = self.business_log()
        self.audit("prepared", legacy_log_id=dispatch.pk)
        for phase in ("handed_off", "transfer_started", "transfer_completed"):
            self.audit(
                phase, evidence_source="client_observed", transport="browser_streaming",
                status="success" if phase == "transfer_completed" else "pending", actual_bytes=4096,
            )
        self.assert_no_download_learning()
        feedback = build_feedback()
        self.assertEqual(feedback["signals"]["download_count"], 0)
        self.assertEqual(feedback["signals"]["provider_signals"], [])

    def test_associated_cancel_compatibility_row_is_not_a_provider_failure(self):
        dispatch = self.business_log()
        self.audit("prepared", legacy_log_id=dispatch.pk)
        prior_terminal_mirror = self.business_log(status="failed", failure_type="provider_failed", actual_bytes=256)
        self.audit(
            "cancelled", legacy_log_id=prior_terminal_mirror.pk, transport="server_attachment",
            status="cancelled", actual_bytes=256,
        )
        self.assert_no_download_learning()
        feedback = build_feedback()["signals"]
        self.assertEqual(feedback["download_count"], 0)
        self.assertEqual(feedback["provider_signals"], [])
        self.assertEqual(feedback["failures"], [])
        self.assertEqual(provider_log_stats(("tiantianjiexi",))["recent_failure"], "")

    def test_real_unlinked_legacy_business_samples_remain_available(self):
        self.business_log(actual_bytes=100)
        self.business_log(status="failed", error_code="UPSTREAM_TIMEOUT", actual_bytes=20)
        dispatch = self.business_log(actual_bytes=9999)
        self.audit("prepared", legacy_log_id=dispatch.pk)
        learning = build_policy_learning_snapshot()
        self.assertEqual(learning["download_count"], 2)
        self.assertEqual(learning["route_distribution"]["proxy"]["success"], 1)
        self.assertEqual(learning["cost_summary"]["actual_server_bytes"], 120)
        feedback = build_policy_feedback_summary()
        self.assertEqual(feedback["success_rate"], 0.5)
        self.assertEqual(feedback["by_provider"]["tiantianjiexi"], {"count": 2, "failed": 1})

    def test_pending_cancelled_unknown_and_client_legacy_rows_are_not_outcomes(self):
        trusted = self.business_log()
        for status in ("pending", "cancelled", "unknown"):
            self.business_log(status=status)
        self.business_log(evidence_source="client_observed")
        self.business_log(attempt_id="partial-audit-marker")
        self.assertEqual(
            list(operational_download_logs(DownloadRequestLog.objects.all()).values_list("pk", flat=True)),
            [trusted.pk],
        )
        self.assertEqual(DownloadRequestLog.objects.count(), 6)
        self.assertEqual(legacy_download_logs(DownloadRequestLog.objects.all()).count(), 5)
        rows = build_admin_console_context()["system_stability"]["provider_rows"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["count"], 1)
        self.assertEqual(rows[0]["failure_rate"], "0.0%")

    def test_admin_counts_one_sw_attempt_and_no_legacy_dispatch_sample(self):
        dispatch = self.business_log()
        self.audit("prepared", legacy_log_id=dispatch.pk)
        for phase in ("handed_off", "transfer_started", "transfer_completed"):
            self.audit(
                phase, evidence_source="client_observed", transport="browser_streaming",
                status="success" if phase == "transfer_completed" else "pending", actual_bytes=4096,
            )
        context = build_admin_console_context()
        cards = {row["label"]: row["value"] for row in context["cards"]}
        self.assertEqual(cards["今日下载次数（去重尝试）"], 1)
        self.assertEqual(cards["传输完成率（已观测终态，非落盘）"], "100.0%")
        self.assertEqual(cards["今日下载实际流量（服务端已发送）"], "0 B")
        self.assertEqual(cards["今日客户端上报接收量（非服务端流量）"], "4.0 KB")
        self.assertEqual(cards["今日 legacy 行数（旧口径，不计新完成率）"], 0)
        self.assertEqual(context["system_stability"]["provider_rows"], [])

    def test_prepared_only_has_unknown_completion_not_success(self):
        dispatch = self.business_log()
        self.audit("prepared", legacy_log_id=dispatch.pk)
        context = build_admin_console_context()
        cards = {row["label"]: row["value"] for row in context["cards"]}
        self.assertEqual(cards["今日下载次数（去重尝试）"], 1)
        self.assertEqual(cards["传输完成率（已观测终态，非落盘）"], "未知")
        self.assertEqual(cards["今日 legacy 行数（旧口径，不计新完成率）"], 0)

    def test_routing_dispatch_success_stays_separate_from_download_samples(self):
        RoutingDecisionLog.objects.create(route_type="proxy", success=True, provider="tiantianjiexi")
        feedback = build_feedback()
        route = feedback["routes"]["proxy"]
        self.assertEqual(route["routing_count"], 1)
        self.assertEqual(route["routing_success"], 1)
        self.assertEqual(route["download_count"], 0)
        self.assertEqual(route["download_success"], 0)
        self.assertIsNone(route["success_rate"])
        self.assertIn("未知", route["success_rate_label"])
        self.assertEqual(build_insights(feedback)["insights"][0]["kind"], "insufficient_data")
        self.assert_no_download_learning()
