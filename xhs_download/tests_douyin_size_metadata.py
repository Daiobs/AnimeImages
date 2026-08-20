import time
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.template.loader import render_to_string
from django.test import SimpleTestCase

from .services.platforms.douyin import DouyinAdapter
from .services.platforms.douyin_providers import (
    DouyinParseContext,
    DouyinTianTianJieXiProvider,
    build_external_parse_result,
)
from .services.platforms.tiantianjiexi_generic import (
    TianTianJieXiResultNormalizer,
)
from .views import build_result_display_context


AWEME_ID = "7669858827638845160"
MEDIA_URL = "https://v26-default.365yg.com/controlled-video.mp4"
SIZE_BYTES = 79_910_405


def provider_settings():
    return SimpleNamespace(
        provider_is_enabled=lambda _name: True,
        ultra_total_timeout_seconds=6.0,
        enabled_provider_order=("tiantianjiexi", "native"),
        ultra_kukutool_grace_seconds=1.0,
        kukutool_sidecar_timeout_seconds=1.0,
    )


def normalized_payload(size_bytes=SIZE_BYTES):
    return TianTianJieXiResultNormalizer.normalize_payload(
        {
            "title": "controlled title",
            "cover": "https://p3.douyinpic.com/controlled-cover.jpeg",
            "videos": [
                {
                    "url": MEDIA_URL,
                    "qualityLabel": "原画",
                    "sizeInBytes": size_bytes,
                }
            ],
        }
    )


class DouyinSizeMetadataRegressionTests(SimpleTestCase):
    def setUp(self):
        self.adapter = DouyinAdapter()
        self.context = DouyinParseContext(
            url=f"https://www.douyin.com/video/{AWEME_ID}",
            final_url=f"https://www.douyin.com/video/{AWEME_ID}",
            aweme_id=AWEME_ID,
            prefer_ultra_hd=True,
            deadline_monotonic=time.monotonic() + 6,
        )
        self.provider = DouyinTianTianJieXiProvider(
            provider_settings=provider_settings(),
            provider=Mock(),
            reserve_fallback_grace=False,
        )

    def build_pipeline(self, size_bytes=SIZE_BYTES):
        payload = normalized_payload(size_bytes)
        candidate = self.provider.build_candidates(payload, self.adapter)[0]
        result = build_external_parse_result(
            self.adapter,
            self.context,
            candidate,
            provider_name="tiantianjiexi",
            base_filename=payload["title"],
            cover_url=payload["cover"],
        )
        display = build_result_display_context(
            result,
            probe_missing_video_sizes=False,
        )
        return payload, candidate, result, display

    def test_size_in_bytes_survives_normalizer_candidate_asset_and_display(self):
        payload, candidate, result, display = self.build_pipeline()

        self.assertEqual(payload["videos"][0]["size"], SIZE_BYTES)
        self.assertEqual(candidate.size_bytes, SIZE_BYTES)
        self.assertEqual(result.video_assets[0].size_bytes, SIZE_BYTES)
        self.assertEqual(display["video_items"][0]["size_bytes"], SIZE_BYTES)
        self.assertEqual(display["video_items"][0]["size_display_label"], "76.2 MB")

    def test_rendered_result_shows_exact_size_instead_of_unknown(self):
        _payload, _candidate, _result, display = self.build_pipeline()

        html = render_to_string(
            "partials/xhs_results_region.html",
            {
                **display,
                "proxy_image_url": "/media/proxy-image/",
                "show_internal_diagnostics": False,
            },
        )

        self.assertIn("视频资源 · 76.2 MB", html)
        self.assertNotIn("视频资源 · 大小未知", html)

    @patch("xhs_download.views.probe_media_size")
    def test_provider_size_does_not_add_media_probe(self, mock_probe_size):
        _payload, _candidate, _result, display = self.build_pipeline()

        self.assertEqual(display["video_items"][0]["size_bytes"], SIZE_BYTES)
        mock_probe_size.assert_not_called()

    @patch("xhs_download.views.probe_media_size")
    def test_zero_size_remains_unknown_without_media_probe(self, mock_probe_size):
        payload, candidate, result, display = self.build_pipeline(0)

        self.assertEqual(payload["videos"][0]["size"], 0)
        self.assertEqual(candidate.size_bytes, 0)
        self.assertEqual(result.video_assets[0].size_bytes, 0)
        self.assertEqual(display["video_items"][0]["size_bytes"], 0)
        self.assertEqual(display["video_items"][0]["size_display_label"], "大小未知")
        mock_probe_size.assert_not_called()
