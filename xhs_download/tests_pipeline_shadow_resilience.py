"""Fault injection at telemetry storage/pipe boundaries, never media requests."""
from datetime import date
import os
from threading import Event, Thread
from types import SimpleNamespace
from unittest.mock import patch

from django.db import OperationalError, close_old_connections, connection, transaction
from django.db.models.query import QuerySet
from django.test import SimpleTestCase, TransactionTestCase

from .models import PipelineShadowDailyCount, ProviderHealthTestRun
from .services import pipeline_shadow as shadow
from .services import worker_raw_capture as worker
from .services.pipeline_shadow_capture import Capture, collect_raw


class ShadowPersistenceResilienceTests(TransactionTestCase):
    def setUp(self):
        self.session = Capture()
        self.session.result = SimpleNamespace(platform="bilibili")
        compare = patch("xhs_download.services.pipeline_shadow_compare.compare_capture", return_value={})
        codes = patch("xhs_download.services.pipeline_shadow_compare.count_codes",
                      return_value=[("bilibili_native", "TRACKS_UNKNOWN")])
        compare.start()
        codes.start()
        self.addCleanup(compare.stop)
        self.addCleanup(codes.stop)

    def persist(self):
        shadow.persist_public(self.session, date(2026, 10, 26))

    def test_transient_locked_write_retries_then_counts_once(self):
        manager = PipelineShadowDailyCount.objects
        original = manager.get_or_create
        calls = []
        def locked(**kwargs):
            calls.append(kwargs)
            if len(calls) < 3:
                raise OperationalError("database table is locked")
            return original(**kwargs)
        with patch.object(manager, "get_or_create", side_effect=locked):
            self.persist()
        self.assertEqual(len(calls), 3)
        self.assertEqual(list(manager.values_list("count", flat=True)), [1])

    def test_failed_increment_rolls_back_before_retry(self):
        original = QuerySet.update
        calls = []
        def update(queryset, **kwargs):
            result = original(queryset, **kwargs)
            calls.append(kwargs)
            if len(calls) == 1:
                raise OperationalError("database is locked")
            return result
        with patch.object(QuerySet, "update", update):
            self.persist()
        self.assertEqual(len(calls), 2)
        self.assertEqual(list(PipelineShadowDailyCount.objects.values_list("count", flat=True)), [1])

    def test_retry_does_not_recount_other_successful_dimensions(self):
        manager = PipelineShadowDailyCount.objects
        original = manager.get_or_create
        locked_once = []
        def get_or_create(**kwargs):
            if kwargs["difference_code"] == "TRACKS_UNKNOWN" and not locked_once:
                locked_once.append(True)
                raise OperationalError("database is locked")
            return original(**kwargs)
        with patch.object(manager, "get_or_create", side_effect=get_or_create), \
                patch("xhs_download.services.pipeline_shadow_compare.count_codes", return_value=[
                    ("bilibili_native", "TRACKS_UNKNOWN"), ("bilibili_native", "SHADOW_NO_RAW")]):
            self.persist()
        self.assertEqual(list(manager.order_by("difference_code").values_list("count", flat=True)), [1, 1])

    def test_persistent_lock_is_bounded_and_fail_open_with_safe_log(self):
        with patch.object(PipelineShadowDailyCount.objects, "get_or_create",
                          side_effect=OperationalError("database is locked")) as write:
            with self.assertLogs("xhs_download.pipeline_shadow", level="WARNING") as logs:
                self.persist()
        self.assertEqual(write.call_count, 3)
        self.assertFalse(PipelineShadowDailyCount.objects.exists())
        self.assertIn("OperationalError", " ".join(logs.output))
        self.assertIn("bilibili_native", " ".join(logs.output))
        self.assertIn("TRACKS_UNKNOWN", " ".join(logs.output))

    def test_non_lock_error_does_not_retry_or_expose_exception_text(self):
        secret = "https://secret.invalid/?token=private title work-id"
        with patch.object(PipelineShadowDailyCount.objects, "get_or_create",
                          side_effect=OperationalError(secret)) as write:
            with self.assertLogs("xhs_download.pipeline_shadow", level="WARNING") as logs:
                self.persist()
        self.assertEqual(write.call_count, 1)
        self.assertNotIn(secret, " ".join(logs.output))
        self.assertNotIn("Traceback", " ".join(logs.output))

    def test_real_sqlite_shared_cache_write_lock_recovers_once(self):
        if connection.vendor != "sqlite":
            self.skipTest("SQLite shared-cache contention regression")
        row = PipelineShadowDailyCount.objects.create(
            date=date(2026, 10, 26), platform="bilibili", provider="bilibili_native",
            difference_code="TRACKS_UNKNOWN", count=0)
        locked, release, finished = Event(), Event(), Event()
        errors = []
        def hold_lock():
            close_old_connections()
            try:
                with transaction.atomic():
                    PipelineShadowDailyCount.objects.filter(pk=row.pk).update(count=0)
                    locked.set()
                    if not release.wait(5):
                        raise AssertionError("test lock was not released")
            except Exception as error:
                errors.append(error)
            finally:
                close_old_connections()
                finished.set()
        holder = Thread(target=hold_lock)
        holder.start()
        def release_at_retry(_seconds):
            release.set()
            self.assertTrue(finished.wait(2))
        try:
            self.assertTrue(locked.wait(2))
            with patch.object(shadow.time, "sleep", side_effect=release_at_retry) as delay:
                with self.assertLogs("xhs_download.pipeline_shadow", level="WARNING") as logs:
                    self.persist()
            delay.assert_called_once()
            self.assertIn("OperationalError", " ".join(logs.output))
        finally:
            release.set()
            holder.join(5)
        self.assertFalse(holder.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(list(PipelineShadowDailyCount.objects.values_list("count", flat=True)), [1])

    def test_health_write_lock_retries_without_replacing_comparison(self):
        run = ProviderHealthTestRun.objects.create(request_id="shadow-lock", test_key="health_ready")
        details = {"observations": [{"difference_codes": ["TRACKS_UNKNOWN"]}]}
        original = QuerySet.update
        calls = []
        def update(queryset, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise OperationalError("database is locked")
            return original(queryset, **kwargs)
        with patch("xhs_download.services.pipeline_shadow_compare.compare_capture", return_value=details), \
                patch.object(QuerySet, "update", update):
            shadow._persist_health(self.session, run, {"provider": "bilibili_native"})
        run.refresh_from_db()
        self.assertEqual(calls, [{"shadow_details": details}, {"shadow_details": details}])
        self.assertEqual(run.shadow_details, details)


class ShadowDropDiagnosticsTests(SimpleTestCase):
    def test_daily_shadow_write_waits_for_business_orchestration_to_return(self):
        written = Event()
        run = SimpleNamespace(pk=1)
        execute = shadow.health_shadow(lambda context, item: run)
        def daily():
            self.assertIs(execute(None, {"provider": "bilibili_native"}), run)
            self.assertFalse(written.wait(0.1), "shadow write overlapped Daily business writes")
            return "original daily result"
        scoped = getattr(shadow, "daily_shadow_scope", lambda function: function)(daily)
        with patch.object(shadow.transaction, "on_commit", side_effect=lambda callback, **kwargs: callback()), \
                patch.object(shadow, "_persist_health", side_effect=lambda *args: written.set()):
            try:
                self.assertEqual(scoped(), "original daily result")
            finally:
                self.assertFalse(shadow.wait_for_shadow())
        self.assertTrue(written.is_set())

    def test_failed_background_submission_finishes_and_releases_slot(self):
        with patch.object(shadow._slots, "acquire", return_value=True), \
                patch.object(shadow._slots, "release") as release, \
                patch.object(shadow._executor, "submit", side_effect=RuntimeError("private")):
            with self.assertLogs("xhs_download.pipeline_shadow", level="WARNING") as logs:
                shadow._after_capture(Capture(), lambda: None)
        release.assert_called_once()
        self.assertFalse(shadow.wait_for_shadow())
        self.assertNotIn("private", " ".join(logs.output))

    def test_full_comparison_slots_log_and_finish_without_running_work(self):
        with patch.object(shadow._slots, "acquire", return_value=False):
            with patch.object(shadow._executor, "submit") as submit:
                with self.assertLogs("xhs_download.pipeline_shadow", level="WARNING") as logs:
                    shadow._after_capture(Capture(), lambda: self.fail("must not run"))
        submit.assert_not_called()
        self.assertFalse(shadow.wait_for_shadow())
        self.assertIn("slot", " ".join(logs.output).lower())

    def test_invalid_worker_pipe_logs_without_raw_data(self):
        read_fd, write_fd = os.pipe()
        os.write(write_fd, b"https://secret.invalid/?token=private")
        os.close(write_fd)
        with collect_raw() as session:
            with patch.object(worker._slots, "release"):
                with self.assertLogs("xhs_download.pipeline_shadow", level="WARNING") as logs:
                    worker._receive(read_fd, "ytdlp", "https://private.invalid/source", {})
        self.assertEqual(session.records, ())
        self.assertIn("ytdlp", session.errors)
        self.assertNotIn("https://", " ".join(logs.output))

    def test_full_reader_slots_do_not_change_request(self):
        request = {"action": "probe"}
        with collect_raw() as session:
            with patch.object(worker._slots, "acquire", return_value=False):
                with self.assertLogs("xhs_download.pipeline_shadow", level="WARNING") as logs:
                    with worker.worker_capture(request, "ytdlp", "https://private.invalid") as result:
                        self.assertIs(result[0], request)
                        self.assertEqual(result[1], {})
        self.assertIn("ytdlp", session.errors)
        self.assertIn("slot", " ".join(logs.output).lower())
        self.assertNotIn("https://", " ".join(logs.output))
