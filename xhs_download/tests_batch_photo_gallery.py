import tempfile
from io import BytesIO
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

from django.template.loader import render_to_string
from django.test import Client, SimpleTestCase, TestCase, override_settings

from .services.assets import MediaAsset, ParseResult
from .services.common import decode_url, encode_url
from .services.downloads import (
    ARCHIVE_PROFILE_BATCH_IMAGES,
    build_download_archive_file,
)
from .services.rate_limits import LimitResult
from .views import build_result_display_context


def gallery_row(count, *, platform="xiaohongshu"):
    image_items = []
    for index in range(1, count + 1):
        url = f"https://ci.xiaohongshu.com/notes_pre_post/image-{index}.jpg"
        image_items.append({
            "type": "image",
            "url": url,
            "encoded_url": encode_url(url),
            "encoded_preview_url": encode_url(url),
            "encoded_card_preview_url": encode_url(url),
            "encoded_lightbox_preview_url": encode_url(url),
            "download_extension": "jpg",
            "download_strategy": "browser_direct",
        })
    return {
        "status": "success",
        "source_text": "source",
        "platform": platform,
        "platform_name": "小红书",
        "post_type": "image",
        "base_filename": "gallery",
        "image_items": image_items,
        "video_items": [],
        "duration_ms": 10,
    }


def render_gallery(count, *, download_allowed=True, raw_allowed=False):
    return render_to_string(
        "partials/batch_result_card.html",
        {
            "row": gallery_row(count),
            "batch_download_allowed": download_allowed,
            "batch_raw_links_allowed": raw_allowed,
            "proxy_image_url": "/media/proxy-image/",
        },
    )


class BatchPhotoGalleryRenderTests(SimpleTestCase):
    def test_renders_every_image_for_three_five_and_ten_item_rows(self):
        for count in (3, 5, 10):
            with self.subTest(count=count):
                html = render_gallery(count)
                self.assertEqual(html.count("data-batch-photo-item"), count)
                self.assertIn(f"图片 {count}", html)
                self.assertNotIn('slice:":4"', html)

    def test_multi_image_public_row_has_individual_and_download_all_actions(self):
        html = render_gallery(5, download_allowed=True, raw_allowed=False)

        self.assertEqual(html.count("data-batch-image-download"), 5)
        self.assertEqual(html.count("data-batch-photo-download-all"), 1)
        self.assertNotIn("复制直链", html)
        self.assertNotIn("打开直链", html)

    def test_photo_cards_share_preview_metadata_and_action_layout_structure(self):
        html = render_gallery(3, download_allowed=True)

        self.assertEqual(html.count('class="batch-photo-item"'), 3)
        self.assertEqual(html.count('class="batch-photo-preview"'), 3)
        self.assertEqual(html.count('class="batch-photo-meta"'), 3)
        self.assertEqual(html.count('class="batch-photo-actions"'), 3)

    def test_single_image_row_does_not_duplicate_download_all_action(self):
        html = render_gallery(1, download_allowed=True, raw_allowed=False)

        self.assertEqual(html.count("data-batch-image-download"), 1)
        self.assertNotIn("data-batch-photo-download-all", html)

    def test_staff_row_retains_raw_actions(self):
        html = render_gallery(3, download_allowed=True, raw_allowed=True)

        self.assertEqual(html.count("复制直链"), 3)
        self.assertEqual(html.count("打开直链"), 3)
        self.assertIn("data-batch-photo-download-all", html)

    def test_xhs_context_separates_card_lightbox_and_download_candidates(self):
        download_url = "https://ci.xiaohongshu.com/notes_pre_post/original-image"
        result = ParseResult(
            platform="xiaohongshu",
            post_type="image",
            base_filename="gallery",
            assets=(
                MediaAsset(
                    media_type="image",
                    url=download_url,
                    platform="xiaohongshu",
                    ext="png",
                ),
            ),
        )

        item = build_result_display_context(result)["image_items"][0]

        self.assertEqual(item["url"], download_url)
        self.assertIn("/w/1280/format/webp", item["card_preview_url"])
        self.assertIn("/w/1600/format/webp", item["lightbox_preview_url"])
        self.assertEqual(decode_url(item["encoded_url"]), download_url)
        self.assertEqual(item["download_extension"], "png")

    def test_non_xhs_context_keeps_platform_neutral_preview(self):
        download_url = "https://i0.hdslb.com/image.jpg"
        preview_url = "https://i0.hdslb.com/preview.jpg"
        result = ParseResult(
            platform="bilibili",
            post_type="image",
            base_filename="gallery",
            assets=(
                MediaAsset(
                    media_type="image",
                    url=download_url,
                    preview_url=preview_url,
                    platform="bilibili",
                ),
            ),
        )

        item = build_result_display_context(result)["image_items"][0]

        self.assertEqual(item["card_preview_url"], preview_url)
        self.assertEqual(item["lightbox_preview_url"], preview_url)
        self.assertEqual(item["url"], download_url)


class BatchPhotoArchiveTests(SimpleTestCase):
    def build_archive(self, content_types, *, base_filename="post", failure_index=0):
        bodies = [f"image-{index}".encode() for index in range(1, len(content_types) + 1)]
        paths = []
        call_index = 0

        def fake_fetch(_item, **_kwargs):
            nonlocal call_index
            call_index += 1
            if call_index == failure_index:
                raise RuntimeError("controlled upstream failure")
            handle = tempfile.NamedTemporaryFile(delete=False)
            handle.write(bodies[call_index - 1])
            handle.close()
            paths.append(handle.name)
            return {
                "path": handle.name,
                "content_type": content_types[call_index - 1],
                "final_url": f"https://ci.xiaohongshu.com/item-{call_index}",
                "size": len(bodies[call_index - 1]),
            }

        items = [
            {"type": "image", "url": f"https://ci.xiaohongshu.com/item-{index}"}
            for index in range(1, len(content_types) + 1)
        ]
        try:
            with patch(
                "xhs_download.services.downloads.fetch_item_to_temp_path",
                side_effect=fake_fetch,
            ):
                archive = build_download_archive_file(
                    items,
                    base_filename,
                    archive_profile=ARCHIVE_PROFILE_BATCH_IMAGES,
                )
            return archive, bodies
        finally:
            for path in paths:
                Path(path).unlink(missing_ok=True)

    def test_batch_archive_preserves_order_extensions_and_exact_bytes(self):
        archive, bodies = self.build_archive(
            ["image/jpeg", "image/png", "image/webp", "image/gif", "image/avif"],
        )
        try:
            with ZipFile(archive["file"]) as bundle:
                self.assertEqual(bundle.namelist(), [
                    "post_01.jpg",
                    "post_02.png",
                    "post_03.webp",
                    "post_04.gif",
                    "post_05.avif",
                ])
                self.assertEqual(
                    [bundle.read(name) for name in bundle.namelist()],
                    bodies,
                )
                self.assertTrue(
                    all(
                        info.date_time != (1980, 1, 1, 0, 0, 0)
                        for info in bundle.infolist()
                    )
                )
            self.assertEqual(archive["filename"], "post_images.zip")
            self.assertEqual(archive["saved_count"], 5)
            self.assertEqual(archive["failed_count"], 0)
        finally:
            archive["file"].close()

    def test_batch_archive_partial_failure_keeps_successes_and_failure_report(self):
        archive, _bodies = self.build_archive(
            ["image/jpeg"] * 5,
            failure_index=3,
        )
        try:
            with ZipFile(archive["file"]) as bundle:
                self.assertEqual(bundle.namelist(), [
                    "post_01.jpg",
                    "post_02.jpg",
                    "post_04.jpg",
                    "post_05.jpg",
                    "failed.txt",
                ])
                self.assertIn("image #3", bundle.read("failed.txt").decode())
            self.assertEqual(archive["saved_count"], 4)
            self.assertEqual(archive["failed_count"], 1)
        finally:
            archive["file"].close()

    def test_batch_archive_sanitizes_path_control_and_long_title(self):
        archive, _bodies = self.build_archive(
            ["image/jpeg"],
            base_filename="../\\bad:\x00 title🔥" + ("x" * 300),
        )
        try:
            with ZipFile(archive["file"]) as bundle:
                name = bundle.namelist()[0]
                self.assertNotIn("/", name)
                self.assertNotIn("\\", name)
                self.assertNotIn("\x00", name)
                self.assertTrue(name.endswith("_01.jpg"))
                self.assertLessEqual(len(name), 170)
            self.assertTrue(archive["filename"].endswith("_images.zip"))
        finally:
            archive["file"].close()

    def test_default_archive_profile_keeps_existing_partial_failure_contract(self):
        media_items = [
            {"type": "image", "url": "https://ci.xiaohongshu.com/one.jpg"},
            {"type": "unsupported", "url": "https://ci.xiaohongshu.com/two.jpg"},
        ]
        handle = tempfile.NamedTemporaryFile(delete=False)
        handle.write(b"image-one")
        handle.close()
        try:
            with patch(
                "xhs_download.services.downloads.fetch_item_to_temp_path",
                return_value={
                    "path": handle.name,
                    "content_type": "image/jpeg",
                    "final_url": media_items[0]["url"],
                    "size": len(b"image-one"),
                },
            ):
                archive = build_download_archive_file(media_items, "post")
            try:
                with ZipFile(archive["file"]) as bundle:
                    self.assertEqual(bundle.namelist(), ["post_1.jpg", "failed.txt"])
                self.assertEqual(archive["saved_count"], 1)
                self.assertEqual(archive["failed_count"], 1)
            finally:
                archive["file"].close()
        finally:
            Path(handle.name).unlink(missing_ok=True)


class BatchPhotoArchiveSecurityTests(TestCase):
    def assert_rejected_without_network(self, url):
        with patch("xhs_download.services.downloads.requests.get") as mock_get:
            response = Client().post(
                "/media/download-images/",
                data={
                    "media_items": [{"type": "image", "url": url}],
                    "base_filename": "post",
                    "download_mode": "zip",
                    "archive_profile": ARCHIVE_PROFILE_BATCH_IMAGES,
                },
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 400)
        mock_get.assert_not_called()

    def test_sync_archive_rejects_ssrf_and_unapproved_hosts_before_network(self):
        for url in (
            "http://127.0.0.1/image.jpg",
            "http://169.254.169.254/latest/meta-data/",
            "file:///etc/passwd",
            "https://evil.example/image.jpg",
        ):
            with self.subTest(url=url):
                self.assert_rejected_without_network(url)

    @override_settings(DOWNLOAD_ASYNC_TASKS_ENABLED=True)
    def test_async_archive_rejects_non_image_payload_before_task_creation(self):
        with patch("xhs_download.views.create_download_task") as mock_create:
            response = Client().post(
                "/media/download-tasks/",
                data={
                    "media_items": [{
                        "type": "video",
                        "url": "https://v5-default.365yg.com/video.mp4",
                    }],
                    "archive_profile": ARCHIVE_PROFILE_BATCH_IMAGES,
                },
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 400)
        mock_create.assert_not_called()

    @override_settings(DOWNLOAD_ASYNC_TASKS_ENABLED=True)
    @patch("xhs_download.views.acquire_concurrency", return_value=LimitResult(allowed=True))
    @patch("xhs_download.views.check_download_request_limits", return_value=("client", None))
    @patch("xhs_download.views.create_download_task")
    def test_async_archive_forwards_batch_profile_to_existing_task_backend(
        self,
        mock_create,
        _mock_limits,
        _mock_concurrency,
    ):
        mock_create.return_value = {
            "task_id": "task-id",
            "status": "queued",
            "progress": 0,
            "message": "等待开始",
            "filename": "",
            "saved_count": 0,
            "failed_count": 0,
        }
        media_items = [
            {"type": "image", "url": "https://ci.xiaohongshu.com/one.jpg"},
            {"type": "image", "url": "https://ci.xiaohongshu.com/two.jpg"},
        ]

        response = Client().post(
            "/media/download-tasks/",
            data={
                "media_items": media_items,
                "base_filename": "post",
                "archive_profile": ARCHIVE_PROFILE_BATCH_IMAGES,
            },
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        payload = mock_create.call_args.args[0]
        self.assertEqual(payload["media_items"], media_items)
        self.assertEqual(payload["archive_profile"], ARCHIVE_PROFILE_BATCH_IMAGES)
        self.assertEqual(mock_create.call_args.kwargs["owner_identity"], "client")

    @patch("xhs_download.services.platforms.xiaohongshu.XiaohongshuAdapter.parse_url")
    @patch("xhs_download.views.build_download_archive_file")
    def test_sync_archive_reuses_download_backend_without_provider_reparse(
        self,
        mock_archive,
        mock_parse,
    ):
        mock_archive.return_value = {
            "filename": "post_images.zip",
            "file": BytesIO(b"zip-content"),
            "saved_count": 2,
            "failed_count": 0,
        }
        media_items = [
            {"type": "image", "url": "https://ci.xiaohongshu.com/one.jpg"},
            {"type": "image", "url": "https://ci.xiaohongshu.com/two.jpg"},
        ]

        response = Client().post(
            "/media/download-images/",
            data={
                "media_items": media_items,
                "base_filename": "post",
                "download_mode": "zip",
                "archive_profile": ARCHIVE_PROFILE_BATCH_IMAGES,
            },
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/zip")
        self.assertEqual(response["X-Saved-Count"], "2")
        self.assertEqual(response["X-Failed-Count"], "0")
        self.assertEqual(
            mock_archive.call_args.kwargs["archive_profile"],
            ARCHIVE_PROFILE_BATCH_IMAGES,
        )
        self.assertEqual(mock_archive.call_args.kwargs["media_items"], media_items)
        mock_parse.assert_not_called()
