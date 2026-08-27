import tempfile
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

from django.test import SimpleTestCase

from .services.common import XhsDownloadError
from .services.downloads import (
    ARCHIVE_PROFILE_BATCH_IMAGES,
    build_download_archive_file,
    resolve_temp_image_format,
    validate_response_content_type,
)
from .services.download_tasks import (
    create_download_task,
    get_download_task,
    pop_download_task,
)
from .services.image_formats import resolve_image_format


JPEG = b"\xff\xd8\xff\xe0jpeg"
PNG = b"\x89PNG\r\n\x1a\npng"
GIF = b"GIF89agif"
WEBP = b"RIFF\x04\x00\x00\x00WEBPwebp"
AVIF = (
    b"\x00\x00\x00\x18ftypavif"
    b"\x00\x00\x00\x00avifmif1"
)
HEIC = (
    b"\x00\x00\x00\x18ftypheic"
    b"\x00\x00\x00\x00heicmif1"
)


class ImageFormatResolutionTests(SimpleTestCase):
    def test_magic_signatures_cover_supported_raster_formats(self):
        cases = (
            (JPEG, "jpg", "image/jpeg"),
            (PNG, "png", "image/png"),
            (GIF, "gif", "image/gif"),
            (WEBP, "webp", "image/webp"),
            (AVIF, "avif", "image/avif"),
            (HEIC, "heic", "image/heic"),
        )
        for content, extension, mime_type in cases:
            with self.subTest(extension=extension):
                result = resolve_image_format(
                    content,
                    content_type="application/octet-stream",
                )
                self.assertTrue(result.valid)
                self.assertEqual(result.extension, extension)
                self.assertEqual(result.mime_type, mime_type)
                self.assertEqual(result.confidence, "magic")

    def test_magic_wins_over_conflicting_content_type(self):
        result = resolve_image_format(PNG, content_type="image/jpeg")

        self.assertEqual(result.extension, "png")
        self.assertTrue(result.metadata_mismatch)

    def test_specific_content_type_and_safe_url_suffix_are_fallbacks(self):
        from_mime = resolve_image_format(b"binary", content_type="image/webp")
        from_url = resolve_image_format(
            b"binary",
            content_type="application/octet-stream",
            url="https://ci.xiaohongshu.com/path/image.avif?variant=controlled",
        )

        self.assertEqual(from_mime.extension, "webp")
        self.assertEqual(from_mime.confidence, "content_type")
        self.assertEqual(from_url.extension, "avif")
        self.assertEqual(from_url.confidence, "url_suffix")

    def test_generic_unknown_does_not_become_jpg(self):
        result = resolve_image_format(
            b"\x01\x02\x03",
            content_type="application/octet-stream",
            url="https://ci.xiaohongshu.com/suffixless",
        )

        self.assertFalse(result.valid)
        self.assertEqual(result.error_code, "IMAGE_FORMAT_UNDETERMINED")
        self.assertEqual(result.extension, "")

    def test_generic_binary_mime_reaches_bounded_image_magic_detection(self):
        for content_type in (
            "application/octet-stream",
            "binary/octet-stream",
            "application/binary",
            "application/download",
        ):
            with self.subTest(content_type=content_type):
                validate_response_content_type(content_type, "image")
                result = resolve_image_format(PNG, content_type=content_type)
                self.assertEqual(result.extension, "png")
                self.assertEqual(result.confidence, "magic")

    def test_html_json_xml_and_svg_are_invalid(self):
        for content in (
            b"<!doctype html>",
            b'{"error": true}',
            b"<?xml version='1.0'?><error/>",
            b"<svg xmlns='http://www.w3.org/2000/svg'/>",
        ):
            with self.subTest(content=content[:12]):
                result = resolve_image_format(
                    content,
                    content_type="image/jpeg",
                )
                self.assertEqual(result.error_code, "IMAGE_CONTENT_INVALID")


class ServerArchiveImageFormatTests(SimpleTestCase):
    def build_archive(self, bodies):
        paths = []
        index = 0

        def fake_fetch(_item, **_kwargs):
            nonlocal index
            body = bodies[index]
            index += 1
            handle = tempfile.NamedTemporaryFile(delete=False)
            handle.write(body)
            handle.close()
            paths.append(handle.name)
            return {
                "path": handle.name,
                "content_type": "application/octet-stream",
                "final_url": f"https://ci.xiaohongshu.com/resource-{index}",
                "size": len(body),
            }

        items = [
            {"type": "image", "url": f"https://ci.xiaohongshu.com/resource-{item_index}"}
            for item_index in range(len(bodies))
        ]
        try:
            with patch(
                "xhs_download.services.downloads.fetch_item_to_temp_path",
                side_effect=fake_fetch,
            ):
                return build_download_archive_file(
                    items,
                    "mixed",
                    archive_profile=ARCHIVE_PROFILE_BATCH_IMAGES,
                )
        finally:
            for path in paths:
                Path(path).unlink(missing_ok=True)

    def test_mixed_suffixless_octet_stream_archive_uses_magic_extensions(self):
        bodies = [PNG, JPEG, WEBP, GIF, AVIF, HEIC]
        archive = self.build_archive(bodies)
        try:
            with ZipFile(archive["file"]) as bundle:
                self.assertEqual(
                    bundle.namelist(),
                    [
                        "mixed_01.png",
                        "mixed_02.jpg",
                        "mixed_03.webp",
                        "mixed_04.gif",
                        "mixed_05.avif",
                        "mixed_06.heic",
                    ],
                )
                self.assertEqual(
                    [bundle.read(name) for name in bundle.namelist()],
                    bodies,
                )
        finally:
            archive["file"].close()

    def test_unknown_archive_item_is_reported_without_a_fake_jpg(self):
        archive = self.build_archive([PNG, b"unknown-binary"])
        try:
            with ZipFile(archive["file"]) as bundle:
                self.assertEqual(bundle.namelist(), ["mixed_01.png", "failed.txt"])
                self.assertIn(
                    "IMAGE_FORMAT_UNDETERMINED",
                    bundle.read("failed.txt").decode("utf-8"),
                )
        finally:
            archive["file"].close()

    def test_all_unknown_archive_items_fail_instead_of_creating_empty_success(self):
        with self.assertRaisesRegex(XhsDownloadError, "没有资源保存成功"):
            self.build_archive([b"unknown-binary"])

    def test_temp_resolver_logs_metadata_mismatch_without_url(self):
        handle = tempfile.NamedTemporaryFile(delete=False)
        handle.write(PNG)
        handle.close()
        try:
            with self.assertLogs(
                "xhs_download.services.downloads",
                level="WARNING",
            ) as captured:
                result = resolve_temp_image_format({
                    "path": handle.name,
                    "content_type": "image/jpeg",
                    "final_url": "https://ci.xiaohongshu.com/private?auth=redacted",
                })
            self.assertEqual(result.extension, "png")
            self.assertNotIn("ci.xiaohongshu.com", "\n".join(captured.output))
            self.assertNotIn("auth=redacted", "\n".join(captured.output))
        finally:
            Path(handle.name).unlink(missing_ok=True)

    def test_async_archive_uses_same_resolver_and_reports_artifact_size(self):
        paths = []
        bodies = [PNG, JPEG]
        index = 0

        def fake_fetch(_item, **_kwargs):
            nonlocal index
            body = bodies[index]
            index += 1
            handle = tempfile.NamedTemporaryFile(delete=False)
            handle.write(body)
            handle.close()
            paths.append(handle.name)
            return {
                "path": handle.name,
                "content_type": "application/octet-stream",
                "final_url": f"https://ci.xiaohongshu.com/resource-{index}",
                "size": len(body),
            }

        class ImmediateThread:
            def __init__(self, *, target, args, daemon):
                self.target = target
                self.args = args

            def start(self):
                self.target(*self.args)

        task = None
        try:
            with (
                patch(
                    "xhs_download.services.downloads.fetch_item_to_temp_path",
                    side_effect=fake_fetch,
                ),
                patch(
                    "xhs_download.services.download_tasks.threading.Thread",
                    ImmediateThread,
                ),
            ):
                created = create_download_task(
                    {
                        "media_items": [
                            {
                                "type": "image",
                                "url": "https://ci.xiaohongshu.com/a",
                            },
                            {
                                "type": "image",
                                "url": "https://ci.xiaohongshu.com/b",
                            },
                        ],
                        "base_filename": "async",
                        "archive_profile": ARCHIVE_PROFILE_BATCH_IMAGES,
                        "download_runtime_limits": {
                            "single_archive_bytes_limit": 0,
                            "daily_bytes_limit_per_ip": 0,
                            "global_daily_bytes_limit": 0,
                        },
                    },
                    "test-owner",
                )
                task = get_download_task(created["task_id"])

            self.assertEqual(task["status"], "completed")
            self.assertGreater(task["size_bytes"], 0)
            with ZipFile(task["file"]) as bundle:
                self.assertEqual(
                    bundle.namelist(),
                    ["async_01.png", "async_02.jpg"],
                )
                self.assertEqual(bundle.read("async_01.png"), PNG)
                self.assertEqual(bundle.read("async_02.jpg"), JPEG)
        finally:
            if task and task.get("task_id"):
                popped = pop_download_task(task["task_id"])
                if popped and popped.get("file"):
                    popped["file"].close()
            for path in paths:
                Path(path).unlink(missing_ok=True)
