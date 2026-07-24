import hashlib
import json
import time
from concurrent.futures import Future
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests
from django.core.cache import cache
from django.http import HttpResponse
from django.test import Client, SimpleTestCase, TestCase, override_settings

from .services.assets import MediaAsset, ParseResult
from .services.common import XhsDownloadError, XhsProviderTimeoutError
from .services.download_handoff import (
    DOWNLOAD_HANDOFF_TTL_SECONDS,
    DOWNLOAD_STREAM_START_TIMEOUT_SECONDS,
    DownloadHandoffError,
    configured_download_stream_start_timeout_seconds,
    issue_download_handoff_tokens,
    load_download_handoff,
    load_download_status,
    update_download_status,
)
from .services.downloads import (
    fetch_item_to_temp_path,
    observable_download_error,
    request_public_provider_get_with_retry,
)
from .services.douyin_continuation import (
    DOUYIN_CONTINUATION_MAX_POLLS,
    DouyinContinuationCoordinator,
    DouyinContinuationError,
    DouyinContinuationPendingError,
    build_douyin_continuation_cache_key,
    issue_douyin_continuation_token,
    poll_douyin_continuation,
    serialize_douyin_continuation_result,
)
from .services.platforms.douyin import DouyinAdapter
from .services.platforms.douyin_kukutool import DouyinKukuToolProvider
from .services.platforms.douyin_providers import (
    DouyinParseContext,
    DouyinTianTianJieXiProvider,
    DouyinUltraRaceProvider,
)
from .services.provider_settings import ProviderSettings
from .services.provider_manifest import build_provider_manifest_token
from .services.provider_recovery import (
    TianTianJieXiRecoveryError,
    get_kukutool_manifest_identity,
    get_tiantianjiexi_manifest_identity,
    refresh_tiantianjiexi_download_item,
)
from .services.rate_limits import LimitResult
from .views import (
    DownloadStreamStartTimeout,
    apply_upstream_startup_read_timeout,
    build_single_attachment_response,
    inspect_single_attachment_download,
    open_single_attachment_upstream,
)


AWEME_ID = "123456789012345678"
MEDIA_URL = "https://v3-dy-o.zjcdn.com/path/video.mp4"


class FakeMonotonicClock:
    def __init__(self, value=100.0):
        self.value = float(value)

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += float(seconds)


def build_provider_result(
    provider="tiantianjiexi",
    *,
    url=MEDIA_URL,
    size_bytes=80 * 1024 * 1024,
    aweme_id=AWEME_ID,
    quality_label="超高清",
):
    asset_id = f"douyin:{aweme_id}:video:{provider}"
    cache_key = f"douyin:{provider}:validated:{aweme_id}"
    token = build_provider_manifest_token(
        asset_id=asset_id,
        provider=provider,
        provider_resource_id=aweme_id,
        provider_cache_key=cache_key,
        url=url,
        media_type="video",
    )
    return ParseResult(
        platform="douyin",
        post_type="video",
        base_filename="douyin_demo",
        assets=(
            MediaAsset(
                media_type="video",
                url=url,
                platform="douyin",
                quality_label=quality_label,
                size_bytes=size_bytes,
                asset_id=asset_id,
                provider=provider,
                provider_resource_id=aweme_id,
                provider_cache_key=cache_key,
                provider_manifest_token=token,
            ),
        ),
        metadata={"selected_provider": provider},
    )


def build_tiantianjiexi_item(url=MEDIA_URL, *, aweme_id=AWEME_ID):
    return build_provider_result(
        url=url,
        aweme_id=aweme_id,
    ).video_assets[0].to_download_item()


def build_native_fallback_result(*, quality_label="4k"):
    return ParseResult(
        platform="douyin",
        post_type="video",
        base_filename="douyin_native",
        assets=(
            MediaAsset(
                media_type="video",
                url=MEDIA_URL,
                platform="douyin",
                quality_label=quality_label,
            ),
        ),
        metadata={
            "selected_provider": "native",
            "fallback_reason": "kukutool_deadline",
            "kukutool_background_refresh": True,
        },
    )


class SlowRaceProvider:
    name = "tiantianjiexi"

    def __init__(
        self,
        *,
        name="tiantianjiexi",
        result=None,
        error=None,
        delay_seconds=0.06,
    ):
        self.name = name
        self.result = result
        self.error = error
        self.delay_seconds = delay_seconds
        self.calls = 0

    def parse(self, _adapter, _context):
        self.calls += 1
        time.sleep(self.delay_seconds)
        if self.error:
            raise self.error
        return self.result


@override_settings(
    THIRD_PARTY_PROVIDER_ENABLED=True,
    TIANTIANJIEXI_PROVIDER_ENABLED=True,
    TIANTIANJIEXI_PHONE="phone",
    TIANTIANJIEXI_PASSWORD="password",
)
class DouyinExternalMetadataDeadlineTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    def tearDown(self):
        cache.clear()

    def test_tiantianjiexi_media_does_not_wait_for_optional_native_metadata(self):
        class FakeTianTianProvider:
            def parse(self, _url, timeout_seconds=None):
                return {
                    "title": "天天解析视频",
                    "cover": "https://p3.douyinpic.com/cover.jpeg",
                    "videos": [
                        {
                            "url": MEDIA_URL,
                            "quality": "4K",
                            "size": 80 * 1024 * 1024,
                        },
                    ],
                }

        adapter = DouyinAdapter()
        adapter.fetch_mobile_feed_result = Mock(
            side_effect=AssertionError(
                "optional native metadata must not delay a valid media result"
            )
        )
        provider = DouyinTianTianJieXiProvider(
            provider_settings=ProviderSettings(
                ultra_hd_enabled=True,
                provider_order=("tiantianjiexi", "native"),
            ),
            provider=FakeTianTianProvider(),
            reserve_fallback_grace=False,
        )
        context = DouyinParseContext(
            url=f"https://www.douyin.com/video/{AWEME_ID}",
            final_url=f"https://www.douyin.com/video/{AWEME_ID}",
            aweme_id=AWEME_ID,
            deadline_monotonic=time.monotonic() + 12.0,
        )

        result = provider.parse(adapter, context)

        self.assertEqual(result.video_assets[0].provider, "tiantianjiexi")
        self.assertEqual(result.video_assets[0].quality_label, "4K")
        self.assertEqual(result.base_filename, "天天解析视频")
        self.assertEqual(
            result.video_assets[0].cover_url,
            "https://p3.douyinpic.com/cover.jpeg",
        )
        adapter.fetch_mobile_feed_result.assert_not_called()

    def test_tiantianjiexi_can_fill_missing_display_metadata_from_native(self):
        class FakeTianTianProvider:
            def parse(self, _url, timeout_seconds=None):
                return {
                    "title": "",
                    "cover": "",
                    "videos": [
                        {
                            "url": MEDIA_URL,
                            "quality": "4K",
                            "size": 80 * 1024 * 1024,
                        },
                    ],
                }

        native_result = ParseResult(
            platform="douyin",
            post_type="video",
            base_filename="native title",
            assets=(
                MediaAsset(
                    media_type="video",
                    url="https://v3-dy-o.zjcdn.com/path/native.mp4",
                    platform="douyin",
                    cover_url="https://p3.douyinpic.com/native-cover.jpeg",
                ),
            ),
        )
        adapter = DouyinAdapter()
        adapter.fetch_mobile_feed_result = Mock(return_value=native_result)
        provider = DouyinTianTianJieXiProvider(
            provider_settings=ProviderSettings(
                ultra_hd_enabled=True,
                provider_order=("tiantianjiexi", "native"),
            ),
            provider=FakeTianTianProvider(),
            reserve_fallback_grace=False,
        )
        context = DouyinParseContext(
            url=f"https://www.douyin.com/video/{AWEME_ID}",
            final_url=f"https://www.douyin.com/video/{AWEME_ID}",
            aweme_id=AWEME_ID,
            deadline_monotonic=time.monotonic() + 12.0,
        )

        result = provider.parse(adapter, context)

        self.assertEqual(result.base_filename, "native title")
        self.assertEqual(
            result.video_assets[0].cover_url,
            "https://p3.douyinpic.com/native-cover.jpeg",
        )
        adapter.fetch_mobile_feed_result.assert_called_once()


class DouyinContinuationServiceTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    def tearDown(self):
        cache.clear()

    def race_provider(self, fake_provider):
        settings = SimpleNamespace(
            ultra_total_timeout_seconds=0.01,
            provider_is_enabled=lambda _name: True,
        )
        return DouyinUltraRaceProvider(
            provider_settings=settings,
            providers=(fake_provider,),
        )

    def test_running_provider_returns_pending_then_cache_only_success(self):
        fake_provider = SlowRaceProvider(result=build_provider_result())
        provider = self.race_provider(fake_provider)
        context = DouyinParseContext(
            url=f"https://www.douyin.com/video/{AWEME_ID}",
            aweme_id=AWEME_ID,
            prefer_ultra_hd=True,
            deadline_monotonic=time.monotonic() + 0.01,
            allow_continuation=True,
        )

        with self.assertRaises(DouyinContinuationPendingError) as raised:
            provider.parse(Mock(), context)
        token = issue_douyin_continuation_token(
            task_id=raised.exception.task_id,
            client_identity="client-a",
            platform_key="tiktok",
        )

        first, _platform_key = poll_douyin_continuation(
            token,
            client_identity="client-a",
        )
        self.assertEqual(first.status, "continuation_pending")
        time.sleep(0.08)
        completed, platform_key = poll_douyin_continuation(
            token,
            client_identity="client-a",
        )

        self.assertEqual(completed.status, "success")
        self.assertEqual(completed.result.video_assets[0].provider, "tiantianjiexi")
        self.assertEqual(platform_key, "tiktok")
        self.assertEqual(fake_provider.calls, 1)

    def test_background_failure_becomes_stable_failed_state(self):
        fake_provider = SlowRaceProvider(error=ValueError("provider body secret"))
        provider = self.race_provider(fake_provider)
        context = DouyinParseContext(
            url=f"https://www.douyin.com/video/{AWEME_ID}",
            aweme_id=AWEME_ID,
            prefer_ultra_hd=True,
            deadline_monotonic=time.monotonic() + 0.01,
            allow_continuation=True,
        )

        with self.assertRaises(DouyinContinuationPendingError) as raised:
            provider.parse(Mock(), context)
        time.sleep(0.08)
        token = issue_douyin_continuation_token(
            task_id=raised.exception.task_id,
            client_identity="client-a",
            platform_key="home",
        )
        outcome, _platform_key = poll_douyin_continuation(
            token,
            client_identity="client-a",
        )

        self.assertEqual(outcome.status, "failed")
        self.assertNotIn("secret", outcome.message)
        self.assertEqual(fake_provider.calls, 1)

    def test_weak_candidate_preserves_running_upgrade_for_continuation(self):
        weak_result = build_provider_result(
            provider="kukutool",
            size_bytes=4 * 1024 * 1024,
            quality_label="1080p",
        )
        strong_result = build_provider_result(
            size_bytes=105 * 1024 * 1024,
            quality_label="4K",
        )
        weak_provider = SlowRaceProvider(
            name="kukutool",
            result=weak_result,
            delay_seconds=0.002,
        )
        strong_provider = SlowRaceProvider(
            result=strong_result,
            delay_seconds=0.06,
        )
        settings = SimpleNamespace(
            ultra_total_timeout_seconds=0.02,
            provider_is_enabled=lambda _name: True,
        )
        provider = DouyinUltraRaceProvider(
            provider_settings=settings,
            providers=(weak_provider, strong_provider),
        )
        context = DouyinParseContext(
            url=f"https://www.douyin.com/video/{AWEME_ID}",
            aweme_id=AWEME_ID,
            prefer_ultra_hd=True,
            deadline_monotonic=time.monotonic() + 0.02,
            allow_continuation=True,
        )

        with self.assertRaises(DouyinContinuationPendingError) as raised:
            provider.parse(Mock(), context)

        fallback = raised.exception.fallback_result
        self.assertIsNotNone(fallback)
        self.assertEqual(
            fallback.video_assets[0].provider,
            "kukutool",
        )
        self.assertEqual(
            fallback.video_assets[0].size_bytes,
            4 * 1024 * 1024,
        )

        token = issue_douyin_continuation_token(
            task_id=raised.exception.task_id,
            client_identity="client-a",
            platform_key="home",
        )
        pending, _platform_key = poll_douyin_continuation(
            token,
            client_identity="client-a",
        )
        self.assertEqual(pending.status, "continuation_pending")

        time.sleep(0.08)
        completed, _platform_key = poll_douyin_continuation(
            token,
            client_identity="client-a",
        )
        self.assertEqual(completed.status, "success")
        self.assertEqual(
            completed.result.video_assets[0].provider,
            "tiantianjiexi",
        )
        self.assertEqual(
            completed.result.video_assets[0].size_bytes,
            105 * 1024 * 1024,
        )
        self.assertEqual(weak_provider.calls, 1)
        self.assertEqual(strong_provider.calls, 1)

    def test_native_fallback_label_does_not_end_external_race(self):
        native_fallback = build_native_fallback_result()
        strong_result = build_provider_result(
            size_bytes=105 * 1024 * 1024,
            quality_label="4K",
        )
        fallback_provider = SlowRaceProvider(
            name="kukutool",
            result=native_fallback,
            delay_seconds=0.002,
        )
        strong_provider = SlowRaceProvider(
            result=strong_result,
            delay_seconds=0.06,
        )
        settings = SimpleNamespace(
            ultra_total_timeout_seconds=0.02,
            provider_is_enabled=lambda _name: True,
        )
        provider = DouyinUltraRaceProvider(
            provider_settings=settings,
            providers=(fallback_provider, strong_provider),
        )
        context = DouyinParseContext(
            url=f"https://www.douyin.com/video/{AWEME_ID}",
            aweme_id=AWEME_ID,
            prefer_ultra_hd=True,
            deadline_monotonic=time.monotonic() + 0.02,
            allow_continuation=True,
        )

        with self.assertRaises(DouyinContinuationPendingError) as raised:
            provider.parse(Mock(), context)

        fallback = raised.exception.fallback_result
        self.assertEqual(
            fallback.metadata["selected_provider"],
            "native",
        )
        self.assertEqual(
            fallback.metadata["ultra_race_winner"],
            "native",
        )

        token = issue_douyin_continuation_token(
            task_id=raised.exception.task_id,
            client_identity="client-a",
            platform_key="home",
        )
        time.sleep(0.08)
        completed, _platform_key = poll_douyin_continuation(
            token,
            client_identity="client-a",
        )
        self.assertEqual(completed.status, "success")
        self.assertEqual(
            completed.result.video_assets[0].provider,
            "tiantianjiexi",
        )
        self.assertEqual(fallback_provider.calls, 1)
        self.assertEqual(strong_provider.calls, 1)

    def test_weak_candidate_is_not_terminal_while_upgrade_is_pending(self):
        coordinator = DouyinContinuationCoordinator(
            2,
            settle_seconds=0,
        )
        weak_future = Future()
        weak_future.set_result(
            build_provider_result(
                provider="kukutool",
                size_bytes=4 * 1024 * 1024,
                quality_label="1080p",
            )
        )
        coordinator.capture_future(
            weak_future,
            provider_name="kukutool",
            score_result=lambda result: (
                result.video_assets[0].size_bytes,
                0,
            ),
            is_strong_result=lambda _result: False,
            prepare_result=lambda value: value,
        )
        token = issue_douyin_continuation_token(
            task_id=coordinator.task_id,
            client_identity="client-a",
            platform_key="home",
        )

        outcome, _platform_key = poll_douyin_continuation(
            token,
            client_identity="client-a",
        )

        self.assertEqual(outcome.status, "continuation_pending")
        coordinator.abandon()

    def test_tampered_expired_and_cross_client_tokens_are_rejected(self):
        token = issue_douyin_continuation_token(
            task_id="task-id",
            client_identity="client-a",
            platform_key="home",
        )
        with self.assertRaises(DouyinContinuationError) as tampered:
            poll_douyin_continuation(
                f"{token}tampered",
                client_identity="client-a",
            )
        self.assertEqual(
            tampered.exception.error_code,
            "DOUYIN_CONTINUATION_INVALID",
        )

        with self.assertRaises(DouyinContinuationError) as mismatched:
            poll_douyin_continuation(
                token,
                client_identity="client-b",
            )
        self.assertEqual(
            mismatched.exception.error_code,
            "DOUYIN_CONTINUATION_CLIENT_MISMATCH",
        )

        with patch(
            "xhs_download.services.douyin_continuation."
            "DOUYIN_CONTINUATION_TTL_SECONDS",
            -1,
        ):
            with self.assertRaises(DouyinContinuationError) as expired:
                poll_douyin_continuation(
                    token,
                    client_identity="client-a",
                )
        self.assertEqual(
            expired.exception.error_code,
            "DOUYIN_CONTINUATION_EXPIRED",
        )

    def test_poll_limit_is_terminal_and_cache_payload_is_bounded(self):
        task_id = "pending-task"
        cache.set(
            build_douyin_continuation_cache_key(task_id),
            {
                "status": "pending",
                "created_at": time.time(),
                "unresolved_count": 1,
            },
            timeout=15,
        )
        token = issue_douyin_continuation_token(
            task_id=task_id,
            client_identity="client-a",
            platform_key="home",
        )

        outcome = None
        for _ in range(DOUYIN_CONTINUATION_MAX_POLLS + 1):
            outcome, _platform_key = poll_douyin_continuation(
                token,
                client_identity="client-a",
            )

        self.assertEqual(outcome.status, "timeout")
        self.assertEqual(
            outcome.error_code,
            "DOUYIN_CONTINUATION_POLL_LIMIT",
        )
        with patch(
            "xhs_download.services.douyin_continuation."
            "DOUYIN_CONTINUATION_MAX_CACHE_BYTES",
            10,
        ):
            with self.assertRaises(ValueError):
                serialize_douyin_continuation_result(
                    build_provider_result()
                )

    def test_fast_success_does_not_return_pending(self):
        future = Future()
        coordinator = DouyinContinuationCoordinator(1)
        result = build_provider_result()
        future.set_result(result)
        coordinator.capture_future(
            future,
            provider_name="tiantianjiexi",
            score_result=lambda _result: (1000, 1000),
            is_strong_result=lambda _result: True,
            prepare_result=lambda value: value,
        )

        self.assertEqual(
            coordinator.get_best_result().video_assets[0].url,
            MEDIA_URL,
        )
        self.assertFalse(coordinator.has_pending_work())
        coordinator.abandon()

    def test_strong_candidate_is_terminal_while_other_work_is_pending(self):
        future = Future()
        coordinator = DouyinContinuationCoordinator(2)
        result = build_provider_result(
            size_bytes=105 * 1024 * 1024,
            quality_label="4K",
        )
        future.set_result(result)
        coordinator.capture_future(
            future,
            provider_name="tiantianjiexi",
            score_result=lambda _result: (1000, 1000),
            is_strong_result=lambda _result: True,
            prepare_result=lambda value: value,
        )

        best_result, pending, is_strong = (
            coordinator.snapshot_result_state()
        )

        self.assertEqual(best_result.video_assets[0].url, MEDIA_URL)
        self.assertTrue(pending)
        self.assertTrue(is_strong)
        token = issue_douyin_continuation_token(
            task_id=coordinator.task_id,
            client_identity="client-a",
            platform_key="home",
        )
        outcome, _platform_key = poll_douyin_continuation(
            token,
            client_identity="client-a",
        )
        self.assertEqual(outcome.status, "success")
        coordinator.abandon()

    def test_pending_continuation_is_not_hidden_by_native_fallback(self):
        adapter = DouyinAdapter()
        direct_result = ParseResult(
            platform="douyin",
            post_type="video",
            base_filename="native",
            assets=(
                MediaAsset(
                    media_type="video",
                    url=MEDIA_URL,
                    platform="douyin",
                ),
            ),
        )
        context = DouyinParseContext(
            url=f"https://www.douyin.com/video/{AWEME_ID}",
            final_url=f"https://www.douyin.com/video/{AWEME_ID}",
            aweme_id=AWEME_ID,
            direct_result=direct_result,
        )
        pending_provider = Mock()
        pending_provider.name = "ultra_race"
        pending_provider.parse.side_effect = DouyinContinuationPendingError(
            "task-id"
        )
        native_provider = Mock()
        native_provider.name = "native"
        native_provider.parse.return_value = direct_result

        with (
            patch.object(
                adapter,
                "build_parse_context",
                return_value=context,
            ),
            patch.object(
                adapter,
                "get_result_providers",
                return_value=(pending_provider, native_provider),
            ),
        ):
            with self.assertRaises(DouyinContinuationPendingError):
                adapter.parse_url(
                    context.url,
                    prefer_ultra_hd=True,
                    allow_continuation=True,
                )

        pending_provider.parse.assert_called_once()
        native_provider.parse.assert_not_called()


class DownloadHandoffTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    def tearDown(self):
        cache.clear()

    def test_handoff_is_client_bound_one_use_and_opaque(self):
        raw_url = "https://ci.xiaohongshu.com/path/image.jpg?token=secret"
        tokens = issue_download_handoff_tokens(
            item={"type": "image", "url": raw_url},
            base_filename="demo",
            client_identity="client-a",
        )

        self.assertNotIn(raw_url, tokens.stream_token)
        self.assertNotIn(raw_url, tokens.status_token)
        initial_status = load_download_status(
            tokens.status_token,
            client_identity="client-a",
        )
        self.assertEqual(initial_status.status, "issued")
        loaded = load_download_handoff(
            tokens.stream_token,
            client_identity="client-a",
        )
        self.assertEqual(loaded.item["url"], raw_url)
        with self.assertRaises(DownloadHandoffError) as reused:
            load_download_handoff(
                tokens.stream_token,
                client_identity="client-a",
            )
        self.assertEqual(
            reused.exception.error_code,
            "DOWNLOAD_HANDOFF_ALREADY_USED",
        )
        self.assertEqual(
            load_download_status(
                tokens.status_token,
                client_identity="client-a",
            ).status,
            "issued",
        )
        with self.assertRaises(DownloadHandoffError) as mismatched:
            load_download_handoff(
                tokens.stream_token,
                client_identity="client-b",
                consume=False,
            )
        self.assertEqual(
            mismatched.exception.error_code,
            "DOWNLOAD_HANDOFF_CLIENT_MISMATCH",
        )
        with self.assertRaises(DownloadHandoffError) as status_mismatch:
            load_download_status(
                tokens.status_token,
                client_identity="client-b",
            )
        self.assertEqual(
            status_mismatch.exception.error_code,
            "DOWNLOAD_HANDOFF_CLIENT_MISMATCH",
        )

    def test_handoff_item_is_allowlisted_and_sensitive_fields_are_removed(self):
        item = {
            **build_tiantianjiexi_item(),
            "filename": "douyin-demo.mp4",
            "size_bytes": 1024,
            "cookie": "secret-cookie",
            "headers": {"Authorization": "secret"},
            "source_url": "https://www.douyin.com/video/secret",
            "provider_response": {"token": "secret"},
            "unknown_client_field": "secret",
        }
        tokens = issue_download_handoff_tokens(
            item=item,
            base_filename="demo",
            client_identity="client-a",
        )

        loaded = load_download_handoff(
            tokens.stream_token,
            client_identity="client-a",
            consume=False,
        )

        self.assertEqual(
            set(loaded.item),
            {
                "type",
                "url",
                "filename",
                "size_bytes",
                "provider",
                "provider_resource_id",
                "provider_cache_key",
                "provider_manifest_token",
                "asset_id",
            },
        )
        serialized = json.dumps(loaded.item)
        for forbidden in (
            "secret-cookie",
            "Authorization",
            "source_url",
            "provider_response",
            "unknown_client_field",
        ):
            self.assertNotIn(forbidden, serialized)
        status_payload = load_download_status(
            tokens.status_token,
            client_identity="client-a",
        ).as_public_dict()
        self.assertNotIn("url", json.dumps(status_payload).lower())

    def test_handoff_rejects_oversized_fields_and_unsafe_urls(self):
        with self.assertRaises(DownloadHandoffError) as oversized:
            issue_download_handoff_tokens(
                item={
                    "type": "image",
                    "url": "https://ci.xiaohongshu.com/demo.jpg",
                    "filename": "x" * 241,
                },
                base_filename="demo",
                client_identity="client-a",
            )
        self.assertEqual(
            oversized.exception.error_code,
            "DOWNLOAD_HANDOFF_INVALID",
        )

        with self.assertRaises(DownloadHandoffError) as unsafe:
            issue_download_handoff_tokens(
                item={
                    "type": "image",
                    "url": "https://evil.example/private.jpg",
                },
                base_filename="demo",
                client_identity="client-a",
            )
        self.assertEqual(
            unsafe.exception.error_code,
            "DOWNLOAD_HANDOFF_INVALID",
        )

    def test_provider_recovery_identity_survives_handoff_sanitization(self):
        tiantianjiexi_item = build_tiantianjiexi_item()
        kukutool_item = build_provider_result(
            provider="kukutool",
            url="https://v26.douyinvod.com/path/video.mp4",
        ).video_assets[0].to_download_item()

        loaded_items = []
        for item in (tiantianjiexi_item, kukutool_item):
            tokens = issue_download_handoff_tokens(
                item=item,
                base_filename="demo",
                client_identity="client-a",
            )
            loaded_items.append(
                load_download_handoff(
                    tokens.stream_token,
                    client_identity="client-a",
                ).item
            )

        self.assertEqual(
            get_tiantianjiexi_manifest_identity(loaded_items[0]),
            get_tiantianjiexi_manifest_identity(tiantianjiexi_item),
        )
        self.assertEqual(
            get_kukutool_manifest_identity(loaded_items[1]),
            get_kukutool_manifest_identity(kukutool_item),
        )

    def test_handoff_ttl_covers_startup_budget_with_buffer(self):
        self.assertGreaterEqual(
            DOWNLOAD_HANDOFF_TTL_SECONDS,
            int(DOWNLOAD_STREAM_START_TIMEOUT_SECONDS) + 10,
        )

    def test_stream_start_timeout_environment_value_is_safe_and_bounded(self):
        environment = (
            "xhs_download.services.download_handoff.os.environ"
        )
        with patch.dict(environment, {}, clear=True):
            self.assertEqual(
                configured_download_stream_start_timeout_seconds(),
                50.0,
            )
        with patch.dict(
            environment,
            {"DOWNLOAD_STREAM_START_TIMEOUT_SECONDS": "invalid"},
            clear=True,
        ):
            self.assertEqual(
                configured_download_stream_start_timeout_seconds(),
                50.0,
            )
        with patch.dict(
            environment,
            {"DOWNLOAD_STREAM_START_TIMEOUT_SECONDS": "0"},
            clear=True,
        ):
            self.assertEqual(
                configured_download_stream_start_timeout_seconds(),
                1.0,
            )
        with patch.dict(
            environment,
            {"DOWNLOAD_STREAM_START_TIMEOUT_SECONDS": "120"},
            clear=True,
        ):
            self.assertEqual(
                configured_download_stream_start_timeout_seconds(),
                55.0,
            )

    def test_handoff_tamper_and_expiry_are_rejected(self):
        tokens = issue_download_handoff_tokens(
            item={"type": "image", "url": "https://ci.xiaohongshu.com/demo.jpg"},
            base_filename="demo",
            client_identity="client-a",
        )
        with self.assertRaises(DownloadHandoffError) as tampered:
            load_download_handoff(
                f"{tokens.stream_token}tampered",
                client_identity="client-a",
            )
        self.assertEqual(tampered.exception.error_code, "DOWNLOAD_HANDOFF_INVALID")
        with patch(
            "xhs_download.services.download_handoff."
            "DOWNLOAD_HANDOFF_TTL_SECONDS",
            -1,
        ):
            with self.assertRaises(DownloadHandoffError) as expired:
                load_download_handoff(
                    tokens.stream_token,
                    client_identity="client-a",
                )
            with self.assertRaises(DownloadHandoffError) as status_expired:
                load_download_status(
                    tokens.status_token,
                    client_identity="client-a",
                )
        self.assertEqual(expired.exception.error_code, "DOWNLOAD_HANDOFF_EXPIRED")
        self.assertEqual(
            status_expired.exception.error_code,
            "DOWNLOAD_HANDOFF_EXPIRED",
        )

    def test_status_states_are_safe_repeatable_and_reads_do_not_extend_ttl(self):
        tokens = issue_download_handoff_tokens(
            item={
                "type": "video",
                "url": "https://v3-dy-o.zjcdn.com/video.mp4?token=secret",
            },
            base_filename="demo",
            client_identity="client-a",
        )

        for status in (
            "connecting",
            "refreshing",
            "streaming",
            "completed",
            "failed",
        ):
            updated = update_download_status(
                load_download_status(
                    tokens.status_token,
                    client_identity="client-a",
                ).ticket_id,
                status,
                error_code=(
                    "UPSTREAM_SSL_ERROR"
                    if status == "failed"
                    else ""
                ),
                recovered=status in {"streaming", "completed"},
                upstream_attempt_count=2,
            )
            self.assertTrue(updated)
            with patch.object(cache, "set") as mock_cache_set:
                loaded = load_download_status(
                    tokens.status_token,
                    client_identity="client-a",
                )
            self.assertEqual(loaded.status, status)
            self.assertEqual(
                loaded.stream_started,
                status in {"streaming", "completed", "failed"},
            )
            mock_cache_set.assert_not_called()
            serialized = json.dumps(loaded.as_public_dict())
            self.assertNotIn("zjcdn.com", serialized)
            self.assertNotIn("token=secret", serialized)

        failed = load_download_status(
            tokens.status_token,
            client_identity="client-a",
        ).as_public_dict()
        self.assertEqual(failed["error_code"], "UPSTREAM_SSL_ERROR")
        self.assertEqual(
            failed["message"],
            "上游资源连接异常，请稍后重试。",
        )
        self.assertTrue(failed["stream_started"])

    def test_failed_after_streaming_preserves_sticky_start_fact(self):
        tokens = issue_download_handoff_tokens(
            item={
                "type": "video",
                "url": "https://v3-dy-o.zjcdn.com/video.mp4?token=secret",
            },
            base_filename="demo",
            client_identity="client-a",
        )
        ticket_id = load_download_status(
            tokens.status_token,
            client_identity="client-a",
        ).ticket_id

        update_download_status(ticket_id, "streaming")
        update_download_status(
            ticket_id,
            "failed",
            error_code="UPSTREAM_CONNECTION_ERROR",
        )
        failed = load_download_status(
            tokens.status_token,
            client_identity="client-a",
        )

        self.assertEqual(failed.status, "failed")
        self.assertTrue(failed.stream_started)
        self.assertTrue(failed.as_public_dict()["stream_started"])


class DownloadPrepareViewTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = Client()
        self.payload = {
            "media_items": [
                {
                    "type": "image",
                    "url": "https://ci.xiaohongshu.com/demo.jpg",
                }
            ],
            "base_filename": "demo",
        }

    def test_prepare_is_local_only_and_returns_stream_and_status_urls(self):
        client_identity = hashlib.sha256(
            b"127.0.0.1"
        ).hexdigest()[:24]
        with (
            patch(
                "xhs_download.views.check_download_request_limits",
                return_value=(client_identity, None),
            ),
            patch(
                "xhs_download.views.acquire_concurrency",
                return_value=LimitResult(allowed=True),
            ),
            patch(
                "xhs_download.views.decide_download_route",
                return_value={
                    "route_type": "proxy",
                    "download_strategy": "server_single_attachment",
                },
            ),
            patch("xhs_download.views.acquire_concurrency") as mock_acquire,
            patch(
                "xhs_download.views.inspect_single_attachment_download"
            ) as mock_inspect,
            patch(
                "xhs_download.views.open_single_attachment_upstream"
            ) as mock_open,
            patch(
                "xhs_download.views.fetch_media_response"
            ) as mock_fetch,
            patch(
                "xhs_download.views.build_single_attachment_response"
            ) as mock_build,
        ):
            response = self.client.post(
                "/media/download-prepare/",
                data=json.dumps(self.payload),
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["status"], "issued")
        self.assertTrue(body["download_url"].startswith("/media/download-stream/"))
        self.assertTrue(body["status_url"].startswith("/media/download-status/"))
        self.assertEqual(body["poll_after_ms"], 500)
        self.assertEqual(body["expires_in_seconds"], 60)
        self.assertNotIn(self.payload["media_items"][0]["url"], body["download_url"])
        self.assertNotIn(self.payload["media_items"][0]["url"], body["status_url"])
        self.assertNotIn(
            self.payload["media_items"][0]["url"],
            response.content.decode(),
        )
        status_response = self.client.get(body["status_url"])
        self.assertEqual(status_response.status_code, 200)
        self.assertEqual(status_response.json()["status"], "issued")
        mock_acquire.assert_not_called()
        mock_inspect.assert_not_called()
        mock_open.assert_not_called()
        mock_fetch.assert_not_called()
        mock_build.assert_not_called()

    def test_prepare_errors_are_json_and_csrf_is_enforced(self):
        bad = self.client.post(
            "/media/download-prepare/",
            data="{bad",
            content_type="application/json",
        )
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(bad.json()["error_code"], "INVALID_JSON")

        csrf_client = Client(enforce_csrf_checks=True)
        forbidden = csrf_client.post(
            "/media/download-prepare/",
            data=json.dumps(self.payload),
            content_type="application/json",
        )
        self.assertEqual(forbidden.status_code, 403)

    def test_prepare_guard_and_rate_limit_failures_are_observable_json(self):
        with patch(
            "xhs_download.views.check_download_cost_preflight",
            return_value=(
                "VISITOR_VIDEO_DOWNLOAD_DISABLED",
                "当前环境暂不允许访客下载视频资源。",
                403,
            ),
        ):
            forbidden = self.client.post(
                "/media/download-prepare/",
                data=json.dumps(self.payload),
                content_type="application/json",
            )

        self.assertEqual(forbidden.status_code, 403)
        self.assertEqual(
            forbidden.json()["error_code"],
            "VISITOR_VIDEO_DOWNLOAD_DISABLED",
        )

        with patch(
            "xhs_download.views.check_download_request_limits",
            return_value=(
                "identity",
                HttpResponse(
                    json.dumps(
                        {
                            "status": "error",
                            "error_code": "RATE_LIMITED",
                            "message": "请求过于频繁，请稍后再试。",
                        }
                    ),
                    status=429,
                    content_type="application/json",
                ),
            ),
        ):
            limited = self.client.post(
                "/media/download-prepare/",
                data=json.dumps(self.payload),
                content_type="application/json",
            )

        self.assertEqual(limited.status_code, 429)
        self.assertEqual(limited.json()["error_code"], "RATE_LIMITED")

    @patch("xhs_download.views.open_single_attachment_upstream")
    def test_legacy_inspection_helper_reads_only_first_nonempty_chunk(self, mock_open):
        upstream = Mock()
        upstream.headers = {
            "Content-Type": "video/mp4",
            "Content-Length": "18",
        }
        upstream.url = MEDIA_URL
        yielded = []

        def iter_content(*, chunk_size):
            self.assertGreater(chunk_size, 0)
            for chunk in (b"first-", b"second", b"-third"):
                yielded.append(chunk)
                yield chunk

        upstream.iter_content.side_effect = iter_content
        item = build_tiantianjiexi_item()
        mock_open.return_value = (upstream, item, False)

        prepared = inspect_single_attachment_download(
            item,
            "douyin-demo",
        )

        self.assertEqual(prepared["content_length"], 18)
        self.assertEqual(yielded, [b"first-"])
        upstream.close.assert_called_once_with()


class DownloadStreamStartupDeadlineTests(SimpleTestCase):
    def build_upstream(self, chunks, *, content_type="video/mp4"):
        upstream = Mock()
        upstream.headers = {
            "Content-Type": content_type,
            "Content-Length": str(sum(len(chunk) for chunk in chunks)),
        }
        upstream.url = MEDIA_URL
        upstream.iter_content.side_effect = lambda *, chunk_size: iter(chunks)
        return upstream

    def test_recovery_attempts_share_one_startup_budget(self):
        clock = FakeMonotonicClock()
        original = build_tiantianjiexi_item()
        refreshed = build_tiantianjiexi_item(
            "https://v5-dy-o.zjcdn.com/path/refreshed.mp4"
        )
        upstream = self.build_upstream([b"video"])
        attempts = []

        def fetch(_url, **kwargs):
            attempts.append(kwargs)
            if len(attempts) == 1:
                clock.advance(30)
                raise requests.exceptions.ConnectionError("first")
            return upstream

        def refresh(_item, **kwargs):
            self.assertEqual(kwargs["deadline_monotonic"], 150.0)
            clock.advance(10)
            return refreshed

        with (
            patch("xhs_download.views.time.monotonic", side_effect=clock),
            patch(
                "xhs_download.views.fetch_media_response",
                side_effect=fetch,
            ),
            patch(
                "xhs_download.views.refresh_tiantianjiexi_download_item",
                side_effect=refresh,
            ),
            patch(
                "xhs_download.views.invalidate_external_provider_caches"
            ),
        ):
            response, _item, recovered = open_single_attachment_upstream(
                original,
                "video",
                60,
                include_context=True,
                startup_deadline=150.0,
            )

        self.assertTrue(recovered)
        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[0]["timeout"], 50.0)
        self.assertEqual(attempts[1]["timeout"], 10.0)
        self.assertEqual(attempts[0]["deadline_monotonic"], 150.0)
        self.assertEqual(attempts[1]["deadline_monotonic"], 150.0)
        response.close()

    def test_provider_request_retries_use_remaining_deadline(self):
        clock = FakeMonotonicClock()
        observed_timeouts = []

        def request_get(*_args, **kwargs):
            observed_timeouts.append(kwargs["timeout"])
            if len(observed_timeouts) == 1:
                clock.advance(30)
                raise requests.exceptions.ConnectionError("first")
            clock.advance(21)
            raise requests.exceptions.ReadTimeout("second")

        with (
            patch(
                "xhs_download.services.downloads.time.monotonic",
                side_effect=clock,
            ),
            patch(
                "xhs_download.services.downloads."
                "validate_provider_signed_download_url"
            ),
            patch(
                "xhs_download.services.downloads.requests.get",
                side_effect=request_get,
            ),
        ):
            with self.assertRaises(requests.exceptions.ReadTimeout):
                request_public_provider_get_with_retry(
                    MEDIA_URL,
                    timeout=60,
                    retries=2,
                    provider_item=build_tiantianjiexi_item(),
                    deadline_monotonic=150.0,
                )

        self.assertEqual(observed_timeouts, [50.0, 20.0])

    def test_first_chunk_socket_timeout_uses_remaining_budget(self):
        response = requests.Response()
        upstream_socket = Mock()
        response.raw = SimpleNamespace(
            _connection=SimpleNamespace(sock=upstream_socket),
            _fp=None,
        )

        apply_upstream_startup_read_timeout(response, 12.5)

        upstream_socket.settimeout.assert_called_once_with(12.5)

    def test_first_connection_exhaustion_stops_before_refresh(self):
        clock = FakeMonotonicClock()

        def fetch(_url, **_kwargs):
            clock.advance(51)
            raise requests.exceptions.ReadTimeout("slow")

        with (
            patch("xhs_download.views.time.monotonic", side_effect=clock),
            patch(
                "xhs_download.views.fetch_media_response",
                side_effect=fetch,
            ),
            patch(
                "xhs_download.views.refresh_tiantianjiexi_download_item"
            ) as mock_refresh,
        ):
            with self.assertRaises(DownloadStreamStartTimeout) as raised:
                open_single_attachment_upstream(
                    build_tiantianjiexi_item(),
                    "video",
                    60,
                    startup_deadline=150.0,
                )

        self.assertEqual(
            raised.exception.error_code,
            "DOWNLOAD_STREAM_START_TIMEOUT",
        )
        mock_refresh.assert_not_called()

    def test_refresh_exhaustion_stops_before_second_connection(self):
        clock = FakeMonotonicClock()
        refreshed = build_tiantianjiexi_item(
            "https://v5-dy-o.zjcdn.com/path/refreshed.mp4"
        )

        def fetch(_url, **_kwargs):
            clock.advance(20)
            raise requests.exceptions.ConnectionError("first")

        def refresh(_item, **_kwargs):
            clock.advance(31)
            return refreshed

        with (
            patch("xhs_download.views.time.monotonic", side_effect=clock),
            patch(
                "xhs_download.views.fetch_media_response",
                side_effect=fetch,
            ) as mock_fetch,
            patch(
                "xhs_download.views.refresh_tiantianjiexi_download_item",
                side_effect=refresh,
            ),
            patch(
                "xhs_download.views.invalidate_external_provider_caches"
            ),
        ):
            with self.assertRaises(DownloadStreamStartTimeout):
                open_single_attachment_upstream(
                    build_tiantianjiexi_item(),
                    "video",
                    60,
                    startup_deadline=150.0,
                )

        self.assertEqual(mock_fetch.call_count, 1)

    def test_first_chunk_must_arrive_inside_startup_budget(self):
        clock = FakeMonotonicClock()
        upstream = self.build_upstream([], content_type="image/jpeg")
        upstream.url = "https://ci.xiaohongshu.com/demo.jpg"

        def slow_chunks(*, chunk_size):
            self.assertGreater(chunk_size, 0)
            clock.advance(51)
            yield b"image-data"

        upstream.iter_content.side_effect = slow_chunks
        with (
            patch("xhs_download.views.time.monotonic", side_effect=clock),
            patch(
                "xhs_download.views.fetch_media_response",
                return_value=upstream,
            ),
        ):
            with self.assertRaises(DownloadStreamStartTimeout):
                build_single_attachment_response(
                    {
                        "type": "image",
                        "url": "https://ci.xiaohongshu.com/demo.jpg",
                    },
                    "demo",
                    startup_deadline=150.0,
                )

        upstream.close.assert_called_once_with()

    def test_startup_deadline_stops_limiting_after_first_chunk(self):
        clock = FakeMonotonicClock()
        upstream = self.build_upstream(
            [b"first", b"-second"],
            content_type="image/jpeg",
        )
        upstream.url = "https://ci.xiaohongshu.com/demo.jpg"

        with (
            patch("xhs_download.views.time.monotonic", side_effect=clock),
            patch(
                "xhs_download.views.fetch_media_response",
                return_value=upstream,
            ),
        ):
            response, _actual_bytes = build_single_attachment_response(
                {
                    "type": "image",
                    "url": "https://ci.xiaohongshu.com/demo.jpg",
                },
                "demo",
                startup_deadline=150.0,
            )
            clock.advance(1000)
            content = b"".join(response.streaming_content)

        self.assertEqual(content, b"first-second")
        upstream.close.assert_called_once_with()

    def test_first_chunk_restores_the_normal_stream_read_timeout(self):
        clock = FakeMonotonicClock()
        upstream_socket = Mock()
        upstream = requests.Response()
        upstream.status_code = 200
        upstream.headers = {
            "Content-Type": "image/jpeg",
            "Content-Length": "12",
        }
        upstream.url = "https://ci.xiaohongshu.com/demo.jpg"
        upstream.raw = SimpleNamespace(
            _connection=SimpleNamespace(sock=upstream_socket),
            _fp=None,
        )
        upstream.iter_content = Mock(
            return_value=iter([b"first", b"-second"])
        )
        upstream.close = Mock()

        with (
            patch("xhs_download.views.time.monotonic", side_effect=clock),
            patch(
                "xhs_download.views.fetch_media_response",
                return_value=upstream,
            ),
        ):
            response, _actual_bytes = build_single_attachment_response(
                {
                    "type": "image",
                    "url": "https://ci.xiaohongshu.com/demo.jpg",
                },
                "demo",
                startup_deadline=150.0,
            )
            content = b"".join(response.streaming_content)

        self.assertEqual(content, b"first-second")
        self.assertEqual(
            [
                call.args[0]
                for call in upstream_socket.settimeout.call_args_list
            ],
            [50.0, 20],
        )
        upstream.close.assert_called_once_with()

    @patch(
        "xhs_download.services.platforms.douyin_kukutool."
        "submit_provider_task"
    )
    def test_kukutool_download_refresh_preserves_startup_deadline(
        self,
        mock_submit,
    ):
        cache.clear()
        self.addCleanup(cache.clear)
        completed = Future()
        completed.set_result(None)
        mock_submit.return_value = completed
        deadline = time.monotonic() + 20
        context = DouyinParseContext(
            url=f"https://www.douyin.com/video/{AWEME_ID}",
            final_url=f"https://www.douyin.com/video/{AWEME_ID}",
            aweme_id=AWEME_ID,
            deadline_monotonic=deadline,
        )

        result = DouyinKukuToolProvider().refresh_for_download(
            DouyinAdapter(),
            context,
        )

        self.assertIsNone(result)
        submitted_context = mock_submit.call_args.args[5]
        self.assertEqual(submitted_context.deadline_monotonic, deadline)


class DownloadSingleConnectionStreamTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = Client()
        self.client_identity = hashlib.sha256(
            b"127.0.0.1"
        ).hexdigest()[:24]
        self.item = {
            "type": "image",
            "url": "https://ci.xiaohongshu.com/demo.jpg",
            "size_bytes": 11,
        }

    def tearDown(self):
        cache.clear()

    def issue(self, item=None):
        return issue_download_handoff_tokens(
            item=item or self.item,
            base_filename="demo",
            client_identity=self.client_identity,
        )

    def build_upstream(self, chunks, *, content_type="image/jpeg", url=None):
        upstream = Mock()
        upstream.headers = {
            "Content-Type": content_type,
            "Content-Length": str(
                sum(len(chunk) for chunk in chunks if isinstance(chunk, bytes))
            ),
        }
        upstream.url = url or self.item["url"]
        upstream.iter_content.side_effect = lambda *, chunk_size: iter(chunks)
        return upstream

    def stream_request_patches(self, upstream):
        return (
            patch(
                "xhs_download.views.fetch_media_response",
                return_value=upstream,
            ),
            patch(
                "xhs_download.views.acquire_concurrency",
                return_value=LimitResult(allowed=True),
            ),
            patch(
                "xhs_download.views.record_download_archive_bytes",
                return_value=(True, ""),
            ),
            patch("xhs_download.views.release_concurrency"),
        )

    def test_normal_stream_uses_one_connection_and_releases_after_completion(self):
        tokens = self.issue()
        upstream = self.build_upstream([b"first-", b"last"])
        fetch_patch, acquire_patch, bytes_patch, release_patch = (
            self.stream_request_patches(upstream)
        )

        with (
            fetch_patch as mock_fetch,
            acquire_patch,
            bytes_patch,
            release_patch as mock_release,
        ):
            response = self.client.get(
                f"/media/download-stream/{tokens.stream_token}/"
            )
            self.assertEqual(response.status_code, 200)
            mock_fetch.assert_called_once()
            mock_release.assert_not_called()
            status_before_body = load_download_status(
                tokens.status_token,
                client_identity=self.client_identity,
            )
            self.assertEqual(status_before_body.status, "connecting")
            self.assertFalse(status_before_body.stream_started)
            self.assertEqual(status_before_body.upstream_attempt_count, 1)

            iterator = iter(response.streaming_content)
            first_chunk = next(iterator)
            status_after_first_next = load_download_status(
                tokens.status_token,
                client_identity=self.client_identity,
            )
            self.assertEqual(status_after_first_next.status, "streaming")
            self.assertTrue(status_after_first_next.stream_started)
            content = first_chunk + b"".join(iterator)

            self.assertEqual(content, b"first-last")
            mock_fetch.assert_called_once()
            upstream.close.assert_called_once_with()
            mock_release.assert_called_once_with(
                "download-concurrent",
                self.client_identity,
            )

        completed = load_download_status(
            tokens.status_token,
            client_identity=self.client_identity,
        )
        self.assertEqual(completed.status, "completed")
        self.assertTrue(completed.stream_started)
        self.assertFalse(completed.recovered)
        self.assertEqual(completed.upstream_attempt_count, 1)

    def test_close_before_iteration_fails_without_marking_stream_started(self):
        tokens = self.issue()
        upstream = self.build_upstream([b"image-data"])
        fetch_patch, acquire_patch, bytes_patch, release_patch = (
            self.stream_request_patches(upstream)
        )

        with (
            fetch_patch,
            acquire_patch,
            bytes_patch,
            release_patch as mock_release,
        ):
            response = self.client.get(
                f"/media/download-stream/{tokens.stream_token}/"
            )
            response._animeimages_attachment_stream.close()

        failed = load_download_status(
            tokens.status_token,
            client_identity=self.client_identity,
        )
        self.assertEqual(failed.status, "failed")
        self.assertFalse(failed.stream_started)
        upstream.close.assert_called_once_with()
        mock_release.assert_called_once_with(
            "download-concurrent",
            self.client_identity,
        )

    def test_first_chunk_is_validated_then_yielded_from_same_iterator(self):
        yielded = []
        upstream = Mock()
        upstream.headers = {
            "Content-Type": "image/jpeg",
            "Content-Length": "18",
        }
        upstream.url = self.item["url"]

        def iter_content(*, chunk_size):
            self.assertGreater(chunk_size, 0)
            for chunk in (b"first-", b"second", b"-third"):
                yielded.append(chunk)
                yield chunk

        upstream.iter_content.side_effect = iter_content
        events = []
        telemetry = {}

        with patch(
            "xhs_download.views.fetch_media_response",
            return_value=upstream,
        ) as mock_fetch:
            response, _actual_bytes = build_single_attachment_response(
                self.item,
                "demo",
                telemetry=telemetry,
                status_callback=(
                    lambda status, snapshot, error=None:
                    events.append((status, snapshot, error))
                ),
            )
            self.assertEqual(yielded, [b"first-"])
            self.assertEqual(
                [event[0] for event in events],
                ["connecting"],
            )

            self.assertEqual(
                b"".join(response.streaming_content),
                b"first-second-third",
            )

        mock_fetch.assert_called_once()
        self.assertEqual(yielded, [b"first-", b"second", b"-third"])
        self.assertEqual(
            [event[0] for event in events],
            ["connecting", "streaming", "completed"],
        )
        upstream.close.assert_called_once_with()

    def test_streaming_status_waits_for_download_bytes_guard(self):
        tokens = self.issue()
        upstream = self.build_upstream([b"image-data"])

        with (
            patch(
                "xhs_download.views.fetch_media_response",
                return_value=upstream,
            ),
            patch(
                "xhs_download.views.acquire_concurrency",
                return_value=LimitResult(allowed=True),
            ),
            patch(
                "xhs_download.views.record_download_archive_bytes",
                return_value=(False, "DOWNLOAD_DAILY_BYTES_LIMIT_IP"),
            ),
            patch("xhs_download.views.release_concurrency") as mock_release,
        ):
            response = self.client.get(
                f"/media/download-stream/{tokens.stream_token}/"
            )

        self.assertEqual(response.status_code, 429)
        status = load_download_status(
            tokens.status_token,
            client_identity=self.client_identity,
        )
        self.assertEqual(status.status, "failed")
        self.assertEqual(
            status.error_code,
            "DOWNLOAD_DAILY_BYTES_LIMIT_IP",
        )
        upstream.close.assert_called_once_with()
        mock_release.assert_called_once_with(
            "download-concurrent",
            self.client_identity,
        )

    def test_duplicate_stream_claim_is_rejected_before_second_open(self):
        tokens = self.issue()
        upstream = self.build_upstream([b"image-data"])
        fetch_patch, acquire_patch, bytes_patch, release_patch = (
            self.stream_request_patches(upstream)
        )

        with (
            fetch_patch as mock_fetch,
            acquire_patch,
            bytes_patch,
            release_patch,
        ):
            first = self.client.get(
                f"/media/download-stream/{tokens.stream_token}/"
            )
            second = self.client.get(
                f"/media/download-stream/{tokens.stream_token}/"
            )
            self.assertEqual(second.status_code, 409)
            self.assertEqual(
                second.json()["error_code"],
                "DOWNLOAD_HANDOFF_ALREADY_USED",
            )
            self.assertEqual(mock_fetch.call_count, 1)
            self.assertEqual(
                b"".join(first.streaming_content),
                b"image-data",
            )

    def test_expired_stream_is_rejected_before_open(self):
        tokens = self.issue()
        with (
            patch(
                "xhs_download.services.download_handoff."
                "DOWNLOAD_HANDOFF_TTL_SECONDS",
                -1,
            ),
            patch("xhs_download.views.fetch_media_response") as mock_fetch,
        ):
            response = self.client.get(
                f"/media/download-stream/{tokens.stream_token}/"
            )

        self.assertEqual(response.status_code, 410)
        self.assertEqual(
            response.json()["error_code"],
            "DOWNLOAD_HANDOFF_EXPIRED",
        )
        mock_fetch.assert_not_called()

    def test_initial_connection_failure_is_safe_and_releases_concurrency(self):
        tokens = self.issue()
        with (
            patch(
                "xhs_download.views.fetch_media_response",
                side_effect=requests.exceptions.SSLError(
                    "https://secret.example/video?token=secret"
                ),
            ) as mock_fetch,
            patch(
                "xhs_download.views.acquire_concurrency",
                return_value=LimitResult(allowed=True),
            ),
            patch("xhs_download.views.release_concurrency") as mock_release,
        ):
            response = self.client.get(
                f"/media/download-stream/{tokens.stream_token}/"
            )

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["error_code"], "UPSTREAM_SSL_ERROR")
        self.assertNotIn("secret.example", response.content.decode())
        mock_fetch.assert_called_once()
        mock_release.assert_called_once()
        failed = load_download_status(
            tokens.status_token,
            client_identity=self.client_identity,
        )
        self.assertEqual(failed.status, "failed")
        self.assertEqual(failed.error_code, "UPSTREAM_SSL_ERROR")
        self.assertEqual(failed.upstream_attempt_count, 1)

    def test_startup_timeout_is_stable_and_releases_concurrency(self):
        tokens = self.issue(build_tiantianjiexi_item())
        clock = FakeMonotonicClock()
        upstream = self.build_upstream(
            [],
            content_type="video/mp4",
            url=MEDIA_URL,
        )

        def slow_chunks(*, chunk_size):
            self.assertGreater(chunk_size, 0)
            clock.advance(51)
            yield b"video-data"

        upstream.iter_content.side_effect = slow_chunks

        with (
            patch("xhs_download.views.time.monotonic", side_effect=clock),
            patch(
                "xhs_download.views.fetch_media_response",
                return_value=upstream,
            ),
            patch(
                "xhs_download.views.acquire_concurrency",
                return_value=LimitResult(allowed=True),
            ),
            patch("xhs_download.views.release_concurrency") as mock_release,
            patch(
                "xhs_download.views.refresh_tiantianjiexi_download_item"
            ) as mock_refresh,
        ):
            response = self.client.get(
                f"/media/download-stream/{tokens.stream_token}/"
            )

        self.assertEqual(response.status_code, 504)
        self.assertEqual(
            response.json()["error_code"],
            "DOWNLOAD_STREAM_START_TIMEOUT",
        )
        self.assertEqual(
            response.json()["message"],
            "资源连接超时，请重新点击下载或重新解析后再试。",
        )
        mock_refresh.assert_not_called()
        upstream.close.assert_called_once_with()
        mock_release.assert_called_once_with(
            "download-concurrent",
            self.client_identity,
        )
        failed = load_download_status(
            tokens.status_token,
            client_identity=self.client_identity,
        )
        self.assertEqual(failed.status, "failed")
        self.assertEqual(
            failed.error_code,
            "DOWNLOAD_STREAM_START_TIMEOUT",
        )
        self.assertFalse(failed.stream_started)

    def test_recovery_refreshes_once_and_uses_at_most_two_media_gets(self):
        original = build_tiantianjiexi_item()
        refreshed = build_tiantianjiexi_item(
            "https://v5-dy-o.zjcdn.com/path/refreshed.mp4"
        )
        tokens = self.issue(original)
        upstream = self.build_upstream(
            [b"video-data"],
            content_type="video/mp4",
            url=refreshed["url"],
        )

        with (
            patch(
                "xhs_download.views.fetch_media_response",
                side_effect=[
                    requests.exceptions.SSLError("first connection failed"),
                    upstream,
                ],
            ) as mock_fetch,
            patch(
                "xhs_download.views.refresh_tiantianjiexi_download_item",
                return_value=refreshed,
            ) as mock_refresh,
            patch(
                "xhs_download.views.invalidate_external_provider_caches"
            ),
            patch(
                "xhs_download.views.acquire_concurrency",
                return_value=LimitResult(allowed=True),
            ),
            patch(
                "xhs_download.views.record_download_archive_bytes",
                return_value=(True, ""),
            ),
            patch("xhs_download.views.release_concurrency"),
        ):
            response = self.client.get(
                f"/media/download-stream/{tokens.stream_token}/"
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(
                b"".join(response.streaming_content),
                b"video-data",
            )

        self.assertEqual(mock_fetch.call_count, 2)
        mock_refresh.assert_called_once()
        self.assertEqual(mock_refresh.call_args.args[0], original)
        self.assertIn(
            "deadline_monotonic",
            mock_refresh.call_args.kwargs,
        )
        completed = load_download_status(
            tokens.status_token,
            client_identity=self.client_identity,
        )
        self.assertEqual(completed.status, "completed")
        self.assertTrue(completed.recovered)
        self.assertEqual(completed.upstream_attempt_count, 2)

    def test_recovery_failure_never_opens_a_third_media_connection(self):
        original = build_tiantianjiexi_item()
        refreshed = build_tiantianjiexi_item(
            "https://v5-dy-o.zjcdn.com/path/refreshed.mp4"
        )
        tokens = self.issue(original)

        with (
            patch(
                "xhs_download.views.fetch_media_response",
                side_effect=[
                    requests.exceptions.ConnectionError("first"),
                    requests.exceptions.ConnectionError("second"),
                ],
            ) as mock_fetch,
            patch(
                "xhs_download.views.refresh_tiantianjiexi_download_item",
                return_value=refreshed,
            ) as mock_refresh,
            patch(
                "xhs_download.views.invalidate_external_provider_caches"
            ),
            patch(
                "xhs_download.views.acquire_concurrency",
                return_value=LimitResult(allowed=True),
            ),
            patch("xhs_download.views.release_concurrency"),
        ):
            response = self.client.get(
                f"/media/download-stream/{tokens.stream_token}/"
            )

        self.assertEqual(response.status_code, 502)
        self.assertEqual(mock_fetch.call_count, 2)
        mock_refresh.assert_called_once()
        self.assertEqual(mock_refresh.call_args.args[0], original)
        self.assertIn(
            "deadline_monotonic",
            mock_refresh.call_args.kwargs,
        )
        failed = load_download_status(
            tokens.status_token,
            client_identity=self.client_identity,
        )
        self.assertEqual(failed.status, "failed")
        self.assertEqual(failed.upstream_attempt_count, 2)

    def test_generator_failure_marks_failed_and_releases_concurrency(self):
        tokens = self.issue()
        upstream = Mock()
        upstream.headers = {
            "Content-Type": "image/jpeg",
            "Content-Length": "12",
        }
        upstream.url = self.item["url"]

        def failing_chunks(*, chunk_size):
            yield b"first-"
            raise requests.exceptions.ConnectionError(
                "https://secret.example/image?token=secret"
            )

        upstream.iter_content.side_effect = failing_chunks
        fetch_patch, acquire_patch, bytes_patch, release_patch = (
            self.stream_request_patches(upstream)
        )

        with (
            fetch_patch,
            acquire_patch,
            bytes_patch,
            release_patch as mock_release,
            self.assertLogs("xhs_download.views", level="WARNING") as logs,
        ):
            response = self.client.get(
                f"/media/download-stream/{tokens.stream_token}/"
            )
            with self.assertRaises(requests.exceptions.ConnectionError):
                b"".join(response.streaming_content)

        failed = load_download_status(
            tokens.status_token,
            client_identity=self.client_identity,
        )
        self.assertEqual(failed.status, "failed")
        self.assertEqual(failed.error_code, "UPSTREAM_CONNECTION_ERROR")
        self.assertTrue(failed.stream_started)
        mock_release.assert_called_once()
        self.assertNotIn("secret.example", "\n".join(logs.output))
        self.assertNotIn("token=secret", "\n".join(logs.output))

    def test_status_endpoint_is_bound_safe_and_never_touches_upstream(self):
        tokens = self.issue()
        issued = load_download_status(
            tokens.status_token,
            client_identity=self.client_identity,
        )
        update_download_status(
            issued.ticket_id,
            "refreshing",
            recovered=False,
            upstream_attempt_count=1,
        )

        with (
            patch("xhs_download.views.fetch_media_response") as mock_fetch,
            patch(
                "xhs_download.views.refresh_tiantianjiexi_download_item"
            ) as mock_refresh,
        ):
            response = self.client.get(
                f"/media/download-status/{tokens.status_token}/"
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertEqual(response.json()["status"], "refreshing")
        self.assertEqual(response.json()["upstream_attempt_count"], 1)
        serialized = response.content.decode()
        self.assertNotIn(self.item["url"], serialized)
        mock_fetch.assert_not_called()
        mock_refresh.assert_not_called()

        mismatched = self.client.get(
            f"/media/download-status/{tokens.status_token}/",
            REMOTE_ADDR="127.0.0.2",
        )
        self.assertEqual(mismatched.status_code, 403)
        tampered = self.client.get(
            f"/media/download-status/{tokens.status_token}tampered/"
        )
        self.assertEqual(tampered.status_code, 403)


class TianTianJieXiRecoveryTests(SimpleTestCase):
    def test_manifest_identity_requires_exact_provider_contract(self):
        item = build_tiantianjiexi_item()
        self.assertEqual(
            get_tiantianjiexi_manifest_identity(item),
            (
                AWEME_ID,
                f"douyin:tiantianjiexi:validated:{AWEME_ID}",
            ),
        )
        rejected = {**item, "url": "https://evil.example.com/video.mp4"}
        rejected["provider_manifest_token"] = build_provider_manifest_token(
            asset_id=rejected["asset_id"],
            provider=rejected["provider"],
            provider_resource_id=rejected["provider_resource_id"],
            provider_cache_key=rejected["provider_cache_key"],
            url=rejected["url"],
            media_type="video",
        )
        self.assertIsNone(get_tiantianjiexi_manifest_identity(rejected))

    @patch("xhs_download.views.refresh_tiantianjiexi_download_item")
    @patch("xhs_download.views.fetch_media_response")
    def test_successful_first_download_does_not_refresh(
        self,
        mock_fetch,
        mock_refresh,
    ):
        upstream = Mock()
        mock_fetch.return_value = upstream

        response, effective_item, recovered = open_single_attachment_upstream(
            build_tiantianjiexi_item(),
            "video",
            5,
            include_context=True,
        )

        self.assertIs(response, upstream)
        self.assertEqual(effective_item["provider"], "tiantianjiexi")
        self.assertFalse(recovered)
        mock_fetch.assert_called_once()
        mock_refresh.assert_not_called()

    @patch("xhs_download.views.invalidate_external_provider_caches")
    @patch("xhs_download.views.refresh_tiantianjiexi_download_item")
    @patch("xhs_download.views.fetch_media_response")
    @patch(
        "xhs_download.views.time.monotonic",
        side_effect=(10.0, 12.5, 13.0, 14.0, 15.0, 18.25),
    )
    def test_recoverable_failure_refreshes_and_retries_once(
        self,
        _mock_monotonic,
        mock_fetch,
        mock_refresh,
        _mock_invalidate,
    ):
        original = build_tiantianjiexi_item()
        refreshed = build_tiantianjiexi_item(
            "https://v5-dy-o.zjcdn.com/path/refreshed.mp4"
        )
        upstream = Mock()
        mock_fetch.side_effect = [
            XhsDownloadError("HTTP 403"),
            upstream,
        ]
        mock_refresh.return_value = refreshed
        events = []
        telemetry = {}

        response, effective_item, recovered = open_single_attachment_upstream(
            original,
            "video",
            5,
            include_context=True,
            telemetry=telemetry,
            status_callback=(
                lambda status, snapshot, error=None:
                events.append((status, snapshot, error))
            ),
        )

        self.assertIs(response, upstream)
        self.assertEqual(effective_item["url"], refreshed["url"])
        self.assertTrue(recovered)
        self.assertEqual(mock_fetch.call_count, 2)
        mock_refresh.assert_called_once_with(original)
        self.assertEqual(
            [event[0] for event in events],
            ["connecting", "refreshing", "connecting"],
        )
        self.assertEqual(telemetry["upstream_attempt_count"], 2)
        self.assertTrue(telemetry["recovery_used"])
        self.assertEqual(
            telemetry["upstream_attempt_1_duration_ms"],
            2500,
        )
        self.assertEqual(
            telemetry["provider_refresh_duration_ms"],
            1000,
        )
        self.assertEqual(
            telemetry["upstream_attempt_2_duration_ms"],
            3250,
        )
        self.assertEqual(
            telemetry["last_upstream_error_type"],
            "XhsDownloadError",
        )

    @patch("xhs_download.views.refresh_tiantianjiexi_download_item")
    @patch("xhs_download.views.fetch_media_response")
    def test_nonrecoverable_stream_error_never_refreshes(
        self,
        mock_fetch,
        mock_refresh,
    ):
        mock_fetch.side_effect = XhsDownloadError(
            "资源超过允许的大小限制"
        )

        with self.assertRaisesRegex(XhsDownloadError, "大小限制"):
            open_single_attachment_upstream(
                build_tiantianjiexi_item(),
                "video",
                5,
            )

        mock_fetch.assert_called_once()
        mock_refresh.assert_not_called()

    @patch("xhs_download.views.invalidate_external_provider_caches")
    @patch("xhs_download.views.refresh_tiantianjiexi_download_item")
    @patch("xhs_download.views.fetch_media_response")
    def test_refresh_failure_does_not_loop(
        self,
        mock_fetch,
        mock_refresh,
        _mock_invalidate,
    ):
        original = build_tiantianjiexi_item()
        mock_fetch.side_effect = XhsDownloadError("HTTP 404")
        mock_refresh.side_effect = TianTianJieXiRecoveryError(
            "refresh failed",
            error_code="TIANTIANJIEXI_REFRESH_FAILED",
        )

        with self.assertRaises(TianTianJieXiRecoveryError):
            open_single_attachment_upstream(
                original,
                "video",
                5,
            )

        self.assertEqual(mock_fetch.call_count, 1)
        mock_refresh.assert_called_once_with(original)

    def test_download_errors_have_stable_safe_contracts(self):
        ssl_contract = observable_download_error(
            requests.exceptions.SSLError("secret"),
            provider="tiantianjiexi",
        )
        connection_contract = observable_download_error(
            requests.exceptions.ConnectionError("secret"),
            provider="tiantianjiexi",
        )
        expired_contract = observable_download_error(
            XhsDownloadError("HTTP 403"),
            provider="tiantianjiexi",
        )

        self.assertEqual(ssl_contract["error_code"], "UPSTREAM_SSL_ERROR")
        self.assertEqual(
            connection_contract["error_code"],
            "UPSTREAM_CONNECTION_ERROR",
        )
        self.assertEqual(
            expired_contract["error_code"],
            "TIANTIANJIEXI_RESOURCE_EXPIRED",
        )

    @patch(
        "xhs_download.services.provider_recovery."
        "invalidate_tiantianjiexi_download_cache"
    )
    @patch(
        "xhs_download.services.provider_recovery."
        "DouyinTianTianJieXiProvider.parse"
    )
    def test_refresh_uses_only_tiantianjiexi_with_a_bounded_deadline(
        self,
        mock_parse,
        mock_invalidate,
    ):
        original = build_tiantianjiexi_item()
        refreshed_url = "https://v5-dy-o.zjcdn.com/path/refreshed.mp4"
        mock_parse.return_value = build_provider_result(url=refreshed_url)

        started_at = time.monotonic()
        refreshed = refresh_tiantianjiexi_download_item(original)

        self.assertEqual(refreshed["url"], refreshed_url)
        self.assertEqual(
            get_tiantianjiexi_manifest_identity(refreshed),
            get_tiantianjiexi_manifest_identity(original),
        )
        mock_invalidate.assert_called_once()
        mock_parse.assert_called_once()
        context = mock_parse.call_args.args[1]
        self.assertTrue(context.prefer_ultra_hd)
        self.assertGreater(context.deadline_monotonic, started_at)

    @patch(
        "xhs_download.services.provider_recovery."
        "invalidate_tiantianjiexi_download_cache"
    )
    @patch(
        "xhs_download.services.provider_recovery."
        "DouyinTianTianJieXiProvider.parse",
        side_effect=XhsProviderTimeoutError("provider timeout"),
    )
    def test_refresh_timeout_has_stable_error_code(
        self,
        mock_parse,
        _mock_invalidate,
    ):
        with self.assertRaises(TianTianJieXiRecoveryError) as raised:
            refresh_tiantianjiexi_download_item(
                build_tiantianjiexi_item()
            )

        self.assertEqual(
            raised.exception.error_code,
            "TIANTIANJIEXI_REFRESH_TIMEOUT",
        )
        mock_parse.assert_called_once()

    @patch(
        "xhs_download.services.provider_recovery."
        "invalidate_tiantianjiexi_download_cache"
    )
    @patch(
        "xhs_download.services.provider_recovery."
        "DouyinTianTianJieXiProvider.parse"
    )
    def test_refresh_rejects_changed_resource_identity(
        self,
        mock_parse,
        _mock_invalidate,
    ):
        mock_parse.return_value = build_provider_result(
            aweme_id="987654321098765432",
        )

        with self.assertRaises(TianTianJieXiRecoveryError) as raised:
            refresh_tiantianjiexi_download_item(
                build_tiantianjiexi_item()
            )

        self.assertEqual(
            raised.exception.error_code,
            "PROXY_FAILED_RECOVERY_BLOCKED",
        )

    @patch(
        "xhs_download.services.provider_recovery."
        "invalidate_tiantianjiexi_download_cache"
    )
    @patch(
        "xhs_download.services.provider_recovery."
        "DouyinTianTianJieXiProvider.parse"
    )
    def test_refresh_rejects_media_outside_douyin_allowlist(
        self,
        mock_parse,
        _mock_invalidate,
    ):
        mock_parse.return_value = build_provider_result(
            url="https://evil.example.com/refreshed.mp4",
        )

        with self.assertRaises(TianTianJieXiRecoveryError) as raised:
            refresh_tiantianjiexi_download_item(
                build_tiantianjiexi_item()
            )

        self.assertEqual(
            raised.exception.error_code,
            "PROXY_FAILED_RECOVERY_BLOCKED",
        )

    @patch("xhs_download.services.downloads.refresh_tiantianjiexi_download_item")
    @patch("xhs_download.services.downloads.fetch_to_temp_path")
    def test_temp_file_download_refreshes_and_retries_at_most_once(
        self,
        mock_fetch,
        mock_refresh,
    ):
        original = build_tiantianjiexi_item()
        refreshed = build_tiantianjiexi_item(
            "https://v5-dy-o.zjcdn.com/path/refreshed.mp4"
        )
        mock_refresh.return_value = refreshed
        mock_fetch.side_effect = [
            XhsDownloadError("HTTP 403"),
            {"path": "/tmp/media", "size": 10},
        ]

        result = fetch_item_to_temp_path(
            original,
            expected_type="video",
        )

        self.assertEqual(result["size"], 10)
        self.assertEqual(mock_fetch.call_count, 2)
        self.assertEqual(
            mock_fetch.call_args_list[1].kwargs["provider_signed"],
            refreshed,
        )
        mock_refresh.assert_called_once_with(original)

    @patch("xhs_download.services.downloads.refresh_tiantianjiexi_download_item")
    @patch("xhs_download.services.downloads.fetch_to_temp_path")
    def test_temp_file_download_does_not_loop_after_retried_failure(
        self,
        mock_fetch,
        mock_refresh,
    ):
        original = build_tiantianjiexi_item()
        mock_refresh.return_value = build_tiantianjiexi_item(
            "https://v5-dy-o.zjcdn.com/path/refreshed.mp4"
        )
        mock_fetch.side_effect = [
            XhsDownloadError("HTTP 403"),
            XhsDownloadError("HTTP 404"),
        ]

        with self.assertRaises(XhsDownloadError):
            fetch_item_to_temp_path(
                original,
                expected_type="video",
            )

        self.assertEqual(mock_fetch.call_count, 2)
        mock_refresh.assert_called_once_with(original)

    @patch("xhs_download.services.downloads.refresh_tiantianjiexi_download_item")
    @patch(
        "xhs_download.services.downloads.fetch_to_temp_path",
        side_effect=XhsDownloadError("资源内容为空"),
    )
    def test_nonrecoverable_download_error_does_not_refresh(
        self,
        mock_fetch,
        mock_refresh,
    ):
        with self.assertRaises(XhsDownloadError):
            fetch_item_to_temp_path(
                build_tiantianjiexi_item(),
                expected_type="video",
            )

        mock_fetch.assert_called_once()
        mock_refresh.assert_not_called()


class DouyinContinuationViewTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client_identity = hashlib.sha256(
            b"127.0.0.1"
        ).hexdigest()[:24]

    @patch("xhs_download.views.parse_media_page")
    @patch(
        "xhs_download.views.consume_fixed_window",
        return_value=LimitResult(allowed=True),
    )
    @patch("xhs_download.views.ultra_hd_is_available", return_value=True)
    def test_initial_ajax_timeout_returns_pending_contract(
        self,
        _mock_available,
        _mock_limit,
        mock_parse,
    ):
        mock_parse.side_effect = DouyinContinuationPendingError("task-id")

        response = Client().get(
            "/tiktok/results/",
            {
                "q": f"https://www.douyin.com/video/{AWEME_ID}",
                "ultra_hd": "1",
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "continuation_pending")
        self.assertTrue(body["continuation_token"])
        self.assertGreater(body["poll_after_ms"], 0)
        self.assertTrue(
            mock_parse.call_args.kwargs["allow_continuation"]
        )

    @patch("xhs_download.views.parse_media_page")
    @patch(
        "xhs_download.views.consume_fixed_window",
        return_value=LimitResult(allowed=True),
    )
    @patch("xhs_download.views.ultra_hd_is_available", return_value=True)
    def test_initial_ajax_pending_renders_available_fallback(
        self,
        _mock_available,
        _mock_limit,
        mock_parse,
    ):
        fallback = build_provider_result(
            provider="kukutool",
            size_bytes=4 * 1024 * 1024,
            quality_label="1080p",
        )
        mock_parse.side_effect = DouyinContinuationPendingError(
            "task-id",
            fallback_result=fallback,
        )

        response = Client().get(
            "/tiktok/results/",
            {
                "q": f"https://www.douyin.com/video/{AWEME_ID}",
                "ultra_hd": "1",
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "continuation_pending")
        self.assertTrue(body["has_results"])
        self.assertIn("asset-card", body["html"])
        self.assertTrue(body["continuation_token"])
        self.assertIn("自动续查", body["message"])

    @patch("xhs_download.views.probe_media_size")
    @patch("xhs_download.views.parse_media_page")
    @patch(
        "xhs_download.views.consume_fixed_window",
        return_value=LimitResult(allowed=True),
    )
    @patch("xhs_download.views.ultra_hd_is_available", return_value=True)
    def test_ultra_result_render_does_not_add_media_size_probe(
        self,
        _mock_available,
        _mock_limit,
        mock_parse,
        mock_probe_size,
    ):
        mock_parse.return_value = build_native_fallback_result()

        response = Client().get(
            "/tiktok/results/",
            {
                "q": f"https://www.douyin.com/video/{AWEME_ID}",
                "ultra_hd": "1",
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 200)
        html = response.json()["html"]
        self.assertIn("大小未知", html)
        self.assertIn("本次先回退本地解析", html)
        self.assertNotIn("本次由 KuKuTool 返回", html)
        mock_probe_size.assert_not_called()

    @patch("xhs_download.views.parse_media_page")
    def test_poll_success_renders_cache_without_provider_call(
        self,
        mock_parse,
    ):
        coordinator = DouyinContinuationCoordinator(1)
        future = Future()
        future.set_result(build_provider_result())
        coordinator.capture_future(
            future,
            provider_name="tiantianjiexi",
            score_result=lambda _result: (1000, 1000),
            is_strong_result=lambda _result: True,
            prepare_result=lambda result: result,
        )
        token = issue_douyin_continuation_token(
            task_id=coordinator.task_id,
            client_identity=self.client_identity,
            platform_key="tiktok",
        )

        response = Client().post(
            "/media/douyin-continuation/",
            data=json.dumps({"continuation_token": token}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "success")
        self.assertIn("asset-card", body["html"])
        mock_parse.assert_not_called()
        coordinator.abandon()

    def test_poll_is_post_only_and_csrf_protected(self):
        self.assertEqual(
            Client().get("/media/douyin-continuation/").status_code,
            405,
        )
        response = Client(enforce_csrf_checks=True).post(
            "/media/douyin-continuation/",
            data=json.dumps({"continuation_token": "invalid"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)
