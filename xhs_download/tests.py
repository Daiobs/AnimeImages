from django.test import SimpleTestCase

from .views import clean_filename, normalize_quality, normalize_save_format, normalize_video_save_mode


class XhsUtilityTests(SimpleTestCase):
    def test_clean_filename_removes_invalid_characters(self):
        self.assertEqual(clean_filename('a/b:c*?"<>|.jpg'), "abc.jpg")

    def test_clean_filename_falls_back_when_empty(self):
        self.assertEqual(clean_filename('***'), "xhs_image")

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
