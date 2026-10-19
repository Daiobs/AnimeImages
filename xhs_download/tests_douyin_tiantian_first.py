"""Sequential Douyin service policy through real providers and isolated boundaries."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import BoundedSemaphore, Event
import time
from unittest.mock import Mock, patch

import requests
from django.core.cache import cache
from django.test import SimpleTestCase, override_settings

from . import tests_douyin_format_selection as fixtures
from .services.assets import MediaAsset, ParseResult
from .services.common import XhsProviderTimeoutError
from .services.douyin_ultra_attempts import DouyinUltraAttemptController, DouyinUltraAttemptRateLimitedError
from .services.platforms.douyin import DouyinAdapter
from .services.platforms.douyin_kukutool import build_kukutool_cache_key
from .services.platforms.douyin_providers import (
    DouyinParseContext, DouyinTianTianFirstProvider, DouyinNativeProvider,
    build_douyin_result_providers, build_douyin_metadata_cache_key,
    submit_provider_task,
)
from .services.platforms.tiantianjiexi_generic import (
    TianTianJieXiApiClient, TianTianJieXiGenericProvider,
    build_payload_cache_key, build_single_flight_key, source_key_for_request,
)
from .services.platforms.tiantian_serial import inspect_or_recover
from .services.provider_settings import ProviderSettings
from .services.rate_limits import LimitResult
from .services.registry import PlatformRegistry
from .tests_douyin_kukutool_retention import resource


MODULE = "xhs_download.services.platforms.douyin_providers"
AWEME = fixtures.AWEME


@override_settings(
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
    THIRD_PARTY_PROVIDER_ENABLED=True, TIANTIANJIEXI_PROVIDER_ENABLED=True,
    TIANTIANJIEXI_PHONE="fixture", TIANTIANJIEXI_PASSWORD="fixture",
    TIANTIANJIEXI_RETRIES=1, TIANTIANJIEXI_REQUEST_LIMIT_PER_MINUTE=0,
    TIANTIANJIEXI_GLOBAL_MAX_CONCURRENCY=0,
)
class DouyinTianTianFirstTests(SimpleTestCase):
    def setUp(self):
        self.case = fixtures.DouyinFormatSelectionTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        real_monotonic = time.monotonic
        self.wall = time.time()
        self.elapsed = 0
        self.clock = lambda: real_monotonic() + self.elapsed
        self.tasks = []
        self.release = Event()
        for name, value in (
            ("time.time", lambda: self.wall),
            ("time.monotonic", self.clock),
            (f"{MODULE}.time", lambda: self.wall),
            (f"{MODULE}.monotonic", self.clock),
            ("xhs_download.services.platforms.douyin.monotonic", self.clock),
            ("xhs_download.services.platforms.douyin_kukutool.time", lambda: self.wall),
            ("xhs_download.services.platforms.douyin_kukutool.monotonic", self.clock),
            ("xhs_download.services.platforms.tiantian_serial.clock", self.clock),
            ("xhs_download.services.platforms.douyin_kukutool.is_kukutool_admin_disabled", lambda: False),
        ):
            active = patch(name, side_effect=value)
            active.start(); self.addCleanup(active.stop)

        def tracked_submit(*args):
            future = submit_provider_task(*args)
            if future is not None:
                self.tasks.append(future)
            return future
        active = patch(f"{MODULE}.submit_provider_task", side_effect=tracked_submit)
        active.start(); self.addCleanup(active.stop)
        self.addCleanup(self.finish_tasks)
        self.settings = ProviderSettings(
            ultra_hd_enabled=True, provider_order=("tiantianjiexi", "kukutool", "native"),
            ultra_total_timeout_seconds=5, ultra_native_timeout_seconds=3,
            ultra_kukutool_grace_seconds=1, kukutool_validated_cache_ttl_seconds=300,
            kukutool_cache_poll_interval_seconds=0.005,
        )
        self.adapter = DouyinAdapter()
        self.adapter.fetch_mobile_feed_result = Mock(return_value=None)
        self.adapter.fetch_mobile_share_result = Mock(return_value=None)
        self.events = []
        self.response = self.case.session.post.return_value
        self.case.session.post.side_effect = self.post
        self.runner = Mock(side_effect=self.run_kuku)
        self.coordinator = build_douyin_result_providers(self.settings)[0]
        self.assertIsInstance(self.coordinator, DouyinTianTianFirstProvider)
        self.tian, self.kuku = self.coordinator.providers
        self.tian.provider = TianTianJieXiGenericProvider(
            client=TianTianJieXiApiClient(session=self.case.session),
        )
        self.kuku.runner = self.runner
        self.consume = Mock(return_value=LimitResult(allowed=True))
        self.source = f"https://www.douyin.com/video/{AWEME}"
        self.source_key = source_key_for_request(self.source, f"douyin:{AWEME}")

    def finish_tasks(self):
        self.release.set()
        for future in self.tasks:
            try:
                future.result(timeout=3)
            except Exception:
                pass
            self.assertTrue(future.done(), "An isolated provider task escaped the test")

    def advance(self, seconds):
        self.elapsed += seconds
        self.wall += seconds

    def post(self, *args, **kwargs):
        self.events.append("tiantianjiexi")
        return self.response

    def run_kuku(self, *args, **kwargs):
        self.events.append("kukutool")
        return {"videos": [{"video_fullinfo": [resource("1080P")]}]}

    def context(self):
        return DouyinParseContext(
            url=self.source, aweme_id=AWEME, prefer_ultra_hd=True,
            deadline_monotonic=self.clock() + self.settings.ultra_total_timeout_seconds,
            allow_continuation=True, continuation_deadline_monotonic=self.clock() + 100,
            ultra_attempt_controller=DouyinUltraAttemptController(
                client_identity="fixture", limit=10, consume=self.consume,
            ),
        )

    def seed_kuku(self):
        self.kuku.resolve_validated_candidate(self.adapter, replace(self.context(), ultra_attempt_controller=None))
        self.events.clear(); self.runner.reset_mock()

    def seed_tian(self, ttl=120):
        cache.set(build_payload_cache_key(self.source_key), {
            "title": "Current cached work", "cover": "https://p3.douyinpic.com/fixture.jpg",
            "videos": [{"url": resource("720P")["url"], "quality": "720P", "size": 5000}],
        }, ttl)

    def fail_tian(self):
        self.response.json.return_value = {"code": 400, "msg": "Controlled refusal"}

    def parse_entry(self, *, controller=None, allow_continuation=True):
        with patch("xhs_download.services.platforms.douyin.get_platform_provider_settings", return_value=self.settings), \
                patch.object(self.adapter, "get_result_providers", return_value=(self.coordinator, DouyinNativeProvider())):
            return self.adapter.parse_url(
                self.source, prefer_ultra_hd=True, allow_continuation=allow_continuation,
                ultra_attempt_controller=controller,
            )

    def test_real_builder_selects_sequence_and_success_starts_only_tian(self):
        result = self.coordinator.parse(self.adapter, self.context())
        self.assertEqual(result.metadata["selected_provider"], "tiantianjiexi")
        self.assertEqual(len(result.metadata["upstream_candidates"]), 3)
        self.assertNotIn("ultra_race_winner", result.metadata)
        self.assertEqual(self.events, ["tiantianjiexi"])
        self.runner.assert_not_called()
        self.adapter.fetch_mobile_feed_result.assert_not_called()
        self.consume.assert_called_once()

    def test_low_quality_success_does_not_trigger_fallback_or_native_enrichment(self):
        data = self.response.json.return_value["data"]
        data["title"] = ""; data["cover"] = ""
        data["videos"][0]["streams"] = [data["videos"][0]["streams"][-1]]
        with patch(f"{MODULE}.get_reference_result", side_effect=AssertionError("No historical metadata")):
            result = self.coordinator.parse(self.adapter, self.context())
        self.assertEqual(result.metadata["selected_provider"], "tiantianjiexi")
        self.assertEqual(self.events, ["tiantianjiexi"])
        self.adapter.fetch_mobile_feed_result.assert_not_called()

    def test_valid_tian_media_with_malformed_title_does_not_trigger_fallback(self):
        self.response.json.return_value["data"]["title"] = "https://[broken"
        result = self.coordinator.parse(self.adapter, self.context())
        self.assertTrue(result.video_assets)
        self.assertEqual(result.metadata["selected_provider"], "tiantianjiexi")
        self.assertEqual(self.events, ["tiantianjiexi"])
        self.runner.assert_not_called()
        self.adapter.fetch_mobile_feed_result.assert_not_called()
        self.adapter.fetch_mobile_share_result.assert_not_called()

    def test_tian_cache_is_zero_external_and_read_does_not_renew_ttl(self):
        self.seed_tian(ttl=2)
        result = self.coordinator.parse(self.adapter, self.context())
        self.assertEqual(result.metadata["selected_provider"], "tiantianjiexi")
        self.assertTrue(result.metadata["tiantianjiexi_cache_hit"])
        self.assertEqual(self.events, [])
        self.consume.assert_not_called()
        self.adapter.fetch_mobile_feed_result.assert_not_called()
        self.wall += 3
        self.assertIsNone(cache.get(build_payload_cache_key(self.source_key)))

    def test_expired_tian_with_valid_kuku_cache_still_refreshes_tian(self):
        self.seed_tian(ttl=2); self.seed_kuku()
        self.wall += 3
        result = self.coordinator.parse(self.adapter, self.context())
        self.assertEqual(result.metadata["selected_provider"], "tiantianjiexi")
        self.assertEqual(self.events, ["tiantianjiexi"])
        self.runner.assert_not_called()
        self.assertIsNotNone(cache.get(build_kukutool_cache_key(AWEME)))

    def test_registry_reparse_after_tian_expiry_does_not_reuse_longer_lived_result(self):
        self.seed_tian(ttl=2)
        self.seed_kuku()
        registry = PlatformRegistry(adapters=(self.adapter,))
        with patch("xhs_download.services.platforms.douyin.get_platform_provider_settings", return_value=self.settings), \
                patch.object(self.adapter, "get_result_providers", return_value=(self.coordinator, DouyinNativeProvider())), \
                patch("xhs_download.services.upstream_routes.cached_parse_result",
                      side_effect=AssertionError("Douyin must not use generic route result cache")), \
                patch("xhs_download.services.platforms.douyin.find_active_douyin_continuation",
                      side_effect=AssertionError("No longer-lived continuation result")):
            first = registry.parse_url(self.source, prefer_ultra_hd=True, allow_continuation=True)
            self.assertEqual(first.metadata["title"], "Current cached work")
            self.assertEqual(self.events, [])
            self.wall += 3
            self.assertIsNone(cache.get(build_payload_cache_key(self.source_key)))
            self.assertIsNotNone(cache.get(build_kukutool_cache_key(AWEME)))
            second = registry.parse_url(self.source, prefer_ultra_hd=True, allow_continuation=True)
        self.assertEqual(second.metadata["title"], "Fixture work")
        self.assertEqual(second.metadata["selected_provider"], "tiantianjiexi")
        self.assertEqual(self.events, ["tiantianjiexi"])
        self.runner.assert_not_called()
        self.adapter.fetch_mobile_feed_result.assert_not_called()
        self.adapter.fetch_mobile_share_result.assert_not_called()

    def test_failure_uses_kuku_cache_only_after_tian_attempt(self):
        self.seed_kuku(); self.fail_tian()
        result = self.coordinator.parse(self.adapter, self.context())
        self.assertEqual(result.metadata["selected_provider"], "kukutool")
        self.assertEqual(self.events, ["tiantianjiexi"])
        self.runner.assert_not_called()
        self.adapter.fetch_mobile_feed_result.assert_not_called()

    def test_failure_starts_one_new_kuku_after_tian(self):
        self.fail_tian()
        result = self.coordinator.parse(self.adapter, self.context())
        self.assertEqual(result.metadata["selected_provider"], "kukutool")
        self.assertEqual(self.events, ["tiantianjiexi", "kukutool"])
        self.consume.assert_called_once()
        self.adapter.fetch_mobile_feed_result.assert_not_called()

    def test_disabled_tian_remains_in_same_sequence_and_does_not_prewarm_native(self):
        with override_settings(TIANTIANJIEXI_PROVIDER_ENABLED=False):
            built = build_douyin_result_providers(self.settings)
            self.assertIsInstance(built[0], DouyinTianTianFirstProvider)
            result = self.coordinator.parse(self.adapter, self.context())
        self.assertEqual(result.metadata["selected_provider"], "kukutool")
        self.assertEqual(self.events, ["kukutool"])
        self.adapter.fetch_mobile_feed_result.assert_not_called()

    def test_unconfigured_tian_does_not_send_and_falls_back(self):
        with patch("xhs_download.services.platforms.tiantianjiexi_generic.provider_configured", return_value=False):
            result = self.coordinator.parse(self.adapter, self.context())
        self.assertEqual(result.metadata["selected_provider"], "kukutool")
        self.assertEqual(self.events, ["kukutool"])

    def test_user_quota_denial_still_allows_valid_cached_fallback(self):
        self.seed_kuku()
        self.consume.return_value = LimitResult(allowed=False, retry_after=20)
        result = self.coordinator.parse(self.adapter, self.context())
        self.assertEqual(result.metadata["selected_provider"], "kukutool")
        self.case.session.post.assert_not_called(); self.runner.assert_not_called()

    def test_user_quota_denial_blocks_new_kuku_but_permits_native(self):
        self.consume.return_value = LimitResult(allowed=False, retry_after=20)
        self.adapter.fetch_mobile_feed_result.return_value = ParseResult(
            platform="douyin", post_type="video", base_filename="Current Native",
            assets=(MediaAsset(media_type="video", url=resource("native")["url"], platform="douyin"),),
        )
        result = self.parse_entry(controller=self.context().ultra_attempt_controller)
        self.assertEqual(result.metadata["selected_provider"], "mobile_feed")
        self.case.session.post.assert_not_called(); self.runner.assert_not_called()
        self.adapter.fetch_mobile_feed_result.assert_called_once()

    def test_quota_error_is_preserved_when_all_authorized_fallbacks_fail(self):
        self.consume.return_value = LimitResult(allowed=False, retry_after=20)
        with self.assertRaises(DouyinUltraAttemptRateLimitedError):
            self.parse_entry(controller=self.context().ultra_attempt_controller)
        self.case.session.post.assert_not_called(); self.runner.assert_not_called()

    def test_real_timeout_marks_account_unknown_and_next_request_does_not_repeat(self):
        self.seed_kuku()
        self.case.session.post.side_effect = requests.Timeout("Controlled final HTTP timeout")
        first = self.coordinator.parse(self.adapter, self.context())
        self.assertEqual(first.metadata["selected_provider"], "kukutool")
        before = inspect_or_recover()
        self.assertEqual(before["marker"], "U")
        second = self.coordinator.parse(self.adapter, self.context())
        self.assertEqual(second.metadata["selected_provider"], "kukutool")
        self.case.session.post.assert_called_once()
        self.assertEqual(inspect_or_recover()["state_fingerprint"], before["state_fingerprint"])

    def test_existing_single_flight_wait_does_not_send_or_clear_owner(self):
        self.seed_kuku()
        key = build_single_flight_key(self.source_key)
        cache.set(key, "existing-owner", 30)
        def advance_wait(seconds):
            self.advance(seconds)
        with patch("xhs_download.services.platforms.tiantianjiexi_generic.time.sleep", side_effect=advance_wait):
            result = self.coordinator.parse(self.adapter, self.context())
        self.assertEqual(result.metadata["selected_provider"], "kukutool")
        self.case.session.post.assert_not_called()
        self.assertEqual(cache.get(key), "existing-owner")

    def test_concurrent_same_work_uses_account_serial_and_one_fresh_request(self):
        entered = Event()
        def blocking_post(*args, **kwargs):
            self.events.append("tiantianjiexi")
            entered.set()
            self.release.wait(2)
            return self.response
        self.case.session.post.side_effect = blocking_post
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.coordinator.parse, self.adapter, self.context())
            self.assertTrue(entered.wait(1))
            second = pool.submit(self.coordinator.parse, self.adapter, self.context())
            self.release.set()
            results = (first.result(timeout=3), second.result(timeout=3))
        self.assertTrue(all(r.metadata["selected_provider"] == "tiantianjiexi" for r in results))
        self.case.session.post.assert_called_once(); self.runner.assert_not_called()

    def test_five_second_budget_reserves_one_second_each_for_kuku_and_native(self):
        captured = {}
        def tian_timeout(*args, **kwargs):
            captured["tian_timeout"] = kwargs["timeout"]
            self.advance(3)
            raise requests.Timeout("Controlled Tian stage timeout")
        def kuku_timeout(*args, **kwargs):
            captured["kuku_timeout"] = kwargs["timeout_seconds"]
            self.advance(1)
            raise RuntimeError("Controlled Kuku failure")
        self.case.session.post.side_effect = tian_timeout
        self.runner.side_effect = kuku_timeout
        context = self.context()
        deadline = context.deadline_monotonic
        self.coordinator.parse(self.adapter, context)
        self.assertAlmostEqual(captured["tian_timeout"], 3, delta=0.1)
        self.assertAlmostEqual(captured["kuku_timeout"], 1, delta=0.1)
        self.assertGreater(deadline - self.clock(), 0.8)
        self.assertLess(deadline - self.clock(), 1.1)

    def test_kuku_task_dequeued_after_deadline_does_not_reserve_or_send(self):
        context = replace(self.context(), deadline_monotonic=self.clock() + 0.5)
        entered = Event()
        def occupy_worker():
            entered.set()
            self.release.wait(2)
        with patch("xhs_download.services.platforms.douyin_kukutool.reserve_fixed_window") as reserve, \
                ThreadPoolExecutor(max_workers=1) as pool:
            blocker = pool.submit(occupy_worker)
            self.assertTrue(entered.wait(1))
            queued = pool.submit(self.kuku.resolve_validated_candidate, self.adapter, context)
            self.advance(1)
            self.release.set()
            blocker.result(timeout=1)
            with self.assertRaises(XhsProviderTimeoutError):
                queued.result(timeout=1)
        reserve.assert_not_called()
        self.consume.assert_not_called()
        self.runner.assert_not_called()

    def test_kuku_semaphore_acquired_after_deadline_does_not_reserve_or_send(self):
        entered = Event()
        class ObservedSemaphore(BoundedSemaphore):
            def acquire(self, *args, **kwargs):
                entered.set()
                return super().acquire(*args, **kwargs)
        semaphore = ObservedSemaphore(1)
        semaphore.acquire()
        entered.clear()
        context = replace(self.context(), deadline_monotonic=self.clock() + 0.5)
        with patch("xhs_download.services.platforms.douyin_kukutool.get_kukutool_semaphore", return_value=semaphore), \
                patch("xhs_download.services.platforms.douyin_kukutool.reserve_fixed_window") as reserve, \
                ThreadPoolExecutor(max_workers=1) as pool:
            queued = pool.submit(self.kuku.resolve_validated_candidate, self.adapter, context)
            try:
                self.assertTrue(entered.wait(1))
                self.advance(1)
            finally:
                semaphore.release()
            with self.assertRaises(XhsProviderTimeoutError):
                queued.result(timeout=1)
        self.assertTrue(semaphore.acquire(blocking=False))
        semaphore.release()
        reserve.assert_not_called()
        self.consume.assert_not_called()
        self.runner.assert_not_called()

    def test_kuku_reservation_crossing_deadline_is_released_before_user_quota(self):
        context = replace(self.context(), deadline_monotonic=self.clock() + 0.5)
        reservation = Mock(result=LimitResult(allowed=True))
        def slow_reserve(*args):
            self.advance(1)
            return reservation
        with patch("xhs_download.services.platforms.douyin_kukutool.reserve_fixed_window", side_effect=slow_reserve):
            with self.assertRaises(XhsProviderTimeoutError):
                self.kuku.resolve_validated_candidate(self.adapter, context)
        reservation.commit.assert_not_called()
        reservation.release.assert_called_once()
        self.consume.assert_not_called()
        self.runner.assert_not_called()

    def test_kuku_quota_returning_after_deadline_does_not_send(self):
        context = replace(self.context(), deadline_monotonic=self.clock() + 0.5)
        context.ultra_attempt_controller.configure_providers(("kukutool",))
        reservation = Mock(result=LimitResult(allowed=True))
        def slow_consume(*args):
            self.advance(1)
            return LimitResult(allowed=True)
        self.consume.side_effect = slow_consume
        with patch("xhs_download.services.platforms.douyin_kukutool.reserve_fixed_window", return_value=reservation):
            with self.assertRaises(XhsProviderTimeoutError):
                self.kuku.resolve_validated_candidate(self.adapter, context)
        self.consume.assert_called_once()
        reservation.commit.assert_not_called()
        reservation.release.assert_called_once()
        self.runner.assert_not_called()

    def test_entry_skips_old_continuation_and_never_reads_historical_metadata(self):
        cache.set(build_douyin_metadata_cache_key(AWEME), {
            "platform": "douyin", "base_filename": "Old title", "reference_url": resource("old")["url"],
            "metadata": {"title": "Old title"}, "cover_url": "https://p3.douyinpic.com/old.jpg",
        }, 1800)
        with patch("xhs_download.services.platforms.douyin.find_active_douyin_continuation",
                   side_effect=AssertionError("No old continuation lookup")), \
                patch(f"{MODULE}.get_cached_douyin_metadata", side_effect=AssertionError("No old metadata")):
            result = self.parse_entry()
        self.assertEqual(result.metadata["title"], "Fixture work")
        self.assertEqual(result.metadata["selected_provider"], "tiantianjiexi")
        self.runner.assert_not_called()

    def test_explicit_order_only_uses_authorized_services_without_race(self):
        for order, expected in (
            (("tiantianjiexi", "native"), ["tiantianjiexi"]),
            (("kukutool", "native"), ["kukutool"]),
            (("kukutool", "tiantianjiexi", "native"), ["tiantianjiexi", "kukutool"]),
            (("ultra_race", "native"), ["tiantianjiexi", "kukutool"]),
        ):
            with self.subTest(order=order):
                providers = build_douyin_result_providers(self.settings, provider_order=order)
                self.assertIsInstance(providers[0], DouyinTianTianFirstProvider)
                self.assertEqual([p.name for p in providers[0].providers], expected)

    def test_late_tian_does_not_replace_returned_kuku_or_create_continuation(self):
        self.settings = replace(self.settings, ultra_total_timeout_seconds=0.2,
                                ultra_kukutool_grace_seconds=0.1)
        self.coordinator.provider_settings = self.settings
        entered = Event()
        def delayed_post(*args, **kwargs):
            entered.set(); self.release.wait(1)
            return self.response
        self.case.session.post.side_effect = delayed_post
        result = self.coordinator.parse(self.adapter, self.context())
        self.assertTrue(entered.is_set())
        self.assertEqual(result.metadata["selected_provider"], "kukutool")
        selected = result.video_assets[0].url
        self.release.set(); self.finish_tasks()
        self.assertEqual(result.video_assets[0].url, selected)
        self.assertEqual(result.metadata["selected_provider"], "kukutool")
        self.assertEqual(result.metadata["service_execution_policy"], "tiantian_first")
