import logging
import threading
from pathlib import Path
from time import monotonic
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, override_settings

from .checks import check_tiantianjiexi_shared_concurrency_backend
from .services.platforms.third_party import GenericThirdPartyAdapter
from .services.platforms.douyin_providers import (
    DouyinTianTianJieXiProvider,
    DouyinUltraRaceProvider,
)
from .services.platforms.tiantianjiexi_generic import (
    TianTianJieXiApiClient,
    TianTianJieXiGenericProvider,
    TianTianJieXiProviderBusyError,
    build_payload_cache_key,
    parse_lease_ttl_seconds,
)
from .services.platforms.kukutool_generic import source_key_for_url
from .services.provider_concurrency import (
    TIANTIANJIEXI_GLOBAL_MAX_CONCURRENCY,
    TIANTIANJIEXI_PARSE_LEASE_KEY,
    acquire_cache_lease,
)


ROOT_DIR = Path(__file__).resolve().parents[1]


class FakeLeaseCache:
    def __init__(self, clock):
        self.clock = clock
        self.values = {}
        self.lock = threading.Lock()

    def _expire(self, key):
        value = self.values.get(key)
        if value and value[1] <= self.clock():
            self.values.pop(key, None)

    def add(self, key, value, timeout):
        with self.lock:
            self._expire(key)
            if key in self.values:
                return False
            self.values[key] = (value, self.clock() + timeout)
            return True

    def get(self, key, default=None):
        with self.lock:
            self._expire(key)
            value = self.values.get(key)
            return value[0] if value else default

    def delete(self, key):
        with self.lock:
            return self.values.pop(key, None) is not None


class ProviderLeaseTests(SimpleTestCase):
    def test_two_independent_callers_share_one_slot(self):
        now = [100.0]
        backend = FakeLeaseCache(lambda: now[0])

        first = acquire_cache_lease(
            TIANTIANJIEXI_PARSE_LEASE_KEY,
            ttl_seconds=30,
            require_cross_process=False,
            cache_backend=backend,
            token="process-a",
        )
        second = acquire_cache_lease(
            TIANTIANJIEXI_PARSE_LEASE_KEY,
            ttl_seconds=30,
            require_cross_process=False,
            cache_backend=backend,
            token="process-b",
        )

        self.assertTrue(first.acquired)
        self.assertFalse(second.acquired)
        self.assertEqual(second.reason, "provider_busy")
        self.assertEqual(TIANTIANJIEXI_GLOBAL_MAX_CONCURRENCY, 1)

    def test_release_makes_slot_available(self):
        now = [100.0]
        backend = FakeLeaseCache(lambda: now[0])
        first = acquire_cache_lease(
            "lease:release",
            ttl_seconds=30,
            require_cross_process=False,
            cache_backend=backend,
        )

        self.assertTrue(first.lease.release())
        second = acquire_cache_lease(
            "lease:release",
            ttl_seconds=30,
            require_cross_process=False,
            cache_backend=backend,
        )
        self.assertTrue(second.acquired)

    def test_expired_owner_cannot_delete_new_lease(self):
        now = [100.0]
        backend = FakeLeaseCache(lambda: now[0])
        old = acquire_cache_lease(
            "lease:ttl",
            ttl_seconds=2,
            require_cross_process=False,
            cache_backend=backend,
            token="old-owner",
        )
        now[0] = 103.0
        current = acquire_cache_lease(
            "lease:ttl",
            ttl_seconds=30,
            require_cross_process=False,
            cache_backend=backend,
            token="new-owner",
        )

        self.assertTrue(current.acquired)
        self.assertFalse(old.lease.release())
        self.assertEqual(backend.get("lease:ttl"), "new-owner")

    def test_backend_failure_fails_closed(self):
        backend = Mock()
        backend.add.side_effect = RuntimeError("cache unavailable")

        with self.assertLogs(
            "xhs_download.services.provider_concurrency",
            level=logging.ERROR,
        ):
            attempt = acquire_cache_lease(
                "lease:error",
                ttl_seconds=30,
                require_cross_process=False,
                cache_backend=backend,
            )

        self.assertFalse(attempt.acquired)
        self.assertEqual(attempt.reason, "lease_backend_error")

    def test_busy_wait_is_bounded(self):
        now = [0.0]
        backend = FakeLeaseCache(lambda: now[0])
        acquire_cache_lease(
            "lease:bounded",
            ttl_seconds=30,
            require_cross_process=False,
            cache_backend=backend,
        )

        def sleep_and_advance(seconds):
            now[0] += seconds

        attempt = acquire_cache_lease(
            "lease:bounded",
            ttl_seconds=30,
            wait_seconds=20,
            require_cross_process=False,
            cache_backend=backend,
            monotonic=lambda: now[0],
            sleeper=sleep_and_advance,
        )

        self.assertFalse(attempt.acquired)
        self.assertEqual(attempt.reason, "provider_busy")
        self.assertLessEqual(attempt.waited_ms, 250)


@override_settings(
    REQUIRE_PRODUCTION_CONFIG=False,
    THIRD_PARTY_PROVIDER_ENABLED=True,
    TIANTIANJIEXI_PROVIDER_ENABLED=True,
    TIANTIANJIEXI_PHONE="13800000000",
    TIANTIANJIEXI_PASSWORD="secret",
    TIANTIANJIEXI_REQUEST_LIMIT_PER_MINUTE=0,
    TIANTIANJIEXI_GLOBAL_SLOT_WAIT_SECONDS=0,
    TIANTIANJIEXI_RETRIES=1,
)
class TianTianJieXiProviderIsolationTests(TestCase):
    source_url = "https://www.tiktok.com/@demo/video/100"

    def setUp(self):
        cache.clear()

    def test_payload_cache_hit_does_not_acquire_any_lease(self):
        source_key = source_key_for_url(self.source_url)
        cache.set(
            build_payload_cache_key(source_key),
            {"title": "cached", "video_url": "https://v16.tiktokcdn.com/cached.mp4"},
            timeout=60,
        )

        with patch(
            "xhs_download.services.platforms.tiantianjiexi_generic.acquire_cache_lease",
            side_effect=AssertionError("cache hit must not acquire a lease"),
        ):
            payload = TianTianJieXiGenericProvider().parse(
                self.source_url,
                timeout_seconds=1,
            )

        self.assertEqual(payload["title"], "cached")

    @override_settings(
        TIANTIANJIEXI_GLOBAL_LEASE_TTL_SECONDS=5,
        TIANTIANJIEXI_TIMEOUT_SECONDS=15,
    )
    def test_lease_ttl_covers_scalar_connect_and_read_timeout_window(self):
        self.assertEqual(parse_lease_ttl_seconds(15), 35)

    @override_settings(TIANTIANJIEXI_GLOBAL_SLOT_WAIT_SECONDS=0.25)
    def test_slot_wait_is_charged_to_the_assigned_provider_deadline(self):
        client = Mock()
        client.parse_url.return_value = {
            "title": "uncached upstream",
            "video_url": "https://v16.tiktokcdn.com/uncached-upstream.mp4",
        }
        single_flight_lease = Mock()
        parse_lease = Mock()
        lease_attempts = (
            SimpleNamespace(
                acquired=True,
                reason="acquired",
                waited_ms=0,
                shared_backend=False,
                lease=single_flight_lease,
            ),
            SimpleNamespace(
                acquired=True,
                reason="acquired",
                waited_ms=200,
                shared_backend=True,
                lease=parse_lease,
            ),
        )

        with (
            patch(
                "xhs_download.services.platforms.tiantianjiexi_generic.acquire_cache_lease",
                side_effect=lease_attempts,
            ) as acquire_lease,
            patch(
                "xhs_download.services.platforms.tiantianjiexi_generic.time.monotonic",
                side_effect=(100.0, 100.2, 100.25),
            ),
        ):
            payload = TianTianJieXiGenericProvider(client=client).parse(
                self.source_url,
                timeout_seconds=1,
            )

        self.assertEqual(payload["title"], "uncached upstream")
        self.assertEqual(acquire_lease.call_args_list[1].kwargs["wait_seconds"], 0.25)
        self.assertAlmostEqual(
            client.parse_url.call_args.kwargs["timeout_seconds"],
            0.75,
        )
        single_flight_lease.release.assert_called_once_with()
        parse_lease.release.assert_called_once_with()

    def test_global_busy_skips_real_upstream_and_emits_safe_event(self):
        active = acquire_cache_lease(
            TIANTIANJIEXI_PARSE_LEASE_KEY,
            ttl_seconds=30,
            require_cross_process=False,
        )
        client = Mock()
        provider = TianTianJieXiGenericProvider(client=client)

        try:
            with self.assertLogs(
                "xhs_download.services.platforms.tiantianjiexi_generic",
                level=logging.INFO,
            ) as captured:
                with self.assertRaises(TianTianJieXiProviderBusyError):
                    provider.parse(self.source_url, timeout_seconds=1)
        finally:
            active.lease.release()

        client.parse_url.assert_not_called()
        records = [record for record in captured.records if record.msg == "provider_busy"]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].busy_scope, "global_parse")
        self.assertEqual(records[0].busy_reason, "provider_busy")
        rendered = " ".join(captured.output)
        self.assertNotIn(self.source_url, rendered)
        self.assertNotIn("secret", rendered)

    def test_exception_releases_global_slot(self):
        class FailingClient:
            def parse_url(self, url, timeout_seconds=None):
                raise RuntimeError("upstream failed")

        with self.assertRaises(RuntimeError):
            TianTianJieXiGenericProvider(client=FailingClient()).parse(
                self.source_url,
                timeout_seconds=1,
            )

        next_attempt = acquire_cache_lease(
            TIANTIANJIEXI_PARSE_LEASE_KEY,
            ttl_seconds=30,
            require_cross_process=False,
        )
        self.assertTrue(next_attempt.acquired)
        next_attempt.lease.release()

    def test_single_and_batch_callers_share_one_global_upstream_slot(self):
        started = threading.Event()
        release = threading.Event()
        state_lock = threading.Lock()
        active_calls = 0
        max_active_calls = 0
        outcomes = []

        class BlockingClient:
            def parse_url(self, url, timeout_seconds=None):
                nonlocal active_calls, max_active_calls
                with state_lock:
                    active_calls += 1
                    max_active_calls = max(max_active_calls, active_calls)
                started.set()
                try:
                    release.wait(timeout=2)
                    return {
                        "title": "ok",
                        "video_url": "https://v16.tiktokcdn.com/ok.mp4",
                    }
                finally:
                    with state_lock:
                        active_calls -= 1

        def run(source_url):
            try:
                TianTianJieXiGenericProvider(client=BlockingClient()).parse(
                    source_url,
                    timeout_seconds=1,
                )
                outcomes.append("success")
            except TianTianJieXiProviderBusyError:
                outcomes.append("provider_busy")

        first = threading.Thread(target=run, args=(self.source_url,))
        second = threading.Thread(
            target=run,
            args=("https://www.tiktok.com/@demo/video/200",),
        )
        first.start()
        self.assertTrue(started.wait(timeout=1))
        second.start()
        second.join(timeout=1)
        release.set()
        first.join(timeout=2)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(max_active_calls, 1)
        self.assertCountEqual(outcomes, ("success", "provider_busy"))

    def test_douyin_provider_race_respects_shared_slot_and_uses_fallback(self):
        from .services.assets import MediaAsset, ParseResult

        client = Mock()
        tiantian_started = threading.Event()
        tiantian_finished = threading.Event()
        provider_settings = SimpleNamespace(
            ultra_total_timeout_seconds=1.0,
            enabled_provider_order=("tiantianjiexi", "kukutool"),
            provider_is_enabled=lambda name: name in {"tiantianjiexi", "kukutool"},
        )
        guarded_provider = TianTianJieXiGenericProvider(client=client)

        class SignalingProvider:
            def parse(self, url, timeout_seconds=None):
                tiantian_started.set()
                try:
                    return guarded_provider.parse(url, timeout_seconds=timeout_seconds)
                finally:
                    tiantian_finished.set()

        tiantian = DouyinTianTianJieXiProvider(
            provider_settings=provider_settings,
            provider=SignalingProvider(),
            reserve_fallback_grace=False,
        )

        class KukuFallback:
            name = "kukutool"

            def parse(self, adapter, context):
                if not tiantian_started.wait(timeout=1):
                    raise AssertionError("TianTian race caller did not start")
                if not tiantian_finished.wait(timeout=1):
                    raise AssertionError("TianTian race caller did not finish")
                return ParseResult(
                    platform="douyin",
                    post_type="video",
                    base_filename="fallback",
                    assets=(
                        MediaAsset(
                            media_type="video",
                            url="https://v16.tiktokcdn.com/fallback.mp4",
                            platform="douyin",
                            provider="kukutool",
                            quality_label="4K",
                            size_bytes=80 * 1024 * 1024,
                        ),
                    ),
                    metadata={"selected_provider": "kukutool"},
                )

        context = SimpleNamespace(
            url=self.source_url,
            final_url=self.source_url,
            aweme_id="100",
            deadline_monotonic=monotonic() + 1,
            allow_continuation=False,
        )
        held = acquire_cache_lease(
            TIANTIANJIEXI_PARSE_LEASE_KEY,
            ttl_seconds=30,
            require_cross_process=False,
        )
        self.assertTrue(held.acquired)
        try:
            result = DouyinUltraRaceProvider(
                provider_settings=provider_settings,
                providers=(tiantian, KukuFallback()),
            ).parse(Mock(platform_id="douyin"), context)
        finally:
            held.lease.release()

        self.assertEqual(result.metadata["selected_provider"], "kukutool")
        client.parse_url.assert_not_called()

    def test_generic_adapter_records_busy_then_falls_back(self):
        class BusyProvider:
            def parse(self, url, timeout_seconds=None):
                raise TianTianJieXiProviderBusyError("busy")

        class SuccessfulProvider:
            def parse(self, url, timeout_seconds=None):
                return {"video_url": "https://v16.tiktokcdn.com/fallback.mp4"}

        class Normalizer:
            def normalize(self, **kwargs):
                from .services.assets import MediaAsset, ParseResult

                return ParseResult(
                    platform="third_party",
                    post_type="video",
                    base_filename="fallback",
                    assets=(
                        MediaAsset(
                            media_type="video",
                            url="https://v16.tiktokcdn.com/fallback.mp4",
                            platform="third_party",
                        ),
                    ),
                )

        class Adapter(GenericThirdPartyAdapter):
            def iter_provider_specs(self, rule, *, respect_enabled=True):
                normalizer = Normalizer()
                yield "tiantianjiexi_generic", BusyProvider(), normalizer
                yield "yt_dlp_generic", SuccessfulProvider(), normalizer

        result = Adapter().parse_url(self.source_url, timeout_seconds=1)

        self.assertEqual(result.metadata["selected_provider"], "yt_dlp_generic")
        self.assertEqual(
            [attempt["status"] for attempt in result.metadata["provider_attempts"]],
            ["provider_busy", "success"],
        )

    def test_account_health_uses_separate_lease_from_parse(self):
        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                return {"available": True}

        parse_lease = acquire_cache_lease(
            TIANTIANJIEXI_PARSE_LEASE_KEY,
            ttl_seconds=30,
            require_cross_process=False,
        )
        session = Mock()
        session.get.return_value = Response()
        try:
            status = TianTianJieXiApiClient(session=session).query_account_status()
        finally:
            parse_lease.lease.release()

        self.assertTrue(status["available"])
        session.get.assert_called_once()


class WebConcurrencyConfigurationTests(SimpleTestCase):
    def test_tracked_gunicorn_candidate_is_two_by_four_gthread(self):
        unit = (ROOT_DIR / "ops/systemd/animeimages.service").read_text()
        exec_start = next(
            line for line in unit.splitlines() if line.startswith("ExecStart=")
        )

        self.assertIn("--worker-class gthread", exec_start)
        self.assertIn("--workers 2", exec_start)
        self.assertIn("--threads 4", exec_start)
        self.assertIn("--timeout 60", exec_start)
        self.assertNotIn("--timeout 600", exec_start)

    @override_settings(
        REQUIRE_PRODUCTION_CONFIG=True,
        TIANTIANJIEXI_PROVIDER_ENABLED=True,
        CACHES={
            "default": {
                "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
                "LOCATION": "provider-isolation-check",
            }
        },
    )
    @patch.dict("os.environ", {"TIANTIANJIEXI_GLOBAL_MAX_CONCURRENCY": "1"})
    def test_production_tiantian_requires_cross_process_cache(self):
        issues = check_tiantianjiexi_shared_concurrency_backend(None)

        self.assertEqual([issue.id for issue in issues], ["xhs_download.E013"])
