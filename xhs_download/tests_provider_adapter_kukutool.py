"""Synthetic raw mapping counterexamples, not real-provider acceptance."""
from copy import deepcopy
from unittest import TestCase

from .services.pipeline import Failure, FailureKind, Fetchable, Kind, Tracks, Work, validate_work
from .services.platforms.douyin import DOUYIN_MEDIA_HOST_SUFFIXES
from .services.provider_adapters.kukutool import KukutoolAdapter
from .tests_provider_adapters import deny_map_io


SOURCE = "https://www.douyin.com/video/6961737553342991651"


class KukutoolMappingBoundaryTests(TestCase):
    def setUp(self):
        self.raw = {"title": "Synthetic work", "type": "video", "videos": [{
            "url": "https://cdn.example/high.mp4", "video_fullinfo": [
                {"url": "https://cdn.example/high.mp4", "type": "1080P+", "size": 2000},
                {"url": "https://cdn.example/source.webm", "type": "原画", "size": 3000},
            ],
        }], "url": "https://cdn.example/high.mp4"}

    def map(self, raw=None, source=SOURCE):
        raw = self.raw if raw is None else raw
        before = deepcopy(raw)
        with deny_map_io():
            work = KukutoolAdapter().map(raw, source)
            self.assertEqual(work, KukutoolAdapter().map(raw, source))
        self.assertEqual(raw, before)
        if isinstance(work, Work):
            self.assertEqual(validate_work(work), ())
            self.assertEqual((work.platform, work.provider, work.groups), ("", "", ()))
        return work

    def test_root_alias_original_and_explicit_height_labels(self):
        work = self.map()
        self.assertEqual(len(work.items), 1)
        item = work.items[0]
        self.assertEqual(len(item.variants), 2)
        self.assertEqual(item.variants[0].height, 1080)
        self.assertIsNone(item.variants[1].height)
        self.assertIs(item.default_variant, item.variants[1])
        self.assertEqual([v.container for v in item.variants], ["mp4", "webm"])
        self.assertTrue(all(v.tracks is Tracks.UNKNOWN for v in item.variants))

    def test_labels_do_not_invent_dimensions(self):
        row = self.raw["videos"][0]["video_fullinfo"][0]
        for label, expected in (("2160P", 2160), ("1080P+", 1080), ("4K", None), ("高清", None), ("video", None)):
            with self.subTest(label=label):
                row["type"] = label
                variant = self.map().items[0].variants[0]
                self.assertEqual(variant.height, expected)
                self.assertIsNone(variant.width)
                if label == "video":
                    self.assertEqual(variant.display_label, "默认清晰度")

    def test_multiple_videos_keep_ownership_and_no_artificial_pair(self):
        self.raw["videos"].append({"url": "https://cdn.example/second.mp4", "video_fullinfo": []})
        self.raw["pics"] = ["https://cdn.example/still.jpg", "https://cdn.example/other.jpg"]
        work = self.map()
        self.assertEqual([i.kind for i in work.items], [Kind.VIDEO, Kind.VIDEO, Kind.IMAGE, Kind.IMAGE])
        self.assertEqual([len(i.variants) for i in work.items], [2, 1, 1, 1])

    def test_distinct_labels_preserve_shared_url_restrictions(self):
        rows = self.raw["videos"][0]["video_fullinfo"]
        rows[0].update(has_drm=True, method="POST", headers={"Cookie": "secret"})
        rows.append({"url": rows[0]["url"], "type": "2160P"})
        variants = self.map().items[0].variants
        self.assertEqual(len(variants), 3)
        for variant in (variants[0], variants[2]):
            self.assertTrue(variant.drm)
            self.assertEqual(variant.request_method, "POST")
            self.assertEqual(variant.required_headers, ("cookie",))
            self.assertFalse(variant.deliverable)
        self.assertEqual(variants[1].required_headers, ())

    def test_drm_segmented_post_and_unpaired_video_remain_visible(self):
        rows = self.raw["videos"][0]["video_fullinfo"]
        rows.extend([
            {"url": "https://cdn.example/a.m3u8", "type": "2160P"},
            {"url": "https://cdn.example/b.mp4", "method": "POST", "body": "private-body"},
            {"url": "https://cdn.example/c.mp4", "vcodec": "h264", "acodec": "none"},
        ])
        item = self.map().items[0]
        self.assertEqual(len(item.variants), 5)
        self.assertEqual([v.deliverable for v in item.variants[2:]], [False, False, False])
        self.assertNotIn("private-body", repr(item))
        self.assertEqual(item.variants[-1].merge_with, "")

    def test_shared_url_retains_known_tracks_across_quality_aliases(self):
        rows = self.raw["videos"][0]["video_fullinfo"]
        rows[0].update(vcodec="h264", acodec="none")
        rows.append({"url": rows[0]["url"], "type": "原画"})
        item = self.map().items[0]
        for variant in (item.variants[0], item.variants[2]):
            self.assertIs(variant.tracks, Tracks.VIDEO_ONLY)
            self.assertFalse(variant.deliverable)
        self.assertIs(item.default_variant, item.variants[1])
        rows[-1]["tracks"] = "av"
        self.assertIs(self.map().kind, FailureKind.INVALID_RESULT)

    def test_root_format_list_is_a_video_with_all_its_variants(self):
        rows = deepcopy(self.raw["videos"][0]["video_fullinfo"])
        raw = {"video_fullinfo": rows, "url": rows[0]["url"]}
        for with_root_url in (True, False):
            with self.subTest(with_root_url=with_root_url):
                if not with_root_url:
                    raw.pop("url")
                item, = self.map(raw=raw).items
                self.assertEqual([v.url for v in item.variants], [row["url"] for row in rows])
                self.assertEqual([v.quality_label for v in item.variants], ["1080P+", "原画"])
                self.assertIs(item.default_variant, item.variants[1])
        raw["videos"] = [{"url": "https://cdn.example/other.mp4"}]
        work = self.map(raw=raw)
        self.assertEqual([len(item.variants) for item in work.items], [2, 1])

    def test_explicit_codecs_dimensions_and_estimated_size(self):
        row = self.raw["videos"][0]["video_fullinfo"][0]
        row.update(width=1280, height=720, fps=60, bitrate=1200000,
                   size="1.5 MiB", vcodec="h264", acodec="aac")
        variant = self.map().items[0].variants[0]
        self.assertEqual((variant.width, variant.height, variant.fps, variant.bitrate), (1280, 720, 60, 1200000))
        self.assertEqual(variant.size_bytes, 1572864)
        self.assertFalse(variant.size_exact)
        self.assertIs(variant.tracks, Tracks.AV)

    def test_ordinary_header_values_do_not_make_platform_attachment_only(self):
        self.raw["headers"] = {"Referer": "https://www.bilibili.com/", "Origin": "https://www.bilibili.com", "User-Agent": "UA", "Accept": "*/*"}
        item = self.map(source="https://www.bilibili.com/video/BV13x41117TL/").items[0]
        self.assertTrue(all(v.required_headers == () for v in item.variants))
        self.assertTrue(all(v.browser_fetchable is Fetchable.UNKNOWN for v in item.variants))

    def test_host_policy_and_identity_have_honest_sources(self):
        work = self.map()
        self.assertEqual(work.work_id, "6961737553342991651")
        self.assertEqual(work.field_sources["work_id"], "request:work_id")
        self.assertTrue(all(v.host_policy == tuple(DOUYIN_MEDIA_HOST_SUFFIXES) for v in work.items[0].variants))
        self.raw["id"] = "other"
        self.assertIs(self.map().kind, FailureKind.INVALID_RESULT)
        work = self.map(source="https://source.example/opaque")
        self.assertEqual(work.items[0].variants[0].host_policy, ("cdn.example",))
        self.assertEqual(work.field_sources["work_id"], "kukutool_generic")

    def test_no_id_does_not_use_url_hash_or_old_normalized_identity(self):
        self.assertIs(self.map(source="https://v.douyin.com/opaque").kind, FailureKind.INVALID_RESULT)
        for key in ("_sidecar", "_retained_video_fullinfo", "_service_format_rows"):
            with self.subTest(key=key):
                self.assertIs(self.map(raw={**self.raw, key: []}).kind, FailureKind.INVALID_RESULT)

    def test_image_and_audio_arrays_are_independent_items(self):
        work = self.map(raw={"pics": ["https://cdn.example/a.webp"], "audios": [{"url": "https://cdn.example/a.flac", "tracks": "audio_only"}]})
        self.assertEqual([i.kind for i in work.items], [Kind.IMAGE, Kind.AUDIO])
        self.assertEqual([i.default_variant.container for i in work.items], ["webp", "flac"])
        self.assertIs(work.items[1].default_variant.tracks, Tracks.AUDIO_ONLY)

    def test_image_root_alias_and_distinct_array_entries_are_all_preserved(self):
        raw = {"type": "image", "url": "https://cdn.example/a.jpg", "size": 123,
               "pics": ["https://cdn.example/a.jpg"],
               "images": ["https://cdn.example/b.webp"],
               "audios": ["https://cdn.example/a.mp3"],
               "audio_url": "https://cdn.example/b.flac"}
        work = self.map(raw=raw)
        self.assertEqual([i.default_variant.url for i in work.items],
                         [raw["url"], raw["images"][0], raw["audios"][0], raw["audio_url"]])
        self.assertEqual(work.items[0].default_variant.size_bytes, 123)

    def test_sidecar_verified_size_is_an_observed_fact(self):
        work = self.map(raw={"contract_version": "douyin-source-resolver.v1", "status": "ok",
                             "best": {"url": "https://cdn.example/a.mp4", "verified_size": 1234}})
        variant = work.items[0].default_variant
        self.assertEqual(variant.size_bytes, 1234)
        self.assertTrue(variant.size_exact)

    def test_sidecar_raw_best_is_alias_not_a_separate_video(self):
        raw = {"contract_version": "douyin-source-resolver.v1", "status": "ok", "aweme_id": "6961737553342991651",
               "best": {"url": "https://cdn.example/raw.mp4", "final_url": "https://cdn.example/final.mp4", "type": "1080P", "size": 1000},
               "resources": [{"url": "https://cdn.example/final.mp4", "type": "1080P"},
                             {"url": "https://cdn.example/low.mp4", "type": "720P"}]}
        item = self.map(raw=raw).items[0]
        self.assertEqual(len(item.variants), 2)
        self.assertEqual(item.default_variant.url, raw["best"]["final_url"])
        self.assertEqual(item.default_variant.size_bytes, 1000)

    def test_sidecar_does_not_filter_non_bytecdn_resources(self):
        work = self.map(raw={"contract_version": "douyin-source-resolver.v1", "status": "ok",
                             "resources": [{"url": "https://cdn.example/a.mp4", "type": "720P"}]})
        self.assertIsInstance(work, Work)
        self.assertEqual(work.items[0].default_variant.host_policy, tuple(DOUYIN_MEDIA_HOST_SUFFIXES))

    def test_failures_have_no_unproven_deleted_semantics(self):
        for raw in ({"code": 1001, "msg": "已删除"}, {"status": -1},
                    {"contract_version": "douyin-source-resolver.v1", "status": "failed", "reason": "deleted"}):
            self.assertIs(self.map(raw=raw).kind, FailureKind.UNAVAILABLE)
        for raw in ({}, {"videos": [None]}, {"contract_version": "v2", "status": "ok"}, []):
            self.assertIs(self.map(raw=raw).kind, FailureKind.INVALID_RESULT)

    def test_shared_caption_preserves_body_topics_without_filename_decision(self):
        title = "简单几分钟调出高光比通透感"
        body = "先调整画面的整体曝光，再处理高光和阴影，让人物与背景之间保持自然的明暗层次。" * 2
        self.raw["title"] = f"{title} {body} #3分钟学会拍照"
        work = self.map()
        self.assertEqual((work.title, work.description, work.topics), (title, body, ("3分钟学会拍照",)))
