"""Synthetic, isolated ledger acceptance tests. No live provider requests."""
from contextlib import ExitStack
from copy import deepcopy
import json
from pathlib import Path
import re
from time import perf_counter
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from .models import EvaluationBatch, EvaluationExecution, UpstreamRoute, UpstreamRouteAudit
from .services.provider_lab import history, ledger


SOURCE = "https://media-ledger.org/work/1"
PROVIDER = "parsehub_repo"
LOCAL_CACHE = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache",
                           "LOCATION": "platform-ledger-tests"}}


def result(**changes):
    value = {"status": "success", "executed": True, "result_kind": "video",
             "metadata": {"identity_confirmed": True},
             "assets": [{"asset_id": "video-1", "media_type": "video", "role": "primary", "height": 1080}]}
    value.update(changes)
    return value


def execution(key="e1", **changes):
    value = {"execution_key": key, "sample_key": "sample-1", "source_url": SOURCE,
             "provider_id": PROVIDER, "state": "returned", "title": "A work, not a platform name",
             "started_at": "2026-09-01T00:00:00Z", "finished_at": "2026-09-01T00:00:01Z",
             "result": result()}
    value.update(changes)
    return value


@override_settings(UPSTREAM_PROVIDER_LAB_ENABLED=True, CACHES=LOCAL_CACHE)
class PlatformLedgerTests(TestCase):
    def setUp(self):
        cache.clear()
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.run = stack.enter_context(patch("xhs_download.provider_lab_views.run_provider",
            side_effect=AssertionError("Unexpected provider call")))
        stack.enter_context(patch("socket.socket.connect", side_effect=AssertionError("Unexpected network")))
        stack.enter_context(patch("subprocess.Popen", side_effect=AssertionError("Unexpected worker")))
        stack.enter_context(patch.object(history, "current_version", return_value={"test": "isolated"}))
        self.staff = get_user_model().objects.create_user(username="ledger-staff", is_staff=True)
        self.client.force_login(self.staff)
        self.url = reverse("provider_lab_history")

    def add(self, row=None, *, batch="test-batch", metadata=None):
        response = self.client.post(self.url, json.dumps({"schema_version": 1,
            "batch": {"batch_key": batch, "label": "Synthetic ledger test", "metadata": metadata or {}},
            "executions": [row or execution()]}), content_type="application/json")
        self.assertEqual(response.status_code, 200, response.content)
        return EvaluationExecution.objects.filter(batch_id=response.json()["batch_id"]).order_by("-pk").first()

    def get(self, **params):
        response = self.client.get(self.url, params or {"view": "platforms"})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response["Cache-Control"].find("no-store") >= 0, True)
        return response.json()

    def platform(self, host="media-ledger.org"):
        return self.get(view="platform", source_host=host)["platform"]

    def test_old_success_recent_failure_and_late_old_import_use_actual_time(self):
        self.add()
        self.add(execution("failure", started_at="2026-10-01T00:00:00Z", finished_at="2026-10-01T00:00:01Z",
            result=result(status="failed", error_detail="SERVICE_TLS_ERROR", assets=[])), batch="new")
        self.add(execution("late", started_at="2020-01-01T00:00:00Z", finished_at="2020-01-01T00:00:01Z"), batch="late")
        p = self.platform()["providers"][0]
        self.assertEqual((p["evidence"], p["positive_count"], p["latest_status"]), ("confirmed", 2, "failed"))
        self.assertEqual(p["latest_error"], "SERVICE_TLS_ERROR")
        self.assertEqual(p["latest_at"], "2026-10-01T00:00:01+00:00")
        self.assertEqual(p["last_success_at"], "2026-09-01T00:00:01+00:00")

    def test_missing_and_naive_dates_never_fall_back_to_created_at(self):
        self.add(execution(started_at=None, finished_at="2026-10-16T00:00:00"))
        p = self.platform()
        self.assertIsNone(p["latest_at"])
        self.assertIsNone(p["providers"][0]["last_success_at"])
        self.assertEqual(p["providers"][0]["undated_count"], 1)
        row = self.get(view="platform", source_host="media-ledger.org")["executions"][0]
        self.assertIsNone(row["finished_at"])
        self.assertIsNotNone(row["batch"]["created_at"])

    def test_product_success_is_not_implicit_target_identity_confirmation(self):
        for identity in (None, False, True):
            metadata = {} if identity is None else {"identity_confirmed": identity}
            host = {None: "unknown-ledger.org", False: "unconfirmed-ledger.org", True: "confirmed-ledger.org"}[identity]
            self.add(execution(str(identity), source_url=f"https://{host}/work/1",
                               result=result(metadata=metadata)))
            platform = self.platform(host)
            provider = platform["providers"][0]
            self.assertEqual(provider["latest_status"], "success")
            self.assertIsNotNone(provider["last_success_at"])
            self.assertEqual(provider["evidence"], "confirmed" if identity is True else "unconfirmed")
            self.assertEqual(provider["positive_count"], 1 if identity is True else 0)
            self.assertEqual(platform["route_hint"], "unique_positive" if identity is True else "unconfirmed_media")

    def test_shared_history_dom_cases_use_actual_ledger_classification_without_rewriting_history(self):
        cases = json.loads((Path(__file__).parent / "fixtures" / "route_evidence_cases.json").read_text())
        for index, case in enumerate(cases):
            with self.subTest(case=case["name"]):
                host = f"case{index}.evidence.example.org"
                changes = {key: case[key] for key in ("status", "executed", "metadata", "assets", "client_status") if key in case}
                record = self.add(execution(case["name"], source_url=f"https://{host}/work",
                    state=case.get("state", "returned"), result=result(**changes)))
                original = deepcopy(record.summary), record.fingerprint
                self.assertEqual(self.platform(host)["providers"][0]["evidence"], case["evidence"])
                rows = self.get(batch_id=record.batch_id)["executions"]
                returned = next(row for row in rows if row["id"] == record.pk)
                self.assertEqual(returned["result"], original[0]["result"])
                record.refresh_from_db()
                self.assertEqual((record.summary, record.fingerprint), original)
        self.assertFalse(UpstreamRoute.objects.exists())
        self.assertFalse(UpstreamRouteAudit.objects.exists())
        self.run.assert_not_called()

    def test_early_success_beyond_100_batches_and_200_rows(self):
        self.add(execution(result=result(result_kind="gallery", assets=[
            {"asset_id": "image", "media_type": "image", "role": "primary"}])))
        batches = EvaluationBatch.objects.bulk_create([EvaluationBatch(batch_key=f"later-{i}", label="Synthetic", origin="scout") for i in range(110)])
        EvaluationExecution.objects.bulk_create([EvaluationExecution(batch=batches[i % 110], execution_key=f"failure-{i}", fingerprint="f",
            summary=history.execution_summary(execution(f"failure-{i}", result=result(status="failed", assets=[])))) for i in range(220)])
        data = self.get(view="platforms", content_type="image", evidence="confirmed")
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["platforms"][0]["providers"][0]["positive_count"], 1)
        self.assertEqual(data["record_count"], 221)
        self.assertEqual(self.get(view="platforms", content_type="gallery")["total"], 1)

    def test_native_identity_partial_media_covers_unexecuted_and_declaration(self):
        self.add(execution("native", result=result(status="native_observed", metadata={
            "evidence_kind": "native_capture_replay", "identity_confirmed": True, "capture_status": "RETURNED"})))
        self.add(execution("unconfirmed", provider_id="gallery_dl_existing", result=result(status="native_observed",
            metadata={"identity_confirmed": False, "capture_status": "RETURNED"})))
        self.add(execution("partial", provider_id="yt_dlp_existing", result=result(status="failed")))
        self.add(execution("cover", provider_id="kukutool_existing", result=result(result_kind="image", assets=[
            {"asset_id": "cover", "media_type": "image", "role": "cover"}])))
        self.add(execution("notrun", provider_id="tiantian_existing", state="not_executed", result=result(
            executed=False, status="not_run", assets=[], metadata={"scope_label": "EXPLICIT_SUPPORT"})))
        by_id = {p["provider_id"]: p for p in self.platform()["providers"]}
        self.assertEqual(by_id["parsehub_generic"]["positive_count"], 1)
        self.assertEqual(by_id["parsehub_generic"]["native_observation_count"], 1)
        self.assertEqual(by_id["parsehub_generic"]["attempt_count"], 0)
        self.assertIsNone(by_id["parsehub_generic"]["last_success_at"])
        self.assertEqual(by_id["parsehub_generic"]["adoptability"], "draft_candidate")
        self.assertEqual(by_id["gallery_dl_generic"]["evidence"], "unconfirmed")
        self.assertEqual(by_id["yt_dlp_generic"]["partial_media_count"], 1)
        self.assertEqual(by_id["kukutool_generic"]["evidence"], "none")
        self.assertEqual(by_id["tiantianjiexi_generic"]["latest_status"], "not_executed")
        self.assertEqual(by_id["tiantianjiexi_generic"]["evidence"], "declared")
        self.assertEqual(by_id["tiantianjiexi_generic"]["observation_count"], 0)
        cover = self.get(view="platform", source_host="media-ledger.org")["executions"]
        self.assertEqual(next(r for r in cover if r["execution_key"] == "cover")["content_types"], ["unknown"])

    def test_image_gallery_are_same_content_not_conflicting_providers(self):
        self.add(execution(result=result(result_kind="image", assets=[{"asset_id": "a", "media_type": "image"}])))
        self.add(execution("gallery", provider_id="gallery_dl_existing", result=result(result_kind="gallery", assets=[
            {"asset_id": "b", "media_type": "image"}, {"asset_id": "c", "media_type": "image"}])))
        self.assertEqual(self.platform()["content_types"], ["image"])
        self.assertEqual(self.platform()["route_hint"], "comparable")
        self.add(execution("video", provider_id="yt_dlp_existing"))
        self.assertEqual(self.platform()["route_hint"], "content_conflict")

    def test_mixed_media_retains_actual_types(self):
        self.add(execution(result=result(result_kind="multimedia", assets=[
            {"asset_id": "v", "media_type": "video"}, {"asset_id": "a", "media_type": "audio"}])))
        self.assertEqual(self.platform()["content_types"], ["audio", "video"])
        self.assertEqual(self.get(view="platforms", content_type="multimedia")["total"], 1)

    def test_linked_copies_deduplicate_and_real_retests_remain(self):
        original = history.begin_execution(SOURCE, PROVIDER, None)
        history.finish_execution(original, result(request_id="call-1"))
        imported = self.add(execution(result=result(request_id="call-1")))
        self.assertEqual(imported.application_record_id, original.pk)
        self.add(execution(result=result(request_id="call-1")), batch="test-batch")
        p = self.platform()
        self.assertEqual((p["record_count"], p["observation_count"], p["providers"][0]["positive_count"]), (2, 1, 1))
        retest = history.begin_execution(SOURCE, PROVIDER, None, replay_of=original.pk)
        history.finish_execution(retest, result(request_id="call-2"))
        self.assertEqual(self.platform()["observation_count"], 2)
        self.assertEqual(self.platform()["providers"][0]["positive_count"], 2)

    def test_exact_request_time_dedup_does_not_merge_distinct_retests(self):
        self.add(execution(result=result(request_id="stable")))
        self.add(execution(result=result(request_id="stable")), batch="copy")
        self.add(execution(started_at="2026-10-01T00:00:00Z", finished_at="2026-10-01T00:00:01Z",
                           result=result(request_id="stable")), batch="retest")
        self.assertEqual(self.platform()["observation_count"], 2)

    def test_same_native_bytes_with_distinct_capture_times_are_real_retests(self):
        native = result(status="native_observed", metadata={"identity_confirmed": True,
            "capture_status": "RETURNED", "archive_sha256": "abc123"})
        self.add(execution(result=native))
        self.add(execution(result=native), batch="copy")
        self.add(execution(result=native, started_at="2026-10-01T00:00:00Z",
                           finished_at="2026-10-01T00:00:01Z"), batch="retest")
        self.assertEqual(self.platform()["observation_count"], 2)
        self.assertEqual(self.platform()["providers"][0]["positive_count"], 2)

    def test_native_observed_at_overrides_conversion_time(self):
        self.add(execution(result=result(status="native_observed", metadata={"identity_confirmed": True,
            "capture_status": "RETURNED", "observed_at": "2020-01-01T00:00:00Z"})))
        self.assertEqual(self.platform()["latest_at"], "2020-01-01T00:00:00+00:00")

    def test_offline_replay_never_counts_as_real_attempt_or_success(self):
        self.add(execution(result=result(metadata={"evidence_kind": "offline_replay"})))
        p = self.platform()["providers"][0]
        self.assertEqual(p["attempt_count"], 0)
        self.assertEqual(p["positive_count"], 0)
        self.assertEqual(p["evidence"], "unconfirmed")
        self.assertIsNone(p["last_success_at"])

    def test_baseline_not_independent_provider_unknown_host_preserved(self):
        self.add(execution(provider_id="animeimages_current"))
        self.add(execution("secret", source_url="https://media-ledger.org/work?token=private"))
        data = self.get()
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["unknown_source_count"], 1)
        self.assertEqual(data["record_count"], 2)
        self.assertEqual(data["platforms"][0]["providers"], [])
        unknown = self.get(view="platform", source_host="unknown")
        self.assertEqual(unknown["total"], 1)
        self.assertEqual(unknown["platform"]["route_hint"], "unknown_source")
        self.assertEqual(unknown["platform"]["providers"][0]["adoptability"], "unknown_source")
        self.assertFalse(unknown["executions"][0]["can_compare_work"])
        self.assertNotIn("private", json.dumps(unknown))

    def test_route_only_hosts_default_order_truth_protection_and_refresh(self):
        route = UpstreamRoute.objects.create(source_host="route-ledger.org", provider_order=["parsehub_generic"], active=True, revision=2)
        UpstreamRoute.objects.create(source_host="www.douyin.com", provider_order=["parsehub_generic"], active=True, revision=3)
        self.add()
        p = self.platform("route-ledger.org")
        self.assertEqual(p["record_count"], 0)
        self.assertEqual(p["route_hint"], "no_records")
        self.assertEqual(p["current_route"]["effective_provider_order"], ["parsehub_generic"])
        self.assertTrue(self.platform()["current_route"]["effective_provider_order"])
        self.assertEqual(self.get(view="platforms", route_state="protected")["total"], 1)
        route.active = False
        route.revision += 1
        route.save()
        self.assertEqual(self.platform("route-ledger.org")["current_route"]["mode"], "default")
        self.assertEqual(self.get(view="platforms", route_state="manual")["total"], 0)

    def test_common_unknown_options_reference_only_valid_dedicated_draft(self):
        self.add(execution(provider_id="videodl_repo", options={"mode": "common_only"}))
        self.assertEqual(self.platform()["providers"][0]["adoptability"], "reference_only")
        self.add(execution("dedicated", provider_id="videodl_repo", options={"mode": "dedicated_only"}))
        self.assertEqual(self.platform()["providers"][0]["adoptability"], "draft_candidate")

    def test_running_record_update_and_import_idempotency_are_visible(self):
        record = history.begin_execution(SOURCE, PROVIDER, None)
        self.assertEqual(self.platform()["providers"][0]["positive_count"], 0)
        history.finish_execution(record, result())
        self.assertEqual(self.platform()["providers"][0]["positive_count"], 1)
        self.add(execution(source_url="https://new-ledger.org/work/2"))
        self.add(execution(source_url="https://new-ledger.org/work/2"))
        self.assertEqual(self.get()["total"], 2)
        self.assertEqual(self.platform("new-ledger.org")["providers"][0]["positive_count"], 1)

    def test_normal_run_endpoint_persists_without_repeat_parse(self):
        self.run.side_effect = None
        self.run.return_value = result(provider_id=PROVIDER, request_id="controlled-call")
        response = self.client.post(reverse("provider_lab_run"), json.dumps({"source": SOURCE,
            "provider_id": PROVIDER, "confirmations": {"external_sharing": True}}), content_type="application/json")
        self.assertEqual(response.status_code, 200, response.content)
        execution_id = response.json()["history"]["id"]
        self.assertEqual(self.platform()["providers"][0]["positive_count"], 1)
        self.assertEqual(self.get(execution_id=execution_id)["execution"]["id"], execution_id)
        self.run.assert_called_once()

    def test_filter_aliases_search_sort_and_stable_pagination(self):
        self.add()
        self.add(execution("z", source_url="https://z-ledger.org/a", started_at="2026-10-01T00:00:00Z",
                           finished_at="2026-10-01T00:00:01Z"))
        for evidence in ("confirmed", "confirmed_media"):
            for provider in ("parsehub_repo", "parsehub_generic"):
                data = self.get(view="platforms", provider_id=provider, evidence=evidence, q="ledger", page_size=1, sort="latest")
                self.assertEqual(data["total"], 2)
                self.assertEqual(data["platforms"][0]["source_host"], "z-ledger.org")
        data = self.get(view="platforms", sort="host", page_size=1, page=2)
        self.assertEqual(data["platforms"][0]["source_host"], "z-ledger.org")
        self.assertEqual(self.get(view="platforms", sort="evidence")["total"], 2)
        self.assertEqual(self.get(view="platforms", q="A work, not a platform name")["total"], 0)

    def test_provider_and_evidence_filter_must_match_same_provider(self):
        self.add()
        self.add(execution("failed", provider_id="yt_dlp_existing", result=result(status="failed", assets=[])))
        self.assertEqual(self.get(view="platforms", provider_id="yt_dlp_existing", evidence="confirmed")["total"], 0)

    def test_work_comparison_exact_source_across_batches_without_exposing_queries(self):
        source = SOURCE + "?id=1"
        first = self.add(execution(source_url=source))
        self.add(execution(source_url=source, provider_id="yt_dlp_existing"), batch="second")
        self.add(execution(source_url=SOURCE + "?id=2"), batch="different")
        detail = self.get(view="platform", source_host="media-ledger.org", work_id=first.pk)
        self.assertEqual(detail["total"], 2)
        self.assertEqual(detail["paging"]["total"], 2)
        self.assertTrue(all(r["can_compare_work"] and not r["source_url"] for r in detail["executions"]))
        self.assertEqual(len({r["work_key"] for r in detail["executions"]}), 1)
        self.assertNotIn("?id=1", json.dumps(detail))
        other = self.add(execution(source_url="https://other-ledger.org/a"), batch="other")
        self.assertEqual(self.client.get(self.url, {"view": "platform", "source_host": "media-ledger.org", "work_id": other.pk}).status_code, 404)

    def test_missing_assets_remain_unknown_in_list_and_full_summary(self):
        r = result()
        del r["assets"]
        record = self.add(execution(result=r))
        detail = self.get(view="platform", source_host="media-ledger.org")["executions"][0]
        self.assertIsNone(detail["asset_count"])
        full = self.get(execution_id=record.pk)["execution"]
        self.assertIsNone(full["asset_count"])
        self.assertIsNone(full["asset_paging"])
        self.assertNotIn("assets", full["result"])

    def test_oversized_row_never_blocks_following_records_and_assets_page(self):
        huge = self.add(execution(result=result(assets=[{"asset_id": f"a-{i}", "media_type": "video",
            "quality_label": "x" * 512} for i in range(1000)], metadata={"huge": ["x" * 512] * 1000})))
        next_record = self.add(execution("next"))
        first = self.get(view="platform", source_host="media-ledger.org", page=1, page_size=1)
        second = self.get(view="platform", source_host="media-ledger.org", page=2, page_size=1)
        self.assertEqual({first["executions"][0]["id"], second["executions"][0]["id"]}, {huge.pk, next_record.pk})
        self.assertNotIn("assets", second["executions"][0]["result"])
        response = self.client.get(self.url, {"execution_id": huge.pk, "asset_page_size": 1000})
        self.assertEqual(response.status_code, 200)
        self.assertLess(len(response.content), ledger.MAX_BYTES)
        self.assertEqual(len(response.json()["execution"]["result"]["assets"]), 1000)
        page = self.get(execution_id=huge.pk, asset_page=2, asset_page_size=100)["execution"]
        self.assertEqual(page["result"]["assets"][0]["asset_id"], "a-100")

    def test_full_summary_removes_credentials_and_preserves_capture_provenance(self):
        record = self.add(execution(result=result(status="native_observed", metadata={
            "identity_confirmed": True, "capture_status": "RETURNED", "evidence_kind": "native_capture_replay",
            "raw_payload": "PRIVATE", "authorization": "PRIVATE"}, assets=[
                {"asset_id": "a", "media_type": "video", "resource_token": "PRIVATE", "url": "https://cdn.org/PRIVATE"}])))
        full = self.get(execution_id=record.pk)["execution"]
        self.assertEqual(full["state"], "returned")
        self.assertEqual(full["result"]["status"], "native_observed")
        self.assertEqual(full["result"]["metadata"]["capture_status"], "RETURNED")
        self.assertNotIn("PRIVATE", json.dumps(full))

    def test_large_legacy_structures_still_advance_record_and_asset_pages(self):
        record = self.add()
        nested = {str(i): ["x" * 512] * 32 for i in range(32)}
        record.summary["target_id"] = nested
        record.summary["options"] = nested
        record.summary["result"]["metadata"] = nested
        record.summary["result"]["assets"] = [{"asset_id": "a", "media_type": "video",
            "width": nested, "height": nested} for _ in range(5)]
        record.save(update_fields=["summary"])
        response = self.client.get(self.url, {"view": "platform", "source_host": "media-ledger.org"})
        self.assertEqual(response.status_code, 200)
        self.assertLess(len(response.content), 12 * 1024)
        full = self.client.get(self.url, {"execution_id": record.pk})
        self.assertEqual(full.status_code, 200)
        self.assertLess(len(full.content), ledger.MAX_BYTES)

    def test_read_failure_is_not_a_successful_empty_ledger(self):
        with patch.object(ledger, "_scan", side_effect=RuntimeError("read failed")):
            with self.assertRaises(RuntimeError):
                ledger.platforms_payload({})

    def test_5000_providers_have_bounded_preview_and_independent_detail_pages(self):
        batch = EvaluationBatch.objects.create(batch_key="provider-budget", label="Synthetic", origin="scout")
        records = []
        for i in range(5000):
            provider = f"legacy_{i:04d}" if i < 4999 else "yt_dlp_existing"
            summary = history.execution_summary(execution(str(i), provider_id=provider))
            records.append(EvaluationExecution(batch=batch, execution_key=str(i), summary=summary, fingerprint="test"))
        EvaluationExecution.objects.bulk_create(records, batch_size=200)
        before = list(EvaluationExecution.objects.order_by("pk").values_list("pk", "fingerprint"))
        response = self.client.get(self.url, {"view": "platforms", "page_size": 1})
        self.assertEqual(response.status_code, 200, response.content)
        overview = response.json()["platforms"][0]
        self.assertEqual(overview["provider_total"], 5000)
        self.assertTrue(overview["providers_truncated"])
        self.assertEqual(len(overview["providers"]), 10)
        self.assertEqual((overview["record_count"], overview["observation_count"]), (5000, 5000))
        self.assertEqual(overview["route_hint"], "comparable")
        filtered = self.get(view="platforms", provider_id="yt_dlp_existing", evidence="confirmed")
        self.assertEqual(filtered["total"], 1)
        self.assertEqual(filtered["platforms"][0]["providers"][0]["provider_id"], "yt_dlp_generic")
        self.assertEqual(filtered["filtered_record_count"], 5000)
        first = self.get(view="platform", source_host="media-ledger.org", page_size=1)
        self.assertEqual(first["provider_paging"]["page_size"], 20)
        self.assertEqual(first["provider_paging"]["total"], 5000)
        self.assertEqual(first["provider_paging"]["next_page"], 2)
        second = self.get(view="platform", source_host="media-ledger.org", page_size=1, provider_page=2)
        self.assertEqual(first["executions"], second["executions"])
        self.assertEqual(second["platform"]["providers"][0]["provider_id"], "legacy_0020")
        self.assertTrue(set(p["provider_id"] for p in first["platform"]["providers"]).isdisjoint(
            p["provider_id"] for p in second["platform"]["providers"]))
        last_response = self.client.get(self.url, {"view": "platform", "source_host": "media-ledger.org",
            "page_size": 1, "page": 5000, "provider_page": 100, "provider_page_size": 50})
        self.assertEqual(last_response.status_code, 200, last_response.content)
        last = last_response.json()
        self.assertEqual(last["platform"]["providers"][-1]["provider_id"], "yt_dlp_generic")
        self.assertEqual(last["provider_paging"]["returned"], 50)
        self.assertFalse(last["provider_paging"]["has_next"])
        self.assertIsNone(last["provider_paging"]["next_page"])
        self.assertEqual(last["executions"][0]["id"], records[0].pk)
        self.assertEqual(last["paging"]["total"], 5000)
        self.assertEqual(before, list(EvaluationExecution.objects.order_by("pk").values_list("pk", "fingerprint")))
        self.assertLess(len(response.content), ledger.MAX_BYTES)
        self.assertLess(len(last_response.content), ledger.MAX_BYTES)
        print(f"LEDGER_PROVIDER_BUDGET providers=5000 overview_bytes={len(response.content)} detail_bytes={len(last_response.content)}")

    def test_unicode_source_projection_is_bounded_without_losing_work_comparison(self):
        source = "https://media-ledger.org/" + "\u4e2d" * 4000
        record = self.add(execution(source_url=source))
        original = deepcopy(record.summary)
        rows = []
        for i in range(100):
            summary = history.execution_summary(execution(f"unicode-{i}", source_url=source))
            rows.append(EvaluationExecution(batch=record.batch, execution_key=f"unicode-{i}", summary=summary, fingerprint="test"))
        EvaluationExecution.objects.bulk_create(rows, batch_size=100)
        response = self.client.get(self.url, {"view": "platform", "source_host": "media-ledger.org", "page_size": 100})
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()
        self.assertEqual((data["total"], data["returned"], data["paging"]["next_page"]), (101, 100, 2))
        self.assertEqual(data["platform"]["safe_source_url"], "")
        self.assertTrue(data["platform"]["source_projection_omitted"])
        self.assertTrue(data["platform"]["summary_truncated"])
        for row in data["executions"]:
            self.assertLessEqual(ledger._json_size(row), ledger.MAX_ROW_BYTES)
            self.assertTrue(row["source_projection_omitted"])
            self.assertTrue(row["summary_truncated"])
            self.assertTrue(row["can_compare_work"])
            self.assertFalse(row["can_retest"])
            self.assertEqual(row["source_url"], "")
            self.assertEqual(row["asset_count"], 1)
        next_page = self.get(view="platform", source_host="media-ledger.org", page_size=100, page=2, work_id=record.pk)
        self.assertEqual(next_page["returned"], 1)
        self.assertEqual(next_page["executions"][0]["id"], record.pk)
        self.assertEqual(next_page["executions"][0]["work_key"], data["executions"][0]["work_key"])
        full = self.get(execution_id=record.pk)["execution"]
        self.assertEqual(full["work_key"], next_page["executions"][0]["work_key"])
        self.assertTrue(full["source_projection_omitted"])
        self.assertTrue(full["can_compare_work"])
        record.refresh_from_db()
        self.assertEqual(record.summary, original)
        self.assertLess(len(response.content), ledger.MAX_BYTES)
        print(f"LEDGER_UNICODE_BUDGET rows=100 bytes={len(response.content)} max_row_bytes={max(ledger._json_size(r) for r in data['executions'])}")

    def test_giant_asset_numbers_are_omitted_not_invented_and_pages_never_skip(self):
        large = 10 ** 1200
        assets = [{"asset_id": f"a-{i}", "media_type": "video", "role": "primary", "width": large,
                   "height": large, "duration_seconds": 12} for i in range(1000)]
        record = self.add(execution(result=result(assets=assets)))
        original = deepcopy(record.summary)
        response = self.client.get(self.url, {"execution_id": record.pk, "asset_page_size": 1000})
        self.assertEqual(response.status_code, 200, response.content)
        full = response.json()["execution"]
        self.assertEqual((full["asset_count"], full["asset_paging"]["total"], full["asset_paging"]["returned"]), (1000, 1000, 1000))
        self.assertEqual([a["asset_id"] for a in full["result"]["assets"]], [a["asset_id"] for a in assets])
        for asset in full["result"]["assets"]:
            self.assertLessEqual(ledger._json_size(asset), ledger.MAX_ASSET_BYTES)
            self.assertTrue(asset["summary_truncated"])
            self.assertEqual((asset["media_type"], asset["role"], asset["duration_seconds"]), ("video", "primary", 12))
            self.assertTrue("width" not in asset or "height" not in asset)
            for key in ("width", "height"):
                if key in asset:
                    self.assertEqual(asset[key], large)
        first = self.get(execution_id=record.pk, asset_page_size=500)["execution"]
        second = self.get(execution_id=record.pk, asset_page_size=500, asset_page=2)["execution"]
        self.assertEqual([a["asset_id"] for a in first["result"]["assets"] + second["result"]["assets"]],
                         [a["asset_id"] for a in assets])
        record.refresh_from_db()
        self.assertEqual(record.summary, original)
        self.assertLess(len(response.content), ledger.MAX_BYTES)
        print(f"LEDGER_ASSET_BUDGET assets=1000 bytes={len(response.content)} max_asset_bytes={max(ledger._json_size(a) for a in full['result']['assets'])}")

    def test_oversized_identity_and_legacy_row_text_have_final_byte_caps(self):
        asset = {"asset_id": "\U0001f600" * 512, "media_type": "\U0001f600" * 512,
                 "role": "\U0001f600" * 512, "width": 10 ** 1200}
        projected = ledger._asset_projection(asset)
        self.assertEqual(projected, ledger._asset_projection(asset))
        self.assertLessEqual(ledger._json_size(projected), ledger.MAX_ASSET_BYTES)
        self.assertTrue(projected["summary_truncated"])
        self.assertTrue(all(key in projected for key in ("asset_id", "media_type", "role")))
        record = self.add()
        original = deepcopy(record.summary)
        for key in ("title", "target_id", "source_status", "source_state", "replay_of", "replay_batch_key"):
            record.summary[key] = "\U0001f600" * 300
        record.summary["result"].update(metadata={"long": ["\U0001f600" * 512] * 32},
            error_code="\U0001f600" * 512, error_detail="\U0001f600" * 512)
        record.save(update_fields=["summary"])
        row = self.get(view="platform", source_host="media-ledger.org")["executions"][0]
        self.assertLessEqual(ledger._json_size(row), ledger.MAX_ROW_BYTES)
        self.assertTrue(row["summary_truncated"])
        self.assertEqual(row["asset_count"], 1)
        self.assertEqual(row["result"]["asset_count"], 1)
        self.assertEqual(row["provider_id"], original["provider_id"])
        self.assertEqual(row["state"], original["state"])

    def test_legacy_route_projection_and_provider_previews_keep_factual_aggregates(self):
        self.add()
        large_options = {f"legacy_{i}": "\U0001f600" * 512 for i in range(32)}
        route = UpstreamRoute.objects.create(source_host="media-ledger.org", active=True, revision=7,
            provider_order=["parsehub_generic"], provider_options=large_options, format_preferences=large_options)
        original_options = deepcopy(route.provider_options)
        overview = self.get()["platforms"][0]
        detail = self.get(view="platform", source_host="media-ledger.org")["platform"]
        for platform in (overview, detail):
            self.assertEqual((platform["record_count"], platform["observation_count"], platform["provider_total"]), (1, 1, 1))
            self.assertEqual(platform["providers"][0]["positive_count"], 1)
            self.assertEqual(platform["providers"][0]["adoptability"], "existing_route")
            self.assertEqual(platform["current_route"]["revision"], 7)
            self.assertTrue(platform["current_route"]["active"])
            self.assertTrue(platform["summary_truncated"])
            self.assertTrue(platform["current_route"]["summary_truncated"])
            self.assertEqual(platform["current_route"]["provider_order"], ["parsehub_generic"])
            self.assertLessEqual(ledger._json_size(platform["current_route"]), ledger.MAX_ROUTE_SUMMARY_BYTES)
        route.refresh_from_db()
        self.assertEqual(route.provider_options, original_options)
        self.assertEqual(route.format_preferences, original_options)
        platform = deepcopy(detail)
        platform["providers"] = [{**detail["providers"][0], "provider_id": f"legacy_{i:02d}",
                                  "latest_error": "\U0001f600" * 160} for i in range(50)]
        original = deepcopy(platform)
        preview = ledger._provider_projection(platform, preferred="legacy_49", preview=True)
        self.assertEqual(platform, original)
        self.assertEqual(preview["provider_total"], 50)
        self.assertTrue(preview["providers_truncated"])
        self.assertEqual(preview["providers"][0]["provider_id"], "legacy_49")
        self.assertLessEqual(ledger._json_size(preview), ledger.MAX_PLATFORM_PREVIEW_BYTES)
        self.assertLess(ledger._json_size({"platforms": [preview] * 100}), ledger.MAX_BYTES)

    def test_gets_never_write_history_routes_audits_or_probe(self):
        record = self.add()
        with CaptureQueriesContext(connection) as queries:
            self.get()
            self.get(view="platform", source_host="media-ledger.org")
            self.get(execution_id=record.pk)
        self.assertFalse(any(re.match(r"\s*(INSERT|UPDATE|DELETE)", q["sql"], re.I) for q in queries))
        self.assertEqual(UpstreamRouteAudit.objects.count(), 0)
        self.run.assert_not_called()

    def test_security_gates_and_input_validation(self):
        record = self.add()
        params = [{"view": "platforms"}, {"view": "platform", "source_host": "media-ledger.org"}, {"execution_id": record.pk}]
        self.client.logout()
        for value in params:
            self.assertEqual(self.client.get(self.url, value).status_code, 404)
        nonstaff = get_user_model().objects.create_user(username="nonstaff")
        self.client.force_login(nonstaff)
        for value in params:
            self.assertEqual(self.client.get(self.url, value).status_code, 404)
        self.client.force_login(self.staff)
        for value in ({"view": "platforms", "page": "-1"}, {"view": "platforms", "page_size": "101"},
            {"view": "platforms", "sort": "arbitrary"}, {"view": "platform", "source_host": "127.0.0.1"},
            {"view": "platform", "source_host": "MEDIA-LEDGER.ORG"}, {"view": "platform", "source_host": ""},
            {"execution_id": record.pk, "asset_page_size": "1001"}, {"view": ["platforms", "platform"]}):
            self.assertEqual(self.client.get(self.url, value).status_code, 400, value)
        for value in ({"view": "platform", "source_host": "media-ledger.org", "provider_page": 0},
                      {"view": "platform", "source_host": "media-ledger.org", "provider_page_size": 51}):
            self.assertEqual(self.client.get(self.url, value).status_code, 400, value)
        with override_settings(UPSTREAM_PROVIDER_LAB_ENABLED=False):
            self.assertEqual(self.client.get(self.url, params[0]).status_code, 404)


@override_settings(CACHES=LOCAL_CACHE)
class PlatformLedgerBenchmarkTests(TestCase):
    def test_1000_hosts_10000_light_rows_bounded_queries_and_response(self):
        batch = EvaluationBatch.objects.create(batch_key="benchmark-synthetic", label="Synthetic benchmark", origin="test")
        records = []
        for i in range(10000):
            host = f"host-{i % 1000:04d}.ledger-benchmark.org"
            summary = history.execution_summary(execution(str(i), source_url=f"https://{host}/work/{i}",
                result=result(assets=[{"asset_id": "a", "media_type": "video"}])))
            records.append(EvaluationExecution(batch=batch, execution_key=str(i), summary=summary, fingerprint="test"))
        EvaluationExecution.objects.bulk_create(records, batch_size=200)
        with patch("socket.socket.connect", side_effect=AssertionError("No network")), patch(
                "subprocess.Popen", side_effect=AssertionError("No worker")), CaptureQueriesContext(connection) as queries:
            start = perf_counter()
            data = ledger.platforms_payload({"page_size": "100"})
            elapsed = perf_counter() - start
        encoded = json.dumps(data, ensure_ascii=True).encode()
        self.assertEqual((data["total"], data["record_count"], data["unique_observation_count"]), (1000, 10000, 10000))
        self.assertEqual(len(data["platforms"]), 100)
        self.assertLessEqual(len(queries), 5)
        self.assertLess(len(encoded), ledger.MAX_BYTES)
        self.assertLess(elapsed, 30, "Generous CI regression guard, not a latency SLA")
        print(f"LEDGER_BENCHMARK hosts=1000 records=10000 seconds={elapsed:.3f} queries={len(queries)} bytes={len(encoded)}")
