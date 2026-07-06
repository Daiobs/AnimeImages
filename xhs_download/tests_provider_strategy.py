import itertools
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from .models import ProviderHealthTestStatus
from .services.assets import MediaAsset, ParseResult
from .services.platforms.third_party import (
    GenericThirdPartyAdapter,
    configured_third_party_provider_order,
    get_third_party_rule,
)
from .services.platforms.tiantianjiexi_generic import TianTianJieXiGenericProvider
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

    def test_configured_provider_order_filters_unknown_and_rule_unsupported(self):
        rule = get_third_party_rule("youtube")

        self.assertIsNotNone(rule)
        with override_settings(
            THIRD_PARTY_PROVIDER_ORDER=(
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
        THIRD_PARTY_PROVIDER_ORDER=(
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
        self.assertContains(response, "下载策略矩阵")
        self.assertContains(response, "批量解析")
        self.assertContains(response, "仅管理员可用")

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
        self.assertContains(response, "本次提交 3 条链接")

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
        self.assertContains(response, "浏览器 Blob 优先")
        mock_parse.assert_called_once()

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


class TianTianJieXiHealthHardeningTests(TestCase):
    def test_account_health_test_does_not_parse_video(self):
        request = RequestFactory().post("/admin/provider-health-tests/run/")
        request.user = get_user_model().objects.create_superuser(
            username="admin",
            password="password",
        )
        request.request_id = "rid-health"
        item = provider_health_test_by_key("tiantianjiexi_account_status")

        with override_settings(TIANTIANJIEXI_PROVIDER_ENABLED=True):
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
