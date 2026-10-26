"""Controlled XHS raw evidence boundaries; none of these inputs is a recording.

Native stream field spellings match the installed yt-dlp XHS extractor and
the repository's raw page-state tests. No normalized candidates are inputs.
"""
from copy import deepcopy
from datetime import datetime, timezone
from unittest import TestCase
from unittest.mock import patch

from .services.pipeline import Failure, FailureKind, Fetchable, Kind, Tracks, Work, validate_work
from .services.provider_adapters.xiaohongshu import XiaohongshuMapper
from .tests_provider_adapters import deny_map_io


NOTE_ID = "64a1b2c3d4e5f6789012abcd"
OTHER_ID = "65b2c3d4e5f6789012abcdef"
SOURCE = "https://www.xiaohongshu.com/explore/" + NOTE_ID
IMAGE = "https://sns-webpic-qc.xhscdn.com/notes_pre_post/synthetic-still.webp"
OTHER_IMAGE = "https://sns-webpic-qc.xhscdn.com/notes_pre_post/synthetic-other.webp"
VIDEO = "https://sns-video-bd.xhscdn.com/synthetic-video.mp4"
BACKUP = "https://sns-video-bd.xhscdn.com/synthetic-backup.mp4"
OTHER_VIDEO = "https://sns-video-bd.xhscdn.com/synthetic-other.mp4"


def state(note):
    return {"note": {"noteDetailMap": {NOTE_ID: {"note": note}}}}


def image_note():
    return {
        "noteId": NOTE_ID, "type": "normal", "title": "Controlled note",
        "imageList": [{"urlDefault": IMAGE}],
    }


def video_note(rows):
    return {
        "noteId": NOTE_ID, "type": "video", "title": "Controlled video",
        "video": {"media": {"stream": {"h264": rows}}},
    }


class XhsEvidenceBoundaryTests(TestCase):
    def map(self, raw, source=SOURCE):
        before = deepcopy(raw)
        with deny_map_io(), patch(
            "django.db.backends.base.base.BaseDatabaseWrapper.ensure_connection",
            side_effect=AssertionError("DB_IO_FORBIDDEN"),
        ):
            result = XiaohongshuMapper().map(raw, source)
            self.assertEqual(result, XiaohongshuMapper().map(raw, source))
        self.assertEqual(raw, before)
        if isinstance(result, Work):
            self.assertEqual(validate_work(result), ())
            self.assertEqual((result.platform, result.provider), ("", ""))
        return result

    def assert_invalid(self, result):
        self.assertIsInstance(result, Failure)
        self.assertIs(result.kind, FailureKind.INVALID_RESULT)

    def test_source_final_and_canonical_url_claims_cannot_conflict(self):
        raw_state = state(image_note())
        conflict = SOURCE + "?note_id=" + OTHER_ID
        cases = (
            ("source", raw_state, conflict),
            ("final", {"page_state": raw_state, "page_url": conflict}, SOURCE),
            ("canonical", {
                "page_state": raw_state,
                "html": '<link rel="canonical" href="' + conflict + '">',
            }, SOURCE),
        )
        self.assertIsInstance(self.map(raw_state, SOURCE + "?note_id=" + NOTE_ID), Work)
        for stage, raw, source in cases:
            with self.subTest(stage=stage):
                self.assert_invalid(self.map(raw, source))

    def test_requested_or_final_id_alone_does_not_bind_dom_media(self):
        body = '<div class="note-content"><p>Unbound body</p><img src="' + IMAGE + '"></div>'
        bound = '<link rel="canonical" href="' + SOURCE + '">' + body
        control = self.map(bound)
        self.assertIsInstance(control, Work)
        self.assertEqual(control.items[0].default_variant.url, IMAGE)
        cases = (
            ("request_only", body, SOURCE),
            ("final_only", {"html": body, "page_url": SOURCE}, "https://xhslink.cn/o/synthetic"),
        )
        for label, raw, source in cases:
            with self.subTest(identity=label):
                self.assert_invalid(self.map(raw, source))

    def test_native_stream_facts_do_not_need_normalized_field_names(self):
        row = {
            "masterUrl": VIDEO, "fps": 30, "width": 1920, "height": 1080,
            "videoCodec": "h264", "audioCodec": "aac", "avgBitrate": 2000000,
            "videoBitrate": 1872000, "audioBitrate": 128000,
            "qualityType": "1080P", "duration": 12500, "size": 3125000,
        }
        work = self.map(state(video_note([row])))
        self.assertIsInstance(work, Work)
        item = work.items[0]
        variant = item.default_variant
        self.assertEqual(variant.bitrate, 2000000)
        self.assertEqual(variant.quality_label, "1080P")
        self.assertIs(variant.tracks, Tracks.AV)
        self.assertEqual(work.duration_seconds, 12.5)
        self.assertEqual((variant.width, variant.height, variant.fps, variant.size_bytes),
                         (1920, 1080, 30, 3125000))
        self.assertFalse(variant.is_original)
        prefix = f"items.{item.item_id}.variants.{variant.variant_id}."
        for key in (prefix + "bitrate", prefix + "quality_label", prefix + "tracks", "duration_seconds"):
            self.assertNotIn(work.field_sources[key], ("contract:default", "contract:unknown"))

    def test_live_marker_and_motion_in_different_items_do_not_prove_a_pair(self):
        note = image_note()
        note["imageList"] = [
            {"urlDefault": IMAGE, "livePhoto": True},
            {"urlDefault": OTHER_IMAGE, "livePhoto": False,
             "stream": {"h264": [{"masterUrl": VIDEO}]}},
        ]
        work = self.map(state(note))
        self.assertIsInstance(work, Work)
        self.assertEqual(work.groups, ())
        self.assertEqual([item.kind for item in work.items], [Kind.IMAGE, Kind.IMAGE, Kind.VIDEO])
        self.assertEqual({variant.url for item in work.items for variant in item.variants},
                         {IMAGE, OTHER_IMAGE, VIDEO})

    def test_backup_alias_keeps_missing_context_and_earliest_expiry(self):
        for reverse in (False, True):
            with self.subTest(reverse=reverse):
                restricted = {
                    "masterUrl": VIDEO, "backupUrls": [BACKUP], "expires_at": 1800000000,
                    "headers_present": True, "required_headers": ["Referer"], "method": "POST",
                }
                supplied = {
                    "masterUrl": BACKUP, "expires_at": 1900000000,
                    "headers": {"X-Media-Key": "synthetic-value-not-for-output"}, "method": "GET",
                }
                rows = [supplied, restricted] if reverse else [restricted, supplied]
                rows.append({"masterUrl": OTHER_VIDEO})
                work = self.map(state(video_note(rows)))
                self.assertIsInstance(work, Work)
                aliases = [variant for item in work.items for variant in item.variants
                           if variant.url == BACKUP]
                self.assertTrue(aliases)
                for variant in aliases:
                    self.assertIs(variant.browser_fetchable, Fetchable.FALSE)
                    self.assertEqual(variant.required_headers, ("referer", "x-media-key"))
                    self.assertEqual(variant.request_method, "POST")
                    self.assertEqual(variant.expires_at, datetime.fromtimestamp(1800000000, timezone.utc))
                unrelated = next(variant for item in work.items for variant in item.variants
                                 if variant.url == OTHER_VIDEO)
                self.assertEqual(unrelated.required_headers, ())
                self.assertIs(unrelated.browser_fetchable, Fetchable.UNKNOWN)
                self.assertEqual(unrelated.request_method, "GET")
                self.assertIsNone(unrelated.expires_at)
                self.assertNotIn("synthetic-value-not-for-output", repr(work))
