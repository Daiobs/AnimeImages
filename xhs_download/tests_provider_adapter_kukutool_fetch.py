"""P6 fetch checks through the existing generic provider and helper boundary."""
from concurrent.futures import Future
import json
import subprocess
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

from django.core.cache import cache
from django.test import SimpleTestCase, override_settings

from .services.pipeline import Failure, FailureKind, Kind, Tracks, Work, validate_work
from .services.platforms import douyin_kukutool as helper
from .services.platforms import kukutool_generic as generic
from .services.provider_adapters.kukutool import KukutoolAdapter


SOURCE_URL = "https://www.tiktok.com/@demo/video/123"
MEDIA_URL = "https://v16.tiktokcdn.com/demo-1080.mp4"
NODE = "/isolated-fixture/node"


def raw_video():
    return {
        "id": "123",
        "title": "synthetic fetch evidence",
        "url": MEDIA_URL,
        "videos": [{
            "url": MEDIA_URL,
            "video_fullinfo": [
                {"url": "https://v16.tiktokcdn.com/demo-720.mp4", "type": "720P", "size": 4096},
                {"url": MEDIA_URL, "type": "1080P", "size": 8192},
            ],
        }],
        "upstream_only": {"nested": ["retained", {"value": 7}]},
    }


@override_settings(
    KUKUTOOL_NODE_BINARY=NODE,
    THIRD_PARTY_PROVIDER_ENABLED=True,
    THIRD_PARTY_KUKUTOOL_PROVIDER_ENABLED=True,
    THIRD_PARTY_GLOBAL_REQUEST_LIMIT_PER_MINUTE=100,
    THIRD_PARTY_KUKUTOOL_TIMEOUT_SECONDS=0.25,
    THIRD_PARTY_KUKUTOOL_RETRIES=1,
    THIRD_PARTY_KUKUTOOL_EXECUTOR_WORKERS=1,
    THIRD_PARTY_KUKUTOOL_MAX_PENDING_TASKS=2,
    THIRD_PARTY_KUKUTOOL_GLOBAL_MAX_CONCURRENCY=2,
    THIRD_PARTY_KUKUTOOL_SINGLE_FLIGHT_TTL_SECONDS=5,
    THIRD_PARTY_KUKUTOOL_CACHE_POLL_INTERVAL_SECONDS=0.02,
    THIRD_PARTY_KUKUTOOL_PAYLOAD_CACHE_TTL_SECONDS=120,
    THIRD_PARTY_KUKUTOOL_PROFILE_CACHE_TTL_SECONDS=0,
    THIRD_PARTY_KUKUTOOL_FAILURE_CACHE_TTL_SECONDS=0,
    THIRD_PARTY_KUKUTOOL_CIRCUIT_FAILURE_THRESHOLD=100,
    THIRD_PARTY_KUKUTOOL_CIRCUIT_WINDOW_SECONDS=60,
    THIRD_PARTY_KUKUTOOL_CIRCUIT_OPEN_SECONDS=5,
)
class KukutoolAdapterFetchTests(SimpleTestCase):
    def setUp(self):
        self.enterContext(override_settings(CACHES={
            "default": {
                "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
                "LOCATION": f"kukutool-adapter-fetch-{uuid4().hex}",
            },
        }))
        self.executors = {}
        self.futures = {}
        self.enterContext(patch.object(helper, "PROVIDER_EXECUTORS", self.executors))
        self.enterContext(patch.object(helper, "PROVIDER_CAPACITY", {}))
        self.enterContext(patch.object(generic, "LOCAL_PROVIDER_FUTURES", self.futures))
        self.enterContext(patch.object(helper, "KUKUTOOL_RUNTIME_STATE", {
            "checked": True, "available": True, "node_binary": NODE,
            "error": "", "checked_at": 0,
        }))
        self.http = self.enterContext(patch(
            "requests.sessions.Session.request", side_effect=AssertionError("unexpected HTTP"),
        ))
        self.dns = self.enterContext(patch(
            "socket.getaddrinfo", side_effect=AssertionError("unexpected DNS"),
        ))
        self.raw = raw_video()
        self.process = subprocess.CompletedProcess(
            [NODE], 0, stdout=json.dumps({"result": self.raw}), stderr="",
        )
        self.run = self.enterContext(patch.object(helper.subprocess, "run", return_value=self.process))
        self.adapter = KukutoolAdapter()
        self.source_key = generic.source_key_for_url(SOURCE_URL)
        self.cache_key = generic.build_payload_cache_key(self.source_key)
        # Join workers before restoring their cache, runtime and subprocess fixtures.
        self.addCleanup(self.shutdown_workers)

    def shutdown_workers(self):
        for executor in tuple(self.executors.values()):
            executor.shutdown(wait=True, cancel_futures=True)
        self.http.assert_not_called()
        self.dns.assert_not_called()

    def assert_failure(self, result, kind, code=None):
        self.assertIsInstance(result, Failure)
        self.assertIs(result.kind, kind)
        self.assertRegex(result.code, r"^[A-Z][A-Z0-9_]{0,63}$")
        if code is not None:
            self.assertEqual(result.code, code)
        for private in (SOURCE_URL, "upstream-secret", "Cookie", "Authorization"):
            self.assertNotIn(private, repr(result))

    def set_helper_failure(self, detail):
        self.process.returncode = 1
        self.process.stdout = ""
        self.process.stderr = (
            f"KUKUTOOL_SAFE_ERROR:{detail}\n"
            f"{SOURCE_URL}?token=upstream-secret Cookie Authorization"
        )

    def test_fetch_preserves_and_deepcopies_actual_helper_business_dictionary(self):
        seen = []
        original = generic.KukutoolGenericProvider.cache_payload

        def observe_cache(provider, key, payload):
            seen.append(payload)
            return original(provider, key, payload)

        with patch.object(generic.KukutoolGenericProvider, "cache_payload", observe_cache):
            result = self.adapter.fetch(SOURCE_URL, budget=1)

        self.assertEqual(self.adapter.name, "kukutool_generic")
        self.assertEqual(result, self.raw)
        self.assertEqual(seen, [self.raw])
        self.assertIsNot(result, seen[0])
        self.assertIsNot(result["upstream_only"], seen[0]["upstream_only"])
        self.assertEqual(cache.get(self.cache_key), self.raw)
        self.assertNotIn("result", result)
        self.assertNotIn("_service_format_rows", result)
        result["upstream_only"]["nested"][1]["value"] = 99
        self.assertEqual(seen[0], self.raw)
        self.assertEqual(cache.get(self.cache_key), self.raw)
        self.run.assert_called_once()
        args, kwargs = self.run.call_args
        self.assertEqual(args[0], [NODE, str(helper.KUKUTOOL_HELPER), SOURCE_URL])
        self.assertEqual(kwargs["timeout"], 0.25)
        self.assertFalse(kwargs["check"])

    def test_parse_preserves_two_raw_variants_and_unknown_tracks_without_legacy_conversion(self):
        with patch.object(helper, "run_kukutool_sidecar",
                          side_effect=AssertionError("unexpected sidecar")) as sidecar, \
                patch.object(helper, "convert_sidecar_payload_to_kukutool_payload",
                             side_effect=AssertionError("unexpected sidecar conversion")) as convert, \
                patch.object(generic.KukutoolResultNormalizer, "normalize_payload",
                             side_effect=AssertionError("unexpected normalization")) as normalize_payload, \
                patch.object(generic.KukutoolResultNormalizer, "normalize",
                             side_effect=AssertionError("unexpected normalization")) as normalize:
            work = self.adapter.parse(SOURCE_URL, budget=1)

        self.assertIsInstance(work, Work)
        self.assertEqual(validate_work(work), ())
        self.assertEqual(work.source_url, SOURCE_URL)
        self.assertEqual(work.work_id, "123")
        self.assertEqual(work.title, self.raw["title"])
        self.assertEqual(len(work.items), 1)
        video = work.items[0]
        self.assertIs(video.kind, Kind.VIDEO)
        self.assertEqual(len(video.variants), 2)
        rows = self.raw["videos"][0]["video_fullinfo"]
        self.assertEqual(
            [(variant.url, variant.quality_label, variant.size_bytes) for variant in video.variants],
            [(row["url"], row["type"], row["size"]) for row in rows],
        )
        self.assertEqual([variant.height for variant in video.variants], [720, 1080])
        for variant in video.variants:
            self.assertIs(variant.tracks, Tracks.UNKNOWN)
            self.assertEqual(variant.merge_with, "")
        self.assertEqual(self.raw, raw_video())
        self.assertEqual(cache.get(self.cache_key), self.raw)
        self.run.assert_called_once()
        self.assertEqual(self.run.call_args.args[0], [NODE, str(helper.KUKUTOOL_HELPER), SOURCE_URL])
        sidecar.assert_not_called()
        convert.assert_not_called()
        normalize_payload.assert_not_called()
        normalize.assert_not_called()

    def test_injected_runner_still_uses_existing_helper_pipeline(self):
        runner = Mock(wraps=helper.run_kukutool_url_helper)

        result = KukutoolAdapter(runner=runner).fetch(SOURCE_URL, budget=1)

        self.assertEqual(result, self.raw)
        runner.assert_called_once_with(
            SOURCE_URL, node_binary=NODE, timeout_seconds=0.25,
            profile_cache_ttl_seconds=0, profile_cache_key=generic.build_profile_cache_key(),
        )
        self.run.assert_called_once()

    def test_existing_raw_cache_needs_no_new_request_or_runtime(self):
        generic.KukutoolGenericProvider().cache_payload(self.source_key, self.raw)
        helper.KUKUTOOL_RUNTIME_STATE.update(available=False, node_binary="")

        result = self.adapter.fetch(SOURCE_URL, budget=1)
        result["upstream_only"]["nested"][1]["value"] = 99
        again = KukutoolAdapter().fetch(SOURCE_URL, budget=1)

        self.assertEqual(again, self.raw)
        self.assertEqual(cache.get(self.cache_key), self.raw)
        self.run.assert_not_called()
        self.assertEqual(self.executors, {})

    def test_repeated_fetch_reuses_raw_cache_without_normalization(self):
        first = self.adapter.fetch(SOURCE_URL, budget=1)
        second = KukutoolAdapter().fetch(SOURCE_URL, budget=1)

        self.assertEqual(first, self.raw)
        self.assertEqual(second, first)
        self.assertIsNot(first["upstream_only"], second["upstream_only"])
        self.run.assert_called_once()

    def test_normalized_or_converted_cache_is_rejected_without_refetch(self):
        normalized = generic.KukutoolResultNormalizer.normalize_payload(self.raw)
        payloads = [normalized, dict(normalized)]
        payloads.extend({**self.raw, marker: []} for marker in (
            "_service_format_rows", "_service_other", "_sidecar", "_retained_video_fullinfo",
        ))
        for payload in payloads:
            with self.subTest(payload_type=type(payload).__name__, keys=tuple(payload)):
                cache.set(self.cache_key, payload, timeout=120)
                result = self.adapter.fetch(SOURCE_URL, budget=1)
                self.assert_failure(result, FailureKind.INVALID_RESULT, "KUKUTOOL_NO_RAW")
                self.assertEqual(cache.get(self.cache_key), payload)
        self.run.assert_not_called()
        self.assertEqual(self.executors, {})

    def test_remote_single_flight_reuses_published_raw_and_preserves_owner(self):
        lock_key = generic.build_single_flight_key(self.source_key)
        cache.set(lock_key, "other-owner", timeout=10)

        def publish(_seconds):
            generic.KukutoolGenericProvider().cache_payload(self.source_key, self.raw)

        with patch.object(generic.time, "sleep", side_effect=publish) as sleep:
            result = self.adapter.fetch(SOURCE_URL, budget=1)

        self.assertEqual(result, self.raw)
        sleep.assert_called_once()
        self.assertEqual(cache.get(lock_key), "other-owner")
        self.run.assert_not_called()
        self.assertEqual(self.executors, {})

    def test_local_single_flight_waits_for_existing_helper_without_duplicate_submission(self):
        entered, release = Event(), Event()

        def blocked_helper(*args, **kwargs):
            entered.set()
            if not release.wait(2):
                raise AssertionError("test did not release helper")
            return self.process

        self.run.side_effect = blocked_helper
        self.addCleanup(release.set)
        future, state = generic.KukutoolGenericProvider().get_or_start_future(SOURCE_URL, self.source_key)
        self.assertEqual(state, "started")
        self.assertTrue(entered.wait(2))
        original_result = future.result

        def finish_existing(timeout=None):
            release.set()
            return original_result(timeout=timeout)

        with patch.object(future, "result", side_effect=finish_existing) as wait, \
                patch.object(generic, "submit_provider_task", wraps=generic.submit_provider_task) as submit:
            result = self.adapter.fetch(SOURCE_URL, budget=0.5)

        self.assertEqual(result, self.raw)
        wait.assert_called_once_with(timeout=0.5)
        submit.assert_not_called()
        self.run.assert_called_once()

    def test_existing_local_future_wait_timeout_does_not_launch_another_helper(self):
        future = Future()
        self.futures[self.source_key] = future

        result = self.adapter.fetch(SOURCE_URL, budget=0.1)

        self.assert_failure(result, FailureKind.TIMEOUT)
        self.assertFalse(future.done())
        self.assertIs(self.futures[self.source_key], future)
        self.run.assert_not_called()

    def test_exhausted_or_nonfinite_budget_never_starts_fetch_work(self):
        for method in (self.adapter.fetch, self.adapter.parse):
            for budget in (0, -1, float("nan"), float("inf"), -float("inf")):
                with self.subTest(method=method.__name__, budget=budget):
                    self.assert_failure(method(SOURCE_URL, budget=budget), FailureKind.TIMEOUT,
                                        "ADAPTER_BUDGET_EXHAUSTED")
        self.run.assert_not_called()
        self.assertEqual(self.executors, {})

    def test_parse_drops_result_if_budget_expires_before_mapping(self):
        clock = SimpleNamespace(monotonic=Mock(side_effect=[10.0, 11.0]))
        with patch("xhs_download.services.provider_adapters.base.time", clock), \
                patch.object(self.adapter, "map", wraps=self.adapter.map) as mapper:
            result = self.adapter.parse(SOURCE_URL, budget=0.5)

        self.assert_failure(result, FailureKind.TIMEOUT, "ADAPTER_BUDGET_EXHAUSTED")
        mapper.assert_not_called()
        self.run.assert_called_once()

    def test_subprocess_timeout_is_safe_without_adapter_retry(self):
        self.run.side_effect = subprocess.TimeoutExpired(
            [NODE, SOURCE_URL, "upstream-secret"], timeout=0.25,
        )

        result = self.adapter.fetch(SOURCE_URL, budget=1)

        self.assert_failure(result, FailureKind.TIMEOUT)
        self.run.assert_called_once()

    @override_settings(THIRD_PARTY_KUKUTOOL_RETRIES=2)
    def test_legacy_retry_count_is_reused_not_multiplied(self):
        self.set_helper_failure("SERVICE_PARSE_REJECTED")
        self.process.stderr += " please retry"

        result = self.adapter.fetch(SOURCE_URL, budget=1)

        self.assert_failure(result, FailureKind.UNAVAILABLE)
        self.assertEqual(self.run.call_count, 2)

    def test_safe_http_statuses_are_classified_without_upstream_details(self):
        for status, kind in (
            (404, FailureKind.NOT_FOUND), (410, FailureKind.NOT_FOUND),
            (408, FailureKind.TIMEOUT), (412, FailureKind.UNAVAILABLE),
            (429, FailureKind.UNAVAILABLE), (503, FailureKind.UNAVAILABLE),
        ):
            with self.subTest(status=status):
                self.set_helper_failure(f"SERVICE_HTTP_{status}")
                result = self.adapter.fetch(f"{SOURCE_URL}?case={status}", budget=1)
                self.assert_failure(result, kind, f"HTTP_{status}")
        self.assertEqual(self.run.call_count, 6)

    def test_service_json_diagnostics_keep_http_and_malformed_result_semantics(self):
        for detail, kind in (
            ("SERVICE_JSON_AUTH_HTTP_200_TYPE_HTML", FailureKind.INVALID_RESULT),
            ("SERVICE_JSON_PARSE_HTTP_200_TYPE_JSON", FailureKind.INVALID_RESULT),
            ("SERVICE_JSON_UNKNOWN_HTTP_UNKNOWN_TYPE_MISSING", FailureKind.INVALID_RESULT),
            ("SERVICE_JSON_PARSE_HTTP_404_TYPE_HTML", FailureKind.NOT_FOUND),
            ("SERVICE_JSON_PARSE_HTTP_408_TYPE_TEXT", FailureKind.TIMEOUT),
            ("SERVICE_JSON_AUTH_HTTP_429_TYPE_HTML", FailureKind.UNAVAILABLE),
            ("SERVICE_JSON_PARSE_HTTP_503_TYPE_OTHER", FailureKind.UNAVAILABLE),
        ):
            with self.subTest(detail=detail):
                self.set_helper_failure(detail)
                result = self.adapter.fetch(f"{SOURCE_URL}?case={detail}", budget=1)
                self.assert_failure(result, kind)
        self.assertEqual(self.run.call_count, 7)

    def test_business_and_unclassified_helper_failures_remain_unavailable(self):
        for detail in (
            "SERVICE_AUTH_REJECTED", "SERVICE_PARSE_REJECTED", "SERVICE_NETWORK_ERROR",
            "SERVICE_REDIRECT_REJECTED", "SERVICE_HELPER_FAILED",
            "SERVICE_PARSE_REJECTED_https://example.com/upstream-secret",
        ):
            with self.subTest(detail=detail):
                self.set_helper_failure(detail)
                result = self.adapter.fetch(f"{SOURCE_URL}?case={detail}", budget=1)
                self.assert_failure(result, FailureKind.UNAVAILABLE)
        self.assertEqual(self.run.call_count, 6)

    def test_invalid_json_and_oversize_service_failures_are_invalid_result(self):
        for detail in ("SERVICE_INVALID_JSON", "SERVICE_RESPONSE_TOO_LARGE"):
            with self.subTest(detail=detail):
                self.set_helper_failure(detail)
                result = self.adapter.fetch(f"{SOURCE_URL}?case={detail}", budget=1)
                self.assert_failure(result, FailureKind.INVALID_RESULT)
        self.assertEqual(self.run.call_count, 2)

    def test_malformed_helper_output_does_not_escape_as_exception(self):
        for index, stdout in enumerate(("", "not-json upstream-secret", "[]", "null", '{"result": []}')):
            with self.subTest(stdout=stdout):
                self.process.stdout = stdout
                result = self.adapter.fetch(f"{SOURCE_URL}?case={index}", budget=1)
                self.assert_failure(result, FailureKind.INVALID_RESULT)
        self.assertEqual(self.run.call_count, 5)

    def test_empty_business_dictionary_parse_rejects_missing_media(self):
        self.process.stdout = '{"result": {}}'

        result = self.adapter.parse(SOURCE_URL, budget=1)

        self.assert_failure(result, FailureKind.INVALID_RESULT)
        self.run.assert_called_once()

    def test_business_error_dictionary_is_preserved_then_classified_by_map(self):
        raw = {"code": 1001, "message": "upstream-secret", "data": {}}
        self.process.stdout = json.dumps({"result": raw})

        fetched = self.adapter.fetch(SOURCE_URL, budget=1)
        parsed = self.adapter.parse(SOURCE_URL, budget=1)

        self.assertEqual(fetched, raw)
        self.assert_failure(parsed, FailureKind.UNAVAILABLE)
        self.run.assert_called_once()

    def test_existing_global_concurrency_limit_prevents_subprocess(self):
        key = "limits:third-party-kukutool-global:all"
        cache.set(key, 2, timeout=10)

        result = self.adapter.fetch(SOURCE_URL, budget=1)

        self.assert_failure(result, FailureKind.UNAVAILABLE)
        self.assertEqual(cache.get(key), 2)
        self.assertIsNone(cache.get(generic.build_single_flight_key(self.source_key)))
        self.run.assert_not_called()

    @override_settings(THIRD_PARTY_GLOBAL_REQUEST_LIMIT_PER_MINUTE=1)
    def test_existing_global_rate_limit_is_not_bypassed(self):
        clock = SimpleNamespace(time=lambda: 120.0)
        with patch("xhs_download.services.rate_limits.time", clock):
            first = self.adapter.fetch(SOURCE_URL, budget=1)
            second = self.adapter.fetch(f"{SOURCE_URL}?different=1", budget=1)

        self.assertEqual(first, self.raw)
        self.assert_failure(second, FailureKind.UNAVAILABLE)
        self.run.assert_called_once()

    def test_full_executor_capacity_does_not_create_an_extra_pool(self):
        capacity = helper.get_provider_capacity("third-party-kukutool", 2)
        self.assertTrue(capacity.acquire(blocking=False))
        self.assertTrue(capacity.acquire(blocking=False))
        self.addCleanup(capacity.release)
        self.addCleanup(capacity.release)

        result = self.adapter.fetch(SOURCE_URL, budget=0.02)

        self.assert_failure(result, FailureKind.TIMEOUT)
        self.assertEqual(self.executors, {})
        self.assertIsNone(cache.get(generic.build_single_flight_key(self.source_key)))
        self.run.assert_not_called()

    def test_unavailable_runtime_is_safe_and_does_not_start_a_process(self):
        helper.KUKUTOOL_RUNTIME_STATE.update(available=False, node_binary="")

        result = self.adapter.fetch(SOURCE_URL, budget=1)

        self.assert_failure(result, FailureKind.UNAVAILABLE)
        self.run.assert_not_called()
