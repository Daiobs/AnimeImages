from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch
from zipfile import ZipFile

from django.test import Client, SimpleTestCase, override_settings
from PIL import Image

from .services.common import XhsSecurityError, clean_filename, validate_media_url, validate_page_url
from .services.downloads import (
    build_download_archive,
    normalize_quality,
    normalize_save_format,
    normalize_video_save_mode,
    resolve_server_save_folder,
    save_media_items_to_server,
)
from .services.parser import normalize_xhs_image_url, parse_xhs_html


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
        self.assertEqual(normalize_save_format("jpeg"), "jpg")
        self.assertEqual(normalize_save_format("png"), "png")
        self.assertEqual(normalize_save_format("webp"), "webp")

    def test_normalize_save_format_falls_back_to_jpg(self):
        self.assertEqual(normalize_save_format("bmp"), "jpg")

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


class XhsParserTests(SimpleTestCase):
    def test_normalize_image_url_extracts_spectrum_key(self):
        self.assertEqual(
            normalize_xhs_image_url("https://sns-img.xhscdn.com/spectrum/abc123!webp"),
            "https://ci.xiaohongshu.com/spectrum/abc123?imageView2/format/jpg",
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


class XhsDownloadArchiveTests(SimpleTestCase):
    @patch("xhs_download.services.downloads.request_get_with_retry")
    def test_build_download_archive_returns_zip(self, mock_get):
        response = Mock()
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
    @patch("xhs_download.views.build_download_archive")
    def test_download_endpoint_returns_zip_response(self, mock_archive):
        mock_archive.return_value = {
            "filename": "base.zip",
            "content": b"zip-content",
            "saved_count": 1,
            "failed_count": 0,
        }

        response = Client().post(
            "/xhs_download/download-images/",
            data='{"media_items":[{"type":"image","url":"https://ci.xiaohongshu.com/a"}],"base_filename":"base"}',
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/zip")
        self.assertEqual(response["X-Saved-Count"], "1")

    def test_download_endpoint_rejects_invalid_json(self):
        response = Client().post(
            "/xhs_download/download-images/",
            data="{bad",
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)

    def test_server_save_mode_is_forbidden_when_disabled(self):
        with override_settings(XHS_ALLOW_SERVER_SAVE=False):
            response = Client().post(
                "/xhs_download/download-images/",
                data='{"download_mode":"server","media_items":[{"type":"image","url":"https://ci.xiaohongshu.com/a"}]}',
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 403)
