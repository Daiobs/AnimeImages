from django.test import SimpleTestCase

from .services.platforms.bilibili import (
    BilibiliIncompleteMediaError, get_play_items, play_quality_label,
)


class BilibiliDeliveryTests(SimpleTestCase):
    def test_single_declared_file_preserves_url_size_and_upstream_label(self):
        url = "https://media.bilivideo.com/video.mp4"
        items = get_play_items({
            "quality": 80, "accept_quality": [80], "accept_description": ["1080P 高清"],
            "durl": [{"url": url, "size": 1234}],
        })
        self.assertEqual(items, [{
            "url": url, "size_bytes": 1234, "quality_label": "1080P 高清",
        }])

    def test_quality_id_is_not_a_pixel_height_or_requested_quality(self):
        self.assertEqual(play_quality_label({"quality": 64}), "清晰度编号 64")
        self.assertEqual(play_quality_label({"quality": 80}, 32, height=480), "480p")

    def test_multiple_segments_are_not_independent_complete_videos(self):
        with self.assertRaises(BilibiliIncompleteMediaError) as raised:
            get_play_items({"quality": 80, "durl": [
                {"url": "https://media.bilivideo.com/part1.flv", "order": 1},
                {"url": "https://media.bilivideo.com/part2.flv", "order": 2},
            ]})
        self.assertEqual(raised.exception.delivery_reason, "segmented_file")
        self.assertEqual(raised.exception.formats[0]["segment_count"], 2)

    def test_dash_with_or_without_audio_does_not_issue_download_assets(self):
        for audio in ([], [{"baseUrl": "https://media.bilivideo.com/audio.m4s"}]):
            with self.subTest(audio=bool(audio)):
                with self.assertRaises(BilibiliIncompleteMediaError) as raised:
                    get_play_items({"dash": {"audio": audio, "video": [{
                        "id": 80, "width": 1920, "height": 1080, "codecs": "avc1",
                        "baseUrl": "https://media.bilivideo.com/video.m4s",
                    }]}})
                self.assertEqual(raised.exception.delivery_reason, "separate_tracks")
                self.assertEqual(raised.exception.formats[0]["height"], 1080)
                self.assertEqual(raised.exception.formats[0]["codecs"], "avc1")
                self.assertEqual(raised.exception.formats[0]["separate_audio_count"], len(audio))
                self.assertNotIn("url", raised.exception.formats[0])

    def test_manifest_or_track_url_in_durl_is_not_a_complete_file(self):
        for extension in ("m4s", "m3u8", "mpd"):
            with self.subTest(extension=extension), self.assertRaises(BilibiliIncompleteMediaError):
                get_play_items({"durl": [{
                    "url": f"https://media.bilivideo.com/video.{extension}",
                }]})
