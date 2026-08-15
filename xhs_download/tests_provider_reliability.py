import logging
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock
from types import SimpleNamespace
from unittest.mock import patch

import requests
from django.core.cache import cache
from django.test import SimpleTestCase, override_settings

from .services.assets import MediaAsset, ParseResult
from .services.common import XhsProviderTimeoutError
from .services.douyin_ultra_attempts import (
    DouyinUltraAttemptAccountingError,
    DouyinUltraAttemptRateLimitedError,
    DouyinUltraAttemptSupersededError,
)
from .services.platforms.douyin_providers import (
    DouyinParseContext,
    DouyinTianTianJieXiProvider,
    attach_native_attempt_observation,
    get_fast_native_result,
)
from .services.platforms.douyin_kukutool import (
    DouyinKukuToolProvider,
    build_kukutool_cache_key,
    get_cached_validated_candidate,
)
from .services.platforms.kukutool_generic import source_key_for_url
from .services.platforms.third_party import GenericThirdPartyAdapter
from .services.platforms.tiantianjiexi_generic import (
    TianTianJieXiApiError,
    TianTianJieXiGenericProvider,
    TianTianJieXiRateLimitError,
    build_failure_cache_key,
    build_payload_cache_key,
    build_payload_cache_metadata_key,
    build_single_flight_key,
)
from .services.provider_reliability import (
    ProviderFailureClass,
    ProviderOutcome,
    classify_provider_failure,
    provider_continuation_active,
    record_provider_attempt,
)
from .services.provider_settings import ProviderSettings


AWEME_ID = "123456789012345678"
SOURCE_URL = f"https://www.douyin.com/video/{AWEME_ID}"
MEDIA_URL = "https://v3-dy-o.zjcdn.com/video/resource.mp4"


class SequenceClient:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = 0

    def parse_url(self, _url, timeout_seconds=None):
        result = self.results[self.calls]
        self.calls += 1
        if isinstance(result, Exception):
            raise result
        return result


@override_settings(
    THIRD_PARTY_PROVIDER_ENABLED=True,
    TIANTIANJIEXI_PROVIDER_ENABLED=True,
    TIANTIANJIEXI_PHONE="phone",
    TIANTIANJIEXI_PASSWORD="password",
    TIANTIANJIEXI_RETRIES=1,
    TIANTIANJIEXI_REQUEST_LIMIT_PER_MINUTE=0,
    TIANTIANJIEXI_GLOBAL_MAX_CONCURRENCY=0,
    TIANTIANJIEXI_FAILURE_CACHE_TTL_SECONDS=1,
)
class ProviderReliabilityContractTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    def douyin_provider(self, client):
        return DouyinTianTianJieXiProvider(
            provider_settings=ProviderSettings(
                ultra_hd_enabled=True,
                provider_order=("tiantianjiexi", "native"),
                ultra_total_timeout_seconds=1,
            ),
            provider=TianTianJieXiGenericProvider(client=client),
            reserve_fallback_grace=False,
        )

    def context(self):
        return DouyinParseContext(
            url=SOURCE_URL,
            final_url=SOURCE_URL,
            aweme_id=AWEME_ID,
            prefer_ultra_hd=True,
            deadline_monotonic=time.monotonic() + 1,
        )

    def adapter(self):
        return SimpleNamespace(
            platform_id="douyin",
            headers={},
            media_host_suffixes=("zjcdn.com",),
            fetch_mobile_feed_result=lambda *_args, **_kwargs: None,
        )

    def test_outcome_taxonomy_is_fixed_and_terminal_log_is_url_free(self):
        self.assertEqual(ProviderOutcome.SUCCESS_FRESH.value, "SUCCESS_FRESH")
        self.assertEqual(
            ProviderOutcome.SUCCESS_EXISTING_RESULT.value,
            "SUCCESS_EXISTING_RESULT",
        )
        self.assertEqual(
            ProviderFailureClass.RESULT_INVALID.value,
            "RESULT_INVALID",
        )

        logger = logging.getLogger("xhs_download.provider_reliability")
        with self.assertLogs(logger, level="INFO") as captured:
            record_provider_attempt(
                provider="tiantianjiexi",
                outcome=ProviderOutcome.FAILURE_RESULT_INVALID,
                external_call=True,
                attempt_consumed=True,
                elapsed_ms=12,
                candidate_count=0,
                media_host="https://v3-dy-o.zjcdn.com/video/resource.mp4?token=secret",
                failure_class=ProviderFailureClass.RESULT_INVALID,
            )

        record = captured.records[0]
        self.assertEqual(record.getMessage(), "provider_attempt_completed")
        self.assertEqual(record.media_host, "v3-dy-o.zjcdn.com")
        safe_fields = str(record.__dict__)
        self.assertNotIn("/video/resource.mp4", safe_fields)
        self.assertNotIn("token", safe_fields)
        self.assertNotIn("secret", safe_fields)

    def test_attempt_controller_failures_keep_distinct_outcomes(self):
        cases = (
            (
                DouyinUltraAttemptRateLimitedError(5),
                ProviderOutcome.FAILURE_RATE_LIMIT,
                ProviderFailureClass.RATE_LIMIT,
            ),
            (
                DouyinUltraAttemptAccountingError("controlled accounting failure"),
                ProviderOutcome.FAILURE_UNKNOWN,
                ProviderFailureClass.UNKNOWN,
            ),
            (
                DouyinUltraAttemptSupersededError("controlled existing result"),
                ProviderOutcome.SUCCESS_EXISTING_RESULT,
                ProviderFailureClass.NOT_APPLICABLE,
            ),
        )

        for error, expected_outcome, expected_class in cases:
            with self.subTest(error=type(error).__name__):
                outcome, failure_class = classify_provider_failure(error)
                self.assertEqual(outcome, expected_outcome)
                self.assertEqual(failure_class, expected_class)

    def test_wrapped_upstream_failures_keep_safe_root_cause_class(self):
        cases = (
            (
                requests.HTTPError("controlled HTTP failure"),
                ProviderOutcome.FAILURE_HTTP,
                ProviderFailureClass.HTTP,
            ),
            (
                requests.ConnectionError("controlled network failure"),
                ProviderOutcome.FAILURE_NETWORK,
                ProviderFailureClass.NETWORK,
            ),
            (
                ValueError("controlled schema failure"),
                ProviderOutcome.FAILURE_SCHEMA,
                ProviderFailureClass.SCHEMA,
            ),
        )

        for cause, expected_outcome, expected_class in cases:
            with self.subTest(cause=type(cause).__name__):
                try:
                    raise TianTianJieXiApiError("safe wrapper") from cause
                except TianTianJieXiApiError as error:
                    outcome, failure_class = classify_provider_failure(error)
                self.assertEqual(outcome, expected_outcome)
                self.assertEqual(failure_class, expected_class)

    def test_tiantian_rate_limit_cooldown_keeps_rate_limit_class(self):
        provider = TianTianJieXiGenericProvider(
            client=SequenceClient(
                TianTianJieXiRateLimitError("controlled rate limit")
            )
        )

        with self.assertRaises(TianTianJieXiRateLimitError):
            provider.parse(
                SOURCE_URL,
                timeout_seconds=0.5,
                payload_validator=lambda value: bool(value.get("videos")),
            )

        status = provider.failure_cache_status(source_key_for_url(SOURCE_URL))
        self.assertTrue(status["hit"])
        self.assertEqual(
            status["failure_class"],
            ProviderFailureClass.RATE_LIMIT,
        )

    def test_continuation_state_uses_foreground_deadline(self):
        context = DouyinParseContext(
            url=SOURCE_URL,
            allow_continuation=True,
            deadline_monotonic=100,
        )

        self.assertFalse(
            provider_continuation_active(context, now_monotonic=99.9)
        )
        self.assertTrue(
            provider_continuation_active(context, now_monotonic=100)
        )

    def test_tiantian_fresh_result_after_foreground_is_continuation(self):
        payload = {
            "videos": [{"url": MEDIA_URL, "quality": "4K", "size": 1024}],
        }
        provider = TianTianJieXiGenericProvider(client=SequenceClient(payload))

        with self.assertLogs(
            "xhs_download.provider_reliability",
            level="INFO",
        ) as captured:
            provider.parse(
                SOURCE_URL,
                timeout_seconds=0.5,
                payload_validator=lambda value: bool(value.get("videos")),
                continuation_checker=lambda: True,
            )

        terminal = captured.records[-1]
        self.assertEqual(
            terminal.outcome,
            ProviderOutcome.SUCCESS_CONTINUATION.value,
        )
        self.assertTrue(terminal.continuation)

    def test_native_terminal_log_runs_after_future_becomes_observable(self):
        worker_gate = Event()
        logger_started = Event()
        logger_release = Event()
        observation = {}
        result = ParseResult(
            platform="douyin",
            post_type="video",
            base_filename="controlled",
            assets=(
                MediaAsset(
                    media_type="video",
                    url=MEDIA_URL,
                    platform="douyin",
                    size_bytes=1024,
                ),
            ),
        )

        def fetch_mobile_feed_result(*_args, **_kwargs):
            worker_gate.wait(timeout=1)
            return result

        def slow_record(**_fields):
            logger_started.set()
            logger_release.wait(timeout=1)

        adapter = SimpleNamespace(
            fetch_mobile_feed_result=fetch_mobile_feed_result,
            fetch_mobile_share_result=lambda *_args, **_kwargs: None,
        )
        with patch(
            "xhs_download.services.platforms.douyin_providers.record_provider_attempt",
            side_effect=slow_record,
        ), ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                get_fast_native_result,
                adapter,
                self.context(),
                0.5,
                observation,
            )
            attach_native_attempt_observation(future, observation)
            worker_gate.set()
            try:
                completed = future.result(timeout=0.1)
                self.assertEqual(completed.assets, result.assets)
                self.assertEqual(
                    completed.metadata["selected_provider"],
                    "native",
                )
                self.assertTrue(logger_started.wait(timeout=0.1))
            finally:
                logger_release.set()

    def test_tiantian_cooldown_preserves_fixed_failure_class(self):
        provider = TianTianJieXiGenericProvider(
            client=SequenceClient(XhsProviderTimeoutError("controlled timeout"))
        )
        validator = lambda value: bool(value.get("videos"))

        with self.assertRaises(XhsProviderTimeoutError):
            provider.parse(
                SOURCE_URL,
                timeout_seconds=0.5,
                payload_validator=validator,
            )

        status = provider.failure_cache_status(source_key_for_url(SOURCE_URL))
        self.assertTrue(status["hit"])
        self.assertEqual(status["failure_class"], ProviderFailureClass.TIMEOUT)

    @override_settings(
        THIRD_PARTY_PROVIDER_ORDER_SHORT_VIDEO=("tiantianjiexi_generic",),
    )
    def test_generic_adapter_tolerates_uninspectable_provider_callable(self):
        class Provider:
            def parse(self, _url, timeout_seconds=None):
                return {
                    "title": "controlled",
                    "videos": [{
                        "url": "https://v16.tiktokcdn.com/video.mp4",
                        "quality": "1080P",
                    }],
                }

        adapter = GenericThirdPartyAdapter(
            tiantianjiexi_provider=Provider(),
        )
        with patch(
            "xhs_download.services.platforms.third_party.inspect.signature",
            side_effect=ValueError("controlled uninspectable callable"),
        ):
            result = adapter.parse_url(
                "https://www.tiktok.com/@example/video/123456789012345678"
            )

        self.assertEqual(result.assets[0].provider, "tiantianjiexi_generic")

    def test_valid_fresh_payload_records_cache_age_without_changing_payload(self):
        payload = {
            "title": "valid",
            "videos": [{"url": MEDIA_URL, "quality": "4K", "size": 1024}],
        }
        source_key = source_key_for_url(SOURCE_URL)
        provider = TianTianJieXiGenericProvider(client=SequenceClient(payload))

        result = provider.parse(
            SOURCE_URL,
            timeout_seconds=0.5,
            payload_validator=lambda value: bool(value.get("videos")),
        )
        cached, payload_age_ms = provider.get_cached_payload_entry(
            source_key,
            payload_validator=lambda value: bool(value.get("videos")),
        )

        self.assertEqual(result, payload)
        self.assertEqual(cached, payload)
        self.assertGreaterEqual(payload_age_ms, 0)
        self.assertIsInstance(cache.get(build_payload_cache_metadata_key(source_key)), dict)

    def test_invalid_douyin_candidates_never_poison_success_cache(self):
        invalid_payloads = (
            {"videos": [{"url": "https://attacker.example/video.mp4", "size": 1024}]},
            {"videos": [{"url": "", "size": 1024}]},
            {"videos": [{"quality": "4K", "size": 1024}]},
            {"videos": [{"url": MEDIA_URL, "quality": "4K", "size": -1}]},
        )
        valid_payload = {
            "title": "valid",
            "videos": [{"url": MEDIA_URL, "quality": "4K", "size": 1024}],
        }
        source_key = source_key_for_url(SOURCE_URL)

        for invalid_payload in invalid_payloads:
            with self.subTest(payload=invalid_payload):
                cache.clear()
                client = SequenceClient(invalid_payload, valid_payload)
                provider = self.douyin_provider(client)

                with self.assertRaises(ValueError):
                    provider.parse(self.adapter(), self.context())

                self.assertIsNone(cache.get(build_payload_cache_key(source_key)))
                self.assertIsNone(cache.get(build_payload_cache_metadata_key(source_key)))

                result = provider.parse(
                    self.adapter(),
                    self.context(),
                )
                self.assertEqual(result.metadata["selected_provider"], "tiantianjiexi")
                self.assertEqual(client.calls, 2)

    def test_invalid_legacy_payload_cache_is_deleted_before_fresh_attempt(self):
        source_key = source_key_for_url(SOURCE_URL)
        cache.set(
            build_payload_cache_key(source_key),
            {"videos": [{"url": "https://attacker.example/video.mp4"}]},
            30,
        )
        valid_payload = {
            "title": "valid",
            "videos": [{"url": MEDIA_URL, "quality": "4K", "size": 1024}],
        }
        client = SequenceClient(valid_payload)

        result = self.douyin_provider(client).parse(
            self.adapter(),
            self.context(),
        )

        self.assertEqual(result.metadata["selected_provider"], "tiantianjiexi")
        self.assertEqual(client.calls, 1)

    def test_single_flight_lock_releases_after_exception_and_allows_success(self):
        source_key = source_key_for_url(SOURCE_URL)
        client = SequenceClient(
            XhsProviderTimeoutError("controlled timeout"),
            {"videos": [{"url": MEDIA_URL, "quality": "4K", "size": 1024}]},
        )
        provider = TianTianJieXiGenericProvider(client=client)
        validator = lambda value: bool(value.get("videos"))

        with self.assertRaises(XhsProviderTimeoutError):
            provider.parse(
                SOURCE_URL,
                timeout_seconds=0.5,
                payload_validator=validator,
            )

        self.assertIsNone(cache.get(build_single_flight_key(source_key)))
        cache.delete(build_failure_cache_key(source_key))

        result = provider.parse(
            SOURCE_URL,
            timeout_seconds=0.5,
            payload_validator=validator,
        )
        self.assertTrue(result["videos"])
        self.assertEqual(client.calls, 2)

    @override_settings(
        TIANTIANJIEXI_GLOBAL_MAX_CONCURRENCY=2,
        TIANTIANJIEXI_REQUEST_LIMIT_PER_MINUTE=0,
    )
    def test_tiantian_global_concurrency_never_exceeds_two(self):
        release = Event()
        capacity_reached = Event()
        state_lock = Lock()
        state = {"active": 0, "maximum": 0, "calls": 0}

        class ControlledClient:
            def parse_url(self, _url, timeout_seconds=None):
                with state_lock:
                    state["active"] += 1
                    state["calls"] += 1
                    state["maximum"] = max(state["maximum"], state["active"])
                    if state["active"] == 2:
                        capacity_reached.set()
                release.wait(timeout=1)
                with state_lock:
                    state["active"] -= 1
                return {"videos": [{"url": MEDIA_URL, "quality": "4K", "size": 1024}]}

        provider = TianTianJieXiGenericProvider(client=ControlledClient())

        def parse(index):
            return provider.parse(
                f"https://www.douyin.com/video/{AWEME_ID[:-1]}{index}",
                timeout_seconds=0.8,
                payload_validator=lambda value: bool(value.get("videos")),
            )

        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(parse, index) for index in range(4)]
            self.assertTrue(capacity_reached.wait(timeout=1))
            release.set()
            outcomes = []
            for future in futures:
                try:
                    outcomes.append(bool(future.result(timeout=1)))
                except Exception:
                    outcomes.append(False)

        self.assertEqual(state["maximum"], 2)
        self.assertGreaterEqual(state["calls"], 2)
        self.assertLessEqual(state["calls"], 4)
        self.assertEqual(outcomes.count(True), state["calls"])
        self.assertEqual(outcomes.count(False), 4 - state["calls"])

    def test_kukutool_global_concurrency_never_exceeds_four(self):
        release = Event()
        capacity_reached = Event()
        state_lock = Lock()
        state = {"active": 0, "maximum": 0, "calls": 0}
        expires_hex = f"{int(time.time()) + 3600:08x}"
        media_url = (
            "https://v11-default.365yg.com/"
            f"{expires_hex}/video/tos/cn/tos-cn-ve-15/source.mp4"
        )

        def runner(_aweme_id, **_kwargs):
            with state_lock:
                state["active"] += 1
                state["calls"] += 1
                state["maximum"] = max(state["maximum"], state["active"])
                if state["active"] == 4:
                    capacity_reached.set()
            release.wait(timeout=1)
            with state_lock:
                state["active"] -= 1
            return {
                "videos": [{
                    "video_fullinfo": [{
                        "type": "超高清",
                        "size": 70_481_987,
                        "url": media_url,
                    }],
                }],
            }

        provider = DouyinKukuToolProvider(
            ProviderSettings(
                ultra_hd_enabled=True,
                provider_order=("kukutool", "native"),
                kukutool_max_concurrency=4,
                kukutool_global_max_concurrency=4,
                kukutool_global_request_limit_per_minute=0,
                kukutool_failure_cache_ttl_seconds=0,
            ),
            runner=runner,
        )
        adapter = SimpleNamespace(
            headers={},
            media_host_suffixes=("365yg.com",),
        )

        def resolve(index):
            aweme_id = f"{AWEME_ID[:-1]}{index}"
            return provider.resolve_in_background(
                adapter,
                DouyinParseContext(
                    url=f"https://www.douyin.com/video/{aweme_id}",
                    aweme_id=aweme_id,
                ),
                f"controlled-lock-{index}",
                f"token-{index}",
            )

        with ThreadPoolExecutor(max_workers=6) as executor:
            futures = [executor.submit(resolve, index) for index in range(6)]
            self.assertTrue(capacity_reached.wait(timeout=1))
            release.set()
            results = [future.result(timeout=1) for future in futures]

        self.assertEqual(state["maximum"], 4)
        self.assertGreaterEqual(state["calls"], 4)
        self.assertLessEqual(state["calls"], 6)
        self.assertEqual(
            sum(result is not None for result in results),
            state["calls"],
        )
        self.assertEqual(
            sum(result is None for result in results),
            6 - state["calls"],
        )

    def test_kukutool_validated_cache_reports_payload_age_without_new_call(self):
        expires_hex = f"{int(time.time()) + 3600:08x}"
        media_url = (
            "https://v11-default.365yg.com/"
            f"{expires_hex}/video/tos/cn/tos-cn-ve-15/source.mp4"
        )
        runner = SequenceClient({
            "videos": [{
                "video_fullinfo": [{
                    "type": "超高清",
                    "size": 70_481_987,
                    "url": media_url,
                }],
            }],
        })
        provider = DouyinKukuToolProvider(
            ProviderSettings(
                ultra_hd_enabled=True,
                provider_order=("kukutool", "native"),
                kukutool_global_request_limit_per_minute=0,
            ),
            runner=lambda aweme_id, **kwargs: runner.parse_url(aweme_id),
        )
        adapter = SimpleNamespace(headers={}, media_host_suffixes=("365yg.com",))
        context = DouyinParseContext(
            url=SOURCE_URL,
            aweme_id=AWEME_ID,
        )

        fresh = provider.resolve_validated_candidate(adapter, context)
        cached = provider.resolve_validated_candidate(adapter, context)

        self.assertFalse(fresh.cache_hit)
        self.assertTrue(cached.cache_hit)
        self.assertGreaterEqual(cached.payload_age_ms, 0)
        self.assertEqual(runner.calls, 1)

    def test_kukutool_invalid_cache_metadata_is_evicted(self):
        cache_key = build_kukutool_cache_key(AWEME_ID)
        expires_hex = f"{int(time.time()) + 3600:08x}"
        media_url = (
            "https://v11-default.365yg.com/"
            f"{expires_hex}/video/tos/cn/tos-cn-ve-15/source.mp4"
        )
        cache.set(
            cache_key,
            {
                "url": media_url,
                "size_bytes": 70_481_987,
                "final_url": media_url,
                "object_key": "video/tos/cn/tos-cn-ve-15/source.mp4",
                "signed_url_expires_at": int(time.time()) + 3600,
                "candidate_count": 1,
                "cached_at": "invalid-timestamp",
            },
            300,
        )

        self.assertIsNone(
            get_cached_validated_candidate(cache_key, ("365yg.com",))
        )
        self.assertIsNone(cache.get(cache_key))

    def test_kukutool_cooldown_preserves_fixed_failure_class(self):
        provider = DouyinKukuToolProvider(
            ProviderSettings(
                ultra_hd_enabled=True,
                provider_order=("kukutool", "native"),
                kukutool_failure_cache_ttl_seconds=8,
            )
        )
        context = self.context()
        provider.cache_failure(
            context,
            "controlled_timeout",
            ProviderFailureClass.TIMEOUT,
        )

        with self.assertLogs(
            "xhs_download.provider_reliability",
            level="INFO",
        ) as captured:
            future, flight_state = provider.get_or_start_kukutool_future(
                self.adapter(),
                context,
            )

        self.assertIsNone(future)
        self.assertEqual(flight_state, "failure_cache_hit")
        terminal = captured.records[-1]
        self.assertEqual(
            terminal.outcome,
            ProviderOutcome.FAILURE_COOLDOWN.value,
        )
        self.assertEqual(
            terminal.failure_class,
            ProviderFailureClass.TIMEOUT.value,
        )
