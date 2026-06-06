from io import BytesIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch
from zipfile import ZipFile

from django.test import Client, SimpleTestCase, override_settings
from PIL import Image

from .services.common import (
    MEDIA_HOST_SUFFIXES,
    XhsSecurityError,
    clean_filename,
    request_get_with_retry,
    validate_media_url,
    validate_page_url,
)
from .services.downloads import (
    build_download_archive,
    get_media_headers,
    normalize_quality,
    normalize_save_format,
    normalize_video_save_mode,
    resolve_server_save_folder,
    save_media_items_to_server,
)
from .services.platforms import DouyinAdapter, XiaohongshuAdapter
from .services.platforms.douyin import DOUYIN_HEADERS, extract_aweme_id, get_video_candidates
from .services.parser import normalize_xhs_image_url, normalize_xhs_preview_image_url, parse_xhs_html
from .services.registry import extract_url_from_text, get_adapter_for_url


def make_image_bytes():
    buffer = BytesIO()
    Image.new("RGB", (10, 8), (255, 0, 0)).save(buffer, "JPEG")
    return buffer.getvalue()


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

    @patch("xhs_download.services.platforms.douyin.request_get_with_retry")
    def test_parse_url_falls_back_to_mobile_share_page(self, mock_get):
        desktop_response = Mock()
        desktop_response.status_code = 200
        desktop_response.url = "https://www.douyin.com/video/7324602766213598501"
        desktop_response.text = "<html><body></body></html>"
        desktop_response.close = Mock()

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
        mock_get.side_effect = [desktop_response, mobile_response]

        result = DouyinAdapter().parse_url("https://www.douyin.com/video/7324602766213598501")

        self.assertEqual(result.post_type, "video")
        self.assertEqual(len(result.video_assets), 1)
        self.assertIn("/share/video/7324602766213598501/", mock_get.call_args_list[1].args[0])
        desktop_response.close.assert_called_once()
        mobile_response.close.assert_called_once()

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
    def test_home_route_renders_download_page(self):
        response = Client().get("/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "星空的高清下载小站")
        self.assertContains(response, "theme-home")
        self.assertContains(response, "nav-settings-btn")
        self.assertContains(response, "css/app.css?v=")
        self.assertContains(response, "js/xhs_download.js?v=")
        self.assertContains(response, "feature-card", count=4)
        self.assertContains(response, "无需登录解析去水印，支持视频、图片、批量无水印保存")
        self.assertContains(response, 'placeholder="请将复制的链接粘贴到这里"')
        self.assertContains(response, "paste-link-icon-btn")
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
        mock_parse.assert_called_once_with("https://v.douyin.com/abc123/")

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
        mock_parse.assert_called_once_with("https://v.douyin.com/abc123/")

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

    def test_server_save_mode_is_forbidden_when_disabled(self):
        with override_settings(XHS_ALLOW_SERVER_SAVE=False):
            response = Client().post(
                "/media/download-images/",
                data='{"download_mode":"server","media_items":[{"type":"image","url":"https://ci.xiaohongshu.com/a"}]}',
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 403)
