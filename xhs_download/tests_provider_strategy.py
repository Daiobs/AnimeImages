import itertools
from io import StringIO
from time import monotonic, sleep
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from .models import ProviderHealthTestStatus
from .services.assets import MediaAsset, ParseResult
from .services.common import XhsProviderTimeoutError
from .services.platforms.douyin_providers import (
    DouyinKukuToolProvider,
    DouyinNativeProvider,
    DouyinParseContext,
    DouyinTianTianJieXiProvider,
    DouyinUltraRaceProvider,
    build_douyin_result_providers,
)
from .services.platforms.third_party import (
    GenericThirdPartyAdapter,
    configured_third_party_provider_order,
    get_third_party_rule,
)
from .services.platforms.tiantianjiexi_generic import TianTianJieXiGenericProvider
from .services.provider_settings import ProviderSettings
from .services.provider_manifest import build_provider_manifest_token
from .services.provider_strategy import (
    DOWNLOAD_STRATEGY_BROWSER_BLOB,
    DOWNLOAD_STRATEGY_BROWSER_DIRECT,
    DOWNLOAD_STRATEGY_SERVER_MERGE,
    build_download_strategy,
)
from .services.downloads import provider_manifest_allows_public_download
from .views import (
    PROVIDER_HEALTH_TESTS,
    get_video_size_bytes,
    provider_health_test_by_key,
    run_provider_health_test,
    split_batch_links,
)


class ProviderDownloadStrategyTests(SimpleTestCase):
    def test_tiantianjiexi_video_prefers_browser_blob(self):
        strategy = build_download_strategy(
            MediaAsset(
                media_type="video",
                url="https://v5-default.365yg.com/demo.mp4",
                platform="douyin",
                provider="tiantianjiexi",
            )
        )

        self.assertEqual(strategy["download_strategy"], DOWNLOAD_STRATEGY_BROWSER_BLOB)
        self.assertEqual(strategy["fallback_strategy"], "server_attachment")
        self.assertTrue(strategy["cors_expected"])

    def test_youtube_merge_requires_server_merge(self):
        strategy = build_download_strategy(
            MediaAsset(
                media_type="video",
                url="https://rr1---sn.googlevideo.com/videoplayback",
                platform="third_party",
                provider="youtube_merge",
            )
        )

        self.assertEqual(strategy["download_strategy"], DOWNLOAD_STRATEGY_SERVER_MERGE)
        self.assertTrue(strategy["requires_merge"])

    def test_image_prefers_browser_direct(self):
        strategy = build_download_strategy(
            MediaAsset(
                media_type="image",
                url="https://ci.xiaohongshu.com/demo.jpg",
                platform="xiaohongshu",
            )
        )

        self.assertEqual(strategy["download_strategy"], DOWNLOAD_STRATEGY_BROWSER_DIRECT)

    def test_short_video_rule_allows_kukutool_and_tiantianjiexi(self):
        rule = get_third_party_rule("kuaishou")

        self.assertIsNotNone(rule)
        self.assertEqual(rule.provider_ids[0], "kukutool_generic")
        self.assertIn("tiantianjiexi_generic", rule.provider_ids)
        self.assertIn("kukutool_generic", rule.provider_ids)

    def test_youtube_default_provider_order_prefers_ytdlp(self):
        rule = get_third_party_rule("youtube")

        self.assertIsNotNone(rule)
        self.assertEqual(configured_third_party_provider_order(rule)[0], "yt_dlp_generic")

    def test_short_video_default_provider_order_prefers_kukutool(self):
        for platform_id in ("tiktok", "kuaishou"):
            rule = get_third_party_rule(platform_id)
            self.assertIsNotNone(rule)
            self.assertEqual(configured_third_party_provider_order(rule)[0], "kukutool_generic")

    def test_platform_order_filters_unknown_and_rule_unsupported(self):
        rule = get_third_party_rule("youtube")

        self.assertIsNotNone(rule)
        with override_settings(
            THIRD_PARTY_PROVIDER_ORDER_YOUTUBE=(
                "unknown_provider",
                "gallery_dl_generic",
                "tiantianjiexi_generic",
                "kukutool_generic",
                "yt_dlp_generic",
            )
        ):
            self.assertEqual(
                configured_third_party_provider_order(rule),
                ("tiantianjiexi_generic", "kukutool_generic", "yt_dlp_generic"),
            )

    @override_settings(
        THIRD_PARTY_PROVIDER_ORDER_SHORT_VIDEO=(
            "yt_dlp_generic",
            "kukutool_generic",
            "tiantianjiexi_generic",
            "gallery_dl_generic",
        )
    )
    def test_adapter_iter_provider_specs_uses_configured_order(self):
        rule = get_third_party_rule("kuaishou")
        adapter = GenericThirdPartyAdapter()

        provider_ids = [
            provider_id
            for provider_id, _provider, _normalizer in adapter.iter_provider_specs(
                rule,
                respect_enabled=False,
            )
        ]

        self.assertEqual(
            provider_ids,
            ["yt_dlp_generic", "kukutool_generic", "tiantianjiexi_generic", "gallery_dl_generic"],
        )

    @override_settings(
        THIRD_PARTY_PROVIDER_ORDER_YOUTUBE=("kukutool_generic", "yt_dlp_generic"),
    )
    def test_youtube_provider_order_can_be_overridden(self):
        rule = get_third_party_rule("youtube")

        self.assertEqual(
            configured_third_party_provider_order(rule),
            ("kukutool_generic", "yt_dlp_generic"),
        )

    def test_split_batch_links_dedupes_and_limits(self):
        links = split_batch_links("\n".join(["https://example.com/a", "https://example.com/a"] + [
            f"https://example.com/{index}" for index in range(30)
        ]))

        self.assertEqual(len(links), 20)
        self.assertEqual(links[0], "https://example.com/a")


@override_settings(
    BATCH_PARSE_MINUTE_IP_LIMIT=0,
    BATCH_PARSE_GLOBAL_MINUTE_LIMIT=0,
    BATCH_PARSE_DAILY_IP_LIMIT=0,
)
class BatchParseViewTests(TestCase):
    def setUp(self):
        self.staff_user = get_user_model().objects.create_user(
            username="staff",
            password="password",
            is_staff=True,
        )

    def test_batch_parse_page_renders_strategy_matrix(self):
        self.client.force_login(self.staff_user)
        response = self.client.get(reverse("batch_parse"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "解析设置")
        self.assertContains(response, "batch-settings-panel")
        self.assertContains(response, "高级说明：下载策略矩阵")
        self.assertContains(response, "批量解析")
        self.assertContains(response, "仅管理员可用")
        self.assertNotContains(response, "batch-option-row")
        content = response.content.decode()
        self.assertGreater(content.index("高级说明：下载策略矩阵"), content.index("batch-form"))
        self.assertGreater(content.index('name="ultra_hd"'), content.index("batch-settings-panel"))
        self.assertGreater(content.index('name="youtube_merge"'), content.index("batch-settings-panel"))

    def test_batch_parse_non_staff_default_not_accessible(self):
        response = self.client.get(reverse("batch_parse"))

        self.assertEqual(response.status_code, 404)

    @override_settings(BATCH_PARSE_PUBLIC_ENABLED=True, BATCH_PARSE_PUBLIC_MAX_LINKS=3)
    def test_batch_parse_public_mode_truncates_to_public_max(self):
        response = self.client.post(
            reverse("batch_parse"),
            {"links": "\n".join(f"https://example.com/{index}" for index in range(8))},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "总链接 3")

    @patch("xhs_download.views.parse_media_page")
    def test_batch_parse_post_renders_successful_result(self, mock_parse):
        self.client.force_login(self.staff_user)
        mock_parse.return_value = ParseResult(
            platform="douyin",
            post_type="video",
            base_filename="demo_video",
            assets=(
                MediaAsset(
                    media_type="video",
                    url="https://v5-default.365yg.com/demo.mp4",
                    platform="douyin",
                    provider="tiantianjiexi",
                    size_bytes=123456,
                    quality_label="原画",
                    cover_url="https://p3.douyinpic.com/cover.jpeg",
                ),
            ),
            metadata={"selected_provider": "tiantianjiexi"},
        )

        response = self.client.post(
            reverse("batch_parse"),
            {"links": "https://www.douyin.com/video/7611146072161047526"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "demo_video")
        self.assertContains(response, "Provider tiantianjiexi")
        self.assertContains(response, "120.6 KB")
        self.assertContains(response, "batch-result-cover")
        self.assertContains(response, "复制直链")
        self.assertContains(response, "打开直链")
        self.assertContains(response, "浏览器下载")
        self.assertContains(response, "浏览器 Blob 优先")
        content = response.content.decode()
        self.assertGreater(content.index("高级说明：下载策略矩阵"), content.index("解析结果"))
        mock_parse.assert_called_once()
        call_kwargs = mock_parse.call_args.kwargs
        self.assertLessEqual(call_kwargs["timeout_seconds"], 8.0)
        self.assertGreater(call_kwargs["timeout_seconds"], 0)
        self.assertIn("deadline_monotonic", call_kwargs)

    @override_settings(BATCH_PARSE_PER_LINK_TIMEOUT_SECONDS=2)
    @patch("xhs_download.views.parse_media_page")
    def test_batch_parse_passes_per_link_timeout(self, mock_parse):
        self.client.force_login(self.staff_user)
        mock_parse.side_effect = RuntimeError("slow provider")

        response = self.client.post(
            reverse("batch_parse"),
            {"links": "https://www.douyin.com/video/7611146072161047526"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "RuntimeError")
        self.assertLessEqual(mock_parse.call_args.kwargs["timeout_seconds"], 2.0)

    @patch("xhs_download.views.parse_media_page")
    def test_batch_parse_single_failure_does_not_block_other_links(self, mock_parse):
        self.client.force_login(self.staff_user)

        def parse_side_effect(url, **_kwargs):
            if "bad" in url:
                raise RuntimeError("boom")
            return ParseResult(
                platform="douyin",
                post_type="video",
                base_filename="ok_video",
                assets=(
                    MediaAsset(
                        media_type="video",
                        url="https://v5-default.365yg.com/demo.mp4",
                        platform="douyin",
                        provider="tiantianjiexi",
                    ),
                ),
            )

        mock_parse.side_effect = parse_side_effect

        response = self.client.post(
            reverse("batch_parse"),
            {"links": "https://example.com/bad\nhttps://example.com/ok"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "ok_video")
        self.assertContains(response, "RuntimeError")
        self.assertContains(response, "复制错误")
        self.assertContains(response, "复制原链接")
        self.assertContains(response, "重新解析此链接")
        self.assertContains(response, "成功 1")
        self.assertContains(response, "失败 1")

    @override_settings(BATCH_PARSE_TOTAL_TIMEOUT_SECONDS=1)
    @patch("xhs_download.views.time.monotonic")
    @patch("xhs_download.views.build_batch_result_row")
    def test_batch_parse_total_timeout_marks_remaining_skipped(self, mock_row, mock_monotonic):
        self.client.force_login(self.staff_user)
        mock_monotonic.side_effect = itertools.chain(
            [100, 100, 102],
            itertools.repeat(102),
        )
        mock_row.return_value = {
            "status": "success",
            "source_text": "https://example.com/1",
            "base_filename": "first_video",
            "duration_ms": 0,
            "video_items": (),
            "image_items": (),
        }

        response = self.client.post(
            reverse("batch_parse"),
            {"links": "https://example.com/1\nhttps://example.com/2"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "first_video")
        self.assertContains(response, "批量解析总预算已用尽")

    @override_settings(BATCH_PARSE_PUBLIC_ENABLED=True)
    @patch("xhs_download.views.parse_media_page")
    def test_batch_public_mode_does_not_render_raw_video_url(self, mock_parse):
        mock_parse.return_value = ParseResult(
            platform="douyin",
            post_type="video",
            base_filename="public_video",
            assets=(
                MediaAsset(
                    media_type="video",
                    url="https://v5-default.365yg.com/private-demo.mp4",
                    platform="douyin",
                    provider="tiantianjiexi",
                ),
            ),
        )

        response = self.client.post(
            reverse("batch_parse"),
            {"links": "https://www.douyin.com/video/7611146072161047526"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "public_video")
        self.assertNotContains(response, 'href="https://v5-default.365yg.com/private-demo.mp4"')
        self.assertNotContains(response, "复制全部成功视频直链")
        self.assertNotContains(response, "data-batch-direct-url")

    @override_settings(BATCH_PARSE_PUBLIC_ENABLED=True)
    @patch("xhs_download.views.parse_media_page")
    def test_batch_public_mode_sanitizes_error_and_hides_strategy_matrix(self, mock_parse):
        mock_parse.side_effect = RuntimeError(
            "failed https://v5-default.365yg.com/a.mp4?token=secret&signature=bad"
        )

        response = self.client.post(
            reverse("batch_parse"),
            {"links": "https://www.douyin.com/video/7611146072161047526"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "[url-redacted]")
        self.assertNotContains(response, "token=secret")
        self.assertNotContains(response, "signature=bad")
        self.assertNotContains(response, "高级说明：下载策略矩阵")


class TianTianJieXiHealthHardeningTests(TestCase):
    def build_request(self):
        request = RequestFactory().post("/admin/provider-health-tests/run/")
        request.user = get_user_model().objects.create_superuser(
            username="admin",
            password="password",
        )
        request.request_id = "rid-health"
        return request

    def test_account_health_test_does_not_parse_video(self):
        request = self.build_request()
        item = provider_health_test_by_key("tiantianjiexi_account_status")

        with override_settings(
            TIANTIANJIEXI_PROVIDER_ENABLED=True,
            TIANTIANJIEXI_PHONE="phone",
            TIANTIANJIEXI_PASSWORD="password",
        ):
            with patch.object(
                TianTianJieXiGenericProvider,
                "query_account_status",
                return_value={"available": True, "cached": True},
            ) as mock_status, patch.object(
                TianTianJieXiGenericProvider,
                "parse",
            ) as mock_parse:
                run = run_provider_health_test(request, item)

        self.assertEqual(run.status, ProviderHealthTestStatus.SUCCESS)
        mock_status.assert_called_once()
        mock_parse.assert_not_called()

    def test_health_query_failure_does_not_raise_or_leak_credentials(self):
        class FailingClient:
            def query_account_status(self):
                raise RuntimeError("https://example.com/?token=secret")

        with override_settings(
            TIANTIANJIEXI_PROVIDER_ENABLED=True,
            TIANTIANJIEXI_PHONE="phone-secret",
            TIANTIANJIEXI_PASSWORD="password-secret",
            TIANTIANJIEXI_HEALTH_QUERY_ACCOUNT_STATUS_ENABLED=True,
        ):
            health = TianTianJieXiGenericProvider(client=FailingClient()).health()

        self.assertFalse(health["account_status_available"])
        self.assertNotIn("phone-secret", str(health))
        self.assertNotIn("password-secret", str(health))

    @override_settings(
        TIANTIANJIEXI_PROVIDER_ENABLED=True,
        TIANTIANJIEXI_PHONE="phone",
        TIANTIANJIEXI_PASSWORD="password",
        TIANTIANJIEXI_HEALTH_QUERY_ACCOUNT_STATUS_ENABLED=False,
    )
    def test_health_query_can_be_disabled(self):
        class ExplodingClient:
            def query_account_status(self):
                raise AssertionError("should not query account")

        health = TianTianJieXiGenericProvider(client=ExplodingClient()).health()

        self.assertFalse(health["account_status_available"])

    def test_tiantianjiexi_generic_signed_provider_manifest_allowed(self):
        item = {
            "asset_id": "third_party:kuaishou:abc123:video:0",
            "provider": "tiantianjiexi_generic",
            "provider_resource_id": "abc123",
            "provider_cache_key": "third_party:tiantianjiexi_generic:abc123",
            "type": "video",
            "url": "https://tx2.a.kwimgs.com/demo.mp4",
        }
        item["provider_manifest_token"] = build_provider_manifest_token(
            asset_id=item["asset_id"],
            provider=item["provider"],
            provider_resource_id=item["provider_resource_id"],
            provider_cache_key=item["provider_cache_key"],
            url=item["url"],
            media_type=item["type"],
        )

        self.assertTrue(provider_manifest_allows_public_download(item))

    @override_settings(
        ULTRA_HD_PROVIDER_ENABLED=True,
        THIRD_PARTY_PROVIDER_ENABLED=True,
        TIANTIANJIEXI_PROVIDER_ENABLED=True,
    )
    @patch("xhs_download.views.DouyinTianTianJieXiProvider.parse")
    def test_douyin_tiantianjiexi_health_validates_final_asset_manifest(self, mock_parse):
        request = self.build_request()
        from .models import ProviderHealthTestSetting

        ProviderHealthTestSetting.objects.create(
            singleton_key=1,
            douyin_url="https://www.douyin.com/user/self?modal_id=7611146072161047526",
        )
        asset = MediaAsset(
            media_type="video",
            url="https://v5-default.365yg.com/demo.mp4",
            platform="douyin",
            provider="tiantianjiexi",
            asset_id="douyin:7611146072161047526:video:tiantianjiexi",
            provider_resource_id="7611146072161047526",
            provider_cache_key="douyin:tiantianjiexi:validated:7611146072161047526",
        )
        asset = MediaAsset(
            **{
                **asset.__dict__,
                "provider_manifest_token": build_provider_manifest_token(
                    asset_id=asset.asset_id,
                    provider=asset.provider,
                    provider_resource_id=asset.provider_resource_id,
                    provider_cache_key=asset.provider_cache_key,
                    url=asset.url,
                    media_type=asset.media_type,
                ),
            }
        )
        mock_parse.return_value = ParseResult(
            platform="douyin",
            post_type="video",
            base_filename="demo",
            assets=(asset,),
            metadata={"selected_provider": "tiantianjiexi", "source_provider": "tiantianjiexi"},
        )

        run = run_provider_health_test(
            request,
            provider_health_test_by_key("douyin_tiantianjiexi_ultra"),
        )

        self.assertEqual(run.status, ProviderHealthTestStatus.SUCCESS)

    @override_settings(
        ULTRA_HD_PROVIDER_ENABLED=True,
        THIRD_PARTY_PROVIDER_ENABLED=True,
        TIANTIANJIEXI_PROVIDER_ENABLED=True,
    )
    @patch("xhs_download.views.DouyinTianTianJieXiProvider.parse")
    def test_douyin_tiantianjiexi_health_rejects_missing_manifest(self, mock_parse):
        request = self.build_request()
        from .models import ProviderHealthTestSetting

        ProviderHealthTestSetting.objects.create(
            singleton_key=1,
            douyin_url="https://www.douyin.com/user/self?modal_id=7611146072161047526",
        )
        mock_parse.return_value = ParseResult(
            platform="douyin",
            post_type="video",
            base_filename="demo",
            assets=(
                MediaAsset(
                    media_type="video",
                    url="https://v5-default.365yg.com/demo.mp4",
                    platform="douyin",
                    provider="tiantianjiexi",
                ),
            ),
            metadata={"selected_provider": "tiantianjiexi"},
        )

        run = run_provider_health_test(
            request,
            provider_health_test_by_key("douyin_tiantianjiexi_ultra"),
        )

        self.assertEqual(run.status, ProviderHealthTestStatus.UNAVAILABLE)

    @override_settings(
        THIRD_PARTY_PROVIDER_ENABLED=True,
        TIANTIANJIEXI_PROVIDER_ENABLED=True,
    )
    @patch.object(TianTianJieXiGenericProvider, "parse")
    def test_third_party_tiantianjiexi_health_rejects_invalid_normalized_url(self, mock_parse):
        request = self.build_request()
        from .models import ProviderHealthTestSetting

        ProviderHealthTestSetting.objects.create(
            singleton_key=1,
            third_party_url="https://www.kuaishou.com/short-video/demo",
        )
        mock_parse.return_value = {
            "title": "bad",
            "videos": [{"url": "https://evil.example.com/demo.mp4", "quality": "原画"}],
        }

        run = run_provider_health_test(
            request,
            provider_health_test_by_key("third_party_tiantianjiexi"),
        )

        self.assertEqual(run.status, ProviderHealthTestStatus.FAILED)

    @override_settings(
        THIRD_PARTY_PROVIDER_ENABLED=True,
        TIANTIANJIEXI_PROVIDER_ENABLED=True,
    )
    @patch.object(TianTianJieXiGenericProvider, "parse")
    def test_third_party_tiantianjiexi_health_validates_manifest(self, mock_parse):
        request = self.build_request()
        from .models import ProviderHealthTestSetting

        ProviderHealthTestSetting.objects.create(
            singleton_key=1,
            third_party_url="https://www.kuaishou.com/short-video/demo",
        )
        mock_parse.return_value = {
            "title": "ok",
            "videos": [{"url": "https://tx2.a.kwimgs.com/demo.mp4", "quality": "原画"}],
        }

        run = run_provider_health_test(
            request,
            provider_health_test_by_key("third_party_tiantianjiexi"),
        )

        self.assertEqual(run.status, ProviderHealthTestStatus.SUCCESS)

    @patch("xhs_download.views.probe_media_size")
    def test_signed_tiantianjiexi_generic_video_size_does_not_probe(self, mock_probe):
        size = get_video_size_bytes(
            MediaAsset(
                media_type="video",
                platform="third_party",
                url="https://tx2.a.kwimgs.com/demo.mp4",
                provider="tiantianjiexi_generic",
            )
        )

        self.assertEqual(size, 0)
        mock_probe.assert_not_called()


class DouyinUltraRaceSettingsTests(SimpleTestCase):
    @override_settings(
        DOUYIN_ULTRA_RACE_ENABLED=True,
        THIRD_PARTY_PROVIDER_ENABLED=True,
        TIANTIANJIEXI_PROVIDER_ENABLED=True,
        TIANTIANJIEXI_PHONE="phone",
        TIANTIANJIEXI_PASSWORD="password",
        KUKUTOOL_PROVIDER_ENABLED=True,
    )
    def test_race_enabled_with_both_upstreams_uses_ultra_race(self):
        providers = build_douyin_result_providers(
            ProviderSettings(
                ultra_hd_enabled=True,
                provider_order=("tiantianjiexi", "kukutool", "native"),
                kukutool_enabled=True,
            )
        )

        self.assertIsInstance(providers[0], DouyinUltraRaceProvider)
        self.assertIsInstance(providers[-1], DouyinNativeProvider)
        race_provider = providers[0]
        tiantianjiexi_provider = next(
            provider for provider in race_provider.providers if provider.name == "tiantianjiexi"
        )
        kukutool_provider = next(
            provider for provider in race_provider.providers if provider.name == "kukutool"
        )
        self.assertFalse(tiantianjiexi_provider.reserve_fallback_grace)
        self.assertTrue(tiantianjiexi_provider.suppress_selected_log)
        self.assertTrue(kukutool_provider.suppress_selected_log)

    @override_settings(
        DOUYIN_ULTRA_RACE_ENABLED=False,
        THIRD_PARTY_PROVIDER_ENABLED=True,
        TIANTIANJIEXI_PROVIDER_ENABLED=True,
        TIANTIANJIEXI_PHONE="phone",
        TIANTIANJIEXI_PASSWORD="password",
        KUKUTOOL_PROVIDER_ENABLED=True,
    )
    def test_race_disabled_respects_provider_order(self):
        providers = build_douyin_result_providers(
            ProviderSettings(
                ultra_hd_enabled=True,
                provider_order=("kukutool", "tiantianjiexi", "native"),
                kukutool_enabled=True,
            )
        )

        self.assertIsInstance(providers[0], DouyinKukuToolProvider)
        self.assertIsInstance(providers[1], DouyinTianTianJieXiProvider)
        self.assertTrue(providers[1].reserve_fallback_grace)
        self.assertFalse(providers[1].suppress_selected_log)
        self.assertIsInstance(providers[-1], DouyinNativeProvider)

    @override_settings(
        DOUYIN_ULTRA_RACE_ENABLED=True,
        THIRD_PARTY_PROVIDER_ENABLED=True,
        TIANTIANJIEXI_PROVIDER_ENABLED=False,
        TIANTIANJIEXI_PHONE="",
        TIANTIANJIEXI_PASSWORD="",
        KUKUTOOL_PROVIDER_ENABLED=True,
    )
    def test_unconfigured_tiantianjiexi_is_not_created(self):
        providers = build_douyin_result_providers(
            ProviderSettings(
                ultra_hd_enabled=True,
                provider_order=("tiantianjiexi", "kukutool", "native"),
                kukutool_enabled=True,
            )
        )

        self.assertFalse(any(isinstance(provider, DouyinTianTianJieXiProvider) for provider in providers))
        self.assertTrue(any(isinstance(provider, DouyinKukuToolProvider) for provider in providers))
        self.assertIsInstance(providers[-1], DouyinNativeProvider)


@override_settings(
    THIRD_PARTY_PROVIDER_ENABLED=True,
    TIANTIANJIEXI_PROVIDER_ENABLED=True,
    TIANTIANJIEXI_PHONE="phone",
    TIANTIANJIEXI_PASSWORD="password",
)
class DouyinUltraRaceBudgetTests(SimpleTestCase):
    class Adapter:
        platform_id = "douyin"
        headers = {}
        media_host_suffixes = ("365yg.com", "douyinvod.com")

    class CapturingOfficialProvider:
        def __init__(self):
            self.timeout_seconds = None

        def parse(self, _url, timeout_seconds=None):
            self.timeout_seconds = timeout_seconds
            return {
                "title": "天天解析预算测试",
                "videos": [
                    {
                        "url": "https://v11-default.365yg.com/video/tos/demo.mp4",
                        "quality": "原画",
                        "size": 80 * 1024 * 1024,
                    }
                ],
            }

    class FakeRaceProvider:
        def __init__(
            self,
            name,
            *,
            delay_seconds,
            size_bytes=80 * 1024 * 1024,
            quality_label="原画",
            raises=None,
        ):
            self.name = name
            self.delay_seconds = delay_seconds
            self.size_bytes = size_bytes
            self.quality_label = quality_label
            self.raises = raises

        def parse(self, _adapter, _context):
            sleep(self.delay_seconds)
            if self.raises:
                raise self.raises
            return ParseResult(
                platform="douyin",
                post_type="video",
                base_filename=f"{self.name}_demo",
                assets=(
                    MediaAsset(
                        media_type="video",
                        url=f"https://v11-default.365yg.com/video/{self.name}.mp4",
                        platform="douyin",
                        provider=self.name,
                        quality_label=self.quality_label,
                        size_bytes=self.size_bytes,
                    ),
                ),
                metadata={
                    "selected_provider": self.name,
                    "source_provider": self.name,
                    f"{self.name}_candidate_count": 1,
                },
            )

    def provider_settings(self):
        return ProviderSettings(
            ultra_hd_enabled=True,
            provider_order=("tiantianjiexi", "kukutool", "native"),
            kukutool_enabled=True,
            ultra_total_timeout_seconds=5.0,
            ultra_kukutool_grace_seconds=1.0,
            kukutool_sidecar_timeout_seconds=3.0,
        )

    def context(self, budget_seconds=5.0):
        return DouyinParseContext(
            url="https://www.douyin.com/video/7611146072161047526?token=secret",
            final_url="https://www.douyin.com/video/7611146072161047526?token=secret",
            aweme_id="7611146072161047526",
            deadline_monotonic=monotonic() + budget_seconds,
        )

    @override_settings(
        TIANTIANJIEXI_PROVIDER_ENABLED=True,
        TIANTIANJIEXI_PHONE="phone",
        TIANTIANJIEXI_PASSWORD="password",
        THIRD_PARTY_PROVIDER_ENABLED=True,
    )
    def test_race_tiantianjiexi_does_not_reserve_kukutool_grace(self):
        official_provider = self.CapturingOfficialProvider()
        provider = DouyinTianTianJieXiProvider(
            provider_settings=self.provider_settings(),
            provider=official_provider,
            reserve_fallback_grace=False,
        )

        provider.parse(self.Adapter(), self.context())

        self.assertGreaterEqual(official_provider.timeout_seconds, 4.5)

    @override_settings(
        TIANTIANJIEXI_PROVIDER_ENABLED=True,
        TIANTIANJIEXI_PHONE="phone",
        TIANTIANJIEXI_PASSWORD="password",
        THIRD_PARTY_PROVIDER_ENABLED=True,
    )
    def test_sequential_tiantianjiexi_reserves_kukutool_grace(self):
        official_provider = self.CapturingOfficialProvider()
        provider = DouyinTianTianJieXiProvider(
            provider_settings=self.provider_settings(),
            provider=official_provider,
            reserve_fallback_grace=True,
        )

        provider.parse(self.Adapter(), self.context())

        self.assertLessEqual(official_provider.timeout_seconds, 2.2)

    def test_race_tiantianjiexi_wins_when_it_returns_first(self):
        provider = DouyinUltraRaceProvider(
            provider_settings=self.provider_settings(),
            providers=(
                self.FakeRaceProvider("tiantianjiexi", delay_seconds=0.04),
                self.FakeRaceProvider("kukutool", delay_seconds=0.08),
            ),
        )

        result = provider.parse(self.Adapter(), self.context())

        self.assertEqual(result.metadata["ultra_race_winner"], "tiantianjiexi")
        self.assertEqual(result.metadata["selected_provider"], "tiantianjiexi")
        self.assertEqual(result.metadata["source_provider"], "tiantianjiexi")

    def test_race_kukutool_wins_when_it_returns_strong_candidate_first(self):
        provider = DouyinUltraRaceProvider(
            provider_settings=self.provider_settings(),
            providers=(
                self.FakeRaceProvider("kukutool", delay_seconds=0.04),
                self.FakeRaceProvider("tiantianjiexi", delay_seconds=0.08),
            ),
        )

        result = provider.parse(self.Adapter(), self.context())

        self.assertEqual(result.metadata["ultra_race_winner"], "kukutool")
        self.assertEqual(result.metadata["selected_provider"], "kukutool")
        self.assertEqual(result.metadata["source_provider"], "kukutool")

    def test_race_falls_back_to_kukutool_when_tiantianjiexi_times_out(self):
        provider = DouyinUltraRaceProvider(
            provider_settings=self.provider_settings(),
            providers=(
                self.FakeRaceProvider(
                    "tiantianjiexi",
                    delay_seconds=0.02,
                    raises=XhsProviderTimeoutError(
                        "timeout https://example.com/video.mp4?token=secret&phone=secret"
                    ),
                ),
                self.FakeRaceProvider("kukutool", delay_seconds=0.04),
            ),
        )

        with self.assertLogs(
            "xhs_download.services.platforms.douyin_providers",
            level="INFO",
        ) as logs:
            result = provider.parse(self.Adapter(), self.context())

        output = "\n".join(logs.output)
        self.assertEqual(result.metadata["ultra_race_winner"], "kukutool")
        self.assertNotIn("https://example.com/video.mp4", output)
        self.assertNotIn("token=secret", output)
        self.assertNotIn("phone=secret", output)

    def test_race_loser_does_not_override_winner_metadata(self):
        provider = DouyinUltraRaceProvider(
            provider_settings=self.provider_settings(),
            providers=(
                self.FakeRaceProvider("tiantianjiexi", delay_seconds=0.04),
                self.FakeRaceProvider("kukutool", delay_seconds=0.08),
            ),
        )

        result = provider.parse(self.Adapter(), self.context())

        self.assertEqual(result.metadata["ultra_race_winner"], "tiantianjiexi")
        self.assertEqual(result.metadata["selected_provider"], "tiantianjiexi")
        self.assertEqual(result.video_assets[0].provider, "tiantianjiexi")


class TianTianJieXiSmokeCommandTests(TestCase):
    @patch.object(TianTianJieXiGenericProvider, "parse")
    def test_smoke_api_mode_hides_full_url_and_credentials(self, mock_parse):
        mock_parse.return_value = {
            "videos": [{"url": "https://tx2.a.kwimgs.com/path/video.mp4?token=secret", "quality": "原画"}],
            "images": [],
        }
        out = StringIO()

        with override_settings(
            TIANTIANJIEXI_PROVIDER_ENABLED=True,
            TIANTIANJIEXI_PHONE="phone-secret",
            TIANTIANJIEXI_PASSWORD="password-secret",
        ):
            call_command(
                "smoke_tiantianjiexi_provider",
                "--url",
                "https://www.kuaishou.com/short-video/demo",
                "--mode",
                "api",
                stdout=out,
            )

        output = out.getvalue()
        self.assertIn('"selected_video_host": "tx2.a.kwimgs.com"', output)
        self.assertNotIn("phone-secret", output)
        self.assertNotIn("password-secret", output)
        self.assertNotIn("path/video.mp4", output)
        self.assertNotIn("token=secret", output)

    @patch.object(TianTianJieXiGenericProvider, "parse")
    def test_smoke_parse_result_mode_generates_parse_result(self, mock_parse):
        mock_parse.return_value = {
            "title": "ok",
            "videos": [{"url": "https://tx2.a.kwimgs.com/demo.mp4", "quality": "原画"}],
        }
        out = StringIO()

        call_command(
            "smoke_tiantianjiexi_provider",
            "--url",
            "https://www.kuaishou.com/short-video/demo",
            "--mode",
            "parse-result",
            stdout=out,
        )

        output = out.getvalue()
        self.assertIn('"post_type": "video"', output)
        self.assertIn('"first_asset_provider": "tiantianjiexi_generic"', output)
        self.assertIn('"provider_manifest_token_present": true', output)

    @patch("xhs_download.management.commands.smoke_tiantianjiexi_provider.parse_media_page")
    def test_smoke_douyin_ultra_mode_outputs_selected_provider(self, mock_parse):
        mock_parse.return_value = ParseResult(
            platform="douyin",
            post_type="video",
            base_filename="demo",
            assets=(
                MediaAsset(
                    media_type="video",
                    url="https://v5-default.365yg.com/demo.mp4",
                    platform="douyin",
                    provider="tiantianjiexi",
                    quality_label="原画",
                ),
            ),
            metadata={"selected_provider": "tiantianjiexi", "ultra_race_winner": "tiantianjiexi"},
        )
        out = StringIO()

        call_command(
            "smoke_tiantianjiexi_provider",
            "--url",
            "https://www.douyin.com/user/self?modal_id=7611146072161047526",
            "--mode",
            "douyin-ultra",
            stdout=out,
        )

        output = out.getvalue()
        self.assertIn('"selected_provider": "tiantianjiexi"', output)
        self.assertIn('"ultra_race_winner": "tiantianjiexi"', output)
