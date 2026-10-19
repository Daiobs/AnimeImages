"""Offline history acceptance tests; never run migrations against the daily DB."""

from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timezone
import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import Client, SimpleTestCase, TestCase, TransactionTestCase, override_settings
from django.urls import reverse

from .models import EvaluationBatch, EvaluationExecution, UpstreamRoute, UpstreamRouteAudit
from .services.provider_lab import history
from .services.provider_lab.contracts import LabAssetV1, LabProviderResultV1, LabRelationshipV1
from .services.upstream_routes import read_route, route_matches


SOURCE = "https://www.douban.com/group/topic/123/"
PROVIDER = "parsehub_repo"
AVAILABLE_DESCRIPTOR = {"provider_id": PROVIDER, "execution_provider_id": "parsehub_generic",
                        "available": True, "route_selectable": True}
LOCAL_CACHE = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache",
                           "LOCATION": "evaluation-history-tests"}}


def execution(key="exec-1", **changes):
    row = {"execution_key": key, "sample_key": "sample-1", "source_url": SOURCE,
           "provider_id": PROVIDER, "state": "returned", "title": "Offline fixture",
           "started_at": "2026-09-01T01:02:03Z", "version": {"scout_head": "old-head"},
           "result": {"status": "success", "executed": True, "request_id": key,
                      "assets": [{"asset_id": "asset-1", "height": 1080}]}}
    row.update(changes)
    return row


def envelope(*rows, key="fixture-batch"):
    return {"schema_version": 1, "batch": {"batch_key": key, "label": "Offline batch",
            "metadata": {"scout_head": "old-head"}}, "executions": list(rows or [execution()])}


class NativeEvidenceSummaryTests(SimpleTestCase):
    def test_native_summary_retains_267_candidates_and_metadata_without_resource_authority(self):
        metadata = {"evidence_kind": "native_capture_replay", "target_identity": {"id": "fixture-note"},
                    "identity_confirmed": False, "capture_status": "captured", "conversion_status": "converted",
                    "integrity": {"complete": False}, "scope": "captured subset", "limitations": ["offline only"]}
        row = execution(result={"status": "native_observed", "metadata": metadata,
            "assets": [{"asset_id": f"native-{i}", "media_type": "image", "resource_token": "not-authority",
                        "url": "https://media.example/private"} for i in range(267)]})
        result = history.execution_summary(row)["result"]
        self.assertEqual(result["status"], "native_observed")
        self.assertEqual(result["metadata"], metadata)
        self.assertEqual(len(result["assets"]), 267)
        self.assertTrue(all(set(asset) == {"asset_id", "media_type"} for asset in result["assets"]))

    def test_native_counts_are_separate_from_product_success_and_execution(self):
        for marker in ("status", "result_metadata", "batch_metadata"):
            with self.subTest(marker=marker):
                native = execution("native", result={"status": "native_observed" if marker == "status" else "success",
                    "executed": True, "metadata": {"evidence_kind": "native_capture_replay"} if marker == "result_metadata" else {}})
                rows = [native] if marker == "batch_metadata" else [native, execution("product")]
                manager = Mock()
                manager.values_list.return_value = rows
                batch = SimpleNamespace(pk=1, batch_key="fixture", label="Fixture", origin="scout",
                    metadata={"evidence_kind": "native_capture_replay"} if marker == "batch_metadata" else {},
                    created_at=datetime(2026, 10, 16, tzinfo=timezone.utc), executions=manager)
                counts = history.batch_dict(batch)["counts"]
                self.assertEqual(counts["native_observed"], 1)
                self.assertEqual(counts["success"], 0 if marker == "batch_metadata" else 1)
                self.assertEqual(counts["executed"], counts["success"])
                self.assertEqual(counts["failed"], 0)
                self.assertEqual(counts["not_executed"], 0)


@override_settings(UPSTREAM_PROVIDER_LAB_ENABLED=True, CACHES=LOCAL_CACHE)
class EvaluationHistoryTests(TestCase):
    def setUp(self):
        cache.clear()
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.run = stack.enter_context(patch("xhs_download.provider_lab_views.run_provider",
            side_effect=AssertionError("Unexpected provider execution")))
        self.execute = stack.enter_context(patch("xhs_download.services.quality_execution.execute_provider",
            side_effect=AssertionError("Unexpected shared executor call")))
        self.issue = stack.enter_context(patch("xhs_download.services.provider_lab.resources.issue_resource_token",
            side_effect=AssertionError("History must not mint media authorization")))
        self.descriptors = stack.enter_context(patch(
            "xhs_download.services.provider_lab.providers.list_provider_descriptors",
            return_value=[dict(AVAILABLE_DESCRIPTOR)]))
        stack.enter_context(patch("socket.socket.connect", side_effect=AssertionError("Network forbidden")))
        stack.enter_context(patch.object(history, "current_version", return_value={"animeimages_head": "new-head"}))
        self.staff = get_user_model().objects.create_user(username="history-staff", is_staff=True)
        self.client.force_login(self.staff)
        self.url = reverse("provider_lab_history")

    def post(self, payload, url=None, client=None):
        return (client or self.client).post(url or self.url, json.dumps(payload),
                                          content_type="application/json")

    def imported(self, row=None):
        response = self.post(envelope(row or execution()))
        self.assertEqual(response.status_code, 200, response.content)
        return EvaluationExecution.objects.get()

    def run_payload(self, parent=None):
        payload = {"source": SOURCE, "provider_id": PROVIDER,
                   "confirmations": {"external_sharing": True}}
        if parent is not None:
            payload["history_execution_id"] = parent
        return payload

    def test_import_requires_active_staff_and_feature_flag(self):
        self.client.logout()
        self.assertEqual(self.post(envelope()).status_code, 404)
        for active, staff in ((True, False), (False, True)):
            user = get_user_model().objects.create_user(username=f"user-{active}",
                                                       is_active=active, is_staff=staff)
            self.client.force_login(user)
            self.assertEqual(self.post(envelope()).status_code, 404)
            self.assertEqual(self.client.get(self.url).status_code, 404)
        self.client.force_login(self.staff)
        with override_settings(UPSTREAM_PROVIDER_LAB_ENABLED=False):
            self.assertEqual(self.post(envelope()).status_code, 404)
        self.assertFalse(EvaluationBatch.objects.exists())

    def test_import_csrf_rejection_and_valid_token(self):
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(self.staff)
        self.assertEqual(self.post(envelope(), client=strict).status_code, 403)
        token = "a" * 32
        strict.cookies["csrftoken"] = token
        response = strict.post(self.url, json.dumps(envelope()), content_type="application/json",
                               HTTP_X_CSRFTOKEN=token)
        self.assertEqual(response.status_code, 200)

    def test_duplicate_incremental_import_and_atomic_conflict(self):
        first = self.post(envelope()).json()
        self.assertEqual(first["created"], 1)
        repeated = self.post(envelope()).json()
        self.assertEqual((repeated["created"], repeated["unchanged"]), (0, 1))
        self.assertEqual(self.post(envelope(execution(), execution("exec-2"))).json()["created"], 1)
        before = list(EvaluationExecution.objects.values("id", "summary", "fingerprint"))
        response = self.post(envelope(execution("exec-3"), execution(title="changed")))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error_code"], "HISTORY_EXECUTION_CONFLICT")
        self.assertEqual(list(EvaluationExecution.objects.values("id", "summary", "fingerprint")), before)
        self.assertEqual(EvaluationBatch.objects.count(), 1)
        self.run.assert_not_called()

    def test_batch_metadata_conflict_does_not_overwrite(self):
        self.imported()
        payload = envelope()
        payload["batch"]["metadata"]["scout_head"] = "different-head"
        self.assertEqual(self.post(payload).status_code, 409)
        self.assertEqual(EvaluationBatch.objects.get().metadata["scout_head"], "old-head")

    def test_invalid_payload_limits_and_rollback(self):
        for payload in ([], {}, envelope(execution(sample_key=None)), envelope(*[execution(str(i)) for i in range(201)]),
                        envelope(execution(result={"assets": [{}] * 1001})),
                        envelope(execution(result={"assets": ["invalid"]}))):
            with self.subTest(payload_type=type(payload).__name__):
                self.assertEqual(self.post(payload).status_code, 400)
                self.assertFalse(EvaluationBatch.objects.exists())
        with patch.object(history, "MAX_IMPORT_BYTES", 16):
            self.assertEqual(self.post(envelope()).status_code, 413)

    def test_malformed_summary_import_rejects_and_rolls_back_new_batch(self):
        for changes, code in (({"replay_of": []}, "HISTORY_EXECUTION_INVALID"),
                              ({"result": {"status": {}}}, "HISTORY_RESULT_INVALID")):
            with self.subTest(changes=changes):
                payload = envelope(execution("valid-first"), execution("malformed-second", **changes))
                response = self.post(payload)
                self.assertEqual(response.status_code, 400, response.content)
                self.assertEqual(response.json()["error_code"], code)
                self.assertFalse(EvaluationBatch.objects.exists())
                self.assertFalse(EvaluationExecution.objects.exists())
        self.run.assert_not_called()
        self.execute.assert_not_called()

    def test_malformed_incremental_import_rolls_back_append_and_preserves_existing(self):
        self.imported()
        before_batches = list(EvaluationBatch.objects.order_by("pk").values())
        before_executions = list(EvaluationExecution.objects.order_by("pk").values())
        for changes in ({"replay_of": []}, {"result": {"status": {}}}):
            with self.subTest(changes=changes):
                payload = envelope(execution(), execution("valid-append"),
                                   execution("malformed-append", **changes))
                response = self.post(payload)
                self.assertEqual(response.status_code, 400, response.content)
                self.assertEqual(list(EvaluationBatch.objects.order_by("pk").values()), before_batches)
                self.assertEqual(list(EvaluationExecution.objects.order_by("pk").values()), before_executions)
        self.run.assert_not_called()
        self.execute.assert_not_called()

    def test_stored_malformed_summaries_do_not_break_history_reads(self):
        healthy = self.imported()
        for index, changes in enumerate(({"replay_of": []}, {"result": {"status": {}}})):
            with self.subTest(changes=changes):
                batch = EvaluationBatch.objects.create(batch_key=f"scout:legacy-malformed-{index}",
                                                       label="Legacy malformed fixture", origin="scout")
                valid_summary = history.execution_summary(execution(f"valid-{index}"))
                valid = EvaluationExecution.objects.create(batch=batch, execution_key=f"valid-{index}",
                    summary=valid_summary, fingerprint=history.digest(valid_summary))
                # Bypass today's importer to represent already-persisted legacy data.
                malformed = history.execution_summary(execution(f"malformed-{index}"))
                malformed.update(deepcopy(changes))
                EvaluationExecution.objects.create(batch=batch, execution_key=f"malformed-{index}",
                    summary=malformed, fingerprint=history.digest(malformed))
                before = list(EvaluationExecution.objects.order_by("pk").values())
                cache.clear()
                listing = self.client.get(self.url)
                self.assertEqual(listing.status_code, 200, listing.content)
                self.assertTrue(listing.json()["ok"])
                self.assertTrue({healthy.batch_id, batch.pk}.issubset(
                    {item["id"] for item in listing.json()["batches"]}))
                for batch_id, expected in ((healthy.batch_id, healthy), (batch.pk, valid)):
                    detail = self.client.get(self.url, {"batch_id": batch_id})
                    self.assertEqual(detail.status_code, 200, detail.content)
                    self.assertTrue(detail.json()["ok"])
                    rows = {item["id"]: item for item in detail.json()["executions"]}
                    self.assertEqual(rows[expected.pk]["result"], expected.summary["result"])
                self.assertEqual(list(EvaluationExecution.objects.order_by("pk").values()), before)
        self.run.assert_not_called()
        self.execute.assert_not_called()
        self.issue.assert_not_called()

    def test_sensitive_source_and_media_credentials_not_persisted(self):
        row = execution(source_url=SOURCE + "?xsec_token=source-secret", options={"cookie": "cookie-secret"})
        row["result"]["metadata"] = {"Authorization": "auth-secret", "safe": "kept"}
        row["result"]["assets"][0].update(resource_token="expired-token", url="https://cdn.example/a?token=media-secret",
                                            headers={"Cookie": "cookie-secret"})
        record = self.imported(row)
        stored = json.dumps(record.summary)
        for secret in ("source-secret", "cookie-secret", "auth-secret", "expired-token", "media-secret"):
            self.assertNotIn(secret, stored)
        self.assertEqual(record.summary["source_url"], "")
        self.assertFalse(history.execution_dict(record)["can_retest"])
        self.assertEqual(record.summary["result"]["assets"], [{"asset_id": "asset-1", "height": 1080}])
        cache.clear()
        self.assertEqual(self.client.get(self.url, {"batch_id": record.batch_id}).status_code, 200)
        self.issue.assert_not_called()

    def test_source_url_rejects_local_credentials_and_script_schemes(self):
        for source in ("file:///tmp/private", "javascript:alert(1)", "http://127.0.0.1/private",
                       "https://user:password@example.com/a", "http://localhost/a"):
            with self.subTest(source=source):
                cleaned, host, state = history.source_info(source)
                self.assertEqual((cleaned, host), ("", ""))
                self.assertNotEqual(state, "RETAINED")

    def test_missing_unknown_and_thousand_candidates_are_preserved(self):
        rows = [execution("unknown", source_url=None, state="unknown", result={}),
                execution("full", result={"status": "success", "candidate_count": 1000,
                    "assets": [{"asset_id": str(i), "height": None, "audio_tracks": "unknown"} for i in range(1000)]}),
                execution("old", result={"candidate_count": 1000, "assets": [{"asset_id": str(i)} for i in range(64)],
                    "retention": {"truncated": True, "retained_count": 64}})]
        batch_id = self.post(envelope(*rows)).json()["batch_id"]
        data = self.client.get(self.url, {"batch_id": batch_id}).json()["executions"]
        self.assertNotIn("assets", data[0]["result"])
        self.assertNotIn("cache_hit", data[0]["result"])
        self.assertFalse(data[0]["can_retest"])
        self.assertEqual(len(data[1]["result"]["assets"]), 1000)
        self.assertIsNone(data[1]["result"]["assets"][999]["height"])
        self.assertEqual(data[2]["result"]["retention"], {"truncated": True, "retained_count": 64})

    def test_counts_keep_planned_unexecuted_unknown_and_original_status(self):
        rows = [execution(str(i), state=state, result=result) for i, (state, result) in enumerate([
            ("planned", {}), ("not_executed", {"executed": False}), ("unknown", {}),
            ("returned", {"status": "success", "executed": True}),
            ("returned", {"status": "unsupported", "executed": True}),
            ("returned", {"status": "failed", "executed": True, "error_detail": "AUTH HTML"})])]
        batch_id = self.post(envelope(*rows)).json()["batch_id"]
        data = self.client.get(self.url, {"batch_id": batch_id}).json()
        self.assertEqual(data["batch"]["counts"], {"planned": 1, "not_executed": 1, "unknown": 1,
            "executed": 3, "success": 1, "unsupported": 1, "failed": 1, "native_observed": 0})
        self.assertEqual(data["executions"][-1]["result"]["error_detail"], "AUTH HTML")

    def test_actual_lab_contract_preserves_backend_relationships_without_raw_shape(self):
        result = LabProviderResultV1(request_id="contract-request", provider_id=PROVIDER,
            provider_version="v1", provider_scope="generic", status="success",
            selected_backend="FixtureBackend", claimed_platform="fixture-platform",
            assets=(LabAssetV1(asset_id="video", resource_token="expired-private-token"),
                    LabAssetV1(asset_id="audio")),
            relationships=(LabRelationshipV1("paired_audio", "video", "audio"),),
            warnings=("Missing dimensions",), raw_shape={"debug": "private-shape-marker"}).as_dict()
        record = self.imported(execution(result=result))
        stored = record.summary["result"]
        for field in ("selected_backend", "claimed_platform", "relationships", "warnings", "provider_scope"):
            self.assertEqual(stored[field], result[field])
        self.assertNotIn("raw_shape", stored)
        self.assertNotIn("private-shape-marker", json.dumps(stored))
        self.assertNotIn("expired-private-token", json.dumps(stored))

    def test_returned_but_unexecuted_lab_result_counts_as_not_executed(self):
        record = history.begin_execution(SOURCE, PROVIDER, None)
        history.finish_execution(record, {"status": "failed", "executed": False,
            "error_code": "PROVIDER_POLICY_REJECTED", "unexecuted_reason": "CONFIRMATION_REQUIRED"})
        counts = history.batch_dict(record.batch)["counts"]
        self.assertEqual(counts["executed"], 0)
        self.assertEqual(counts["not_executed"], 1)

    def test_returned_unknown_service_outcome_cannot_retest(self):
        record = self.imported(execution(result={"status": "failed", "executed": False,
            "error_code": "PROVIDER_POLICY_REJECTED", "error_detail": "SERVICE_PREVIOUS_OUTCOME_UNKNOWN"}))
        self.assertFalse(history.execution_dict(record)["can_retest"])
        response = self.post(self.run_payload(record.pk), reverse("provider_lab_run"))
        self.assertEqual(response.status_code, 400)
        self.run.assert_not_called()

    def test_history_reads_never_execute_or_write_routes(self):
        record = self.imported(execution(provider_id="removed-provider"))
        cache.clear()
        for query in ({}, {"batch_id": record.batch_id}):
            response = self.client.get(self.url, query)
            self.assertEqual(response.status_code, 200)
            self.assertIn("no-store", response["Cache-Control"])
            self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        self.run.assert_not_called()
        self.execute.assert_not_called()
        self.issue.assert_not_called()
        self.assertFalse(UpstreamRoute.objects.exists())
        self.assertFalse(UpstreamRouteAudit.objects.exists())

    def test_import_links_existing_application_record_without_changing_provenance(self):
        app_record = history.begin_execution(SOURCE, PROVIDER, None)
        history.finish_execution(app_record, {"request_id": "shared-id", "status": "success"})
        row = execution(result={"request_id": "shared-id", "status": "success"})
        self.assertEqual(self.post(envelope(row)).status_code, 200)
        imported = EvaluationExecution.objects.get(batch__origin="scout")
        self.assertEqual(imported.application_record_id, app_record.pk)
        self.assertEqual(history.execution_dict(imported)["origin"], "scout")

    def test_host_history_covers_batches_and_new_lab_without_writes_or_authority(self):
        for key in ("older", "newer"):
            self.assertEqual(self.post(envelope(execution(key), key=key)).status_code, 200)
        self.post(envelope(execution("other", source_url="https://douban.com/work"), key="other"))
        app = history.begin_execution(SOURCE, PROVIDER, None)
        history.finish_execution(app, {"request_id": "shared", "status": "success",
            "assets": [{"asset_id": "one", "resource_token": "secret", "url": "https://media.invalid/private"}]})
        self.post(envelope(execution("copy", result={"request_id": "shared", "status": "success"}), key="copy"))
        before = (EvaluationBatch.objects.count(), EvaluationExecution.objects.count())
        response = self.client.get(self.url, {"source_host": "www.douban.com"})
        data = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual((data["total"], data["returned"], data["truncated"]), (4, 4, False))
        self.assertEqual([r["id"] for r in data["executions"]],
                         list(EvaluationExecution.objects.filter(summary__source_host="www.douban.com").order_by("-pk").values_list("pk", flat=True)))
        copy = data["executions"][0]
        self.assertEqual((copy["origin"], copy["batch"]["origin"], copy["application_record_id"]), ("scout", "scout", app.pk))
        self.assertEqual(set(copy["batch"]), {"id", "batch_key", "label", "origin", "created_at", "metadata"})
        self.assertNotIn("resource_token", response.content.decode())
        self.assertNotIn("media.invalid", response.content.decode())
        self.assertIn("no-store", response["Cache-Control"])
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        self.assertEqual(before, (EvaluationBatch.objects.count(), EvaluationExecution.objects.count()))
        self.assertFalse(UpstreamRoute.objects.exists())
        self.assertFalse(UpstreamRouteAudit.objects.exists())
        self.run.assert_not_called()
        self.execute.assert_not_called()
        self.issue.assert_not_called()
        fresh = history.begin_execution(SOURCE, PROVIDER, None, replay_of=app.pk)
        refreshed = self.client.get(self.url, {"source_host": "www.douban.com"}).json()
        self.assertEqual(refreshed["total"], 5)
        self.assertEqual(refreshed["executions"][0]["id"], fresh.pk)

    def test_host_history_rejects_noncanonical_private_and_ambiguous_hosts(self):
        for host in ("", "WWW.douban.com", "www.douban.com.", "https://www.douban.com", "www.douban.com/path",
                     "www.douban.com:443", "user@www.douban.com", "www.douban.com?x=1", "localhost",
                     "a.localhost", "a.local", "a.internal", "a.invalid", "127.0.0.1", "8.8.8.8", "[::1]",
                     "a..com", "-a.com", "a-.com", "a_foo.com", "a.123", "xn--.com", "a" * 64 + ".com"):
            with self.subTest(host=host):
                self.assertEqual(self.client.get(self.url, {"source_host": host}).status_code, 400)
        for batch_id in ("", "1"):
            self.assertEqual(self.client.get(self.url, {"source_host": "www.douban.com", "batch_id": batch_id}).status_code, 400)
        self.assertEqual(self.client.get(self.url, {"source_host": ["www.douban.com", "douban.com"]}).status_code, 400)
        self.assertEqual(self.client.get(self.url, {"source_host": "xn--bcher-kva.de"}).status_code, 200)
        self.assertEqual(self.client.get(self.url, {"source_host": "empty.douban.com"}).json()["total"], 0)

    def test_host_history_access_gate_includes_readonly_staff(self):
        self.assertFalse(self.staff.has_perm("xhs_download.change_upstreamroute"))
        self.assertEqual(self.client.get(self.url, {"source_host": "www.douban.com"}).status_code, 200)
        with override_settings(UPSTREAM_PROVIDER_LAB_ENABLED=False):
            self.assertEqual(self.client.get(self.url, {"source_host": "www.douban.com"}).status_code, 404)
        self.client.logout()
        self.assertEqual(self.client.get(self.url, {"source_host": "www.douban.com"}).status_code, 404)
        for active, staff in ((True, False), (False, True)):
            user = get_user_model().objects.create_user(username=f"host-{active}", is_active=active, is_staff=staff)
            self.client.force_login(user)
            self.assertEqual(self.client.get(self.url, {"source_host": "www.douban.com"}).status_code, 404)

    def test_host_history_row_limit_and_light_batch_metadata(self):
        batch = EvaluationBatch.objects.create(batch_key="large", origin="scout", metadata={
            "targets": ["omitted"], "evidence_kind": "native_capture_replay", "version": "v1"})
        EvaluationExecution.objects.bulk_create([EvaluationExecution(batch=batch, execution_key=str(i),
            summary=history.execution_summary(execution(str(i))), fingerprint="test") for i in range(201)])
        response = self.client.get(self.url, {"source_host": "www.douban.com"})
        data = response.json()
        self.assertEqual((data["total"], data["returned"], data["truncated"]), (201, 200, True))
        self.assertLessEqual(len(response.content), 2 * 1024 * 1024)
        self.assertEqual(data["executions"][0]["batch"]["metadata"], {"version": "v1", "evidence_kind": "native_capture_replay"})
        with patch.object(history, "batch_dict", side_effect=AssertionError("No whole-batch aggregation")):
            self.assertEqual(self.client.get(self.url, {"source_host": "www.douban.com"}).status_code, 200)
        batch.executions.order_by("pk").first().delete()
        self.assertFalse(self.client.get(self.url, {"source_host": "www.douban.com"}).json()["truncated"])

    def test_host_history_byte_limit_keeps_whole_records_and_explicit_empty_truncation(self):
        record = self.imported()
        summary = deepcopy(record.summary)
        summary["result"]["assets"] = [{"asset_id": "large", "metadata": "x" * (1024 * 1024)}]
        record.summary = summary
        record.save(update_fields=["summary"])
        EvaluationExecution.objects.create(batch=record.batch, execution_key="second", summary=summary, fingerprint="test")
        response = self.client.get(self.url, {"source_host": "www.douban.com"})
        self.assertLessEqual(len(response.content), 2 * 1024 * 1024)
        self.assertEqual((response.json()["total"], response.json()["returned"], response.json()["truncated"]), (2, 1, True))
        self.assertEqual(response.json()["executions"][0]["result"]["assets"], summary["result"]["assets"])
        with patch("xhs_download.provider_lab_views.HOST_HISTORY_MAX_BYTES", 2048):
            data = self.client.get(self.url, {"source_host": "www.douban.com"}).json()
        self.assertEqual((data["total"], data["returned"], data["truncated"]), (2, 0, True))

    def test_unknown_retest_is_rejected_without_provider_call(self):
        for state in ("unknown", "sent", "reserved", "in_flight", "running"):
            with self.subTest(state=state):
                payload = envelope(execution(state=state), key=state)
                self.post(payload)
                record = EvaluationExecution.objects.get(batch__batch_key="scout:" + state)
                response = self.post(self.run_payload(record.pk), reverse("provider_lab_run"))
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()["error_code"], "PROVIDER_POLICY_REJECTED")
        self.run.assert_not_called()

    def test_retest_state_admission_is_normalized_and_fail_closed(self):
        record = self.imported()
        for state in ("queued", "waiting", "UNKNOWN", " Running ", "WAITING", "QUEUED",
                      None, "", "  ", 1, True, [], {}, "unrecognized"):
            with self.subTest(state=state):
                record.summary["state"] = state
                record.save(update_fields=["summary"])
                before = deepcopy(record.summary)
                self.assertFalse(history.retest_allowed(record))
                response = self.post(self.run_payload(record.pk), reverse("provider_lab_run"))
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()["error_code"], "PROVIDER_POLICY_REJECTED")
                self.run.assert_not_called()
                record.refresh_from_db()
                self.assertEqual(record.summary, before)

        record.summary.pop("state")
        record.save(update_fields=["summary"])
        before = deepcopy(record.summary)
        self.assertFalse(history.retest_allowed(record))
        response = self.post(self.run_payload(record.pk), reverse("provider_lab_run"))
        self.assertEqual(response.status_code, 400)
        self.run.assert_not_called()
        record.refresh_from_db()
        self.assertEqual(record.summary, before)

    def test_retest_terminal_and_unexecuted_states_preserve_unknown_outcome_gate(self):
        record = self.imported()
        for state in ("returned", "SUCCESS", " failed ", "timeout", "unsupported",
                      "not_executed", "planned", "skipped", "cancelled"):
            with self.subTest(state=state):
                record.summary["state"] = state
                record.summary["result"] = {}
                self.assertTrue(history.retest_allowed(record))
                for key, value in (("error_detail", "SERVICE_PREVIOUS_OUTCOME_UNKNOWN"),
                                   ("client_status", "OUTCOME_UNKNOWN")):
                    record.summary["result"] = {key: value}
                    self.assertFalse(history.retest_allowed(record))

    def test_retest_only_selected_provider_and_preserves_parent(self):
        parent = self.imported()
        original = deepcopy(parent.summary)
        fingerprint = parent.fingerprint
        self.run.side_effect = None
        self.run.return_value = {"status": "success", "provider_id": PROVIDER, "request_id": "new-request", "assets": []}
        response = self.post(self.run_payload(parent.pk), reverse("provider_lab_run"))
        self.assertEqual(response.status_code, 200, response.content)
        self.run.assert_called_once_with(provider_id=PROVIDER, source=SOURCE, user_id=self.staff.pk,
                                         options=None, confirmations={"external_sharing": True})
        parent.refresh_from_db()
        self.assertEqual((parent.summary, parent.fingerprint), (original, fingerprint))
        child = EvaluationExecution.objects.get(pk=response.json()["history"]["id"])
        self.assertNotEqual(child.pk, parent.pk)
        self.assertEqual(child.summary["replay_of"], str(parent.pk))
        self.assertEqual(child.summary["version"], {"animeimages_head": "new-head"})
        self.assertNotEqual(child.summary["started_at"], parent.summary["started_at"])
        self.assertEqual(parent.batch.executions.count(), 1)

    def test_history_write_failure_never_repeats_successful_execution(self):
        self.run.side_effect = None
        self.run.return_value = {"status": "success", "request_id": "ok", "assets": []}
        for method in ("begin_execution", "finish_execution"):
            with self.subTest(method=method), patch.object(history, method, side_effect=RuntimeError("DB unavailable")):
                self.run.reset_mock()
                response = self.post(self.run_payload(), reverse("provider_lab_run"))
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["status"], "success")
                self.assertEqual(response.json()["history"], {"saved": False, "error_code": "HISTORY_SAVE_FAILED"})
                self.run.assert_called_once()

    def test_invalid_retest_identity_is_controlled_bad_request(self):
        for invalid in ("not-an-id", {}, [], True, 1.5):
            with self.subTest(identity=invalid):
                response = self.post(self.run_payload(invalid), reverse("provider_lab_run"))
                self.assertEqual(response.status_code, 400)
        self.run.assert_not_called()

    def route_payload(self, record, **changes):
        payload = {"source_url": SOURCE, "expected_revision": 0,
                   "provider_order": ["parsehub_generic"], "reference_execution_id": record.pk}
        payload.update(changes)
        return payload

    def test_reference_route_permissions_csrf_audit_conflict_and_cache_identity(self):
        record = self.imported()
        url = reverse("upstream_routes")
        payload = self.route_payload(record)
        self.assertEqual(self.post(payload, url).status_code, 403)
        self.staff.is_superuser = True
        self.staff.save(update_fields=["is_superuser"])
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(self.staff)
        self.assertEqual(self.post(payload, url, strict).status_code, 403)
        matches = ({"provider_id": "parsehub_generic", "extractor_id": "fixture", "version": "v1"},)
        before = route_matches(SOURCE, matches)[1]["cache_identity"]
        self.assertEqual(self.post(payload, url).status_code, 200)
        audit = UpstreamRouteAudit.objects.get()
        self.assertEqual(audit.after["evaluation_reference"], {"execution_id": record.pk,
            "batch_id": record.batch_id, "execution_fingerprint": record.fingerprint})
        self.assertEqual(audit.actor_id, self.staff.pk)
        self.assertEqual(self.post(payload, url).status_code, 409)
        self.assertEqual(UpstreamRouteAudit.objects.count(), 1)
        self.assertEqual(read_route(SOURCE)["effective_provider_order"], ["parsehub_generic"])
        self.assertNotEqual(route_matches(SOURCE, matches)[1]["cache_identity"], before)
        self.run.assert_not_called()

    def test_invalid_reference_and_mature_routes_are_rejected(self):
        record = self.imported()
        self.staff.is_superuser = True
        self.staff.save(update_fields=["is_superuser"])
        for changes in ({"reference_execution_id": record.pk + 100}, {"reference_execution_id": True},
                        {"source_url": "https://www.coolapk.com/feed/123"},
                        {"provider_order": ["unregistered"]}, {"format_preferences": {"candidate_id": "temporary"}},
                        {"provider_options": {"parsehub_generic": {"url": "https://cdn.example/a"}}}):
            with self.subTest(changes=changes):
                self.assertEqual(self.post(self.route_payload(record, **changes), reverse("upstream_routes")).status_code, 400)
        for source in ("https://www.youtube.com/watch?v=test", "https://www.xiaohongshu.com/explore/123",
                       "https://www.douyin.com/video/123", "https://www.bilibili.com/video/BV123"):
            self.assertEqual(self.post(self.route_payload(record, source_url=source), reverse("upstream_routes")).status_code, 422)
        self.assertFalse(UpstreamRoute.objects.exists())
        self.assertFalse(UpstreamRouteAudit.objects.exists())

    def test_reference_save_rejects_disabled_removed_or_unselectable_current_provider(self):
        row = execution(available=True, route_selectable=True)
        row["result"].update(available=True, route_selectable=True)
        row["result"]["metadata"] = {"available": True, "route_selectable": True}
        record = self.imported(row)
        self.staff.is_superuser = True
        self.staff.save(update_fields=["is_superuser"])
        for label, descriptors in (
            ("disabled", [{**AVAILABLE_DESCRIPTOR, "available": False}]),
            ("removed", []),
            ("unselectable", [{**AVAILABLE_DESCRIPTOR, "route_selectable": False}]),
        ):
            with self.subTest(current_state=label):
                self.descriptors.return_value = descriptors
                self.descriptors.reset_mock()
                response = self.post(self.route_payload(record), reverse("upstream_routes"))
                self.assertEqual(response.status_code, 422, response.content)
                self.assertEqual(response.json()["error_code"], "HISTORY_PROVIDER_UNAVAILABLE")
                self.descriptors.assert_called_once_with()
                self.assertFalse(UpstreamRoute.objects.exists())
                self.assertFalse(UpstreamRouteAudit.objects.exists())
        self.run.assert_not_called()
        self.execute.assert_not_called()
        self.issue.assert_not_called()

    def test_reference_save_rejects_unavailable_fallback_without_changing_existing_route(self):
        record = self.imported()
        self.staff.is_superuser = True
        self.staff.save(update_fields=["is_superuser"])
        url = reverse("upstream_routes")
        self.assertEqual(self.post(self.route_payload(record), url).status_code, 200)
        before_route = read_route(SOURCE)
        before_audits = list(UpstreamRouteAudit.objects.values())
        for descriptors in ([dict(AVAILABLE_DESCRIPTOR)], [dict(AVAILABLE_DESCRIPTOR), {
                "provider_id": "yt_dlp_existing", "execution_provider_id": "yt_dlp_generic",
                "available": False, "route_selectable": True}]):
            with self.subTest(current_descriptors=descriptors):
                self.descriptors.return_value = descriptors
                response = self.post(self.route_payload(record, expected_revision=1,
                    provider_order=["parsehub_generic", "yt_dlp_generic"]), url)
                self.assertEqual(response.status_code, 422, response.content)
                self.assertEqual(response.json()["error_code"], "HISTORY_PROVIDER_UNAVAILABLE")
                self.assertEqual(read_route(SOURCE), before_route)
                self.assertEqual(list(UpstreamRouteAudit.objects.values()), before_audits)
        self.run.assert_not_called()
        self.execute.assert_not_called()


class EvaluationHistoryMigrationTests(TransactionTestCase):
    """Run independently when testing migration fresh/reverse/reapply behavior."""

    def test_reverse_reapply_preserves_preexisting_human_route_and_user(self):
        old = [("xhs_download", "0027_upstream_manual_routes")]
        new = [("xhs_download", "0028_evaluation_history")]
        executor = MigrationExecutor(connection)
        latest = executor.loader.graph.leaf_nodes()
        self.addCleanup(lambda: MigrationExecutor(connection).migrate(latest))
        executor.migrate(old)
        apps = executor.loader.project_state(old).apps
        route = apps.get_model("xhs_download", "UpstreamRoute").objects.create(
            source_host="www.douban.com", revision=3, active=True, provider_order=["parsehub_generic"])
        apps.get_model("xhs_download", "UpstreamRouteAudit").objects.create(
            route_id=route.pk, revision=3, action="save", before={}, after={"revision": 3})
        user = get_user_model().objects.create_user(username="migration-history-human", is_staff=True)
        for target in (new, old, new):
            executor = MigrationExecutor(connection)
            executor.migrate(target)
            state = executor.loader.project_state(target).apps
            saved = state.get_model("xhs_download", "UpstreamRoute").objects.get(pk=route.pk)
            self.assertEqual((saved.revision, saved.provider_order), (3, ["parsehub_generic"]))
            self.assertEqual(state.get_model("xhs_download", "UpstreamRouteAudit").objects.count(), 1)
            self.assertTrue(get_user_model().objects.filter(pk=user.pk, is_staff=True).exists())
            if target == new:
                batch = state.get_model("xhs_download", "EvaluationBatch").objects.create(
                    batch_key="migration-fixture", label="Fixture", origin="scout")
                state.get_model("xhs_download", "EvaluationExecution").objects.create(
                    batch_id=batch.pk, execution_key="exec", summary={}, fingerprint="f" * 64)
                self.assertEqual(state.get_model("xhs_download", "EvaluationExecution").objects.count(), 1)
