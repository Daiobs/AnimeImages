from io import BytesIO
import json
import time
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory
from unittest.mock import Mock, patch
from zipfile import ZipFile

from django.core.cache import cache
from django.test import Client, SimpleTestCase, TestCase, override_settings
from PIL import Image

from .services.common import (
    MEDIA_HOST_SUFFIXES,
    XhsSecurityError,
    clean_filename,
    encode_url,
    request_get_with_retry,
    validate_media_url,
    validate_page_url,
)
from .services.downloads import (
    build_download_archive,
    build_download_archive_file,
    get_media_headers,
    normalize_quality,
    normalize_save_format,
    normalize_video_save_mode,
    resolve_server_save_folder,
    save_media_items_to_server,
)
from .services.assets import MediaAsset, ParseResult
from .services.candidates import MediaCandidate, dedupe_candidates, rank_media_candidates
from .services.diagnostics import CandidateDiagnostic, select_best_media_candidate
from .services.provider_settings import (
    ProviderSettings,
    get_platform_provider_settings,
    normalize_provider_order,
)
from .services.quality import QualityProbe, QualityProbeResult
from .services.rate_limits import get_client_identity, get_daily_total, release_concurrency
from .services.platforms import BilibiliAdapter, DouyinAdapter, XiaohongshuAdapter
from .services.platforms.bilibili import BILIBILI_HEADERS
from .services.platforms.douyin import DOUYIN_HEADERS, extract_aweme_id, get_video_candidates
from .services.platforms.douyin_providers import (
    DouyinParseContext,
    DouyinNativeEnhancedProvider,
    DouyinNativeProvider,
    DouyinTikHubProvider,
    build_tikhub_provider_response,
    build_douyin_result_providers,
    context_log_fields,
)
from .services.parser import normalize_xhs_image_url, normalize_xhs_preview_image_url, parse_xhs_html
from .services.registry import extract_url_from_text, get_adapter_for_url


def make_image_bytes():
    buffer = BytesIO()
    Image.new("RGB", (10, 8), (255, 0, 0)).save(buffer, "JPEG")
    return buffer.getvalue()


class FakeStreamingResponse:
    def __init__(self, content, url="https://ci.xiaohongshu.com/a.jpg", content_type="image/jpeg"):
        self.content = content
        self.url = url
        self.headers = {
            "Content-Length": str(len(content)),
            "Content-Type": content_type,
        }
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def iter_content(self, chunk_size):
        for index in range(0, len(self.content), chunk_size):
            yield self.content[index:index + chunk_size]

    def close(self):
        self.closed = True


class XhsUtilityTests(SimpleTestCase):
    def test_clean_filename_removes_invalid_characters(self):
        self.assertEqual(clean_filename('a/b:c*?"<>|.jpg'), "abc.jpg")

    def test_clean_filename_falls_back_when_empty(self):
        self.assertEqual(clean_filename("***"), "xhs_image")

    def test_normalize_save_format_accepts_supported_formats(self):
        self.assertEqual(normalize_save_format("original"), "original")
        self.assertEqual(normalize_save_format("jpeg"), "jpg")
        self.assertEqual(normalize_save_format("png"), "png")
        self.assertEqual(normalize_save_format("webp"), "webp")

    def test_normalize_save_format_falls_back_to_original(self):
        self.assertEqual(normalize_save_format("bmp"), "original")

    def test_normalize_quality_clamps_range(self):
        self.assertEqual(normalize_quality("0"), 1)
        self.assertEqual(normalize_quality("101"), 100)
        self.assertEqual(normalize_quality("bad"), 95)

    def test_normalize_video_save_mode_falls_back_to_video(self):
        self.assertEqual(normalize_video_save_mode("both"), "both")
        self.assertEqual(normalize_video_save_mode("bad"), "video")

    def test_validate_page_url_rejects_non_xhs_hosts(self):
        with self.assertRaises(XhsSecurityError):
            validate_page_url("https://example.com/post")

    def test_validate_media_url_rejects_localhost(self):
        with self.assertRaises(XhsSecurityError):
            validate_media_url("http://127.0.0.1:8000/private")

    def test_validate_media_url_rejects_private_ip(self):
        with self.assertRaises(XhsSecurityError):
            validate_media_url("http://10.0.0.2/private")

    def test_validate_media_url_allows_douyin_redirect_hosts(self):
        validate_media_url("https://aweme.snssdk.com/aweme/v1/play/?video_id=abc")
        validate_media_url("https://v3-dy-o.zjcdn.com/video/tos/cn/path")
        validate_media_url("https://v9-default.365yg.com/video/tos/cn/path")

    def test_validate_media_url_allows_bilibili_media_hosts(self):
        validate_media_url("https://upos-sz-mirrorcosov.bilivideo.com/upgcxcode/video.mp4")
        validate_media_url("https://i0.hdslb.com/bfs/archive/cover.jpg")

    def test_dedupe_candidates_preserves_first_occurrence(self):
        candidates = dedupe_candidates([
            MediaCandidate(url="https://example.com/a.mp4", size_bytes=100),
            MediaCandidate(url="https://example.com/a.mp4", size_bytes=200),
            MediaCandidate(url="https://example.com/b.mp4", size_bytes=50),
        ])

        self.assertEqual([item.url for item in candidates], ["https://example.com/a.mp4", "https://example.com/b.mp4"])
        self.assertEqual(candidates[0].size_bytes, 100)

    def test_rank_media_candidates_prefers_larger_verified_sources(self):
        candidates = rank_media_candidates([
            MediaCandidate(url="https://example.com/low.mp4", size_bytes=1_000, width=1920, height=1080),
            MediaCandidate(url="https://example.com/high.mp4", size_bytes=20_000, width=1280, height=720),
        ])

        self.assertEqual(candidates[0].url, "https://example.com/high.mp4")

    def test_normalize_provider_order_deduplicates_names(self):
        self.assertEqual(
            normalize_provider_order("native,tikhub,native,"),
            ("native", "tikhub"),
        )

    @override_settings(
        ULTRA_HD_PROVIDER_ENABLED=False,
        TIKHUB_API_KEY="secret",
        DOUYIN_PROVIDER_ORDER=["native", "tikhub"],
    )
    def test_provider_settings_keep_external_providers_disabled_by_default(self):
        provider_settings = get_platform_provider_settings("douyin")

        self.assertEqual(provider_settings.provider_order, ("native", "tikhub"))
        self.assertEqual(provider_settings.enabled_provider_order, ("native",))

    def test_provider_settings_enable_tikhub_when_configured(self):
        provider_settings = ProviderSettings(
            ultra_hd_enabled=True,
            provider_order=("native", "tikhub"),
            tikhub_api_key="secret",
        )

        self.assertEqual(provider_settings.enabled_provider_order, ("native", "tikhub"))

    def test_provider_settings_gate_native_enhanced_provider(self):
        provider_settings = ProviderSettings(
            ultra_hd_enabled=False,
            provider_order=("native_enhanced", "native"),
        )

        self.assertEqual(provider_settings.enabled_provider_order, ("native",))

    @patch("xhs_download.services.quality.requests.head")
    def test_quality_probe_uses_head_content_length(self, mock_head):
        response = Mock()
        response.status_code = 200
        response.url = "https://aweme.snssdk.com/aweme/v1/play/?video_id=abc"
        response.headers = {"Content-Length": "1234", "Content-Type": "video/mp4"}
        response.close = Mock()
        mock_head.return_value = response

        result = QualityProbe().probe("https://aweme.snssdk.com/aweme/v1/play/?video_id=abc")

        self.assertTrue(result.reachable)
        self.assertEqual(result.size_bytes, 1234)
        self.assertEqual(result.method, "HEAD")
        response.close.assert_called_once()

    @patch("xhs_download.services.quality.requests.get")
    @patch("xhs_download.services.quality.requests.head")
    def test_quality_probe_falls_back_to_range_when_head_has_no_size(self, mock_head, mock_get):
        head_response = Mock()
        head_response.status_code = 200
        head_response.url = "https://aweme.snssdk.com/aweme/v1/play/?video_id=abc"
        head_response.headers = {"Content-Type": "video/mp4"}
        head_response.close = Mock()
        mock_head.return_value = head_response

        range_response = Mock()
        range_response.status_code = 206
        range_response.url = "https://aweme.snssdk.com/aweme/v1/play/?video_id=abc"
        range_response.headers = {"Content-Range": "bytes 0-0/5678", "Content-Type": "video/mp4"}
        range_response.close = Mock()
        mock_get.return_value = range_response

        result = QualityProbe().probe("https://aweme.snssdk.com/aweme/v1/play/?video_id=abc")

        self.assertTrue(result.reachable)
        self.assertEqual(result.size_bytes, 5678)
        self.assertEqual(result.method, "RANGE")
        head_response.close.assert_called_once()
        range_response.close.assert_called_once()

    @patch("xhs_download.services.quality.requests.get")
    @patch("xhs_download.services.quality.requests.head")
    def test_quality_probe_rejects_html_when_video_expected(self, mock_head, mock_get):
        head_response = Mock()
        head_response.status_code = 200
        head_response.url = "https://v3-web.douyinvod.com/video.mp4"
        head_response.headers = {"Content-Length": "1234", "Content-Type": "text/html"}
        head_response.close = Mock()
        mock_head.return_value = head_response

        range_response = Mock()
        range_response.status_code = 200
        range_response.url = "https://v3-web.douyinvod.com/video.mp4"
        range_response.headers = {"Content-Length": "1234", "Content-Type": "text/html"}
        range_response.close = Mock()
        mock_get.return_value = range_response

        result = QualityProbe().probe("https://v3-web.douyinvod.com/video.mp4", expected_type="video")

        self.assertFalse(result.reachable)
        self.assertIn("Unexpected content type", result.error)

    def test_select_best_media_candidate_prefers_probed_size(self):
        low = "https://v3-web.douyinvod.com/low.mp4"
        high = "https://v3-web.douyinvod.com/high.mp4"
        probe = Mock()
        probe.probe.side_effect = lambda url, headers=None, timeout=6, expected_type="": QualityProbeResult(
            url=url,
            final_url=url,
            reachable=True,
            size_bytes=20_000 if url == high else 2_000,
            content_type="video/mp4",
            method="HEAD",
        )

        selection = select_best_media_candidate(
            [
                MediaCandidate(url=low, size_bytes=10_000, source="declared_low"),
                MediaCandidate(url=high, size_bytes=1_000, source="declared_high"),
            ],
            quality_probe=probe,
        )

        self.assertEqual(selection.candidate.url, high)
        self.assertEqual(selection.candidate.size_bytes, 20_000)
        self.assertEqual(selection.reason, "selected_reachable_probe")

    def test_candidate_diagnostic_exposes_cdn_quality_params(self):
        diagnostic = CandidateDiagnostic(
            index=1,
            candidate=MediaCandidate(url="https://api-play.amemv.com/aweme/v1/play/?video_id=abc&ratio=540p"),
            reachable=True,
            probed_size_bytes=94_441_025,
            final_url=(
                "https://v11-default.365yg.com/video/tos/cn/path?"
                "br=48808&bt=48808&qs=13&btag=80000e00010000&cquery=106H&dy_q=1780802540"
            ),
            content_type="video/mp4",
            probe_method="HEAD",
        )

        data = diagnostic.as_dict()

        self.assertEqual(data["final_quality_params"]["br"], "48808")
        self.assertEqual(data["final_quality_params"]["bt"], "48808")
        self.assertEqual(data["final_quality_params"]["qs"], "13")
        self.assertEqual(data["final_quality_params"]["cquery"], "106H")

    @patch("xhs_download.services.common.requests.get")
    def test_request_get_with_retry_rejects_bad_final_redirect_host(self, mock_get):
        response = Mock()
        response.status_code = 200
        response.url = "http://127.0.0.1/private"
        response.close = Mock()
        mock_get.return_value = response

        with self.assertRaises(XhsSecurityError):
            request_get_with_retry(
                "https://ci.xiaohongshu.com/a",
                allowed_final_host_suffixes=MEDIA_HOST_SUFFIXES,
                retries=1,
                sleep_seconds=0,
            )

        response.close.assert_called()


class XhsParserTests(SimpleTestCase):
    def test_registry_resolves_xiaohongshu_adapter(self):
        adapter = get_adapter_for_url("https://www.xiaohongshu.com/explore/abc123")

        self.assertIsInstance(adapter, XiaohongshuAdapter)

    def test_registry_resolves_xiaohongshu_short_link_adapter(self):
        adapter = get_adapter_for_url("https://xhslink.com/a/b")

        self.assertIsInstance(adapter, XiaohongshuAdapter)

    def test_registry_resolves_douyin_adapter(self):
        adapter = get_adapter_for_url("https://v.douyin.com/abc123/")

        self.assertIsInstance(adapter, DouyinAdapter)

    def test_registry_resolves_douyin_share_text(self):
        adapter = get_adapter_for_url("复制打开抖音，看看这个作品 https://v.douyin.com/abc123/ ！")

        self.assertIsInstance(adapter, DouyinAdapter)

    def test_registry_resolves_bilibili_adapter(self):
        adapter = get_adapter_for_url("https://www.bilibili.com/video/BV1xx411c7mD/")

        self.assertIsInstance(adapter, BilibiliAdapter)

    def test_registry_resolves_bilibili_short_link_adapter(self):
        adapter = get_adapter_for_url("https://b23.tv/abc123")

        self.assertIsInstance(adapter, BilibiliAdapter)

    def test_extract_url_from_share_text_strips_trailing_punctuation(self):
        self.assertEqual(
            extract_url_from_text("复制打开 https://v.douyin.com/abc123/，"),
            "https://v.douyin.com/abc123/",
        )

    def test_extract_url_from_share_text_handles_url_prefix(self):
        self.assertEqual(
            extract_url_from_text("https://v.douyin.com/abc123/ 复制此链接，打开抖音搜索"),
            "https://v.douyin.com/abc123/",
        )

    def test_extract_aweme_id_from_douyin_urls(self):
        self.assertEqual(
            extract_aweme_id("https://www.douyin.com/video/7324602766213598501"),
            "7324602766213598501",
        )
        self.assertEqual(
            extract_aweme_id("https://www.douyin.com/?modal_id=7324602766213598501"),
            "7324602766213598501",
        )

    def test_normalize_image_url_extracts_spectrum_key(self):
        self.assertEqual(
            normalize_xhs_image_url("https://sns-img.xhscdn.com/spectrum/abc123!webp"),
            "https://ci.xiaohongshu.com/spectrum/abc123?imageView2/format/jpg",
        )

    def test_normalize_preview_image_url_uses_small_webp_variant(self):
        self.assertEqual(
            normalize_xhs_preview_image_url("https://sns-img.xhscdn.com/spectrum/abc123!webp"),
            "https://ci.xiaohongshu.com/spectrum/abc123?imageView2/2/w/640/format/webp",
        )

    def test_parse_html_returns_image_post(self):
        html = """
        <html>
          <span class="username">user</span>
          <div id="detail-title">title</div>
          <meta name="og:image" content="https://sns-img.xhscdn.com/spectrum/abc123!webp">
        </html>
        """

        result = parse_xhs_html(html)

        self.assertEqual(result["post_type"], "image")
        self.assertEqual(result["base_filename"], "user_title")
        self.assertEqual(result["image_urls"], ["https://ci.xiaohongshu.com/spectrum/abc123?imageView2/format/jpg"])

    def test_xiaohongshu_adapter_returns_media_assets(self):
        html = """
        <html>
          <meta name="og:image" content="https://sns-img.xhscdn.com/spectrum/abc123!webp">
        </html>
        """

        result = XiaohongshuAdapter().parse_html(html)

        self.assertEqual(result.platform, "xiaohongshu")
        self.assertEqual(result.post_type, "image")
        self.assertEqual(result.image_assets[0].url, "https://ci.xiaohongshu.com/spectrum/abc123?imageView2/format/jpg")
        self.assertEqual(
            result.image_assets[0].preview_url,
            "https://ci.xiaohongshu.com/spectrum/abc123?imageView2/2/w/640/format/webp",
        )

    def test_parse_html_returns_video_post(self):
        html = """
        <html>
          <meta name="og:image" content="https://sns-img.xhscdn.com/spectrum/cover123!webp">
          <script>{"originVideoKey":"video-key-123456789"}</script>
        </html>
        """

        result = parse_xhs_html(html)

        self.assertEqual(result["post_type"], "video")
        self.assertEqual(result["video_items"][0]["video_url"], "https://sns-video-bd.xhscdn.com/video-key-123456789")
        self.assertEqual(result["video_items"][0]["cover_url"], "https://ci.xiaohongshu.com/spectrum/cover123?imageView2/format/jpg")


class DouyinAdapterTests(SimpleTestCase):
    @patch("xhs_download.services.platforms.douyin.request_get_with_retry")
    def test_parse_url_skips_direct_page_fetch_for_modal_id_without_ultra_hd(self, mock_request_get):
        expected = ParseResult(
            platform="douyin",
            post_type="video",
            base_filename="native",
            assets=(
                MediaAsset(
                    media_type="video",
                    url="https://v3-web.douyinvod.com/native.mp4",
                    platform="douyin",
                ),
            ),
        )

        with patch.object(DouyinAdapter, "fetch_mobile_feed_result", return_value=expected) as mock_feed:
            result = DouyinAdapter().parse_url(
                "https://www.douyin.com/user/self?from_tab_name=main&modal_id=7539896907901062452",
                prefer_ultra_hd=False,
            )

        self.assertEqual(result.video_assets[0].url, "https://v3-web.douyinvod.com/native.mp4")
        mock_feed.assert_called_once_with("7539896907901062452")
        mock_request_get.assert_not_called()

    @patch("xhs_download.services.platforms.douyin.request_get_with_retry")
    @patch("xhs_download.services.platforms.douyin.build_douyin_result_providers")
    def test_parse_url_skips_direct_page_fetch_for_modal_id_with_ultra_hd(self, mock_provider_builder, mock_request_get):
        expected = ParseResult(
            platform="douyin",
            post_type="video",
            base_filename="ultra",
            assets=(
                MediaAsset(
                    media_type="video",
                    url="https://v11-default.365yg.com/ultra.mp4",
                    platform="douyin",
                    size_bytes=90_000_000,
                ),
            ),
        )

        class FakeProvider:
            def parse(self, adapter, context):
                self.context = context
                return expected

        provider = FakeProvider()
        mock_provider_builder.return_value = (provider,)

        result = DouyinAdapter().parse_url(
            "https://www.douyin.com/user/self?from_tab_name=main&modal_id=7539896907901062452",
            prefer_ultra_hd=True,
        )

        self.assertEqual(result.video_assets[0].url, "https://v11-default.365yg.com/ultra.mp4")
        self.assertEqual(provider.context.aweme_id, "7539896907901062452")
        mock_request_get.assert_not_called()

    @override_settings(
        ULTRA_HD_PROVIDER_ENABLED=False,
        TIKHUB_API_KEY="",
        DOUYIN_PROVIDER_ORDER=["tikhub"],
    )
    def test_douyin_provider_builder_falls_back_to_native_provider(self):
        providers = build_douyin_result_providers()

        self.assertEqual(len(providers), 1)
        self.assertIsInstance(providers[0], DouyinNativeProvider)

    def test_douyin_provider_builder_keeps_native_fallback_for_enhanced(self):
        providers = build_douyin_result_providers(ProviderSettings(
            ultra_hd_enabled=True,
            provider_order=("native_enhanced",),
        ))

        self.assertEqual(len(providers), 2)
        self.assertIsInstance(providers[0], DouyinNativeEnhancedProvider)
        self.assertIsInstance(providers[1], DouyinNativeProvider)

    def test_douyin_provider_builder_includes_tikhub_when_enabled(self):
        providers = build_douyin_result_providers(ProviderSettings(
            ultra_hd_enabled=True,
            provider_order=("tikhub",),
            tikhub_api_key="secret",
        ))

        self.assertEqual(len(providers), 2)
        self.assertIsInstance(providers[0], DouyinTikHubProvider)
        self.assertIsInstance(providers[1], DouyinNativeProvider)

    def test_douyin_provider_builder_prioritizes_tikhub_with_request_switch(self):
        providers = build_douyin_result_providers(ProviderSettings(
            ultra_hd_enabled=True,
            provider_order=("native", "tikhub"),
            tikhub_api_key="secret",
        ))

        self.assertEqual(len(providers), 2)
        self.assertIsInstance(providers[0], DouyinTikHubProvider)
        self.assertIsInstance(providers[1], DouyinNativeProvider)

    def test_douyin_provider_builder_does_not_inject_tikhub_when_removed_from_order(self):
        providers = build_douyin_result_providers(ProviderSettings(
            ultra_hd_enabled=True,
            provider_order=("native",),
            tikhub_api_key="secret",
        ))

        self.assertFalse(any(isinstance(provider, DouyinTikHubProvider) for provider in providers))
        self.assertEqual(len(providers), 1)
        self.assertIsInstance(providers[0], DouyinNativeProvider)

    def test_douyin_provider_builder_uses_fast_native_path_without_request_switch(self):
        providers = build_douyin_result_providers(
            ProviderSettings(
                ultra_hd_enabled=True,
                provider_order=("tikhub", "native_enhanced", "native"),
                tikhub_api_key="secret",
            ),
            prefer_ultra_hd=False,
        )

        self.assertEqual(len(providers), 1)
        self.assertIsInstance(providers[0], DouyinNativeProvider)

    def test_tikhub_provider_response_schema_exposes_status_and_candidates(self):
        response = build_tikhub_provider_response(
            {
                "code": 500,
                "message": "provider failed",
                "request_id": "req-1",
                "data": {
                    "original_video_url": "https://v11-default.365yg.com/video/tos/cn/path.mp4?qs=13",
                },
            }
        )

        self.assertFalse(response.ok)
        self.assertEqual(response.code, "500")
        self.assertEqual(response.message, "provider failed")
        self.assertEqual(response.request_id, "req-1")
        self.assertEqual(len(response.candidates), 1)

    def test_native_provider_logs_fallback_failures(self):
        class BrokenProvider:
            name = "broken"

            def parse(self, adapter, context):
                raise RuntimeError("boom")

        class FakeAdapter:
            pass

        with self.assertLogs("xhs_download.services.platforms.douyin_providers", level="INFO") as captured:
            result = DouyinNativeProvider(fallback_providers=(BrokenProvider(),)).parse(
                FakeAdapter(),
                DouyinParseContext(url="https://www.douyin.com/video/123", aweme_id="123"),
            )

        self.assertIsNone(result)
        self.assertTrue(any("douyin_provider_failed" in item for item in captured.output))

    def test_provider_log_fields_exclude_query_parameters(self):
        fields = context_log_fields(DouyinParseContext(
            url="https://www.douyin.com/video/123?token=sensitive",
            aweme_id="123",
        ))

        self.assertEqual(fields["source_host"], "www.douyin.com")
        self.assertEqual(fields["source_path"], "/video/123")
        self.assertNotIn("url", fields)

    @patch("xhs_download.services.diagnostics.QualityProbe.probe")
    @patch("xhs_download.services.platforms.douyin_providers.requests.get")
    def test_tikhub_provider_returns_validated_original_video_url(self, mock_get, mock_probe):
        cache.clear()

        class FakeAdapter:
            platform_id = "douyin"
            headers = DOUYIN_HEADERS
            media_host_suffixes = ("365yg.com",)

            def fetch_mobile_feed_result(self, aweme_id, probe_video_candidates=False):
                return ParseResult(
                    platform="douyin",
                    post_type="video",
                    base_filename="native_title",
                    assets=(
                        MediaAsset(
                            media_type="video",
                            url="https://v3-web.douyinvod.com/native.mp4",
                            platform="douyin",
                            cover_url="https://p3-sign.douyinpic.com/cover.jpeg",
                        ),
                    ),
                )

        response = Mock()
        response.json.return_value = {
            "code": 200,
            "data": {
                "original_video_url": (
                    "https://v11-default.365yg.com/video/tos/cn/path?"
                    "br=48808&bt=48808&qs=13"
                )
            },
        }
        response.raise_for_status = Mock()
        response.close = Mock()
        mock_get.return_value = response
        mock_probe.return_value = QualityProbeResult(
            url=response.json.return_value["data"]["original_video_url"],
            final_url=response.json.return_value["data"]["original_video_url"],
            reachable=True,
            size_bytes=94_441_025,
            content_type="video/mp4",
            method="HEAD",
        )

        result = DouyinTikHubProvider(
            ProviderSettings(
                ultra_hd_enabled=True,
                provider_order=("tikhub",),
                tikhub_api_key="secret",
                tikhub_region="CN",
            )
        ).parse(
            FakeAdapter(),
            DouyinParseContext(
                url="https://www.douyin.com/video/7618531038729648354",
                aweme_id="7618531038729648354",
            ),
        )

        self.assertEqual(result.base_filename, "native_title")
        self.assertEqual(result.video_assets[0].size_bytes, 94_441_025)
        self.assertEqual(result.video_assets[0].quality_label, "原始上传画质")
        self.assertEqual(result.video_assets[0].cover_url, "https://p3-sign.douyinpic.com/cover.jpeg")
        self.assertIn("fetch_video_high_quality_play_url", mock_get.call_args.args[0])
        self.assertEqual(mock_get.call_args.kwargs["params"]["aweme_id"], "7618531038729648354")
        self.assertEqual(mock_get.call_args.kwargs["headers"]["Authorization"], "Bearer secret")

    @patch("xhs_download.services.diagnostics.QualityProbe.probe")
    @patch("xhs_download.services.platforms.douyin_providers.requests.get")
    def test_tikhub_provider_keeps_native_when_tikhub_video_is_smaller(self, mock_get, mock_probe):
        cache.clear()

        class FakeAdapter:
            platform_id = "douyin"
            headers = DOUYIN_HEADERS
            media_host_suffixes = ("365yg.com", "douyinvod.com")

            def fetch_mobile_feed_result(self, aweme_id, probe_video_candidates=False):
                return ParseResult(
                    platform="douyin",
                    post_type="video",
                    base_filename="native_1080p",
                    assets=(
                        MediaAsset(
                            media_type="video",
                            url="https://v3-web.douyinvod.com/native-1080p.mp4",
                            platform="douyin",
                            size_bytes=20_000_000,
                            cover_url="https://p3-sign.douyinpic.com/native-cover.jpeg",
                        ),
                    ),
                )

            def fetch_mobile_share_result(self, aweme_id, probe_video_candidates=False):
                return ParseResult(
                    platform="douyin",
                    post_type="video",
                    base_filename="native_720p",
                    assets=(
                        MediaAsset(
                            media_type="video",
                            url="https://v3-web.douyinvod.com/native-720p.mp4",
                            platform="douyin",
                            size_bytes=2_500_000,
                        ),
                    ),
                )

        response = Mock()
        response.json.return_value = {
            "code": 200,
            "data": {
                "original_video_url": "https://v11-default.365yg.com/video/tos/cn/path?br=1200&bt=1200&qs=13",
            },
        }
        response.raise_for_status = Mock()
        response.close = Mock()
        mock_get.return_value = response
        mock_probe.return_value = QualityProbeResult(
            url=response.json.return_value["data"]["original_video_url"],
            final_url=response.json.return_value["data"]["original_video_url"],
            reachable=True,
            size_bytes=2_000_000,
            content_type="video/mp4",
            method="HEAD",
        )

        result = DouyinTikHubProvider(
            ProviderSettings(
                ultra_hd_enabled=True,
                provider_order=("tikhub",),
                tikhub_api_key="secret",
            )
        ).parse(
            FakeAdapter(),
            DouyinParseContext(
                url="https://www.douyin.com/video/7539896907901062452",
                aweme_id="7539896907901062452",
            ),
        )

        self.assertEqual(result.base_filename, "native_1080p")
        self.assertEqual(result.video_assets[0].url, "https://v3-web.douyinvod.com/native-1080p.mp4")
        self.assertEqual(result.video_assets[0].size_bytes, 20_000_000)

    @patch("xhs_download.services.platforms.douyin_providers.requests.get")
    def test_tikhub_provider_caches_identical_requests(self, mock_get):
        cache.clear()
        response = Mock()
        response.json.return_value = {"code": 200, "data": {"original_video_url": "https://v11-default.365yg.com/a.mp4"}}
        response.raise_for_status = Mock()
        response.close = Mock()
        mock_get.return_value = response
        provider = DouyinTikHubProvider(
            ProviderSettings(
                ultra_hd_enabled=True,
                provider_order=("tikhub",),
                tikhub_api_key="secret",
                tikhub_cache_ttl_seconds=60,
            )
        )
        context = DouyinParseContext(
            url="https://www.douyin.com/video/7618531038729648354",
            aweme_id="7618531038729648354",
        )

        self.assertEqual(provider.fetch_high_quality_data(context), response.json.return_value)
        self.assertEqual(provider.fetch_high_quality_data(context), response.json.return_value)

        self.assertEqual(mock_get.call_count, 1)

    @patch("xhs_download.services.platforms.douyin_providers.requests.get")
    def test_tikhub_daily_limit_uses_shared_cache_counter(self, mock_get):
        cache.clear()
        response = Mock()
        response.json.return_value = {
            "code": 200,
            "data": {"original_video_url": "https://v11-default.365yg.com/a.mp4"},
        }
        response.raise_for_status = Mock()
        response.close = Mock()
        mock_get.return_value = response
        provider = DouyinTikHubProvider(
            ProviderSettings(
                ultra_hd_enabled=True,
                provider_order=("tikhub",),
                tikhub_api_key="secret",
                tikhub_cache_ttl_seconds=0,
                tikhub_daily_limit=1,
            )
        )

        provider.fetch_high_quality_data(DouyinParseContext(
            url="https://www.douyin.com/video/1",
            aweme_id="1",
        ))
        with self.assertRaisesMessage(RuntimeError, "TikHub daily limit reached"):
            provider.fetch_high_quality_data(DouyinParseContext(
                url="https://www.douyin.com/video/2",
                aweme_id="2",
            ))

        self.assertEqual(mock_get.call_count, 1)

    def test_native_enhanced_provider_selects_largest_video_result_across_sources(self):
        class FakeAdapter:
            def fetch_mobile_feed_result(self, aweme_id, probe_video_candidates=False):
                return ParseResult(
                    platform="douyin",
                    post_type="video",
                    base_filename="feed",
                    assets=(MediaAsset(media_type="video", url="https://v3-web.douyinvod.com/feed.mp4", platform="douyin", size_bytes=100),),
                )

            def fetch_mobile_share_result(self, aweme_id, probe_video_candidates=False):
                return ParseResult(
                    platform="douyin",
                    post_type="video",
                    base_filename="share",
                    assets=(MediaAsset(media_type="video", url="https://v3-web.douyinvod.com/share.mp4", platform="douyin", size_bytes=1000),),
                )

            def parse_html(self, html, source_url=None, probe_video_candidates=False):
                return ParseResult(
                    platform="douyin",
                    post_type="video",
                    base_filename="direct",
                    assets=(MediaAsset(media_type="video", url="https://v3-web.douyinvod.com/direct.mp4", platform="douyin", size_bytes=500),),
                )

        result = DouyinNativeEnhancedProvider().parse(
            FakeAdapter(),
            DouyinParseContext(url="https://www.douyin.com/video/123", aweme_id="123", html="<html></html>"),
        )

        self.assertEqual(result.video_assets[0].url, "https://v3-web.douyinvod.com/share.mp4")

    def test_parse_html_returns_image_post_from_render_data(self):
        data = {
            "aweme_detail": {
                "aweme_id": "123",
                "desc": "图文标题",
                "author": {"nickname": "作者"},
                "images": [
                    {"url_list": ["https://p3-sign.douyinpic.com/image-1.webp"]},
                    {"download_url_list": ["https://p6-sign.douyinpic.com/image-2.webp"]},
                ],
            }
        }
        encoded = json.dumps(data, ensure_ascii=False)
        html = f'<script id="RENDER_DATA" type="application/json">{encoded}</script>'

        result = DouyinAdapter().parse_html(html)

        self.assertEqual(result.platform, "douyin")
        self.assertEqual(result.post_type, "image")
        self.assertEqual(result.base_filename, "作者_图文标题")
        self.assertEqual(len(result.image_assets), 2)
        self.assertEqual(result.image_assets[0].url, "https://p3-sign.douyinpic.com/image-1.webp")

    def test_parse_html_returns_highest_bitrate_video(self):
        data = {
            "loaderData": {
                "aweme": {
                    "aweme_id": "456",
                    "desc": "视频标题",
                    "author": {"nickname": "作者"},
                    "video": {
                        "cover": {"url_list": ["https://p3-sign.douyinpic.com/cover.webp"]},
                        "bit_rate": [
                            {
                                "bit_rate": 1000,
                                "play_addr": {"url_list": ["https://v3-web.douyinvod.com/low.mp4"]},
                            },
                            {
                                "bit_rate": 3000,
                                "play_addr": {"url_list": ["https://v3-web.douyinvod.com/high.mp4"]},
                            },
                        ],
                    },
                }
            }
        }
        html = f"""
        <script>
          window._ROUTER_DATA = {json.dumps(data, ensure_ascii=False)};
        </script>
        """

        result = DouyinAdapter().parse_html(html)

        self.assertEqual(result.platform, "douyin")
        self.assertEqual(result.post_type, "video")
        self.assertEqual(result.video_assets[0].url, "https://v3-web.douyinvod.com/high.mp4")
        self.assertEqual(result.video_assets[0].cover_url, "https://p3-sign.douyinpic.com/cover.webp")

    @patch("xhs_download.services.diagnostics.QualityProbe.probe")
    def test_parse_aweme_enhanced_prefers_larger_probed_video(self, mock_probe):
        low = "https://v3-web.douyinvod.com/low.mp4"
        high = "https://v3-web.douyinvod.com/high.mp4"
        mock_probe.side_effect = lambda url, headers=None, timeout=6, expected_type="": QualityProbeResult(
            url=url,
            final_url=url,
            reachable=True,
            size_bytes=25_000 if url == high else 5_000,
            content_type="video/mp4",
            method="HEAD",
        )

        result = DouyinAdapter().parse_aweme(
            {
                "aweme_id": "456",
                "desc": "视频标题",
                "author": {"nickname": "作者"},
                "video": {
                    "bit_rate": [
                        {
                            "bit_rate": 3000,
                            "play_addr": {
                                "data_size": 12_000,
                                "url_list": [low],
                            },
                        },
                        {
                            "bit_rate": 1000,
                            "play_addr": {
                                "data_size": 1_000,
                                "url_list": [high],
                            },
                        },
                    ],
                },
            },
            probe_video_candidates=True,
        )

        self.assertEqual(result.video_assets[0].url, high)
        self.assertEqual(result.video_assets[0].size_bytes, 25_000)

    def test_parse_html_returns_video_from_mobile_share_router_data(self):
        data = {
            "loaderData": {
                "video_(id)/page": {
                    "videoInfoRes": {
                        "item_list": [
                            {
                                "aweme_id": "7324602766213598501",
                                "desc": "视频标题",
                                "author": {"nickname": "作者"},
                                "video": {
                                    "play_addr": {
                                        "url_list": [
                                            "https://aweme.snssdk.com/aweme/v1/playwm/?video_id=abc&ratio=720p"
                                        ]
                                    },
                                    "cover": {"url_list": ["https://p3-sign.douyinpic.com/cover.webp"]},
                                },
                            }
                        ],
                        "status_code": 0,
                    }
                }
            }
        }
        html = f"""
        <script>
          window._ROUTER_DATA = {json.dumps(data, ensure_ascii=False)};
        </script>
        """

        result = DouyinAdapter().parse_html(html)

        self.assertEqual(result.platform, "douyin")
        self.assertEqual(result.post_type, "video")
        self.assertEqual(
            result.video_assets[0].url,
            "https://aweme.snssdk.com/aweme/v1/play/?video_id=abc&ratio=4k",
        )
        self.assertEqual(result.video_assets[0].cover_url, "https://p3-sign.douyinpic.com/cover.webp")

    def test_parse_aweme_prefers_browser_friendly_cover_urls(self):
        result = DouyinAdapter().parse_aweme({
            "aweme_id": "456",
            "desc": "视频标题",
            "author": {"nickname": "作者"},
            "video": {
                "cover": {
                    "url_list": [
                        "https://p3-sign.douyinpic.com/tos/cover~tplv-dy.heic?x=1",
                        "https://p3-sign.douyinpic.com/tos/cover~tplv-dy.jpeg?x=1",
                    ],
                },
                "origin_cover": {
                    "url_list": [
                        "https://p3-sign.douyinpic.com/tos/origin~tplv-dy.heic?x=1",
                    ],
                },
                "play_addr": {
                    "url_list": ["https://aweme.snssdk.com/aweme/v1/playwm/?video_id=abc&ratio=720p"],
                },
            },
        })

        self.assertEqual(
            result.video_assets[0].cover_url,
            "https://p3-sign.douyinpic.com/tos/cover~tplv-dy.jpeg?x=1",
        )
        self.assertEqual(
            result.video_assets[0].cover_preview_url,
            "https://p3-sign.douyinpic.com/tos/cover~tplv-dy.jpeg?x=1",
        )

    @patch("xhs_download.services.platforms.douyin.request_get_with_retry")
    def test_parse_url_falls_back_to_mobile_share_page(self, mock_get):
        mobile_data = {
            "loaderData": {
                "video_(id)/page": {
                    "videoInfoRes": {
                        "item_list": [
                            {
                                "aweme_id": "7324602766213598501",
                                "desc": "视频标题",
                                "author": {"nickname": "作者"},
                                "video": {
                                    "play_addr": {
                                        "url_list": [
                                            "https://aweme.snssdk.com/aweme/v1/playwm/?video_id=abc"
                                        ]
                                    },
                                },
                            }
                        ]
                    }
                }
            }
        }
        mobile_response = Mock()
        mobile_response.status_code = 200
        mobile_response.url = "https://www.iesdouyin.com/share/video/7324602766213598501/?region=CN"
        mobile_response.text = f"<script>window._ROUTER_DATA = {json.dumps(mobile_data)};</script>"
        mobile_response.close = Mock()
        mock_get.side_effect = [ValueError("feed unavailable"), mobile_response]

        result = DouyinAdapter().parse_url("https://www.douyin.com/video/7324602766213598501")

        self.assertEqual(result.post_type, "video")
        self.assertEqual(len(result.video_assets), 1)
        self.assertIn("/aweme/v1/feed/", mock_get.call_args_list[0].args[0])
        self.assertIn("/share/video/7324602766213598501/", mock_get.call_args_list[1].args[0])
        mobile_response.close.assert_called_once()

    @patch("xhs_download.services.platforms.douyin.request_get_with_retry")
    def test_parse_url_continues_to_note_share_when_video_share_fails(self, mock_get):
        note_data = {
            "loaderData": {
                "aweme": {
                    "aweme_id": "7324602766213598501",
                    "desc": "图文标题",
                    "author": {"nickname": "作者"},
                    "images": [{"url_list": ["https://p3-sign.douyinpic.com/image.webp"]}],
                }
            }
        }
        note_response = Mock()
        note_response.status_code = 200
        note_response.url = "https://www.iesdouyin.com/share/note/7324602766213598501/?region=CN"
        note_response.text = f"<script>window._ROUTER_DATA = {json.dumps(note_data)};</script>"
        note_response.close = Mock()
        mock_get.side_effect = [
            ValueError("feed unavailable"),
            ValueError("video share unavailable"),
            note_response,
        ]

        result = DouyinAdapter().parse_url("https://www.douyin.com/note/7324602766213598501")

        self.assertEqual(result.post_type, "image")
        self.assertEqual(result.image_assets[0].url, "https://p3-sign.douyinpic.com/image.webp")
        self.assertIn("/share/video/7324602766213598501/", mock_get.call_args_list[1].args[0])
        self.assertIn("/share/note/7324602766213598501/", mock_get.call_args_list[2].args[0])
        note_response.close.assert_called_once()

    @patch("xhs_download.services.platforms.douyin.request_get_with_retry")
    def test_parse_url_uses_mobile_feed_download_addr_when_available(self, mock_get):
        feed_response = Mock()
        feed_response.json.return_value = {
            "aweme_list": [
                {
                    "aweme_id": "7324602766213598501",
                    "desc": "视频标题",
                    "author": {"nickname": "作者"},
                    "video": {
                        "download_addr": {
                            "data_size": 4_395_633,
                            "height": 720,
                            "width": 720,
                            "url_list": ["https://api-play.amemv.com/aweme/v1/play/?video_id=abc&watermark=1&biz_sign=abc"],
                        },
                        "bit_rate": [
                            {
                                "bit_rate": 626067,
                                "play_addr": {
                                    "data_size": 1_270_448,
                                    "height": 576,
                                    "width": 1024,
                                    "url_list": ["https://v3-web.douyinvod.com/low.mp4"],
                                },
                            }
                        ],
                    },
                }
            ]
        }
        feed_response.close = Mock()
        mock_get.return_value = feed_response

        result = DouyinAdapter().parse_url("https://www.douyin.com/video/7324602766213598501")

        self.assertEqual(result.post_type, "video")
        self.assertEqual(
            result.video_assets[0].url,
            "https://api-play.amemv.com/aweme/v1/play/?video_id=abc&watermark=1&biz_sign=abc",
        )
        self.assertEqual(result.video_assets[0].size_bytes, 4_395_633)
        self.assertIn("/aweme/v1/feed/", mock_get.call_args_list[0].args[0])
        feed_response.close.assert_called_once()

    def test_get_video_candidates_prefers_high_quality_unwatermarked_play_url(self):
        candidates = get_video_candidates({
            "play_addr": {
                "url_list": ["https://aweme.snssdk.com/aweme/v1/playwm/?video_id=abc"]
            }
        })

        self.assertEqual(
            candidates[0],
            "https://aweme.snssdk.com/aweme/v1/play/?video_id=abc&ratio=4k",
        )
        self.assertIn(
            "https://aweme.snssdk.com/aweme/v1/play/?video_id=abc&ratio=1080p",
            candidates,
        )

    def test_get_video_candidates_keeps_ratio_priority_with_bitrate_items(self):
        candidates = get_video_candidates({
            "bit_rate": [
                {
                    "bit_rate": 1200000,
                    "play_addr": {
                        "url_list": ["https://aweme.snssdk.com/aweme/v1/playwm/?video_id=abc&ratio=720p"]
                    },
                }
            ]
        })

        self.assertEqual(
            candidates[0],
            "https://aweme.snssdk.com/aweme/v1/play/?video_id=abc&ratio=4k",
        )

    def test_get_media_headers_uses_douyin_headers_for_douyin_media(self):
        self.assertIs(
            get_media_headers("https://v3-web.douyinvod.com/video.mp4"),
            DOUYIN_HEADERS,
        )


class BilibiliAdapterTests(SimpleTestCase):
    @patch("xhs_download.services.platforms.bilibili.request_get_with_retry")
    def test_parse_url_returns_video_asset_from_public_apis(self, mock_get):
        view_response = Mock()
        view_response.json.return_value = {
            "code": 0,
            "data": {
                "bvid": "BV1xx411c7mD",
                "aid": 2,
                "title": "测试标题",
                "pic": "https://i0.hdslb.com/bfs/archive/cover.jpg",
                "owner": {"name": "作者"},
                "pages": [{"cid": 62131, "page": 1, "part": "P1"}],
            },
        }
        view_response.close = Mock()

        play_response = Mock()
        play_response.json.return_value = {
            "code": 0,
            "data": {
                "quality": 80,
                "durl": [
                    {
                        "order": 2,
                        "size": 100,
                        "url": "https://upos-sz-mirrorcosov.bilivideo.com/upgcxcode/low.mp4",
                    },
                    {
                        "order": 1,
                        "size": 200,
                        "url": "https://upos-sz-mirrorcosov.bilivideo.com/upgcxcode/high.mp4",
                    },
                ],
            },
        }
        play_response.close = Mock()
        mock_get.side_effect = [view_response, play_response]

        result = BilibiliAdapter().parse_url("https://www.bilibili.com/video/BV1xx411c7mD/")

        self.assertEqual(result.platform, "bilibili")
        self.assertEqual(result.post_type, "video")
        self.assertEqual(result.base_filename, "作者_测试标题")
        self.assertEqual(len(result.video_assets), 2)
        self.assertEqual(
            result.video_assets[0].url,
            "https://upos-sz-mirrorcosov.bilivideo.com/upgcxcode/high.mp4",
        )
        self.assertEqual(
            result.video_assets[1].url,
            "https://upos-sz-mirrorcosov.bilivideo.com/upgcxcode/low.mp4",
        )
        self.assertEqual(result.video_assets[0].size_bytes, 200)
        self.assertEqual(result.video_assets[1].size_bytes, 100)
        self.assertEqual(result.video_assets[0].cover_url, "https://i0.hdslb.com/bfs/archive/cover.jpg")
        self.assertIn("x/web-interface/view?bvid=BV1xx411c7mD", mock_get.call_args_list[0].args[0])
        self.assertIn("x/player/playurl?", mock_get.call_args_list[1].args[0])

    @patch("xhs_download.services.platforms.bilibili.request_get_with_retry")
    def test_parse_url_resolves_b23_short_link(self, mock_get):
        resolve_response = Mock()
        resolve_response.url = "https://www.bilibili.com/video/BV1xx411c7mD/?p=1"
        resolve_response.close = Mock()

        view_response = Mock()
        view_response.json.return_value = {
            "code": 0,
            "data": {
                "bvid": "BV1xx411c7mD",
                "aid": 2,
                "title": "测试标题",
                "pic": "//i0.hdslb.com/bfs/archive/cover.jpg",
                "owner": {"name": "作者"},
                "pages": [{"cid": 62131, "page": 1, "part": "P1"}],
            },
        }
        view_response.close = Mock()

        play_response = Mock()
        play_response.json.return_value = {
            "code": 0,
            "data": {
                "quality": 64,
                "durl": [{"size": 100, "url": "//upos-sz-mirrorcosov.bilivideo.com/upgcxcode/video.mp4"}],
            },
        }
        play_response.close = Mock()
        mock_get.side_effect = [resolve_response, view_response, play_response]

        result = BilibiliAdapter().parse_url("https://b23.tv/abc123")

        self.assertEqual(result.video_assets[0].url, "https://upos-sz-mirrorcosov.bilivideo.com/upgcxcode/video.mp4")
        self.assertEqual(result.video_assets[0].cover_url, "https://i0.hdslb.com/bfs/archive/cover.jpg")
        self.assertEqual(mock_get.call_args_list[0].args[0], "https://b23.tv/abc123")

    def test_get_media_headers_uses_bilibili_headers_for_bilibili_media(self):
        self.assertIs(
            get_media_headers("https://upos-sz-mirrorcosov.bilivideo.com/upgcxcode/video.mp4"),
            BILIBILI_HEADERS,
        )


class XhsDownloadArchiveTests(SimpleTestCase):
    @patch("xhs_download.services.downloads.request_get_with_retry")
    def test_build_download_archive_keeps_original_image_bytes_by_default(self, mock_get):
        raw_content = b"raw-webp-content"
        response = Mock()
        response.headers = {"Content-Type": "image/webp", "Content-Length": str(len(raw_content))}
        response.url = "https://sns-img.xhscdn.com/spectrum/a.webp"
        response.iter_content.return_value = [raw_content]
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=None)
        mock_get.return_value = response

        archive = build_download_archive(
            media_items=[{"type": "image", "url": "https://ci.xiaohongshu.com/spectrum/a?imageView2/format/jpg"}],
            base_filename="base",
        )

        with ZipFile(BytesIO(archive["content"])) as zip_file:
            self.assertEqual(zip_file.read("base_1.webp"), raw_content)

        self.assertEqual(archive["saved_count"], 1)
        self.assertEqual(archive["failed_count"], 0)

    @patch("xhs_download.services.downloads.request_get_with_retry")
    def test_build_download_archive_returns_zip(self, mock_get):
        response = Mock()
        response.headers = {"Content-Type": "image/jpeg"}
        response.iter_content.return_value = [make_image_bytes()]
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=None)
        mock_get.return_value = response

        archive = build_download_archive(
            media_items=[{"type": "image", "url": "https://ci.xiaohongshu.com/spectrum/a?imageView2/format/jpg"}],
            base_filename="base",
            save_format="jpg",
        )

        with ZipFile(BytesIO(archive["content"])) as zip_file:
            self.assertIn("base_1.jpg", zip_file.namelist())

        self.assertEqual(archive["saved_count"], 1)
        self.assertEqual(archive["failed_count"], 0)

    def test_resolve_server_save_folder_rejects_absolute_when_disabled(self):
        with self.assertRaisesMessage(Exception, "当前环境不允许保存到绝对路径"):
            resolve_server_save_folder("/tmp/xhs", allow_absolute=False)

    def test_resolve_server_save_folder_keeps_relative_inside_media_root(self):
        with TemporaryDirectory() as tmp_dir:
            with override_settings(MEDIA_ROOT=Path(tmp_dir) / "media", XHS_DEFAULT_SERVER_SAVE_DIR="xhs"):
                folder = resolve_server_save_folder("xhs", allow_absolute=False)

        self.assertTrue(str(folder).endswith("media/xhs"))

    def test_resolve_server_save_folder_rejects_relative_escape(self):
        with TemporaryDirectory() as tmp_dir:
            with override_settings(MEDIA_ROOT=Path(tmp_dir) / "media", XHS_DEFAULT_SERVER_SAVE_DIR="xhs"):
                with self.assertRaisesMessage(Exception, "保存路径不能跳出 MEDIA_ROOT"):
                    resolve_server_save_folder("../outside", allow_absolute=False)

    @patch("xhs_download.services.downloads.request_get_with_retry")
    def test_save_media_items_to_server_writes_file_when_allowed(self, mock_get):
        response = Mock()
        response.headers = {"Content-Type": "image/jpeg"}
        response.iter_content.return_value = [make_image_bytes()]
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=None)
        mock_get.return_value = response

        with TemporaryDirectory() as tmp_dir:
            result = save_media_items_to_server(
                media_items=[{"type": "image", "url": "https://ci.xiaohongshu.com/spectrum/a?imageView2/format/jpg"}],
                base_filename="base",
                save_dir=str(Path(tmp_dir) / "xhs"),
                save_format="jpg",
                allow_absolute=True,
            )

            self.assertEqual(result["saved_count"], 1)
            self.assertTrue(Path(result["saved_files"][0]).exists())


class XhsDownloadViewTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    def test_home_route_renders_download_page(self):
        response = Client().get("/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "星空的高清下载小站")
        self.assertContains(response, "theme-home")
        self.assertContains(response, "nav-settings-btn")
        self.assertContains(response, "css/app.css?v=")
        self.assertContains(response, "js/xhs_download.js?v=")
        self.assertContains(response, "data-download-task-url")

    @override_settings(
        PREVIEW_RATE_LIMIT_PER_MINUTE=1,
        PREVIEW_DAILY_REQUEST_LIMIT_PER_IP=0,
        PREVIEW_GLOBAL_DAILY_REQUEST_LIMIT=0,
        PREVIEW_DAILY_BYTES_LIMIT_PER_IP=0,
        PREVIEW_GLOBAL_DAILY_BYTES_LIMIT=0,
    )
    @patch("xhs_download.views.fetch_media_response")
    def test_preview_proxy_applies_ip_rate_limit(self, mock_fetch):
        mock_fetch.return_value = FakeStreamingResponse(make_image_bytes())
        params = {
            "u": encode_url("https://ci.xiaohongshu.com/spectrum/a.jpg"),
        }

        first = Client().get("/media/proxy-image/", params)
        second = Client().get("/media/proxy-image/", params)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 429)
        self.assertIn("Retry-After", second)
        self.assertEqual(mock_fetch.call_count, 1)

    @override_settings(
        PREVIEW_RATE_LIMIT_PER_MINUTE=0,
        PREVIEW_DAILY_REQUEST_LIMIT_PER_IP=0,
        PREVIEW_GLOBAL_DAILY_REQUEST_LIMIT=0,
        PREVIEW_DAILY_BYTES_LIMIT_PER_IP=1,
        PREVIEW_GLOBAL_DAILY_BYTES_LIMIT=0,
    )
    @patch("xhs_download.views.fetch_media_response")
    def test_preview_proxy_enforces_daily_byte_limit(self, mock_fetch):
        mock_fetch.return_value = FakeStreamingResponse(make_image_bytes())

        response = Client().get(
            "/media/proxy-image/",
            {"u": encode_url("https://ci.xiaohongshu.com/spectrum/a.jpg")},
        )

        self.assertEqual(response.status_code, 429)
        self.assertEqual(
            get_daily_total(
                "preview-bytes-ip",
                get_client_identity(response.wsgi_request),
            ),
            len(make_image_bytes()),
        )

    @patch(
        "xhs_download.views.check_preview_request_limits",
        side_effect=RuntimeError("redis unavailable"),
    )
    def test_preview_proxy_fails_closed_when_cache_is_unavailable(self, _mock_limits):
        response = Client().get(
            "/media/proxy-image/",
            {"u": encode_url("https://ci.xiaohongshu.com/spectrum/a.jpg")},
        )

        self.assertEqual(response.status_code, 503)

    @override_settings(ULTRA_HD_PROVIDER_ENABLED=True, TIKHUB_API_KEY="secret")
    @patch("xhs_download.views.parse_media_page")
    def test_results_passes_ultra_hd_request_flag(self, mock_parse):
        mock_parse.return_value = ParseResult(
            platform="douyin",
            post_type="unknown",
            base_filename="douyin",
            assets=(),
        )

        response = Client().get("/results/", {"q": "https://www.douyin.com/video/123", "ultra_hd": "1"})

        self.assertEqual(response.status_code, 200)
        mock_parse.assert_called_once_with("https://www.douyin.com/video/123", prefer_ultra_hd=True)
        self.assertContains(response, "本次已尝试使用抖音超高清 Provider")
        self.assertContains(response, 'id="ultra-hd-request-input"')
        self.assertContains(response, 'value="1"')

    @override_settings(ULTRA_HD_PROVIDER_ENABLED=True, TIKHUB_API_KEY="secret")
    @patch("xhs_download.views.parse_media_page")
    def test_results_defaults_to_local_provider_without_ultra_hd_flag(self, mock_parse):
        mock_parse.return_value = ParseResult(
            platform="douyin",
            post_type="unknown",
            base_filename="douyin",
            assets=(),
        )

        response = Client().get("/results/", {"q": "https://www.douyin.com/video/123"})

        self.assertEqual(response.status_code, 200)
        mock_parse.assert_called_once_with("https://www.douyin.com/video/123", prefer_ultra_hd=False)
        self.assertContains(response, "feature-card", count=4)
        self.assertContains(response, "无需登录解析去水印，支持视频、图片、批量无水印保存")
        self.assertContains(response, 'placeholder="请将复制的链接粘贴到这里"')
        self.assertContains(response, "paste-link-icon-btn")
        self.assertContains(response, 'id="ultra-hd-request-input"')
        self.assertContains(response, 'id="ultra-hd-setting"')
        self.assertContains(response, "解析质量")
        self.assertContains(response, "默认关闭，关闭时只使用本地解析")
        self.assertNotContains(response, "hero-provider-row")
        self.assertContains(response, "无需登录/注册")
        self.assertContains(response, "打开页面即可解析链接，无使用限制。")
        self.assertContains(response, "原始文件保存")
        self.assertContains(response, "去除水印，保持原始画质。")
        self.assertContains(response, 'action="/results/"')
        self.assertContains(response, 'href="/xhs/"')
        self.assertContains(response, "小红书")
        self.assertContains(response, 'href="/tiktok/"')
        self.assertContains(response, "抖音")
        self.assertContains(response, 'href="/bilibili/"')
        self.assertContains(response, "哔哩哔哩")

    @override_settings(
        ULTRA_HD_PROVIDER_ENABLED=True,
        ULTRA_HD_PUBLIC_ACCESS_ENABLED=False,
        TIKHUB_API_KEY="secret",
    )
    @patch("xhs_download.views.parse_media_page")
    def test_results_ignore_ultra_hd_when_public_access_is_disabled(self, mock_parse):
        mock_parse.return_value = ParseResult(
            platform="douyin",
            post_type="unknown",
            base_filename="douyin",
            assets=(),
        )

        response = Client().get(
            "/results/",
            {"q": "https://www.douyin.com/video/123", "ultra_hd": "1"},
        )

        self.assertEqual(response.status_code, 200)
        mock_parse.assert_called_once_with(
            "https://www.douyin.com/video/123",
            prefer_ultra_hd=False,
        )
        self.assertContains(response, "当前环境未向访客开放超高清 Provider")

    @override_settings(DOWNLOAD_ASYNC_TASKS_ENABLED=False)
    def test_page_omits_async_task_url_when_tasks_are_disabled(self):
        response = Client().get("/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-download-task-url=""')
        self.assertContains(response, 'data-download-async-enabled="false"')

    def test_xhs_route_uses_xhs_results_action(self):
        response = Client().get("/xhs/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "theme-xhs")
        self.assertContains(response, 'action="/xhs/results/"')

    def test_tiktok_route_uses_tiktok_results_action(self):
        response = Client().get("/tiktok/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "theme-tiktok")
        self.assertContains(response, 'action="/tiktok/results/"')

    def test_bilibili_route_uses_bilibili_results_action(self):
        response = Client().get("/bilibili/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "theme-bilibili")
        self.assertContains(response, 'action="/bilibili/results/"')
        self.assertContains(response, "粘贴哔哩哔哩视频链接")

    @patch("xhs_download.views.parse_media_page")
    def test_home_results_keep_home_result_theme(self, mock_parse):
        mock_parse.return_value = DouyinAdapter().parse_aweme({
            "aweme_id": "123",
            "desc": "标题",
            "author": {"nickname": "作者"},
            "images": [{"url_list": ["https://p3-sign.douyinpic.com/image.webp"]}],
        })

        response = Client().get("/results/", {"q": "https://v.douyin.com/abc123/"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "result-theme-home")
        self.assertContains(response, "抖音")
        mock_parse.assert_called_once_with("https://v.douyin.com/abc123/", prefer_ultra_hd=False)

    @patch("xhs_download.views.parse_media_page")
    def test_xhs_route_can_parse_douyin_link(self, mock_parse):
        mock_parse.return_value = DouyinAdapter().parse_aweme({
            "aweme_id": "123",
            "desc": "标题",
            "author": {"nickname": "作者"},
            "images": [{"url_list": ["https://p3-sign.douyinpic.com/image.webp"]}],
        })

        response = Client().get("/xhs/results/", {"q": "https://v.douyin.com/abc123/"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "你粘贴的是抖音链接，已自动使用对应解析器。")
        self.assertContains(response, "抖音")
        self.assertContains(response, "result-theme-xhs")
        mock_parse.assert_called_once_with("https://v.douyin.com/abc123/", prefer_ultra_hd=False)

    @patch("xhs_download.views.parse_media_page")
    def test_bilibili_results_render_video_asset(self, mock_parse):
        mock_parse.return_value = ParseResult(
            platform="bilibili",
            post_type="video",
            base_filename="作者_标题",
            assets=(
                MediaAsset(
                    media_type="video",
                    url="https://upos-sz-mirrorcosov.bilivideo.com/upgcxcode/video.mp4",
                    platform="bilibili",
                    cover_url="https://i0.hdslb.com/bfs/archive/cover.jpg",
                    cover_preview_url="https://i0.hdslb.com/bfs/archive/cover.jpg",
                    size_bytes=1024 * 1024,
                ),
            ),
        )

        response = Client().get("/bilibili/results/", {"q": "https://www.bilibili.com/video/BV1xx411c7mD/"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "视频笔记")
        self.assertContains(response, "哔哩哔哩")
        self.assertContains(response, "已解析到 1 个视频资源")
        self.assertContains(response, "1.0 MB")
        self.assertContains(response, "download-progress-wrap")
        mock_parse.assert_called_once_with("https://www.bilibili.com/video/BV1xx411c7mD/", prefer_ultra_hd=False)

    @patch("xhs_download.views.build_download_archive_file")
    def test_download_endpoint_returns_zip_response(self, mock_archive):
        mock_archive.return_value = {
            "filename": "base.zip",
            "file": BytesIO(b"zip-content"),
            "saved_count": 1,
            "failed_count": 0,
        }

        response = Client().post(
            "/media/download-images/",
            data='{"media_items":[{"type":"image","url":"https://ci.xiaohongshu.com/a"}],"base_filename":"base"}',
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/zip")
        self.assertEqual(response["X-Saved-Count"], "1")

    def test_download_endpoint_rejects_invalid_json(self):
        response = Client().post(
            "/media/download-images/",
            data="{bad",
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)

    def test_download_endpoint_requires_csrf_when_checks_are_enabled(self):
        client = Client(enforce_csrf_checks=True)

        response = client.post(
            "/media/download-images/",
            data='{"media_items":[{"type":"image","url":"https://ci.xiaohongshu.com/a"}]}',
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 403)

    @override_settings(DOWNLOAD_MAX_CONCURRENT_PER_IP=1)
    @patch("xhs_download.views.acquire_concurrency", side_effect=RuntimeError("redis unavailable"))
    def test_download_endpoint_fails_closed_when_concurrency_cache_is_unavailable(self, _mock_acquire):
        response = Client().post(
            "/media/download-images/",
            data='{"media_items":[{"type":"image","url":"https://ci.xiaohongshu.com/a"}]}',
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 503)
        self.assertIn("下载保护服务暂时不可用", response.json()["message"])

    @patch("xhs_download.services.rate_limits.cache.decr", side_effect=RuntimeError("redis unavailable"))
    def test_concurrency_release_cache_failure_does_not_escape(self, _mock_decr):
        with self.assertLogs("xhs_download.services.rate_limits", level="ERROR"):
            release_concurrency("download-concurrent", "client")

    @override_settings(
        DOWNLOAD_RATE_LIMIT_PER_MINUTE=1,
        DOWNLOAD_DAILY_REQUEST_LIMIT_PER_IP=0,
        DOWNLOAD_GLOBAL_DAILY_REQUEST_LIMIT=1,
        DOWNLOAD_MAX_CONCURRENT_PER_IP=0,
        DOWNLOAD_DAILY_BYTES_LIMIT_PER_IP=0,
        DOWNLOAD_GLOBAL_DAILY_BYTES_LIMIT=0,
    )
    @patch("xhs_download.views.build_download_archive_file")
    def test_download_endpoint_applies_ip_rate_limit(self, mock_archive):
        mock_archive.side_effect = [
            {
                "filename": "first.zip",
                "file": BytesIO(b"first"),
                "saved_count": 1,
                "failed_count": 0,
            },
            {
                "filename": "second.zip",
                "file": BytesIO(b"second"),
                "saved_count": 1,
                "failed_count": 0,
            },
        ]
        payload = '{"media_items":[{"type":"image","url":"https://ci.xiaohongshu.com/a"}]}'

        first = Client().post(
            "/media/download-images/",
            data=payload,
            content_type="application/json",
        )
        second = Client().post(
            "/media/download-images/",
            data=payload,
            content_type="application/json",
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 429)
        self.assertIn("Retry-After", second)
        self.assertEqual(mock_archive.call_count, 1)
        self.assertEqual(get_daily_total("download-requests-global", "all"), 1)

    @override_settings(DOWNLOAD_ASYNC_TASKS_ENABLED=False)
    def test_download_task_endpoint_is_disabled_in_production_mode(self):
        response = Client().post(
            "/media/download-tasks/",
            data='{"media_items":[{"type":"image","url":"https://ci.xiaohongshu.com/a"}]}',
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 503)
        self.assertIn("同步下载接口", response.json()["message"])

    def test_server_save_mode_is_forbidden_when_disabled(self):
        with override_settings(XHS_ALLOW_SERVER_SAVE=False):
            response = Client().post(
                "/media/download-images/",
                data='{"download_mode":"server","media_items":[{"type":"image","url":"https://ci.xiaohongshu.com/a"}]}',
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 403)

    @patch("xhs_download.services.downloads.fetch_media_response")
    def test_archive_progress_updates_during_large_file_download(self, mock_fetch):
        content = b"a" * (1024 * 300)
        mock_fetch.return_value = FakeStreamingResponse(content)
        progress_events = []

        archive = build_download_archive_file(
            media_items=[{"type": "image", "url": "https://ci.xiaohongshu.com/a.jpg"}],
            base_filename="base",
            progress_callback=lambda completed, total: progress_events.append((completed, total)),
        )

        try:
            self.assertTrue(any(0 < completed < 1 for completed, _ in progress_events))
            self.assertEqual(progress_events[-1], (1, 1))
        finally:
            archive["file"].close()

    @patch("xhs_download.services.download_tasks.build_download_archive_file")
    def test_download_task_flow_returns_progress_and_file(self, mock_archive):
        archive_file = NamedTemporaryFile(prefix="test_archive_", suffix=".zip")
        archive_file.write(b"zip-content")
        archive_file.seek(0)
        mock_archive.return_value = {
            "filename": "base.zip",
            "file": archive_file,
            "saved_count": 1,
            "failed_count": 0,
        }

        response = Client().post(
            "/media/download-tasks/",
            data=json.dumps({
                "media_items": [{"type": "image", "url": "https://ci.xiaohongshu.com/a"}],
                "base_filename": "base",
            }),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "success")

        status_payload = None
        for _ in range(20):
            status_response = Client().get(payload["status_url"])
            self.assertEqual(status_response.status_code, 200)
            status_payload = status_response.json()
            if status_payload["status"] == "completed":
                break
            time.sleep(0.02)

        self.assertEqual(status_payload["status"], "completed")
        self.assertEqual(status_payload["progress"], 100)
        self.assertEqual(status_payload["saved_count"], 1)

        file_response = Client().get(status_payload["file_url"])
        self.assertEqual(file_response.status_code, 200)
        self.assertEqual(file_response["Content-Type"], "application/zip")
        self.assertEqual(b"".join(file_response.streaming_content), b"zip-content")


class RuntimeHealthViewTests(SimpleTestCase):
    def test_liveness_does_not_depend_on_external_services(self):
        response = Client().get("/health/live/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})
        self.assertEqual(response["Cache-Control"], "no-store")

    @patch("djangoProject.health.get_runtime_readiness")
    def test_readiness_reports_available_components(self, mock_readiness):
        mock_readiness.return_value = {
            "ready": True,
            "components": {"cache": True, "database": True},
        }

        response = Client().get("/health/ready/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ready")
        self.assertEqual(
            response.json()["components"],
            {"cache": True, "database": True},
        )

    @patch("djangoProject.health.get_runtime_readiness")
    def test_readiness_returns_503_without_exposing_error_details(self, mock_readiness):
        mock_readiness.return_value = {
            "ready": False,
            "components": {"cache": False, "database": True},
        }

        response = Client().get("/health/ready/")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["status"], "unavailable")
        self.assertNotIn("error", response.json())


class RuntimeHealthCheckTests(TestCase):
    def test_runtime_readiness_checks_cache_and_database(self):
        from djangoProject.health import get_runtime_readiness

        readiness = get_runtime_readiness()

        self.assertTrue(readiness["ready"])
        self.assertEqual(
            readiness["components"],
            {"cache": True, "database": True},
        )
