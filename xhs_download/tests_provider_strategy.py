from unittest.mock import patch

from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from .services.assets import MediaAsset, ParseResult
from .services.platforms.third_party import get_third_party_rule
from .services.provider_strategy import (
    DOWNLOAD_STRATEGY_BROWSER_BLOB,
    DOWNLOAD_STRATEGY_BROWSER_DIRECT,
    DOWNLOAD_STRATEGY_SERVER_MERGE,
    build_download_strategy,
)
from .views import split_batch_links


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

    def test_short_video_rule_prefers_tiantianjiexi_before_kukutool(self):
        rule = get_third_party_rule("kuaishou")

        self.assertIsNotNone(rule)
        self.assertEqual(rule.provider_ids[0], "tiantianjiexi_generic")
        self.assertIn("kukutool_generic", rule.provider_ids)

    def test_youtube_rule_keeps_ytdlp_first(self):
        rule = get_third_party_rule("youtube")

        self.assertIsNotNone(rule)
        self.assertEqual(rule.provider_ids[0], "yt_dlp_generic")

    def test_split_batch_links_dedupes_and_limits(self):
        links = split_batch_links("\n".join(["https://example.com/a", "https://example.com/a"] + [
            f"https://example.com/{index}" for index in range(30)
        ]))

        self.assertEqual(len(links), 20)
        self.assertEqual(links[0], "https://example.com/a")


class BatchParseViewTests(TestCase):
    def test_batch_parse_page_renders_strategy_matrix(self):
        response = self.client.get(reverse("batch_parse"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "下载策略矩阵")
        self.assertContains(response, "批量解析")

    @patch("xhs_download.views.parse_media_page")
    def test_batch_parse_post_renders_successful_result(self, mock_parse):
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
