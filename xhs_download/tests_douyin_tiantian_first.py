"""INV-01 Douyin policy through real providers and isolated boundaries.

Native runs from the start of the parse beside the configured services. Inside
the admin budget a service result that beats Native is shown at once; when the
budget ends Native is shown and the first later upgrade replaces it.
"""
from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import replace
from threading import BoundedSemaphore, Event
import time
from unittest.mock import Mock, patch

import requests
from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, override_settings

from . import tests_douyin_format_selection as fixtures
from .services.assets import MediaAsset, ParseResult
from .services.common import XhsProviderTimeoutError
from .services.douyin_ultra_attempts import DouyinUltraAttemptController, DouyinUltraAttemptRateLimitedError
from .services.douyin_continuation import (
    DouyinContinuationCoordinator, DouyinContinuationPendingError, issue_douyin_continuation_token,
    poll_douyin_continuation, find_active_douyin_continuation,
)
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
from .services.platforms.tiantian_serial import inspect_account_state
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
        self.callback_completions = []
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
                add_done_callback = future.add_done_callback

                def track_callback(callback):
                    completed = Event()
                    self.callback_completions.append(completed)

                    def tracked_callback(done):
                        try:
                            return callback(done)
                        finally:
                            completed.set()

                    add_done_callback(tracked_callback)

                future.add_done_callback = track_callback
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
        # Future.result() can return before continuation callbacks publish to cache.
        for completed in self.callback_completions:
            self.assertTrue(completed.wait(3), "A continuation callback escaped the test")

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
            allow_continuation=True, continuation_deadline_monotonic=self.clock() + 15,
            continuation_identity="fixture",
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

    def test_real_builder_runs_native_beside_tian_and_shows_tian_success(self):
        result = self.coordinator.parse(self.adapter, self.context())
        self.assertEqual(result.metadata["selected_provider"], "tiantianjiexi")
        self.assertEqual(len(result.metadata["upstream_candidates"]), 3)
        self.assertNotIn("ultra_race_winner", result.metadata)
        self.assertEqual(self.events, ["tiantianjiexi"])
        self.runner.assert_not_called()
        self.consume.assert_called_once()
        # INV-01: Native starts with the parse, even when the preferred service wins.
        self.finish_tasks()
        self.adapter.fetch_mobile_feed_result.assert_called_once()

    def test_low_quality_success_does_not_trigger_fallback_or_native_enrichment(self):
        data = self.response.json.return_value["data"]
        data["title"] = ""; data["cover"] = ""
        data["videos"][0]["streams"] = [data["videos"][0]["streams"][-1]]
        with patch(f"{MODULE}.get_reference_result", side_effect=AssertionError("No historical metadata")):
            result = self.coordinator.parse(self.adapter, self.context())
        self.assertEqual(result.metadata["selected_provider"], "tiantianjiexi")
        self.assertEqual(self.events, ["tiantianjiexi"])
        self.runner.assert_not_called()

    def test_valid_tian_media_with_malformed_title_does_not_trigger_fallback(self):
        self.response.json.return_value["data"]["title"] = "https://[broken"
        result = self.coordinator.parse(self.adapter, self.context())
        self.assertTrue(result.video_assets)
        self.assertEqual(result.metadata["selected_provider"], "tiantianjiexi")
        self.assertEqual(self.events, ["tiantianjiexi"])
        self.runner.assert_not_called()

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
                      side_effect=AssertionError("Douyin must not use generic route result cache")):
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
        # The cached first parse is zero-external; only the fresh one starts Native.
        self.finish_tasks()
        self.adapter.fetch_mobile_feed_result.assert_called_once()

    def test_failure_uses_kuku_cache_only_after_tian_attempt(self):
        self.seed_kuku(); self.fail_tian()
        result = self.coordinator.parse(self.adapter, self.context())
        self.assertEqual(result.metadata["selected_provider"], "kukutool")
        self.assertEqual(self.events, ["tiantianjiexi"])
        self.runner.assert_not_called()
        self.finish_tasks()
        self.adapter.fetch_mobile_feed_result.assert_called_once()

    def test_failure_starts_one_new_kuku_after_tian(self):
        self.fail_tian()
        result = self.coordinator.parse(self.adapter, self.context())
        self.assertEqual(result.metadata["selected_provider"], "kukutool")
        self.assertEqual(self.events, ["tiantianjiexi", "kukutool"])
        self.consume.assert_called_once()
        self.finish_tasks()
        self.adapter.fetch_mobile_feed_result.assert_called_once()

    def test_disabled_tian_enters_parallel_fallback_tier(self):
        with override_settings(TIANTIANJIEXI_PROVIDER_ENABLED=False):
            built = build_douyin_result_providers(self.settings)
            self.assertIsInstance(built[0], DouyinTianTianFirstProvider)
            result = self.coordinator.parse(self.adapter, self.context())
        self.assertEqual(result.metadata["selected_provider"], "kukutool")
        self.assertEqual(self.events, ["kukutool"])
        self.finish_tasks()
        self.adapter.fetch_mobile_feed_result.assert_called_once()

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
        result = self.parse_entry(controller=self.context().ultra_attempt_controller, allow_continuation=False)
        self.assertEqual(result.metadata["selected_provider"], "mobile_feed")
        self.case.session.post.assert_not_called(); self.runner.assert_not_called()
        self.adapter.fetch_mobile_feed_result.assert_called_once()

    def test_quota_error_is_preserved_when_all_authorized_fallbacks_fail(self):
        self.consume.return_value = LimitResult(allowed=False, retry_after=20)
        with self.assertRaises(DouyinUltraAttemptRateLimitedError):
            self.parse_entry(controller=self.context().ultra_attempt_controller)
        self.case.session.post.assert_not_called(); self.runner.assert_not_called()

    @override_settings(TIANTIANJIEXI_RETRIES=3)
    def test_real_timeout_falls_back_without_permanently_blocking_next_request(self):
        self.seed_kuku()
        self.case.session.post.side_effect = requests.Timeout("Controlled final HTTP timeout")
        first = self.coordinator.parse(self.adapter, self.context())
        self.assertEqual(first.metadata["selected_provider"], "kukutool")
        self.case.session.post.assert_called_once()
        self.assertFalse(inspect_account_state()["unknown_outcome_blocks_requests"])
        self.advance(30)
        self.case.session.post.side_effect = self.post
        second = self.coordinator.parse(self.adapter, self.context())
        self.assertEqual(second.metadata["selected_provider"], "tiantianjiexi")
        self.assertEqual(self.case.session.post.call_count, 2)

    def test_existing_single_flight_wait_does_not_send_or_clear_owner(self):
        self.seed_kuku()
        key = build_single_flight_key(self.source_key)
        cache.set(key, "existing-owner", 30)
        def advance_wait(seconds):
            self.advance(seconds)
        with patch("xhs_download.services.platforms.tiantianjiexi_generic.time.sleep", side_effect=advance_wait):
            try:
                self.coordinator.parse(self.adapter, self.context())
            except DouyinContinuationPendingError:
                pass
            self.finish_tasks()
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

    def test_budget_is_not_renewed_for_the_next_service(self):
        captured = {}
        def tian_timeout(*args, **kwargs):
            captured["tian_timeout"] = kwargs["timeout"]
            self.advance(5)
            raise requests.Timeout("Controlled Tian stage timeout")
        def kuku_timeout(*args, **kwargs):
            captured["kuku_timeout"] = kwargs["timeout_seconds"]
            raise RuntimeError("Controlled Kuku failure")
        self.case.session.post.side_effect = tian_timeout
        self.runner.side_effect = kuku_timeout
        context = self.context()
        budget_deadline = context.deadline_monotonic
        try:
            self.coordinator.parse(self.adapter, context)
        except DouyinContinuationPendingError:
            pass
        self.finish_tasks()
        self.assertAlmostEqual(captured["tian_timeout"], 15, delta=0.1)
        # INV-01: no second front-end window. A service that starts after the
        # budget only feeds the page's continuation, within its remaining time.
        self.assertEqual(context.deadline_monotonic, budget_deadline)
        self.assertLessEqual(captured["kuku_timeout"], 10.1)

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

    def test_entry_never_reads_historical_metadata(self):
        cache.set(build_douyin_metadata_cache_key(AWEME), {
            "platform": "douyin", "base_filename": "Old title", "reference_url": resource("old")["url"],
            "metadata": {"title": "Old title"}, "cover_url": "https://p3.douyinpic.com/old.jpg",
        }, 1800)
        with patch(f"{MODULE}.get_cached_douyin_metadata", side_effect=AssertionError("No old metadata")):
            result = self.parse_entry()
        self.assertEqual(result.metadata["title"], "Fixture work")
        self.assertEqual(result.metadata["selected_provider"], "tiantianjiexi")
        self.runner.assert_not_called()

    def test_explicit_order_only_uses_authorized_services_without_race(self):
        for order, expected in (
            (("tiantianjiexi", "native"), ["tiantianjiexi"]),
            (("kukutool", "native"), ["kukutool"]),
            # A1: the configured order is kept, never re-ranked by the code.
            (("kukutool", "tiantianjiexi", "native"), ["kukutool", "tiantianjiexi"]),
            (("ultra_race", "native"), ["tiantianjiexi", "kukutool"]),
        ):
            with self.subTest(order=order):
                providers = build_douyin_result_providers(self.settings, provider_order=order)
                self.assertIsInstance(providers[0], DouyinTianTianFirstProvider)
                self.assertEqual([p.name for p in providers[0].providers], expected)

    def test_late_tian_replaces_native_through_continuation_without_reparse(self):
        self.settings = replace(self.settings, ultra_total_timeout_seconds=0.2)
        self.coordinator.provider_settings = self.settings
        entered = Event()
        def delayed_post(*args, **kwargs):
            entered.set(); self.release.wait(1)
            return self.response
        self.case.session.post.side_effect = delayed_post
        self.runner.side_effect = RuntimeError("Controlled fallback failure")
        self.adapter.fetch_mobile_feed_result.return_value = self.native("720P", 1_000_000)
        self.response.json.return_value["data"]["videos"][0]["streams"][0]["qualityLabel"] = "4K"
        with self.assertRaises(DouyinContinuationPendingError) as raised:
            self.coordinator.parse(self.adapter, self.context())
        # INV-01: when the budget ends Native is shown; the late ultra result replaces it.
        result = raised.exception.fallback_result
        self.assertTrue(entered.is_set())
        self.assertEqual(result.metadata["selected_provider"], "mobile_feed")
        self.assertEqual(result.metadata["service_execution_policy"], "tiantian_first")
        self.release.set(); self.finish_tasks()
        token = issue_douyin_continuation_token(
            task_id=raised.exception.task_id, client_identity="fixture", platform_key="home",
        )
        outcome, _ = poll_douyin_continuation(token, client_identity="fixture")
        self.assertEqual(outcome.status, "success")
        self.assertEqual(outcome.result.metadata["selected_provider"], "tiantianjiexi")
        self.assertEqual(outcome.result.video_assets[0].quality_label, "4K")
        self.assertEqual(len(outcome.result.metadata["upstream_candidates"]), 3)
        self.assertEqual(find_active_douyin_continuation(AWEME, "fixture"), "")
        self.case.session.post.assert_called_once()
        self.adapter.fetch_mobile_feed_result.assert_called_once()

    def test_next_service_gets_remaining_budget_and_native_is_shown_when_it_ends(self):
        self.settings = replace(self.settings, ultra_total_timeout_seconds=0.2)
        self.coordinator.provider_settings = self.settings
        self.fail_tian()
        entered = Event()
        def blocked_kuku(*args, **kwargs):
            self.events.append("kukutool")
            entered.set()
            self.release.wait(2)
            return {"videos": [{"video_fullinfo": [resource("1080P")]}]}
        self.runner.side_effect = blocked_kuku
        self.adapter.fetch_mobile_feed_result.return_value = self.native("540p", 1000)
        with self.assertRaises(DouyinContinuationPendingError) as raised:
            self.parse_entry(controller=self.context().ultra_attempt_controller)
        fallback = raised.exception.fallback_result
        self.assertEqual(fallback.metadata["selected_provider"], "mobile_feed")
        # The failed preferred service hands over at once, inside the same budget.
        self.assertTrue(entered.is_set())
        self.assertEqual(self.events, ["tiantianjiexi", "kukutool"])
        self.assertFalse(self.release.is_set())
        self.release.set(); self.finish_tasks()
        self.case.session.post.assert_called_once()
        self.runner.assert_called_once()
        self.adapter.fetch_mobile_feed_result.assert_called_once()

    def test_result_after_execution_deadline_cannot_upgrade_delivered_fallback(self):
        self.settings = replace(self.settings, ultra_total_timeout_seconds=0.05)
        self.coordinator.provider_settings = self.settings
        def delayed_post(*args, **kwargs):
            self.release.wait(2)
            return self.response
        self.case.session.post.side_effect = delayed_post
        self.runner.side_effect = RuntimeError("Controlled fallback failure")
        self.adapter.fetch_mobile_feed_result.return_value = self.native("540p", 1000)
        with self.assertRaises(DouyinContinuationPendingError) as raised:
            self.coordinator.parse(self.adapter, self.context())
        self.assertEqual(raised.exception.fallback_result.metadata["selected_provider"], "mobile_feed")
        self.advance(16)
        self.release.set(); self.finish_tasks()
        token = issue_douyin_continuation_token(
            task_id=raised.exception.task_id, client_identity="fixture", platform_key="home",
        )
        outcome, _ = poll_douyin_continuation(token, client_identity="fixture")
        if outcome.result is not None:
            self.assertNotEqual(outcome.result.metadata["selected_provider"], "tiantianjiexi")
        else:
            self.assertIn(outcome.status, ("timeout", "failed", "unchanged"))
        self.case.session.post.assert_called_once()

    def test_explicit_batch_deadline_caps_both_tiers_without_new_window_escape(self):
        captured = {}
        context = replace(self.context(), allow_continuation=False,
                          outer_deadline_monotonic=self.clock() + 5)
        def tian_timeout(*args, **kwargs):
            captured["primary"] = kwargs["timeout"]
            self.advance(4)
            raise requests.Timeout("Controlled primary timeout")
        def kuku_timeout(*args, **kwargs):
            captured["fallback"] = kwargs["timeout_seconds"]
            raise RuntimeError("Controlled fallback failure")
        self.case.session.post.side_effect = tian_timeout
        self.runner.side_effect = kuku_timeout
        self.coordinator.parse(self.adapter, context)
        self.finish_tasks()
        self.assertAlmostEqual(captured["primary"], 5, delta=0.1)
        self.assertAlmostEqual(captured["fallback"], 1, delta=0.1)
        self.assertLessEqual(context.deadline_monotonic, context.outer_deadline_monotonic)
        self.assertLessEqual(
            self.adapter.fetch_mobile_feed_result.call_args.kwargs["request_deadline"],
            context.outer_deadline_monotonic,
        )

    def test_synchronous_fallback_does_not_repeat_native_or_create_polling(self):
        self.fail_tian()
        self.runner.side_effect = RuntimeError("Controlled fallback failure")
        self.assertIsNone(self.coordinator.parse(
            self.adapter, replace(self.context(), allow_continuation=False),
        ))
        self.finish_tasks()
        self.adapter.fetch_mobile_feed_result.assert_called_once()
        self.adapter.fetch_mobile_share_result.assert_called_once()

    def native(self, label, size):
        return ParseResult(
            platform="douyin", post_type="video", base_filename=f"Native {label}",
            assets=(MediaAsset(media_type="video", platform="douyin",
                               url=resource("native")["url"], quality_label=label,
                               size_bytes=size),),
        )

    def high_native(self):
        return self.native("4K", 60 * 1024 * 1024)

    def test_late_lower_quality_fallback_does_not_downgrade_displayed_native(self):
        self.settings = replace(self.settings, ultra_total_timeout_seconds=0.2)
        self.coordinator.provider_settings = self.settings
        self.fail_tian()
        context = self.context()
        context.ultra_attempt_controller.record_cooldown("tiantianjiexi", 8)
        entered = Event()
        def delayed_kuku(*args, **kwargs):
            entered.set()
            self.release.wait(2)
            return {"videos": [{"video_fullinfo": [resource("720P")]}]}
        def native(*args, **kwargs):
            self.assertTrue(entered.wait(1))
            return self.high_native()
        self.runner.side_effect = delayed_kuku
        self.adapter.fetch_mobile_feed_result.side_effect = native
        with self.assertRaises(DouyinContinuationPendingError) as raised:
            self.coordinator.parse(self.adapter, context)
        self.assertEqual(raised.exception.fallback_result.video_assets[0].quality_label, "4K")
        self.release.set(); self.finish_tasks()
        token = issue_douyin_continuation_token(
            task_id=raised.exception.task_id, client_identity="fixture", platform_key="home",
        )
        outcome, _ = poll_douyin_continuation(token, client_identity="fixture")
        self.assertEqual(outcome.status, "unchanged")
        self.assertIsNone(outcome.result)
        self.assertEqual(outcome.retry_after_seconds, 8)
        self.assertEqual(find_active_douyin_continuation(AWEME, "fixture"), "")

    def test_service_result_that_does_not_beat_ready_native_is_not_shown(self):
        self.fail_tian()
        self.adapter.fetch_mobile_feed_result.return_value = self.high_native()
        def both_ready(futures, timeout=None, **kwargs):
            # Controlled collection boundary, real coordinator/provider tasks.
            return wait(futures, timeout=timeout)
        with patch(f"{MODULE}.wait", side_effect=both_ready):
            result = self.coordinator.parse(self.adapter, self.context())
        self.assertEqual(result.metadata["selected_provider"], "mobile_feed")
        self.assertEqual(result.video_assets[0].quality_label, "4K")

    def test_late_rejected_native_is_not_reused_by_adapter_fallback(self):
        self.fail_tian()
        self.runner.side_effect = RuntimeError("Controlled fallback failure")
        def late_native(*args, **kwargs):
            self.advance(4)
            return self.high_native()
        self.adapter.fetch_mobile_feed_result.side_effect = late_native
        with self.assertRaises(XhsProviderTimeoutError):
            self.parse_entry(allow_continuation=False)
        self.adapter.fetch_mobile_feed_result.assert_called_once()


@override_settings(
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
    THIRD_PARTY_PROVIDER_ENABLED=True, TIANTIANJIEXI_PROVIDER_ENABLED=True,
    TIANTIANJIEXI_PHONE="fixture", TIANTIANJIEXI_PASSWORD="fixture",
    TIANTIANJIEXI_RETRIES=1, TIANTIANJIEXI_REQUEST_LIMIT_PER_MINUTE=0,
    TIANTIANJIEXI_GLOBAL_MAX_CONCURRENCY=0,
)
class DouyinTierPageTests(TestCase):
    def setUp(self):
        self.case = DouyinTianTianFirstTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        for target, options in (
            ("xhs_download.services.registry.default_registry", {
                "new": PlatformRegistry(adapters=(self.case.adapter,)),
            }),
            ("xhs_download.views.ultra_hd_is_available", {"return_value": True}),
            ("xhs_download.views.consume_fixed_window", {"return_value": LimitResult(allowed=True)}),
            ("xhs_download.services.platforms.douyin.get_platform_provider_settings", {
                "side_effect": lambda *a: self.case.settings,
            }),
        ):
            active = patch(target, **options)
            active.start(); self.addCleanup(active.stop)
        active = patch.object(self.case.adapter, "get_result_providers",
                              return_value=(self.case.coordinator, DouyinNativeProvider()))
        active.start(); self.addCleanup(active.stop)

    def test_homepage_real_controller_returns_native_then_renders_late_primary(self):
        case = self.case
        case.settings = replace(case.settings, ultra_total_timeout_seconds=0.05)
        case.coordinator.provider_settings = case.settings
        case.response.json.return_value["data"]["videos"][0]["streams"][0]["qualityLabel"] = "4K"
        def delayed_post(*args, **kwargs):
            case.events.append("tiantianjiexi")
            case.release.wait(2)
            return case.response
        case.case.session.post.side_effect = delayed_post
        case.runner.side_effect = RuntimeError("Controlled fallback failure")
        case.adapter.fetch_mobile_feed_result.return_value = case.native("540p", 1000)
        response = self.client.get("/results/", {"q": case.source, "ultra_hd": "1"},
                                   HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "continuation_pending")
        self.assertTrue(body["has_results"])
        self.assertIn("540p", body["html"])
        case.release.set(); case.finish_tasks()
        response = self.client.post("/media/douyin-continuation/", {
            "continuation_token": body["continuation_token"],
        }, content_type="application/json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "success")
        self.assertIn("4K", response.json()["html"])
        case.case.session.post.assert_called_once()
        case.runner.assert_called_once()

    def test_repeat_page_keeps_own_fallback_and_shares_upstream_single_flight(self):
        case = self.case
        case.settings = replace(case.settings, ultra_total_timeout_seconds=0.2)
        case.coordinator.provider_settings = case.settings
        case.fail_tian()
        entered = Event()

        def delayed_kuku(*args, **kwargs):
            entered.set()
            case.release.wait(2)
            return {"videos": [{"video_fullinfo": [resource("720P")]}]}

        def native(*args, **kwargs):
            self.assertTrue(entered.wait(1))
            high = case.high_native()
            if case.adapter.fetch_mobile_feed_result.call_count == 1:
                return high
            return replace(high, base_filename="Native 540p", assets=(
                replace(high.video_assets[0], quality_label="540p", size_bytes=1000),
            ))

        case.runner.side_effect = delayed_kuku
        case.adapter.fetch_mobile_feed_result.side_effect = native
        query = {"q": case.source, "ultra_hd": "1"}
        original = self.client.get("/results/", query,
                                   HTTP_X_REQUESTED_WITH="XMLHttpRequest").json()
        repeated = self.client.get("/results/", query,
                                   HTTP_X_REQUESTED_WITH="XMLHttpRequest").json()
        self.assertTrue(original["has_results"])
        self.assertEqual(repeated["status"], "continuation_pending")
        self.assertTrue(repeated["has_results"])
        self.assertIn("4K", original["html"])
        self.assertIn("540p", repeated["html"])
        self.assertNotEqual(original["continuation_token"], repeated["continuation_token"])
        case.release.set(); case.finish_tasks()
        for initial, expected in ((original, "unchanged"), (repeated, "success")):
            response = self.client.post("/media/douyin-continuation/", {
                "continuation_token": initial["continuation_token"],
            }, content_type="application/json")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["status"], expected)
            if expected == "unchanged":
                self.assertNotIn("html", response.json())
            else:
                self.assertIn("720", response.json()["html"])
        case.case.session.post.assert_called_once()
        case.runner.assert_called_once()
        self.assertEqual(case.adapter.fetch_mobile_feed_result.call_count, 2)

    def test_empty_fallback_does_not_claim_available_media_was_returned(self):
        self.case.fail_tian()
        self.case.runner.side_effect = RuntimeError("Controlled failure")
        response = self.client.get("/results/", {"q": self.case.source, "ultra_hd": "1"},
                                   HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertFalse(body["has_results"])
        self.assertNotIn("已返回可用清晰度", body["html"])


@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
class DouyinFirstUpgradeTests(SimpleTestCase):
    """INV-01: the first service result that beats the shown one ends the wait."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        # Only the upgrade rule is under test; provider manifests are covered elsewhere.
        serialize = patch("xhs_download.services.douyin_continuation.serialize_douyin_continuation_result",
                          side_effect=lambda result: {"work": result.base_filename})
        serialize.start()
        self.addCleanup(serialize.stop)

    def capture(self, coordinator, name, score):
        future = Future()
        future.set_result(ParseResult(
            platform="douyin", post_type="video", base_filename=name,
            assets=(MediaAsset(media_type="video", platform="douyin", url=resource(name)["url"]),),
        ))
        coordinator.capture_future(
            future, provider_name=name, score_result=lambda result: score,
            is_strong_result=lambda result: True, prepare_result=lambda result: result,
        )

    def test_first_upgrade_ends_the_wait_and_later_services_are_not_ranked(self):
        coordinator = DouyinContinuationCoordinator(2, first_upgrade_wins=True)
        coordinator.retain_fallback_score((300, 0))
        self.capture(coordinator, "kukutool", (400, 0))
        self.capture(coordinator, "tiantianjiexi", (600, 0))
        best, pending = coordinator.snapshot_result_and_pending()
        self.assertEqual(best.base_filename, "kukutool")
        self.assertFalse(pending)

    def test_result_that_does_not_beat_the_shown_one_does_not_end_the_wait(self):
        coordinator = DouyinContinuationCoordinator(2, first_upgrade_wins=True)
        coordinator.retain_fallback_score((500, 0))
        self.capture(coordinator, "kukutool", (400, 0))
        self.assertTrue(coordinator.has_pending_work())
        self.capture(coordinator, "tiantianjiexi", (600, 0))
        best, _pending = coordinator.snapshot_result_and_pending()
        self.assertEqual(best.base_filename, "tiantianjiexi")
