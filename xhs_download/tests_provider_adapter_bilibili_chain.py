"""Synthetic Native/TianTian raw -> real mappers -> public kernel API.

These are controlled API-shaped inputs, not captured upstream recordings.
The test scheduler executes only the mapper named by a ChainRun Start event.
No fetch, request-path integration, database, or real clock is involved.
"""
from copy import deepcopy
from unittest import TestCase
from unittest.mock import patch

from .services.pipeline import (
    Attempt, ChainConfig, ChainRun, CooldownBook, Failure, FailureKind, Kind,
    Policy, Start, Tracks, Work, validate_work,
)
from .services.provider_adapters.bilibili import BilibiliMapper
from .services.provider_adapters.tiantian import TianTianAdapter
from .tests_provider_adapters import deny_map_io


SOURCE = "https://www.bilibili.com/video/BV13x41117TL/"
COMPLETE_URL = "https://media.bilivideo.com/synthetic-native-complete.mp4"
VIDEO_URL = "https://media.bilivideo.com/synthetic-native-video.m4s"
AUDIO_URL = "https://media.bilivideo.com/synthetic-native-audio.m4s"
FALLBACK_URL = "https://media.bilivideo.com/synthetic-tiantian-complete.mp4"


def native_raw(play):
    return {
        "view": {"code": 0, "data": {
            "bvid": "BV13x41117TL", "aid": 170001,
            "title": "Synthetic Native chain work",
            "pages": [{"page": 1, "cid": 279786, "duration": 12}],
        }},
        "playurl": {"code": 0, "data": play},
        "selection": {
            "page": 1,
            "playurl_params": {"cid": 279786, "bvid": "BV13x41117TL"},
        },
    }


def dash_raw():
    return {
        "video": [{
            "id": 80, "baseUrl": VIDEO_URL, "mimeType": "video/mp4",
            "codecs": "avc1.640028", "width": 1920, "height": 1080,
            "bandwidth": 2000000,
        }],
        "audio": [{
            "id": 30280, "baseUrl": AUDIO_URL, "mimeType": "audio/mp4",
            "codecs": "mp4a.40.2", "bandwidth": 128000,
        }],
    }


def tiantian_raw():
    return {"code": 200, "data": {
        "bvid": "BV13x41117TL", "title": "Synthetic TianTian chain work",
        "videos": [{"url": FALLBACK_URL, "streams": [{
            "url": FALLBACK_URL, "qualityLabel": "720P",
            "sizeInBytes": 12345, "vcodec": "avc1", "acodec": "aac",
        }]}],
    }}


class BilibiliMapperChainTests(TestCase):
    def setUp(self):
        self.native = BilibiliMapper()
        self.fallback = TianTianAdapter()
        self.mappers = {
            self.native.name: self.native,
            self.fallback.name: self.fallback,
        }
        self.config = ChainConfig(
            platform="bilibili", order=(self.native.name, self.fallback.name),
            policy=Policy.FIRST_SUCCESS, budget_seconds=5,
        )
        self.cooldowns = CooldownBook(base_seconds=10, max_seconds=60)
        self.calls = []
        database_guard = patch(
            "django.db.backends.base.base.BaseDatabaseWrapper.ensure_connection",
            side_effect=AssertionError("DB_IO_FORBIDDEN"),
        )
        database_guard.start()
        self.addCleanup(database_guard.stop)
        guard = deny_map_io()
        guard.__enter__()
        self.addCleanup(guard.__exit__, None, None, None)

    def map_started(self, step, raw):
        self.assertEqual(len(step.start), 1)
        self.assertFalse(step.done)
        self.assertIsNone(step.show)
        self.assertIsNone(step.failure)
        start = step.start[0]
        before = deepcopy(raw)
        self.calls.append((start.provider, start.budget_seconds))
        outcome = self.mappers[start.provider].map(raw, SOURCE)
        self.assertEqual(raw, before)
        if isinstance(outcome, Work):
            self.assertEqual(validate_work(outcome), ())
            self.assertEqual((outcome.platform, outcome.provider), ("", ""))
        return outcome

    def assert_fallback_success(self, run, step, at=102):
        self.assertEqual(step.start, (Start(self.fallback.name, 4),))
        outcome = self.map_started(step, tiantian_raw())
        self.assertIsInstance(outcome, Work)
        self.assertEqual(outcome.items[0].default_variant.url, FALLBACK_URL)
        self.assertTrue(outcome.items[0].default_variant.deliverable)
        final = run.on_outcome(self.fallback.name, outcome, at)
        self.assertTrue(final.done)
        self.assertEqual(final.start, ())
        self.assertIsNone(final.failure)
        self.assertIsInstance(final.show, Work)
        self.assertEqual(final.show.items, outcome.items)
        self.assertEqual(final.show.field_sources, outcome.field_sources)
        self.assertEqual(final.show.work_id, "BV13x41117TL")
        self.assertEqual((final.show.platform, final.show.provider),
                         ("bilibili", self.fallback.name))
        self.assertEqual(validate_work(final.show), ())
        self.assertEqual(self.calls,
                         [(self.native.name, 5), (self.fallback.name, 4)])

    def assert_unsupported_fallback(self, raw, code):
        run = ChainRun(self.config, self.cooldowns)
        started = run.start(100)
        self.assertEqual(started.start, (Start(self.native.name, 5),))
        outcome = self.map_started(started, raw)
        self.assertEqual(outcome, Failure(FailureKind.UNSUPPORTED, code))
        next_step = run.on_outcome(self.native.name, outcome, 101)
        self.assertEqual(run.attempts,
                         [Attempt(self.native.name, "unsupported", code)])
        self.assertFalse(self.cooldowns.cooling(self.native.name, 101))
        self.assertEqual(self.cooldowns.snapshot(101), {})
        self.assert_fallback_success(run, next_step)
        self.assertEqual(run.attempts, [
            Attempt(self.native.name, "unsupported", code),
            Attempt(self.fallback.name, "success"),
        ])
        self.assertEqual(self.cooldowns.snapshot(102), {})
        next_run = ChainRun(self.config, self.cooldowns)
        self.assertEqual(next_run.start(103).start,
                         (Start(self.native.name, 5),))

    def test_multi_durl_falls_back_without_work_or_cooldown(self):
        raw = native_raw({"durl": [
            {"order": 1, "url": "https://media.bilivideo.com/synthetic-part-1.flv",
             "length": 6000, "size": 60000},
            {"order": 2, "url": "https://media.bilivideo.com/synthetic-part-2.flv",
             "length": 6000, "size": 60000},
        ]})
        self.assert_unsupported_fallback(raw, "BILIBILI_SEGMENTED_DURL")

    def test_segmented_paths_fall_back_without_cooldown(self):
        for suffix in ("m3u8", "mpd", "m4s"):
            with self.subTest(suffix=suffix):
                self.calls.clear()
                raw = native_raw({"durl": [{
                    "order": 1,
                    "url": f"https://media.bilivideo.com/synthetic-stream.{suffix}?download=1",
                }]})
                self.assert_unsupported_fallback(raw, "BILIBILI_SEGMENTED_PATH")

    def test_separate_tracks_only_fall_back_without_cooldown(self):
        for with_audio in (True, False):
            with self.subTest(with_audio=with_audio):
                self.calls.clear()
                dash = dash_raw()
                if not with_audio:
                    dash["audio"] = []
                self.assert_unsupported_fallback(
                    native_raw({"dash": dash}), "BILIBILI_SEPARATE_TRACKS_ONLY")

    def test_native_unavailable_falls_back_but_cools_then_recovers(self):
        raw = native_raw({})
        raw["playurl_status"] = 412
        raw["playurl"] = {"code": -412, "message": "Synthetic upstream refusal"}
        run = ChainRun(self.config, self.cooldowns)
        outcome = self.map_started(run.start(100), raw)
        self.assertEqual(outcome, Failure(FailureKind.UNAVAILABLE, "HTTP_412"))
        step = run.on_outcome(self.native.name, outcome, 101)
        self.assertTrue(self.cooldowns.cooling(self.native.name, 101))
        self.assertEqual(self.cooldowns.snapshot(101), {self.native.name: 111})
        self.assert_fallback_success(run, step)
        self.assertEqual(run.attempts, [
            Attempt(self.native.name, "unavailable", "HTTP_412"),
            Attempt(self.fallback.name, "success"),
        ])
        self.assertEqual(self.cooldowns.snapshot(102), {self.native.name: 111})
        cooling_run = ChainRun(self.config, self.cooldowns)
        self.assertEqual(cooling_run.start(103).start,
                         (Start(self.fallback.name, 5),))
        self.assertEqual(cooling_run.attempts,
                         [Attempt(self.native.name, "cooling_down")])
        recovered_run = ChainRun(self.config, self.cooldowns)
        self.assertEqual(recovered_run.start(111).start,
                         (Start(self.native.name, 5),))

    def test_complete_durl_with_dash_keeps_tracks_and_never_starts_fallback(self):
        raw = native_raw({
            "quality": 64, "accept_quality": [64],
            "accept_description": ["720P"],
            "durl": [{"order": 1, "url": COMPLETE_URL, "size": 120000}],
            "dash": dash_raw(),
        })
        run = ChainRun(self.config, self.cooldowns)
        outcome = self.map_started(run.start(100), raw)
        self.assertIsInstance(outcome, Work)
        video = next(item for item in outcome.items if item.kind is Kind.VIDEO)
        self.assertEqual(video.default_variant.url, COMPLETE_URL)
        self.assertTrue(video.default_variant.deliverable)
        self.assertIs(video.default_variant.tracks, Tracks.UNKNOWN)
        variants = {variant.url: variant for item in outcome.items
                    for variant in item.variants}
        self.assertEqual(set(variants), {COMPLETE_URL, VIDEO_URL, AUDIO_URL})
        self.assertIs(variants[VIDEO_URL].tracks, Tracks.VIDEO_ONLY)
        self.assertIs(variants[AUDIO_URL].tracks, Tracks.AUDIO_ONLY)
        self.assertTrue(all(variant.merge_with == "" for variant in variants.values()))
        self.assertFalse(variants[VIDEO_URL].deliverable)
        self.assertTrue(variants[VIDEO_URL].segmented)
        self.assertTrue(variants[AUDIO_URL].segmented)
        final = run.on_outcome(self.native.name, outcome, 101)
        self.assertTrue(final.done)
        self.assertEqual(final.start, ())
        self.assertIsNone(final.failure)
        self.assertIsInstance(final.show, Work)
        self.assertEqual(final.show.items, outcome.items)
        self.assertEqual(final.show.field_sources, outcome.field_sources)
        self.assertEqual((final.show.platform, final.show.provider),
                         ("bilibili", self.native.name))
        self.assertEqual(run.attempts, [Attempt(self.native.name, "success")])
        self.assertEqual(self.calls, [(self.native.name, 5)])
        self.assertEqual(self.cooldowns.snapshot(101), {})

    def test_malformed_native_payload_is_invalid_not_unsupported_or_unavailable(self):
        run = ChainRun(self.config, self.cooldowns)
        raw = native_raw({"durl": ["not-a-media-object"]})
        outcome = self.map_started(run.start(100), raw)
        self.assertEqual(outcome, Failure(FailureKind.INVALID_RESULT, "BILIBILI_INVALID_DURL"))
        next_step = run.on_outcome(self.native.name, outcome, 101)
        self.assert_fallback_success(run, next_step)
        self.assertEqual(run.attempts, [
            Attempt(self.native.name, "invalid_result", "BILIBILI_INVALID_DURL"),
            Attempt(self.fallback.name, "success"),
        ])
        self.assertEqual(self.cooldowns.snapshot(102), {})
