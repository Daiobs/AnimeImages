"""Isolated raw-budget and safe-diagnostic boundaries; no upstream or database."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Event
from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from .services import pipeline_shadow_capture as capture


PROVIDER = "douyin_native"
SOURCE = "https://example.test/controlled?token=private-source"
LOGGER = "xhs_download.pipeline_shadow"


class CaptureBudgetTests(SimpleTestCase):
    def test_single_oversize_is_rejected_before_copy(self):
        raw = {"html": "x" * (17 * 1024 * 1024)}
        with capture.collect_raw() as session, patch.object(capture, "deepcopy", wraps=deepcopy) as copy:
            capture.capture_raw(PROVIDER, raw, SOURCE)
        self.assertEqual(copy.call_count, 0)
        self.assertEqual(session.records, ())
        self.assertEqual(session.errors, {PROVIDER})

    def test_cumulative_limit_keeps_first_and_rejects_second_before_copy(self):
        raw = {"html": "x" * 1000}
        with patch.object(capture, "MAX_CAPTURE_BYTES", 4096, create=True), capture.collect_raw() as session:
            capture.capture_raw(PROVIDER, raw, SOURCE)
            with patch.object(capture, "deepcopy", wraps=deepcopy) as copy:
                capture.capture_raw(PROVIDER, raw, SOURCE)
        self.assertEqual(len(session.records), 1)
        self.assertEqual(copy.call_count, 0)
        self.assertEqual(session.errors, {PROVIDER})

    def test_32_records_skip_estimation_and_copy_when_full(self):
        with capture.collect_raw() as session:
            for _ in range(32):
                capture.capture_raw(PROVIDER, {}, SOURCE)
            with patch.object(capture, "deepcopy", wraps=deepcopy) as copy:
                capture.capture_raw(PROVIDER, {}, SOURCE)
        self.assertEqual(copy.call_count, 0)
        self.assertEqual(len(session.records), 32)
        self.assertEqual(session.errors, {PROVIDER})

    def test_unknown_objects_never_run_lazy_or_copy_protocols(self):
        effects = Mock()
        class Lazy:
            def __iter__(self):
                effects("iter")
                return iter(())

            def __str__(self):
                effects("str")
                return "private-value"

            def __deepcopy__(self, memo):
                effects("deepcopy")
                return self

            def __sizeof__(self):
                effects("sizeof")
                return 1

        with capture.collect_raw() as session:
            capture.capture_raw(PROVIDER, {"entries": Lazy()}, SOURCE)
        effects.assert_not_called()
        self.assertEqual(session.records, ())
        self.assertEqual(session.errors, {PROVIDER})

    def test_off_does_not_estimate_copy_or_log(self):
        with patch.object(capture, "deepcopy", side_effect=AssertionError("off")), \
                patch.object(capture, "log_shadow_failure", create=True) as log:
            capture.capture_raw(PROVIDER, object(), SOURCE)
            capture.capture_error(PROVIDER, SOURCE)
        log.assert_not_called()

    def test_eight_mib_ascii_is_not_charged_as_four_byte_utf8(self):
        with capture.collect_raw() as session:
            capture.capture_raw(PROVIDER, {"html": "x" * (8 * 1024 * 1024)}, SOURCE)
        self.assertEqual(len(session.records), 1)
        self.assertLess(session.raw_bytes, 17 * 1024 * 1024)

    def test_supported_raw_and_metadata_remain_complete_and_independent(self):
        raw = {"html": "<p>\u4e2d\U0001f600</p>", "values": [None, True, 4, 1.25],
               "tuple": ("entry", b"binary"), "wire": {"t": "object", "v": {"t": "dict", "v": []}}}
        metadata = {"context": {"headers": ["X-Controlled"]}}
        before = deepcopy(raw)
        with capture.collect_raw() as session:
            capture.capture_raw(PROVIDER, raw, SOURCE, **metadata)
        raw["values"].append(8)
        metadata["context"]["headers"].append("Changed")
        self.assertEqual(session.records[0].raw, before)
        self.assertEqual(session.records[0].metadata["context"]["headers"], ["X-Controlled"])
        self.assertEqual(session.errors, set())

    def test_source_metadata_and_empty_containers_consume_budget(self):
        cases = (({}, {"header": "x" * 2200}, SOURCE),
                 ({}, {}, "https://example.test/" + "x" * 2200),
                 ([[] for _ in range(200)], {}, SOURCE),
                 ({"text": "\U0001f600" * 500}, {}, SOURCE))
        with patch.object(capture, "MAX_CAPTURE_BYTES", 4096, create=True):
            for raw, metadata, source in cases:
                with self.subTest(kind=type(raw).__name__), capture.collect_raw() as session, \
                        patch.object(capture, "deepcopy", wraps=deepcopy) as copy:
                    capture.capture_raw(PROVIDER, raw, source, **metadata)
                self.assertEqual(copy.call_count, 0)
                self.assertEqual(session.records, ())
                self.assertEqual(session.errors, {PROVIDER})

    def test_deepcopy_failure_releases_bytes_and_record_slot(self):
        raw = {"html": "x" * 1000}
        with patch.object(capture, "MAX_CAPTURE_BYTES", 4096, create=True), capture.collect_raw() as session:
            with patch.object(capture, "deepcopy", side_effect=MemoryError("private-copy")):
                capture.capture_raw(PROVIDER, raw, SOURCE)
            self.assertEqual(session.raw_bytes, 0)
            capture.capture_raw(PROVIDER, raw, SOURCE)
            self.assertEqual(len(session.records), 1)
            self.assertGreater(session.raw_bytes, 0)
            self.assertLessEqual(session.raw_bytes, 4096)

    def test_rejected_record_does_not_consume_later_capacity(self):
        with patch.object(capture, "MAX_CAPTURE_BYTES", 4096, create=True), capture.collect_raw() as session:
            capture.capture_raw(PROVIDER, {"html": "x" * 2200}, SOURCE)
            self.assertEqual(session.raw_bytes, 0)
            capture.capture_raw(PROVIDER, {"html": "x" * 1000}, SOURCE)
        self.assertEqual(len(session.records), 1)

    def test_concurrent_reservation_prevents_a_second_copy(self):
        entered, release = Event(), Event()
        raw = {"html": "x" * 1000}
        real_copy = capture.deepcopy
        def slow(value):
            if value is raw:
                entered.set()
                if not release.wait(3):
                    raise TimeoutError("test release")
            return real_copy(value)

        with ThreadPoolExecutor(max_workers=2) as executor, \
                patch.object(capture, "MAX_CAPTURE_BYTES", 4096, create=True), \
                patch.object(capture, "deepcopy", side_effect=slow) as copy, capture.collect_raw() as session:
            first = capture.submit_with_capture(executor, capture.capture_raw, PROVIDER, raw, SOURCE)
            try:
                self.assertTrue(entered.wait(1))
                second = capture.submit_with_capture(executor, capture.capture_raw, PROVIDER, raw, SOURCE)
                second.result(timeout=1)
                self.assertEqual(copy.call_count, 1)
                self.assertGreater(session.raw_bytes, 0)
                self.assertLessEqual(session.raw_bytes, 4096)
            finally:
                release.set()
                first.result(timeout=2)
        self.assertEqual(len(session.records), 1)
        self.assertEqual(session.errors, {PROVIDER})

    def test_pending_record_also_counts_towards_32(self):
        entered, release = Event(), Event()
        raw = {"pending": True}
        real_copy = capture.deepcopy
        def slow(value):
            if value is raw:
                entered.set()
                if not release.wait(3):
                    raise TimeoutError("test release")
            return real_copy(value)

        with capture.collect_raw() as session:
            for _ in range(31):
                capture.capture_raw(PROVIDER, {}, SOURCE)
            with ThreadPoolExecutor(max_workers=1) as executor, patch.object(capture, "deepcopy", side_effect=slow) as copy:
                first = capture.submit_with_capture(executor, capture.capture_raw, PROVIDER, raw, SOURCE)
                try:
                    self.assertTrue(entered.wait(1))
                    capture.capture_raw(PROVIDER, {}, SOURCE)
                    self.assertEqual(copy.call_count, 1)
                finally:
                    release.set()
                    first.result(timeout=2)
        self.assertEqual(len(session.records), 32)
        self.assertEqual(session.errors, {PROVIDER})

    def test_direct_add_cannot_bypass_budget_and_sessions_are_independent(self):
        observation = capture.RawObservation(PROVIDER, {"html": "x" * 1000}, SOURCE)
        with patch.object(capture, "MAX_CAPTURE_BYTES", 4096, create=True):
            first, second = capture.Capture(), capture.Capture()
            first.add(observation)
            first.add(observation)
            second.add(observation)
        self.assertEqual((len(first.records), len(second.records)), (1, 1))
        self.assertEqual(second.errors, set())


class CaptureLogTests(SimpleTestCase):
    def test_fixed_reasons_codes_and_legacy_provider_alias_are_accepted(self):
        reasons = ("public_setting_read", "public_compare", "public_persist", "public_schedule",
                   "health_schedule", "health_compare", "health_persist", "comparison_slots_full",
                   "comparison_submit", "comparison_settle", "comparison_background",
                   "worker_receive", "worker_slots_full", "worker_setup")
        with self.assertLogs(LOGGER, level="WARNING") as logs:
            for reason in reasons:
                capture.log_shadow_failure("ytdlp", reason, ValueError("private-message"), code="TRACKS_UNKNOWN")
        self.assertEqual(len(logs.records), len(reasons))
        for record, reason in zip(logs.records, reasons):
            self.assertEqual(record.provider, "yt_dlp_generic")
            self.assertEqual(record.reason, reason)
            self.assertEqual(record.code, "TRACKS_UNKNOWN")
            self.assertEqual(record.error_type, "ValueError")
            self.assertIsNone(record.exc_info)
            self.assertIsNone(record.stack_info)
        self.assertNotIn("private-message", repr(logs.output))

    def test_untrusted_diagnostic_values_are_not_logged(self):
        secret = "https://example.test/?token=private-secret\nCookie: private-cookie"
        class Unsafe:
            def __str__(self):
                raise AssertionError("must not stringify")

        with self.assertLogs(LOGGER, level="WARNING") as logs:
            capture.log_shadow_failure(secret, secret, ValueError(secret), code=secret)
            capture.log_shadow_failure(Unsafe(), Unsafe(), Unsafe(), code=Unsafe())
        for record in logs.records:
            self.assertEqual(record.provider, "unknown")
            self.assertEqual(record.code, "SHADOW_ERROR")
            self.assertEqual(record.reason, "invalid_reason")
            self.assertNotIn("private", record.getMessage())
            self.assertIsNone(record.exc_info)

    def test_logger_failure_never_escapes_capture_error_or_loses_flag(self):
        with capture.collect_raw() as session, patch.object(capture.logger, "warning", side_effect=RuntimeError("broken log")):
            capture.log_shadow_failure(PROVIDER, "worker_receive", ValueError("private"))
            capture.capture_error(PROVIDER, SOURCE)
        self.assertEqual(session.errors, {PROVIDER})

    def test_copy_failure_logs_class_without_raw_source_or_exception_text(self):
        with self.assertLogs(LOGGER, level="WARNING") as logs, capture.collect_raw() as session, \
                patch.object(capture, "deepcopy", side_effect=ValueError("private-copy-detail")):
            capture.capture_raw(PROVIDER, {"secret": "private-raw"}, SOURCE)
        self.assertEqual(session.records, ())
        self.assertEqual(len(logs.records), 1)
        self.assertEqual(logs.records[0].error_type, "ValueError")
        self.assertNotIn("private", repr(logs.output))
