from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from django.core.cache import cache
from django.test import SimpleTestCase, override_settings

from .services import download_handoff, downloads
from .services.common import XhsDownloadError
from .services.platforms.bilibili_service_result import build_service_result
from .views import build_single_attachment_response


MEDIA = "https://upos-sz-mirrorcos.bilivideo.com/video.mp4"
TRACK = MEDIA.replace(".mp4", ".m4s")
SOURCE = "https://www.bilibili.com/video/BV1xx411c7mD/"


@override_settings(CACHES={"default": {
    "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
    "LOCATION": "bilibili-download-completeness-tests",
}})
class BilibiliDownloadCompletenessTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        for target in ("socket.getaddrinfo", "requests.sessions.Session.request"):
            guard = patch(target, side_effect=AssertionError("network forbidden"))
            guard.start()
            self.addCleanup(guard.stop)

    def response(self, url=MEDIA, content_type="video/mp4"):
        response = Mock(url=url, headers={"Content-Type": content_type})
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.iter_content.return_value = [b"\x00\x00\x00\x18ftypisom"]
        return response

    def test_explicit_cdn_stream_suffixes_rejected_before_transfer(self):
        with patch.object(downloads, "fetch_media_response") as fetch:
            for host in ("bilivideo.com", "mirror.bilivideo.cn", "media.hdslb.com"):
                for suffix in (".m4s", ".MPD", ".m3u8"):
                    url = f"https://{host}/video{suffix}?token=example"
                    item = {"type": "video", "url": url}
                    for consumer in (
                        lambda: downloads.fetch_item_to_temp_path(item),
                        lambda: downloads.fetch_to_temp_path(url, expected_type="video"),
                        lambda: downloads.fetch_binary(url, expected_type="video"),
                    ):
                        with self.subTest(url=url, consumer=consumer):
                            with self.assertRaisesMessage(XhsDownloadError, "完整视频"):
                                consumer()
            fetch.assert_not_called()

    def test_check_is_limited_to_video_path_suffix_on_bili_hosts(self):
        for url, media_type in (
            (MEDIA, "video"),
            (MEDIA + "?backup=track.m4s", "video"),
            (MEDIA.replace(".mp4", ".flv"), "video"),
            ("https://example.com/video.m4s", "video"),
            ("https://bilivideo.com.example.com/video.m4s", "video"),
            (TRACK, "image"),
        ):
            with self.subTest(url=url, media_type=media_type):
                downloads.validate_bilibili_video_completeness(url, media_type)

    def test_direct_attachment_rejected_before_transfer(self):
        with patch("xhs_download.views.fetch_media_response") as fetch, patch(
            "xhs_download.views.single_attachment_request_timeout", return_value=30
        ):
            with self.assertRaisesMessage(XhsDownloadError, "完整视频"):
                build_single_attachment_response({"type": "video", "url": TRACK}, "video")
            fetch.assert_not_called()

    def test_handoff_issuance_rejects_without_url_probe(self):
        with patch.object(download_handoff, "validate_media_url") as validate:
            with self.assertRaisesMessage(download_handoff.DownloadHandoffError, "完整视频"):
                download_handoff.issue_download_handoff_tokens(
                    item={"type": "video", "url": TRACK},
                    base_filename="video",
                    client_identity="test",
                )
            validate.assert_not_called()

    def test_old_handoff_cache_is_rechecked(self):
        with patch.object(download_handoff, "validate_media_url"):
            tokens = download_handoff.issue_download_handoff_tokens(
                item={"type": "video", "url": MEDIA},
                base_filename="video",
                client_identity="test",
            )
            ticket_id = download_handoff._load_signed_ticket(
                tokens.stream_token, salt=download_handoff.DOWNLOAD_HANDOFF_SALT
            )
            key = download_handoff.build_download_handoff_cache_key(ticket_id)
            record = cache.get(key)
            record["item"]["url"] = TRACK
            cache.set(key, record, timeout=60)
            with self.assertRaisesMessage(download_handoff.DownloadHandoffError, "完整视频"):
                download_handoff.load_download_handoff(
                    tokens.stream_token, client_identity="test"
                )

    def test_archive_and_server_save_cannot_report_track_success(self):
        item = {"type": "video", "url": TRACK}
        with patch.object(downloads, "fetch_media_response") as fetch:
            with self.assertRaisesMessage(XhsDownloadError, "完整视频"):
                downloads.build_download_archive_file([item], "video")
            with TemporaryDirectory() as folder:
                with self.assertRaisesMessage(XhsDownloadError, "完整视频"):
                    downloads.save_media_items_to_server(
                        [item], "video", save_dir=folder, allow_absolute=True
                    )
                self.assertEqual([path.name for path in Path(folder).iterdir()], ["failed.txt"])
            fetch.assert_not_called()

    def test_native_mp4_still_saves_and_archives_without_new_credentials(self):
        item = {"type": "video", "url": MEDIA}
        with patch.object(
            downloads, "fetch_media_response", side_effect=lambda *a, **kw: self.response()
        ):
            with TemporaryDirectory() as folder:
                result = downloads.save_media_items_to_server(
                    [item], "video", save_dir=folder, allow_absolute=True
                )
                self.assertEqual(result["saved_count"], 1)
                self.assertTrue(Path(result["saved_files"][0]).is_file())
            archive = downloads.build_download_archive_file([item], "video")
            try:
                self.assertEqual(archive["saved_count"], 1)
                self.assertEqual(archive["failed_count"], 0)
            finally:
                archive["file"].close()

    def test_cover_only_save_does_not_reject_the_parent_track(self):
        cover = "https://i0.hdslb.com/cover.jpg"
        item = {"type": "video", "url": TRACK, "cover_url": cover}
        with patch.object(
            downloads, "fetch_media_response", return_value=self.response(cover, "image/jpeg")
        ) as fetch, TemporaryDirectory() as folder:
            result = downloads.save_media_items_to_server(
                [item], "video", save_dir=folder, allow_absolute=True, video_save_mode="cover"
            )
            self.assertEqual(result["saved_count"], 1)
            self.assertEqual(fetch.call_args.args[0], cover)

    def test_complete_service_item_keeps_manifest_validation(self):
        with patch(
            "xhs_download.services.upstream_candidates.validate_public_download_url",
            side_effect=lambda url, *a, **kw: url,
        ):
            item = build_service_result({"video": MEDIA}, SOURCE).video_assets[0].to_download_item()
        self.assertTrue(downloads.provider_manifest_allows_public_download(item))
        with patch.object(downloads, "fetch_media_response", return_value=self.response()) as fetch:
            result = downloads.fetch_item_to_temp_path(item, expected_type="video")
            try:
                self.assertEqual(fetch.call_args.kwargs["provider_signed"], item)
            finally:
                Path(result["path"]).unlink(missing_ok=True)
        self.assertFalse(downloads.provider_manifest_allows_public_download({
            **item, "provider_manifest_token": "invalid",
        }))
