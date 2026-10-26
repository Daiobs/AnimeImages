"""Replay complete TianTian JSON; synthetic coverage is explicitly identified."""
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from unittest import TestCase
from urllib.parse import parse_qs, urlsplit

from .services.pipeline import (
    Completeness, Failure, FailureKind, Fetchable, Kind, Tracks, Work,
    fact_gaps, field_coverage, validate_work,
)
from .services.platforms.bilibili import BILIBILI_MEDIA_HOST_SUFFIXES
from .services.platforms.douyin import DOUYIN_MEDIA_HOST_SUFFIXES
from .services.platforms.xiaohongshu import XHS_MEDIA_HOST_SUFFIXES
from .services.provider_adapters.recordings import check, load_recording, sanitize
from .services.provider_adapters.tiantian import TianTianAdapter
from .services.work_caption import _TOPIC, work_caption_presentation
from .tests_provider_adapters import deny_map_io


FIXTURES = Path(__file__).parent / "fixtures/provider_raw/tiantianjiexi_generic"
NAME = "tiantianjiexi_generic"


class TianTianRecordedMappingTests(TestCase):
    def replay(self, name, *, kind="recorded"):
        document = load_recording(FIXTURES / name)
        self.assertEqual(document["provenance"], {
            "kind": kind, "raw_kind": "json", "complete": True,
        })
        raw = document["raw"]
        original = deepcopy(raw)
        adapter = TianTianAdapter()
        self.assertEqual(adapter.name, NAME)
        with deny_map_io():
            result = adapter.map(raw, document["source_url"])
            repeated = adapter.map(raw, document["source_url"])
        self.assertEqual(result, repeated)
        self.assertEqual(raw, original)
        return document, result

    def assert_work(self, document, work, work_id, description, topics=()):
        self.assertIsInstance(work, Work)
        self.assertEqual(validate_work(work), ())
        self.assertEqual(work.source_url, document["source_url"])
        self.assertEqual(work.work_id, work_id)
        self.assertEqual((work.platform, work.provider), ("", ""))
        self.assertEqual(work.description, description)
        self.assertEqual(work.topics, topics)
        self.assertEqual(work.author, "")
        self.assertEqual(work.cover_url, document["raw"]["data"]["images"][0])
        self.assertIsNone(work.duration_seconds)
        self.assertIs(work.completeness, Completeness.FINAL)
        self.assertEqual(work.groups, ())
        self.assertEqual([item.index for item in work.items], list(range(1, len(work.items) + 1)))
        # These observations have no native ID; provenance must admit URL identity.
        self.assertIn("work_id", work.field_sources)
        self.assertNotEqual(work.field_sources["work_id"], NAME)
        self.assertEqual(work.field_sources["description"], NAME)
        explicit_title = document["raw"]["data"].get("title")
        if explicit_title:
            self.assertEqual(work.title, explicit_title)
            self.assertEqual(work.field_sources["title"], NAME)
        elif work.title:
            # A presentation title may be derived from the body, never invented.
            self.assertIn(work.title, " ".join(description.split()))

    def assert_variant(self, work, item, variant, *, url, containers, height=None,
                       quality="", size=None, original=False, expiry=None, host_policy=None):
        self.assertEqual(variant.url, url)
        self.assertIn(variant.container, containers)
        self.assertEqual(variant.height, height)
        self.assertIsNone(variant.width)
        self.assertIsNone(variant.fps)
        self.assertIsNone(variant.bitrate)
        self.assertEqual(variant.quality_label, quality)
        self.assertEqual(variant.is_original, original)
        self.assertEqual(variant.size_bytes, size)
        self.assertEqual(variant.size_exact, size is not None)
        self.assertIs(variant.tracks, Tracks.UNKNOWN)
        self.assertEqual(variant.merge_with, "")
        self.assertEqual(variant.required_headers, ())
        self.assertIs(variant.browser_fetchable, Fetchable.UNKNOWN)
        self.assertFalse(variant.drm)
        self.assertFalse(variant.segmented)
        self.assertEqual(variant.request_method, "GET")
        self.assertEqual(variant.expires_at, expiry)
        expected_policy = tuple(host_policy) if host_policy is not None else (urlsplit(url).hostname,)
        self.assertEqual(variant.host_policy, expected_policy)
        path = f"items.{item.item_id}.variants.{variant.variant_id}"
        self.assertEqual(work.field_sources[f"{path}.url"], NAME)
        for field, present in (("quality_label", bool(quality)), ("size_bytes", size is not None)):
            if present:
                self.assertEqual(work.field_sources[f"{path}.{field}"], NAME)
        self.assertNotEqual(work.field_sources.get(f"{path}.tracks"), NAME)

    def assert_stills(self, document, work, *, host_policy=None):
        stills = [item for item in work.items if item.kind is Kind.IMAGE]
        urls = document["raw"]["data"]["images"]
        self.assertEqual(len(stills), len(urls))
        for item, url in zip(stills, urls):
            self.assertEqual(len(item.variants), 1)
            suffix = Path(urlsplit(url).path).suffix.lower().lstrip(".")
            containers = ("jpg", "jpeg") if suffix in ("jpg", "jpeg") else (suffix,)
            self.assert_variant(work, item, item.variants[0], url=url, containers=containers,
                                host_policy=host_policy)
            self.assertIs(item.default_variant, item.variants[0])

    def test_recorded_acfun_retains_seven_streams_and_still_without_false_pair(self):
        document, work = self.replay("recorded_acfun.json")
        data = document["raw"]["data"]
        self.assert_work(document, work, "ac37303684", data["desc"])
        self.assertEqual(len(work.items), 2)
        self.assert_stills(document, work)
        video, = [item for item in work.items if item.kind is Kind.VIDEO]
        self.assertEqual(len(video.variants), 7)
        heights = (360, 480, 540, 720, 1080, 1080, 2160)
        for variant, raw, height in zip(video.variants, data["videos"][0]["streams"], heights):
            self.assert_variant(work, video, variant, url=raw["url"], containers=("mp4",),
                                height=height, quality=raw["qualityLabel"], size=raw["sizeInBytes"])
        self.assertEqual(video.default_variant.url, data["videos"][0]["url"])
        self.assertEqual(video.default_variant.height, 2160)
        self.assertEqual(fact_gaps(work), ("TRACKS_UNKNOWN",))

    def test_recorded_douyin_original_is_not_an_invented_height_or_track_fact(self):
        document, work = self.replay("recorded_douyin.json")
        data = document["raw"]["data"]
        description = data["desc"].removeprefix("#\u6768\u8d85\u8d8a").strip()
        self.assert_work(document, work, "6961737553342991651", description, ("\u6768\u8d85\u8d8a",))
        self.assertEqual(len(work.items), 2)
        self.assert_stills(document, work, host_policy=DOUYIN_MEDIA_HOST_SUFFIXES)
        video, = [item for item in work.items if item.kind is Kind.VIDEO]
        self.assertEqual(len(video.variants), 4)
        for variant, raw, height in zip(video.variants, data["videos"][0]["streams"], (540, 720, 1080, None)):
            self.assert_variant(work, video, variant, url=raw["url"], containers=("mp4",),
                                height=height, quality=raw["qualityLabel"], size=raw["sizeInBytes"],
                                original=raw["qualityLabel"] == "\u539f\u753b",
                                host_policy=DOUYIN_MEDIA_HOST_SUFFIXES)
            parsed_url = urlsplit(raw["url"])
            self.assertEqual(Path(parsed_url.path).suffix, "")
            self.assertEqual(parse_qs(parsed_url.query)["mime_type"], ["video_mp4"])
            path = f"items.{video.item_id}.variants.{variant.variant_id}"
            self.assertEqual(work.field_sources[f"{path}.container"], "adapter:url_mime_type")
        self.assertIs(video.default_variant, video.variants[-1])
        self.assertIsNone(video.default_variant.height)
        self.assertEqual(fact_gaps(work), ("TRACKS_UNKNOWN",))

    def test_recorded_bilibili_empty_streams_retain_parent_and_missing_facts(self):
        document, work = self.replay("recorded_bilibili.json")
        data = document["raw"]["data"]
        self.assert_work(document, work, "BV13x41117TL", data["desc"])
        self.assertEqual(len(work.items), 2)
        self.assert_stills(document, work, host_policy=BILIBILI_MEDIA_HOST_SUFFIXES)
        video, = [item for item in work.items if item.kind is Kind.VIDEO]
        self.assertEqual(len(video.variants), 1)
        self.assert_variant(work, video, video.variants[0], url=data["videos"][0]["url"],
                            containers=("mp4",), expiry=datetime.fromtimestamp(1791885949, timezone.utc),
                            host_policy=BILIBILI_MEDIA_HOST_SUFFIXES)
        self.assertIsNone(field_coverage(work)["quality"])
        self.assertIsNone(field_coverage(work)["size"])
        self.assertEqual(fact_gaps(work), ("TRACKS_UNKNOWN",))

    def test_recorded_weibo_preserves_all_nine_stills_without_path_height_inference(self):
        document, work = self.replay("recorded_weibo_gallery.json")
        data = document["raw"]["data"]
        presentation = work_caption_presentation(data.get("title", ""), data["desc"])
        description = _TOPIC.sub("", presentation["description"]).strip()
        # The shared helper is whitespace-boundary based, not paired-hash syntax.
        self.assertEqual(presentation["topics"], ["\u8fd9\u5f88\u5c3c\u5eb7Z63"])
        self.assertTrue(description.startswith("#"))
        self.assertIn("#\u5c3c\u5eb7\u4e91\u521b#", description)
        self.assert_work(document, work, "RkWevk3D3", description,
                         tuple(presentation["topics"]))
        self.assertEqual(work.title, presentation["title"])
        self.assertEqual(len(work.items), 9)
        self.assert_stills(document, work)
        self.assertEqual(fact_gaps(work), ())
        self.assertIsNone(field_coverage(work)["quality"])
        self.assertIsNone(field_coverage(work)["size"])

    def test_recorded_xhs_equal_arrays_preserve_all_items_without_pair_facts(self):
        cases = (
            ("recorded_xhs_unpaired_media.json", 9, 18),
            ("recorded_xhs_unpaired_media_followup.json", 6, 12),
        )
        for filename, media_count, item_count in cases:
            with self.subTest(filename=filename):
                document, work = self.replay(filename)
                data = document["raw"]["data"]
                self.assertEqual(document["raw"]["code"], 200)
                self.assertEqual(set(data), {"desc", "images", "videos"})
                self.assertEqual(len(data["images"]), media_count)
                self.assertTrue(all(isinstance(image, str) for image in data["images"]))
                self.assertEqual(len(data["videos"]), media_count)
                for video in data["videos"]:
                    self.assertEqual(set(video), {"url", "streams"})
                    self.assertEqual(video["streams"], [])
                presentation = work_caption_presentation("", data["desc"])
                description = _TOPIC.sub("", presentation["description"]).strip()
                work_id = urlsplit(document["source_url"]).path.rstrip("/").rsplit("/", 1)[-1]
                self.assert_work(document, work, work_id, description, tuple(presentation["topics"]))
                self.assertEqual(work.title, presentation["title"])
                self.assertEqual(len(work.items), item_count)
                self.assert_stills(document, work, host_policy=XHS_MEDIA_HOST_SUFFIXES)
                videos = [item for item in work.items if item.kind is Kind.VIDEO]
                self.assertEqual(len(videos), media_count)
                for item, raw in zip(videos, data["videos"]):
                    self.assertEqual(len(item.variants), 1)
                    self.assert_variant(work, item, item.variants[0], url=raw["url"], containers=("mp4",),
                                        host_policy=XHS_MEDIA_HOST_SUFFIXES)
                    self.assertIs(item.default_variant, item.variants[0])
                # Equal array lengths and external LivePhoto knowledge are not raw pair facts.
                self.assertEqual(work.groups, ())
                self.assertEqual(fact_gaps(work), ("TRACKS_UNKNOWN",))
                self.assertIsNone(field_coverage(work)["quality"])
                self.assertIsNone(field_coverage(work)["size"])

    def test_synthetic_xhs_ordinary_video_makes_no_livephoto_claim(self):
        document, work = self.replay("synthetic_xhs_video.json", kind="synthetic")
        data = document["raw"]["data"]
        self.assert_work(document, work, "67abcdef0123456789abcdef", data["desc"])
        self.assertEqual(len(work.items), 2)
        self.assert_stills(document, work, host_policy=XHS_MEDIA_HOST_SUFFIXES)
        video, = [item for item in work.items if item.kind is Kind.VIDEO]
        self.assertEqual(len(video.variants), 2)
        for index, (variant, raw) in enumerate(zip(video.variants, data["videos"][0]["streams"])):
            self.assert_variant(work, video, variant, url=raw["url"], containers=("mp4",),
                                quality=raw["qualityLabel"], size=raw["sizeInBytes"], original=index == 0,
                                host_policy=XHS_MEDIA_HOST_SUFFIXES)
        self.assertIs(video.default_variant, video.variants[0])
        self.assertEqual(fact_gaps(work), ("TRACKS_UNKNOWN",))

    def test_synthetic_xhs_gallery_preserves_order_and_unknown_size(self):
        document, work = self.replay("synthetic_xhs_gallery.json", kind="synthetic")
        self.assert_work(document, work, "68abcdef0123456789abcdef", document["raw"]["data"]["desc"])
        self.assertEqual(len(work.items), 2)
        self.assert_stills(document, work, host_policy=XHS_MEDIA_HOST_SUFFIXES)
        self.assertEqual(fact_gaps(work), ())

    def test_synthetic_business_failures_1001_and_unknown_stay_unavailable(self):
        for filename in ("synthetic_failure_1001.json", "synthetic_failure_unknown.json"):
            with self.subTest(filename=filename):
                document, result = self.replay(filename, kind="synthetic")
                self.assertIsInstance(result, Failure)
                self.assertIs(result.kind, FailureKind.UNAVAILABLE)
                self.assertRegex(result.code, r"^[A-Z][A-Z0-9_]{0,63}$")
                self.assertNotIn(document["raw"]["msg"], result.code)

    def test_all_envelopes_pass_p2_check_and_remain_idempotently_sanitized(self):
        paths = sorted(FIXTURES.glob("*.json"))
        self.assertEqual(len(paths), 10)
        for path in paths:
            with self.subTest(path=path.name):
                document = load_recording(path)
                check(document)
                self.assertEqual(sanitize(document), document)
                self.assertEqual(document["provider"], NAME)
                expected_kind = "recorded" if path.name.startswith("recorded_") else "synthetic"
                self.assertEqual(document["provenance"]["kind"], expected_kind)
