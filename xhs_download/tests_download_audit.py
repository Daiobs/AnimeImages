import json
import time
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.core import signing
from django.core.cache import cache
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.middleware.csrf import get_token
from django.test import Client, RequestFactory, TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.template.loader import render_to_string

from .models import DownloadAuditEvent, DownloadRequestLog, ObservabilityStatus, PolicyControlRecord, RoutingDecisionLog
from .services.admin_console import build_admin_console_context
from .services.download_audit import (
    MAX_BYTES, SCHEMA, TOKEN_MAX_AGE, TOKEN_SALT, download_attempts,
    issue_browser_download_audit, legacy_download_logs, operational_download_logs, record_browser_download_audit,
    record_transfer_audit,
)
from .services.observability import record_download_request_log


@override_settings(DOWNLOAD_TRUST_X_FORWARDED_FOR=False)
class DownloadAuditTests(TestCase):
    def setUp(self):
        self.request = RequestFactory().post(
            "/media/browser-download-telemetry/", REMOTE_ADDR="203.0.113.10",
            HTTP_USER_AGENT="Audit browser",
        )
        self.request.user = SimpleNamespace(is_active=False, is_staff=False, is_authenticated=False)
        self.request.request_id = "audit-request"
        self.item = {
            "url": "https://media.example/video.mp4?signature=secret",
            "provider_manifest_token": "private-media-authorization",
            "provider": "tiantianjiexi", "source_platform": "douyin",
            "type": "video", "size_bytes": 1024, "filename": "Example.mp4",
        }

    def issue(self):
        return issue_browser_download_audit(self.request, self.item)["download_audit"]

    def event(self, receipt, **changes):
        return {
            "schema": SCHEMA, "token": receipt["token"], "phase": "handed_off",
            "transport": "browser_native", "actual_bytes": 0, "error_code": "",
            **changes,
        }

    def test_issue_preserves_item_and_stores_only_preparation_not_success(self):
        original = dict(self.item)
        with patch("requests.sessions.Session.request", side_effect=AssertionError("No network")):
            result = issue_browser_download_audit(self.request, self.item)
        self.assertEqual(self.item, original)
        self.assertEqual({k: v for k, v in result.items() if k != "download_audit"}, original)
        audit = result["download_audit"]
        self.assertEqual(audit["endpoint"], reverse("media_browser_download_telemetry"))
        prepared = DownloadAuditEvent.objects.get()
        self.assertFalse(DownloadRequestLog.objects.exists())
        self.assertEqual(prepared.status, "pending")
        self.assertEqual(prepared.phase, "prepared")
        self.assertEqual(prepared.transport, "")
        self.assertEqual(prepared.actual_bytes, 0)
        self.assertEqual(prepared.provider, "tiantianjiexi")
        self.assertEqual(prepared.source_platform, "douyin")
        receipt = signing.loads(audit["token"], salt=TOKEN_SALT)
        self.assertEqual(set(receipt), {"schema", "attempt_id", "prepared_id", "identity"})
        for secret in (self.item["url"], "private-media-authorization", "tiantianjiexi", "203.0.113.10"):
            self.assertNotIn(secret, str(receipt))

    def test_invalid_selected_action_keeps_rejection_without_audit_exception(self):
        client = Client(raise_request_exception=False)
        with patch('requests.sessions.Session.request', side_effect=AssertionError('Invalid actions must not fetch')):
            for endpoint in ('/media/ytdlp-selected-format/', '/media/service-selected-format/'):
                for action, expected in (([], 403), ({}, 403), (None, 400), (1, 400), ('invalid', 400)):
                    with self.subTest(endpoint=endpoint, action=action):
                        response = client.post(endpoint, data=json.dumps({
                            'action': action, 'candidate_id': 'untrusted', 'resource_token': 'invalid',
                        }), content_type='application/json')
                        self.assertEqual(response.status_code, expected, response.content[:200])
                        self.assertEqual(response.json()['status'], 'error')
        self.assertFalse(DownloadRequestLog.objects.exists())
        self.assertFalse(DownloadAuditEvent.objects.exists())

    def test_issue_fail_open_returns_original(self):
        with patch("xhs_download.services.download_audit.record_transfer_audit", return_value=None):
            self.assertIs(issue_browser_download_audit(self.request, self.item), self.item)
        with patch("xhs_download.services.download_audit.reverse", side_effect=RuntimeError("offline")):
            self.assertIs(issue_browser_download_audit(self.request, self.item), self.item)
        self.assertFalse(DownloadRequestLog.objects.exists())
        self.assertFalse(DownloadAuditEvent.objects.exists())

    def test_existing_dispatch_preparation_is_reused_without_duplicate_attempt(self):
        prepared = record_transfer_audit(
            self.request, phase="prepared", media_items=(self.item,),
            download_type="direct", route_type="direct", final_policy_route="direct",
            browser_egress_estimated_bytes=1024,
        )
        result = issue_browser_download_audit(self.request, self.item, prepared_log=prepared)
        self.assertEqual(result["download_audit"]["attempt_id"], prepared.attempt_id)
        self.assertEqual(DownloadAuditEvent.objects.count(), 1)
        self.assertEqual(DownloadAuditEvent.objects.get().route_type, "direct")
        other = RequestFactory().post("/", REMOTE_ADDR="203.0.113.11")
        other.user = self.request.user
        self.assertIs(issue_browser_download_audit(other, self.item, prepared_log=prepared), self.item)
        self.assertEqual(DownloadAuditEvent.objects.count(), 1)
        self.assertFalse(DownloadRequestLog.objects.exists())

    def test_gallery_receipts_are_not_issued_per_image(self):
        items = [{"type": "image", "url": f"https://ci.xiaohongshu.com/{index}.jpg"}
                 for index in range(1000)]
        with self.assertNumQueries(0):
            result = [issue_browser_download_audit(self.request, item) for item in items]
        self.assertEqual(result, items)
        self.assertFalse(any("download_audit" in item for item in result))
        self.assertFalse(DownloadRequestLog.objects.exists())
        self.assertFalse(DownloadAuditEvent.objects.exists())

    def test_thousand_image_http_dispatch_keeps_one_aggregate_preparation(self):
        items = [{"type": "image", "source_platform": "xiaohongshu",
                  "url": f"https://ci.xiaohongshu.com/{index}.jpg", "size_bytes": 10}
                 for index in range(1000)]
        # Isolate audit scaling from the unchanged production limit of 30 resources.
        with patch("xhs_download.services.downloads.MAX_DOWNLOAD_ITEMS", 1000), patch(
            "requests.sessions.Session.request", side_effect=AssertionError("No media request"),
        ):
            response = self.client.post("/media/download-images/", data=json.dumps({
                "download_mode": "auto", "intent": "download", "media_items": items,
            }), content_type="application/json")
        self.assertEqual(response.status_code, 200, response.content[:200])
        self.assertEqual(response.json()["route_type"], "direct")
        self.assertEqual(len(response.json()["items"]), 1000)
        self.assertFalse(any("download_audit" in item for item in response.json()["items"]))
        log = DownloadAuditEvent.objects.get()
        self.assertEqual((log.phase, log.status), ("prepared", "pending"))
        self.assertEqual(log.image_count, 1000)
        self.assertEqual(log.actual_bytes, 0)
        legacy = DownloadRequestLog.objects.get()
        self.assertEqual((legacy.phase, legacy.status, legacy.image_count), ("", "success", 1000))
        self.assertEqual(log.legacy_log_id, legacy.pk)
        self.assertEqual(RoutingDecisionLog.objects.count(), 1)

    def test_trusted_dispatch_feedback_is_once_and_not_transfer_success(self):
        for _ in range(2):
            prepared = record_transfer_audit(
                self.request, phase="prepared", attempt_id="dispatch-once",
                media_items=(self.item,), download_type="direct", route_type="direct",
                final_policy_route="direct", policy_decision_source="policy_v3_5",
                browser_egress_estimated_bytes=1024, status="success", dispatch_feedback=True,
            )
        self.assertEqual(prepared.status, "pending")
        self.assertFalse(DownloadAuditEvent.objects.filter(status="success").exists())
        legacy = DownloadRequestLog.objects.get()
        self.assertEqual((legacy.status, legacy.phase, legacy.attempt_id), ("success", "", ""))
        self.assertEqual(prepared.legacy_log_id, legacy.pk)
        feedback = RoutingDecisionLog.objects.get()
        self.assertTrue(feedback.success)
        self.assertEqual(feedback.route_type, "direct")
        self.assertEqual(feedback.estimated_browser_bytes, 1024)
        self.assertEqual(PolicyControlRecord.objects.count(), 1)
        receipt = issue_browser_download_audit(self.request, self.item, prepared_log=prepared)["download_audit"]
        for phase in ("handed_off", "transfer_started", "transfer_completed"):
            record_browser_download_audit(self.request, self.event(
                receipt, phase=phase, transport="browser_streaming", actual_bytes=1024,
            ))
        self.assertEqual(RoutingDecisionLog.objects.count(), 1)
        self.assertEqual(PolicyControlRecord.objects.count(), 1)
        self.assertEqual(DownloadRequestLog.objects.count(), 1)
        self.assertEqual(DownloadAuditEvent.objects.count(), 4)

    def test_feedback_flag_cannot_enable_client_or_transfer_phase_feedback(self):
        for phase, evidence in (("handed_off", "client_observed"), ("transfer_completed", "server_observed")):
            record_transfer_audit(
                self.request, phase=phase, transport="browser_streaming",
                evidence_source=evidence, route_type="direct", dispatch_feedback=True,
            )
        self.assertFalse(RoutingDecisionLog.objects.exists())
        self.assertFalse(PolicyControlRecord.objects.exists())
        self.assertFalse(DownloadRequestLog.objects.exists())
        self.assertEqual(DownloadAuditEvent.objects.count(), 2)

    def test_missing_identity_does_not_issue_portable_receipt(self):
        with patch("xhs_download.services.download_audit.request_privacy_fields", return_value={
            "client_identity_hash": "", "ip_hash": "", "user_agent_hash": "", "is_staff": False,
        }):
            self.assertIs(issue_browser_download_audit(self.request, self.item), self.item)
        self.assertFalse(DownloadRequestLog.objects.exists())
        self.assertFalse(DownloadAuditEvent.objects.exists())

    def test_native_handoff_is_pending_and_idempotent(self):
        receipt = self.issue()
        for _ in range(2):
            self.assertTrue(record_browser_download_audit(self.request, self.event(receipt)))
        self.assertEqual(DownloadAuditEvent.objects.count(), 2)
        self.assertFalse(DownloadRequestLog.objects.exists())
        event = DownloadAuditEvent.objects.get(phase="handed_off")
        self.assertEqual(event.status, "pending")
        self.assertEqual(event.evidence_source, "client_observed")
        self.assertEqual(event.provider, "tiantianjiexi")
        self.assertEqual(event.source_platform, "douyin")

    def test_native_transfer_and_bytes_cannot_be_claimed(self):
        receipt = self.issue()
        for changes in ({"phase": "transfer_started"}, {"phase": "transfer_completed"}, {"actual_bytes": 1}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                record_browser_download_audit(self.request, self.event(receipt, **changes))
        self.assertEqual(DownloadAuditEvent.objects.count(), 1)

    def test_client_cannot_claim_server_transport_preparation_or_metadata(self):
        receipt = self.issue()
        for changes in (
            {"transport": "server_attachment"}, {"transport": "server_zip"},
            {"transport": "server_merge"}, {"phase": "prepared"},
            {"provider": "native"}, {"source_platform": "bilibili"},
            {"url": "https://other.example/media"}, {"evidence_source": "server_observed"},
            {"attempt_id": "other"},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                record_browser_download_audit(self.request, self.event(receipt, **changes))

    def test_payload_validation_is_bounded(self):
        receipt = self.issue()
        bad_changes = (
            {"schema": "other"}, {"token": "x" * 4097}, {"token": None},
            {"phase": "x" * 25}, {"phase": []}, {"transport": []},
            {"actual_bytes": -1}, {"actual_bytes": MAX_BYTES + 1},
            {"actual_bytes": True}, {"actual_bytes": 1.5}, {"actual_bytes": "1024"},
            {"error_code": "x" * 81}, {"error_code": "secret=https://example.test"},
            {"error_code": []},
        )
        for changes in bad_changes:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                record_browser_download_audit(self.request, self.event(receipt, **changes))
        for payload in (None, [], "event"):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                record_browser_download_audit(self.request, payload)

    def test_signed_token_tampering_and_expiry_rejected(self):
        receipt = self.issue()
        with self.assertRaises(ValueError):
            record_browser_download_audit(self.request, self.event(receipt, token=receipt["token"] + "x"))
        now = time.time()
        with patch("django.core.signing.time.time", return_value=now + TOKEN_MAX_AGE + 2):
            with self.assertRaises(ValueError):
                record_browser_download_audit(self.request, self.event(receipt))
        self.assertEqual(DownloadAuditEvent.objects.count(), 1)

    def test_token_does_not_renew_or_read_media_authorization(self):
        self.item["selection_expires_at"] = 1
        receipt = self.issue()
        with patch("requests.sessions.Session.request", side_effect=AssertionError("No media access")):
            self.assertTrue(record_browser_download_audit(self.request, self.event(receipt)))
        self.assertEqual(self.item["selection_expires_at"], 1)

    def test_wrong_identity_and_staff_transition_rejected(self):
        receipt = self.issue()
        for key, value in (("REMOTE_ADDR", "203.0.113.11"), ("HTTP_USER_AGENT", "Another browser")):
            original = self.request.META[key]
            self.request.META[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                record_browser_download_audit(self.request, self.event(receipt))
            self.request.META[key] = original
        self.request.user = SimpleNamespace(is_active=True, is_staff=True, is_authenticated=True, pk=7)
        with self.assertRaises(ValueError):
            record_browser_download_audit(self.request, self.event(receipt))

    def test_prepared_record_required_and_cannot_switch_attempt(self):
        receipt = self.issue()
        token_data = signing.loads(receipt["token"], salt=TOKEN_SALT)
        token_data["attempt_id"] = "different"
        with self.assertRaises(ValueError):
            record_browser_download_audit(self.request, self.event(
                receipt, token=signing.dumps(token_data, salt=TOKEN_SALT),
            ))
        DownloadAuditEvent.objects.all().delete()
        with self.assertRaises(ValueError):
            record_browser_download_audit(self.request, self.event(receipt))

    def test_sw_and_blob_completed_only_client_observed(self):
        for transport in ("browser_streaming", "browser_blob"):
            receipt = self.issue()
            for phase in ("transfer_started", "transfer_completed", "handed_off"):
                self.assertTrue(record_browser_download_audit(self.request, self.event(
                    receipt, phase=phase, transport=transport, actual_bytes=1024,
                )))
            completed = DownloadAuditEvent.objects.get(attempt_id=receipt["attempt_id"], phase="transfer_completed")
            self.assertEqual(completed.status, "success")
            self.assertEqual(completed.evidence_source, "client_observed")
            self.assertIn("非磁盘保存", completed.audit_phase_label)

    def test_no_client_audit_routing_or_control_feedback(self):
        with patch("xhs_download.services.observability.record_routing_decision_log_from_download") as route:
            with patch("xhs_download.services.control_execution_tracker.track_control_execution_from_download_log") as control:
                receipt = self.issue()
                record_browser_download_audit(self.request, self.event(
                    receipt, phase="transfer_completed", transport="browser_blob", actual_bytes=1024,
                ))
                route.assert_not_called()
                control.assert_not_called()
        self.assertEqual(RoutingDecisionLog.objects.count(), 0)
        self.assertFalse(DownloadRequestLog.objects.exists())

    def test_duplicate_event_constraint_does_not_rewrite_observation(self):
        receipt = self.issue()
        event = self.event(receipt, phase="failed", transport="browser_streaming", actual_bytes=10, error_code="NETWORK_ERROR")
        record_browser_download_audit(self.request, event)
        record_browser_download_audit(self.request, {**event, "actual_bytes": 999})
        self.assertEqual(DownloadAuditEvent.objects.get(phase="failed").actual_bytes, 10)
        with self.assertRaises(IntegrityError), transaction.atomic():
            DownloadAuditEvent.objects.create(
                attempt_id=receipt["attempt_id"], phase="failed", transport="browser_streaming",
            )

    def test_database_failure_returns_false_not_success(self):
        receipt = self.issue()
        with patch("xhs_download.services.download_audit.record_transfer_audit", return_value=None):
            self.assertFalse(record_browser_download_audit(self.request, self.event(receipt)))

    def test_server_observation_uses_actual_bytes_and_separate_phase(self):
        for transport in ("server_attachment", "server_zip", "server_merge"):
            prepared = record_transfer_audit(self.request, phase="prepared", media_items=(self.item,))
            completed = record_transfer_audit(
                self.request, phase="transfer_completed", transport=transport,
                attempt_id=prepared.attempt_id, media_items=(self.item,), actual_bytes=600,
            )
            self.assertEqual(completed.actual_bytes, 600)
            self.assertEqual(completed.estimated_bytes, 1024)
            self.assertEqual(completed.evidence_source, "server_observed")
            self.assertEqual(prepared.status, "pending")

    def test_unstarted_server_rejection_has_no_transport_or_bytes(self):
        rejected = record_transfer_audit(
            self.request, phase="failed", transport="", actual_bytes=0,
            error_code="CANDIDATE_EXPIRED",
        )
        self.assertIsNotNone(rejected)
        self.assertEqual(rejected.status, "failed")
        self.assertEqual(rejected.transport, "")
        self.assertEqual(rejected.evidence_source, "server_observed")
        self.assertEqual(operational_download_logs(DownloadRequestLog.objects.all()).count(), 0)
        receipt = self.issue()
        with self.assertRaises(ValueError):
            record_browser_download_audit(self.request, self.event(receipt, phase="failed", transport=""))
        with self.assertLogs("xhs_download.services.download_audit", level="WARNING"):
            self.assertIsNone(record_transfer_audit(self.request, phase="failed", transport="", actual_bytes=1))

    def test_legacy_rows_unchanged_and_not_in_attempts(self):
        for _ in range(2):
            record_download_request_log(self.request, download_type="single_prepare", status="success")
        legacy = DownloadRequestLog.objects.first()
        self.assertEqual((legacy.attempt_id, legacy.phase, legacy.transport, legacy.evidence_source), ("", "", "", ""))
        self.assertNotIn("pending", ObservabilityStatus.values)
        self.assertIn("legacy", legacy.audit_phase_label)
        self.assertEqual(legacy_download_logs(DownloadRequestLog.objects.all()).count(), 2)
        self.assertEqual(download_attempts(DownloadAuditEvent.objects.all()).count(), 0)

    def test_attempt_summary_does_not_count_late_handoff_as_completion(self):
        receipt = self.issue()
        for phase in ("transfer_started", "transfer_completed", "handed_off", "unknown"):
            record_browser_download_audit(self.request, self.event(
                receipt, phase=phase, transport="browser_blob", actual_bytes=1024,
            ))
        attempts = download_attempts(DownloadAuditEvent.objects.all())
        self.assertEqual(attempts.count(), 1)
        self.assertEqual(attempts.get().phase, "transfer_completed")
        record_browser_download_audit(self.request, self.event(
            receipt, phase="failed", transport="browser_blob", actual_bytes=0, error_code="HANDOFF_FAILED",
        ))
        self.assertEqual(attempts.get().phase, "failed")
        self.assertEqual(attempts.get().observed_bytes, 1024)

    def test_cancelled_attempt_remains_distinct_from_failed(self):
        receipt = self.issue()
        for phase in ("transfer_completed", "cancelled"):
            record_browser_download_audit(self.request, self.event(receipt, phase=phase, transport="browser_blob"))
        self.assertEqual(download_attempts(DownloadAuditEvent.objects.all()).get().status, "cancelled")

    def test_outcome_is_ranked_across_days_before_filtering_its_time(self):
        from django.utils import timezone

        now = timezone.now()
        before = now - timezone.timedelta(days=2)
        cutoff = now - timezone.timedelta(days=1)
        receipt = self.issue()
        record_browser_download_audit(self.request, self.event(
            receipt, phase="transfer_completed", transport="browser_blob", actual_bytes=100,
        ))
        DownloadAuditEvent.objects.update(created_at=before)
        record_browser_download_audit(self.request, self.event(receipt, transport="browser_blob"))
        today = DownloadAuditEvent.objects.filter(created_at__gte=cutoff)
        self.assertEqual(download_attempts(today).count(), 0)
        previous = download_attempts(DownloadAuditEvent.objects.filter(created_at__lt=cutoff))
        self.assertEqual(previous.get().phase, "transfer_completed")
        self.assertEqual(previous.get().observed_bytes, 100)
        record_browser_download_audit(self.request, self.event(
            receipt, phase="failed", transport="browser_blob", error_code="HANDOFF_FAILED",
        ))
        self.assertEqual(download_attempts(today).get().phase, "failed")
        self.assertEqual(download_attempts(today).get().observed_bytes, 100)
        self.assertFalse(download_attempts(DownloadAuditEvent.objects.filter(created_at__lt=cutoff)).exists())

    def test_large_file_table_labels_client_evidence_not_disk_success(self):
        from download_console.models import DownloadRuntimeSetting

        setting = DownloadRuntimeSetting.load()
        setting.large_download_warning_bytes = 0
        setting.save(update_fields=["large_download_warning_bytes"])
        receipt = self.issue()
        record_browser_download_audit(self.request, self.event(
            receipt, phase="transfer_completed", transport="browser_blob", actual_bytes=1024,
        ))
        context = build_admin_console_context()
        row = context["large_downloads"][0]
        self.assertEqual(row["observed_bytes"], "1.0 KB")
        self.assertEqual(row["bytes_evidence"], "客户端报告接收量")
        self.assertNotIn("status", row)
        html = render_to_string("admin/console.html", context)
        self.assertIn("客户端报告接收量", html)
        self.assertIn("客户端上报（非落盘确认）", html)
        self.assertIn("传输完成（非磁盘保存）", html)
        self.assertIn(receipt["attempt_id"], html)

    def test_admin_summary_counts_attempts_not_events_or_legacy_success(self):
        record_download_request_log(self.request, download_type="single_prepare", status="success", actual_bytes=999)
        native = self.issue()
        record_browser_download_audit(self.request, self.event(native))
        complete = self.issue()
        for phase in ("transfer_started", "transfer_completed", "handed_off"):
            record_browser_download_audit(self.request, self.event(
                complete, phase=phase, transport="browser_streaming", actual_bytes=100,
            ))
        failed = self.issue()
        record_browser_download_audit(self.request, self.event(
            failed, phase="failed", transport="browser_blob", actual_bytes=25,
        ))
        context = build_admin_console_context()
        cards = {row["label"]: row["value"] for row in context["cards"]}
        self.assertEqual(cards["今日下载次数（去重尝试）"], 3)
        self.assertEqual(cards["传输完成率（已观测终态，非落盘）"], "50.0%")
        self.assertEqual(cards["今日未观测终态（含原生移交）"], 1)
        self.assertEqual(cards["今日下载实际流量（服务端已发送）"], "0 B")
        self.assertEqual(cards["今日客户端上报接收量（非服务端流量）"], "125 B")
        self.assertEqual(cards["今日 legacy 行数（旧口径，不计新完成率）"], 1)
        self.assertEqual(context["provider_top"], [{"provider": "tiantianjiexi", "count": 3}])

    def test_admin_log_page_separates_event_and_legacy_without_public_access(self):
        receipt = self.issue()
        record_browser_download_audit(self.request, self.event(receipt))
        record_download_request_log(self.request, status="success", download_type="legacy-only")
        url = reverse("download_logs")
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.get(url, {"records": "legacy"}).status_code, 404)
        user = get_user_model().objects.create_superuser(username="audit-admin", password="test-only-password")
        self.client.force_login(user)
        response = self.client.get(url)
        self.assertEqual(response.context["download_log_records"], "audit")
        self.assertEqual(response.context["page_obj"].paginator.count, 2)
        self.assertTrue(all(isinstance(row, DownloadAuditEvent) for row in response.context["page_obj"]))
        self.assertContains(response, receipt["attempt_id"])
        self.assertContains(response, "原生直链")
        self.assertContains(response, "已移交")
        self.assertContains(response, "客户端上报（非落盘确认）")
        self.assertNotContains(response, "legacy-only")
        self.assertNotContains(response, "legacy（旧口径，未区分阶段）")
        self.assertContains(response, '?records=legacy')
        self.assertContains(response, 'name="records" value="audit"')
        self.assertNotContains(response, self.item["url"])
        legacy_response = self.client.get(url, {"records": "legacy"})
        self.assertEqual(legacy_response.context["download_log_records"], "legacy")
        self.assertEqual(legacy_response.context["page_obj"].paginator.count, 1)
        self.assertTrue(all(isinstance(row, DownloadRequestLog) for row in legacy_response.context["page_obj"]))
        self.assertContains(legacy_response, "legacy-only")
        self.assertContains(legacy_response, "legacy（旧口径，未区分阶段）")
        self.assertContains(legacy_response, 'name="records" value="legacy"')
        self.assertNotContains(legacy_response, receipt["attempt_id"])
        self.assertNotContains(legacy_response, "客户端上报（非落盘确认）")
        filtered = self.client.get(url, {"records": "legacy", "download_type": "legacy-only"})
        self.assertEqual(filtered.context["page_obj"].paginator.count, 1)
        self.assertIn("records=legacy", filtered.context["page_querystring"])

    def test_audit_and_legacy_admins_are_registered_read_only(self):
        user = get_user_model().objects.create_superuser(username="audit-reader", password="test-only-password")
        request = RequestFactory().get("/admin/")
        request.user = user
        for model in (DownloadAuditEvent, DownloadRequestLog):
            with self.subTest(model=model.__name__):
                registered = admin.site._registry[model]
                self.assertTrue(registered.has_view_permission(request))
                self.assertFalse(registered.has_add_permission(request))
                self.assertFalse(registered.has_change_permission(request))
                self.assertIn("audit_phase", registered.list_display)

    def test_audit_log_search_applies_request_provider_and_error_filters(self):
        user = get_user_model().objects.create_superuser(username="audit-search", password="test-only-password")
        self.client.force_login(user)
        wanted = DownloadAuditEvent.objects.create(
            request_id="wanted-request", provider="wanted-provider", error_type="wanted-error",
        )
        DownloadAuditEvent.objects.create(request_id="unrelated")
        for query in ("wanted-request", "wanted-provider", "wanted-error"):
            with self.subTest(query=query):
                response = self.client.get(reverse("download_logs"), {"q": query})
                self.assertEqual([row.pk for row in response.context["page_obj"]], [wanted.pk])


class DownloadAuditMigrationTests(TransactionTestCase):
    def test_existing_rows_keep_empty_audit_fields_and_original_status(self):
        before = [("xhs_download", "0029_providerhealthtestrun_download_smoke_details")]
        executor = MigrationExecutor(connection)
        after = executor.loader.graph.leaf_nodes()
        try:
            executor.migrate(before)
            old_model = executor.loader.project_state(before).apps.get_model("xhs_download", "DownloadRequestLog")
            original = old_model.objects.create(request_id="legacy-preparation", status="success", download_type="single_prepare")
            executor = MigrationExecutor(connection)
            executor.migrate(after)
            migrated = DownloadRequestLog.objects.get(pk=original.pk)
            self.assertEqual(migrated.status, "success")
            self.assertEqual(migrated.download_type, "single_prepare")
            self.assertEqual(migrated.request_id, original.request_id)
            self.assertEqual(migrated.created_at, original.created_at)
            self.assertEqual((migrated.attempt_id, migrated.phase, migrated.transport, migrated.evidence_source), ("", "", "", ""))
            self.assertFalse(DownloadAuditEvent.objects.filter(pk=original.pk).exists())
        finally:
            MigrationExecutor(connection).migrate(after)


@override_settings(DOWNLOAD_TRUST_X_FORWARDED_FOR=False)
class DownloadAuditEndpointTests(TestCase):
    def setUp(self):
        cache.clear()
        self.url = reverse("media_browser_download_telemetry")
        self.client = Client(enforce_csrf_checks=True)
        self.request = RequestFactory().post(
            self.url, REMOTE_ADDR="203.0.113.14", HTTP_USER_AGENT="Audit browser",
        )
        self.request.user = SimpleNamespace(is_active=False, is_staff=False, is_authenticated=False)
        self.csrf_token = get_token(self.request)
        self.client.cookies["csrftoken"] = self.request.META["CSRF_COOKIE"]
        self.item = {
            "type": "video", "url": "https://media.example/video.mp4",
            "provider": "tiantianjiexi", "source_platform": "douyin", "size_bytes": 123,
        }
        self.receipt = issue_browser_download_audit(self.request, self.item)["download_audit"]
        self.network = patch("requests.sessions.Session.request", side_effect=AssertionError("Audit must not fetch media"))
        self.network.start()
        self.addCleanup(self.network.stop)

    def payload(self, **changes):
        return {
            "schema": SCHEMA, "token": self.receipt["token"], "phase": "handed_off",
            "transport": "browser_native", "actual_bytes": 0, "error_code": "", **changes,
        }

    def post(self, payload=None, csrf=True, **extra):
        headers = {"REMOTE_ADDR": "203.0.113.14", "HTTP_USER_AGENT": "Audit browser", **extra}
        if csrf:
            headers["HTTP_X_CSRFTOKEN"] = self.csrf_token
        return self.client.post(self.url, data=json.dumps(payload or self.payload()), content_type="application/json", **headers)

    def test_valid_http_event_is_idempotent_and_private(self):
        for _ in range(2):
            response = self.post()
            self.assertEqual(response.status_code, 204)
            self.assertEqual(response["Cache-Control"], "no-store")
            self.assertEqual(response.content, b"")
        event = DownloadAuditEvent.objects.get(phase="handed_off")
        self.assertEqual(event.evidence_source, "client_observed")
        self.assertEqual(DownloadAuditEvent.objects.count(), 2)
        self.assertFalse(DownloadRequestLog.objects.exists())

    def test_csrf_missing_and_invalid_rejected_before_audit(self):
        self.assertEqual(self.post(csrf=False).status_code, 403)
        self.csrf_token = "invalid"
        self.assertEqual(self.post().status_code, 403)
        self.assertEqual(DownloadAuditEvent.objects.count(), 1)

    def test_fake_token_cross_identity_and_expired_token_rejected(self):
        self.assertEqual(self.post(self.payload(token="fake-token")).status_code, 400)
        self.assertEqual(self.post(REMOTE_ADDR="203.0.113.15").status_code, 400)
        self.assertEqual(self.post(HTTP_USER_AGENT="other-browser").status_code, 400)
        with patch("django.core.signing.time.time", return_value=time.time() + TOKEN_MAX_AGE + 2):
            self.assertEqual(self.post().status_code, 400)
        self.assertEqual(DownloadAuditEvent.objects.count(), 1)

    def test_native_false_completion_and_server_claim_rejected(self):
        for changes in (
            {"phase": "transfer_completed"}, {"phase": "transfer_started"},
            {"actual_bytes": 123}, {"transport": "server_attachment"},
            {"transport": "server_zip"}, {"transport": "server_merge"},
            {"phase": "failed", "transport": ""},
        ):
            with self.subTest(changes=changes):
                self.assertEqual(self.post(self.payload(**changes)).status_code, 400)
        self.assertEqual(DownloadAuditEvent.objects.count(), 1)

    def test_unknown_fields_and_invalid_bounds_rejected(self):
        for changes in (
            {"provider": "native"}, {"evidence_source": "server_observed"},
            {"attempt_id": "other"}, {"url": "https://media.example/other"},
            {"actual_bytes": MAX_BYTES + 1}, {"actual_bytes": -1},
            {"error_code": "x" * 81}, {"token": "x" * 4097},
        ):
            with self.subTest(changes=changes):
                self.assertEqual(self.post(self.payload(**changes)).status_code, 400)
        self.assertEqual(DownloadAuditEvent.objects.count(), 1)

    def test_sw_and_blob_http_completion_is_only_client_observed(self):
        for transport in ("browser_streaming", "browser_blob"):
            self.assertEqual(self.post(self.payload(
                phase="transfer_completed", transport=transport, actual_bytes=123,
            )).status_code, 204)
        self.assertEqual(DownloadAuditEvent.objects.filter(
            phase="transfer_completed", evidence_source="client_observed", actual_bytes=123,
        ).count(), 2)
        self.assertFalse(RoutingDecisionLog.objects.exists())
        self.assertFalse(DownloadRequestLog.objects.exists())

    def test_storage_failure_is_non_blocking_and_does_not_claim_success(self):
        with patch("xhs_download.services.download_audit.record_transfer_audit", return_value=None):
            self.assertEqual(self.post().status_code, 204)
        with patch("xhs_download.services.download_audit.DownloadAuditEvent.objects.filter", side_effect=RuntimeError("audit unavailable")):
            self.assertEqual(self.post().status_code, 204)
        self.assertEqual(DownloadAuditEvent.objects.count(), 1)
        self.assertFalse(DownloadRequestLog.objects.exists())

    def test_rate_limit_and_content_type_boundaries_remain(self):
        self.assertEqual(self.client.get(self.url).status_code, 405)
        response = self.client.post(self.url, data="{}", content_type="text/plain", HTTP_X_CSRFTOKEN=self.csrf_token)
        self.assertEqual(response.status_code, 415)
        with override_settings(BROWSER_DOWNLOAD_TELEMETRY_RATE_LIMIT_PER_MINUTE=1):
            self.assertEqual(self.post().status_code, 204)
            self.assertEqual(self.post().status_code, 429)
        self.assertEqual(DownloadAuditEvent.objects.count(), 2)
        self.assertFalse(DownloadRequestLog.objects.exists())


class DownloadAuditOperationalIsolationTests(TestCase):
    def setUp(self):
        from download_console.models import DownloadRuntimeSetting
        from .models import ProviderRuntimeSetting

        cache.clear()
        DownloadRuntimeSetting.load()
        ProviderRuntimeSetting.load()
        self.legacy = DownloadRequestLog.objects.create(
            request_id="legacy", status="success", route_type="proxy", provider="tiantianjiexi",
            final_policy_route="proxy", actual_bytes=100, estimated_bytes=100,
            server_egress_estimated_bytes=100,
        )
        self.completed = DownloadRequestLog.objects.create(
            request_id="server-end", attempt_id="server-complete", phase="transfer_completed",
            transport="server_attachment", evidence_source="server_observed",
            status="success", route_type="proxy", final_policy_route="proxy",
            provider="tiantianjiexi", actual_bytes=200, estimated_bytes=200,
            server_egress_estimated_bytes=200,
        )

    def add_noise(self, model=DownloadRequestLog):
        from django.utils import timezone

        for phase in ("prepared", "handed_off", "transfer_started", "transfer_completed", "failed", "cancelled", "unknown", ""):
            model.objects.create(
                request_id="client-noise", attempt_id="client-noise", phase=phase,
                transport="browser_blob", evidence_source="client_observed", status="failed",
                provider="tiantianjiexi", route_type="zip", final_policy_route="zip",
                fallback_count=99, recovery_count=99, actual_bytes=2 ** 40,
                estimated_bytes=2 ** 40, server_egress_estimated_bytes=2 ** 40,
            )
        for phase in ("prepared", "handed_off", "transfer_started", "cancelled", "unknown"):
            model.objects.create(
                request_id="server-noise", attempt_id="server-pending", phase=phase,
                transport="server_attachment", evidence_source="server_observed", status="pending",
                provider="tiantianjiexi", route_type="zip", final_policy_route="zip",
                actual_bytes=2 ** 40, server_egress_estimated_bytes=2 ** 40,
            )
        model.objects.filter(request_id__in=("client-noise", "server-noise")).update(
            created_at=timezone.now() - timezone.timedelta(seconds=1),
        )

    def test_defensive_filter_retains_legacy_and_server_terminal_only(self):
        self.add_noise()
        self.assertSetEqual(
            set(operational_download_logs(DownloadRequestLog.objects.all()).values_list("pk", flat=True)),
            {self.legacy.pk, self.completed.pk},
        )
        self.assertEqual(DownloadRequestLog.objects.count(), 15)

    def test_defensive_legacy_terminal_filter_deduplicates_and_excludes_cancellation(self):
        failed = DownloadRequestLog.objects.create(
            attempt_id=self.completed.attempt_id, phase="failed", transport="server_attachment",
            evidence_source="server_observed", status="failed", actual_bytes=200,
        )
        records = operational_download_logs(DownloadRequestLog.objects.all())
        self.assertSetEqual(set(records.values_list("pk", flat=True)), {self.legacy.pk, failed.pk})
        DownloadRequestLog.objects.create(
            attempt_id=self.completed.attempt_id, phase="cancelled", transport="server_attachment",
            evidence_source="server_observed", status="cancelled", actual_bytes=200,
        )
        self.assertEqual(list(records.values_list("pk", flat=True)), [self.legacy.pk])
        cards = {row["label"]: row["value"] for row in build_admin_console_context()["cards"]}
        self.assertEqual(cards["今日下载实际流量（服务端已发送）"], "0 B")
        self.assertEqual(cards["今日下载次数（去重尝试）"], 0)

    def test_event_noise_does_not_change_legacy_rows_or_admin_business_metrics(self):
        legacy_rows = list(DownloadRequestLog.objects.order_by("pk").values())
        before = build_admin_console_context()
        self.add_noise(DownloadAuditEvent)
        DownloadAuditEvent.objects.create(
            attempt_id="audit-only-server-complete", phase="transfer_completed",
            transport="server_attachment", evidence_source="server_observed", status="success",
            actual_bytes=4096, provider="audit-only", route_type="zip", final_policy_route="zip",
        )
        after = build_admin_console_context()
        self.assertEqual(list(DownloadRequestLog.objects.order_by("pk").values()), legacy_rows)
        self.assertEqual(DownloadRequestLog.objects.count(), 2)
        self.assertEqual(DownloadAuditEvent.objects.count(), 14)
        self.assertEqual(operational_download_logs(DownloadRequestLog.objects.all()).count(), 2)
        for key in ("routing_summary", "system_stability", "failure_top", "blocked_rows"):
            with self.subTest(panel=key):
                self.assertEqual(after[key], before[key])
        before_cards = {row["label"]: row["value"] for row in before["cards"]}
        after_cards = {row["label"]: row["value"] for row in after["cards"]}
        for label in (
            "今日 legacy 行数（旧口径，不计新完成率）",
            "今日 legacy 记录流量（未复核）", "今日限流 / 阻止次数",
        ):
            self.assertEqual(after_cards[label], before_cards[label])
        self.assertEqual(after_cards["今日下载次数（去重尝试）"], 3)

    def test_operational_consumers_unchanged_by_client_and_stage_noise(self):
        from django.utils import timezone
        from .policy.adaptive import build_policy_feedback_summary, build_adaptive_weight_recommendation
        from .policy.learning import build_policy_learning_snapshot
        from .services.autonomous_evolution_engine import build_autonomous_evolution_context
        from .services.control_plane import build_core_metrics, build_download_cost_status, provider_log_stats
        from .services.feedback_loop import build_feedback
        from .services.governance_engine import build_governance_v8_context
        from .services.policy_risk_engine import policy_area_historical_instability
        from .services.production_intelligence import build_observability_v2_context
        from .services.rollout_guard import build_rollout_guard_context
        from .services.stabilization_engine import build_stabilization_snapshot, build_stability_trend

        request = RequestFactory().get("/")
        request.user = SimpleNamespace(is_staff=False, is_active=False, is_authenticated=False)
        readers = {
            "learning": build_policy_learning_snapshot,
            "adaptive_feedback": build_policy_feedback_summary,
            "adaptive_weights": build_adaptive_weight_recommendation,
            "feedback_loop": build_feedback,
            "production_intelligence": build_observability_v2_context,
            "governance": build_governance_v8_context,
            "autonomous_evolution": build_autonomous_evolution_context,
            "stabilization": build_stabilization_snapshot,
            "stability_trend": build_stability_trend,
            "risk_cost": lambda: policy_area_historical_instability(impact_area="cost"),
            "risk_provider": lambda: policy_area_historical_instability(impact_area="provider"),
            "risk_routing": lambda: policy_area_historical_instability(impact_area="routing"),
            "control_core": build_core_metrics,
            "control_cost": build_download_cost_status,
            "control_provider": lambda: provider_log_stats(("tiantianjiexi",)),
            "rollout": lambda: build_rollout_guard_context(request),
        }
        with patch("django.utils.timezone.now", return_value=timezone.now()):
            before = {name: reader() for name, reader in readers.items()}
            legacy_rows = list(DownloadRequestLog.objects.order_by("pk").values())
            for model in (DownloadAuditEvent, DownloadRequestLog):
                self.add_noise(model)
                cache.clear()
                if model is DownloadAuditEvent:
                    self.assertEqual(list(DownloadRequestLog.objects.order_by("pk").values()), legacy_rows)
                    self.assertEqual(DownloadRequestLog.objects.count(), 2)
                    self.assertEqual(DownloadAuditEvent.objects.count(), 13)
                for name, reader in readers.items():
                    with self.subTest(storage=model.__name__, consumer=name):
                        self.assertEqual(reader(), before[name])
