import hashlib
import json
import time
from concurrent.futures import Future
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests
from django.core.cache import cache
from django.http import HttpResponse
from django.test import Client, SimpleTestCase, TestCase

from .services.assets import MediaAsset, ParseResult
from .services.common import XhsDownloadError, XhsProviderTimeoutError
from .services.download_handoff import (
    DownloadHandoffError,
    issue_download_handoff,
    load_download_handoff,
)
from .services.downloads import fetch_item_to_temp_path, observable_download_error
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
from .services.platforms.douyin_providers import (
    DouyinParseContext,
    DouyinUltraRaceProvider,
)
from .services.provider_manifest import build_provider_manifest_token
from .services.provider_recovery import (
    TianTianJieXiRecoveryError,
    get_tiantianjiexi_manifest_identity,
    refresh_tiantianjiexi_download_item,
)
from .services.rate_limits import LimitResult
from .views import inspect_single_attachment_download, open_single_attachment_upstream


AWEME_ID = "123456789012345678"
MEDIA_URL = "https://v3-dy-o.zjcdn.com/path/video.mp4"


def build_provider_result(
    provider="tiantianjiexi",
    *,
    url=MEDIA_URL,
    size_bytes=80 * 1024 * 1024,
    aweme_id=AWEME_ID,
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
                quality_label="超高清",
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


class SlowRaceProvider:
    name = "tiantianjiexi"

    def __init__(self, *, result=None, error=None, delay_seconds=0.06):
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
        token = issue_download_handoff(
            item={"type": "image", "url": raw_url},
            base_filename="demo",
            client_identity="client-a",
        )

        self.assertNotIn(raw_url, token)
        loaded = load_download_handoff(
            token,
            client_identity="client-a",
        )
        self.assertEqual(loaded.item["url"], raw_url)
        with self.assertRaises(DownloadHandoffError) as reused:
            load_download_handoff(
                token,
                client_identity="client-a",
            )
        self.assertEqual(reused.exception.error_code, "DOWNLOAD_HANDOFF_USE_LIMIT")
        with self.assertRaises(DownloadHandoffError) as mismatched:
            load_download_handoff(
                token,
                client_identity="client-b",
                consume=False,
            )
        self.assertEqual(
            mismatched.exception.error_code,
            "DOWNLOAD_HANDOFF_CLIENT_MISMATCH",
        )

    def test_handoff_tamper_and_expiry_are_rejected(self):
        token = issue_download_handoff(
            item={"type": "image", "url": "https://ci.xiaohongshu.com/demo.jpg"},
            base_filename="demo",
            client_identity="client-a",
        )
        with self.assertRaises(DownloadHandoffError) as tampered:
            load_download_handoff(
                f"{token}tampered",
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
                    token,
                    client_identity="client-a",
                )
        self.assertEqual(expired.exception.error_code, "DOWNLOAD_HANDOFF_EXPIRED")


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

    @patch("xhs_download.views.release_concurrency")
    @patch(
        "xhs_download.views.record_download_archive_bytes",
        return_value=(True, ""),
    )
    @patch("xhs_download.views.build_single_attachment_response")
    def test_prepare_returns_observable_same_origin_stream(
        self,
        mock_build_response,
        _mock_record_bytes,
        _mock_release,
    ):
        mock_build_response.return_value = (HttpResponse(b"image"), 5)
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
            patch(
                "xhs_download.views.inspect_single_attachment_download",
                return_value={
                    "item": self.payload["media_items"][0],
                    "content_type": "image/jpeg",
                    "content_length": 5,
                    "filename": "demo.jpg",
                    "recovered": False,
                },
            ),
        ):
            response = self.client.post(
                "/media/download-prepare/",
                data=json.dumps(self.payload),
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertTrue(body["download_url"].startswith("/media/download-stream/"))
        self.assertNotIn(self.payload["media_items"][0]["url"], body["download_url"])
        self.assertNotIn(
            self.payload["media_items"][0]["url"],
            response.content.decode(),
        )
        stream_response = self.client.get(body["download_url"])
        self.assertEqual(stream_response.status_code, 200)
        self.assertEqual(stream_response.content, b"image")
        second_stream = self.client.get(body["download_url"])
        self.assertEqual(second_stream.status_code, 409)

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

    @patch(
        "xhs_download.views.inspect_single_attachment_download",
        side_effect=requests.exceptions.SSLError("signed-url-secret"),
    )
    @patch(
        "xhs_download.views.decide_download_route",
        return_value={
            "route_type": "proxy",
            "download_strategy": "server_single_attachment",
        },
    )
    @patch(
        "xhs_download.views.acquire_concurrency",
        return_value=LimitResult(allowed=True),
    )
    @patch(
        "xhs_download.views.check_download_request_limits",
        return_value=("identity", None),
    )
    def test_prepare_failure_has_stable_safe_error(
        self,
        _mock_limits,
        _mock_acquire,
        _mock_route,
        _mock_inspect,
    ):
        response = self.client.post(
            "/media/download-prepare/",
            data=json.dumps(self.payload),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["error_code"], "UPSTREAM_SSL_ERROR")
        self.assertNotIn("signed-url-secret", response.content.decode())

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
    def test_prepare_probe_reads_only_the_first_nonempty_chunk(self, mock_open):
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
    def test_recoverable_failure_refreshes_and_retries_once(
        self,
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

        response, effective_item, recovered = open_single_attachment_upstream(
            original,
            "video",
            5,
            include_context=True,
        )

        self.assertIs(response, upstream)
        self.assertEqual(effective_item["url"], refreshed["url"])
        self.assertTrue(recovered)
        self.assertEqual(mock_fetch.call_count, 2)
        mock_refresh.assert_called_once_with(original)

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
