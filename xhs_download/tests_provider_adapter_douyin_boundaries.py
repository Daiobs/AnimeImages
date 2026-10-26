"""Synthetic raw context boundaries beyond the main Douyin mapper fixtures.

These cases are controlled declarations, not recordings of Native responses.
They do not define content-item selection or add a Work-level audio default.
"""
from copy import deepcopy
from unittest import TestCase
from unittest.mock import patch

from .services.pipeline import Fetchable, Kind, Tracks, Work, validate_work
from .services.provider_adapters.douyin import DouyinMapper
from .tests_provider_adapters import deny_map_io


WORK_ID = "7324602766213598501"
SOURCE = "https://www.douyin.com/video/" + WORK_ID
MEDIA = "https://v3-web.douyinvod.com/synthetic-primary.mp4"
ALIAS = "https://v3-web.douyinvod.com/synthetic-alias.mp4"


class DouyinContextBoundaryTests(TestCase):
    def raw(self):
        return {
            "aweme_id": WORK_ID,
            "video": {"play_addr": {"url_list": [MEDIA], "height": 720}},
        }

    def map(self, raw):
        before = deepcopy(raw)
        with deny_map_io(), patch(
            "django.db.backends.base.base.BaseDatabaseWrapper.ensure_connection",
            side_effect=AssertionError("DB_IO_FORBIDDEN"),
        ):
            work = DouyinMapper().map(raw, SOURCE)
            self.assertEqual(work, DouyinMapper().map(raw, SOURCE))
        self.assertEqual(raw, before)
        self.assertIsInstance(work, Work)
        self.assertEqual(validate_work(work), ())
        self.assertEqual((work.platform, work.provider), ("", ""))
        return work

    def test_missing_context_alias_is_not_filled_by_another_observation(self):
        for reverse in (False, True):
            with self.subTest(reverse=reverse):
                raw = self.raw()
                missing = {"play_addr": {
                    "url_list": [ALIAS], "headers_present": True,
                    "headers": {"X-Access": None},
                }}
                supplied = {"play_addr": {
                    "url_list": [ALIAS],
                    "headers": {"X-Media-Key": "synthetic-value-not-for-output"},
                }}
                raw["video"]["bit_rate"] = (
                    [supplied, missing] if reverse else [missing, supplied]
                )
                work = self.map(raw)
                aliases = [variant for item in work.items for variant in item.variants
                           if variant.url == ALIAS]
                self.assertTrue(aliases)
                for variant in aliases:
                    self.assertIs(variant.browser_fetchable, Fetchable.FALSE)
                    self.assertEqual(variant.required_headers, ("x-media-key",))
                primary = next(variant for item in work.items for variant in item.variants
                               if variant.url == MEDIA)
                self.assertIs(primary.browser_fetchable, Fetchable.UNKNOWN)
                self.assertEqual(primary.required_headers, ())
                self.assertNotIn("synthetic-value-not-for-output", repr(work))

    def test_url_scoped_audio_fact_does_not_reclassify_another_address(self):
        raw = self.raw()
        raw["video"].update(
            url=ALIAS, tracks="audio_only",
            download_addr={"url_list": [ALIAS]},
        )
        work = self.map(raw)
        video_items = [item for item in work.items if item.kind is Kind.VIDEO]
        audio_items = [item for item in work.items if item.kind is Kind.AUDIO]
        self.assertEqual(len(video_items), 1)
        self.assertEqual(len(audio_items), 1)
        primary = video_items[0].default_variant
        self.assertEqual(primary.url, MEDIA)
        self.assertIs(primary.tracks, Tracks.UNKNOWN)
        audio = audio_items[0].default_variant
        self.assertEqual(audio.url, ALIAS)
        self.assertIs(audio.tracks, Tracks.AUDIO_ONLY)
        self.assertEqual(primary.merge_with, "")

    def test_restricted_higher_quality_format_is_not_the_video_default(self):
        for field, restricted, local_default in (
            ("drm", True, False),
            ("segmented", True, False),
            ("method", "POST", "GET"),
        ):
            with self.subTest(field=field):
                raw = self.raw()
                raw["video"]["bit_rate"] = [{
                    field: restricted,
                    "play_addr": {
                        "url_list": [ALIAS], "height": 2160,
                        "is_original": True, field: local_default,
                    },
                }]
                work = self.map(raw)
                item = work.items[0]
                high = next(variant for variant in item.variants if variant.url == ALIAS)
                self.assertTrue(high.is_original)
                self.assertEqual(high.height, 2160)
                self.assertFalse(high.deliverable)
                self.assertEqual(getattr(high, "request_method" if field == "method" else field),
                                 restricted)
                self.assertEqual(item.default_variant.url, MEDIA)
                self.assertTrue(item.default_variant.deliverable)
