"""Pure TianTian mapping boundaries, using explicitly synthetic raw JSON."""
from copy import deepcopy
from unittest import TestCase

from .services.pipeline import Failure, FailureKind, Fetchable, Kind, Tracks, Work, validate_work
from .services.platforms.bilibili import BILIBILI_MEDIA_HOST_SUFFIXES
from .services.platforms.douyin import DOUYIN_MEDIA_HOST_SUFFIXES
from .services.platforms.third_party import THIRD_PARTY_PLATFORM_RULES
from .services.platforms.xiaohongshu import XHS_MEDIA_HOST_SUFFIXES
from .services.provider_adapters.tiantian import TianTianAdapter
from .tests_provider_adapters import deny_map_io


SOURCE = "https://www.douyin.com/video/6961737553342991651"


class TianTianMappingBoundaryTests(TestCase):
    def setUp(self):
        self.raw = {"code": 200, "data": {"title": "Synthetic work", "videos": [{
            "url": "https://cdn.example/original.mp4", "streams": [
                {"url": "https://cdn.example/original.mp4", "qualityLabel": "原画", "sizeInBytes": 42000},
                {"url": "https://cdn.example/1080.mp4", "qualityLabel": "1080P+", "sizeInBytes": 21000},
            ],
        }]}}

    def map(self, raw=None, source=SOURCE):
        raw = self.raw if raw is None else raw
        before = deepcopy(raw)
        with deny_map_io():
            result = TianTianAdapter().map(raw, source)
            self.assertEqual(result, TianTianAdapter().map(raw, source))
        self.assertEqual(raw, before)
        if isinstance(result, Work):
            self.assertEqual(validate_work(result), ())
        return result

    def test_source_modal_identity_and_raw_conflict(self):
        work = self.map(source="https://www.douyin.com/jingxuan?modal_id=6961737553342991651")
        self.assertEqual(work.work_id, "6961737553342991651")
        self.raw["data"]["aweme_id"] = "other"
        self.assertIs(self.map().kind, FailureKind.INVALID_RESULT)

    def test_opaque_source_needs_upstream_identity_not_a_url_hash(self):
        self.assertEqual(self.map(source="https://v.douyin.com/opaque/"),
                         Failure(FailureKind.INVALID_RESULT, "TIANTIAN_MISSING_WORK_ID"))
        self.raw["data"]["aweme_id"] = "6961737553342991651"
        work = self.map(source="https://v.douyin.com/opaque/")
        self.assertEqual(work.work_id, "6961737553342991651")
        self.assertEqual(work.field_sources["work_id"], "tiantianjiexi_generic")

    def test_normalized_payload_and_old_objects_are_not_raw(self):
        self.raw["data"]["_service_format_rows"] = []
        self.assertIs(self.map().kind, FailureKind.INVALID_RESULT)
        self.assertIs(self.map(raw=("candidate", "token")).kind, FailureKind.INVALID_RESULT)

    def test_same_url_labels_and_multiple_videos_are_not_collapsed(self):
        first = self.raw["data"]["videos"][0]
        first["streams"].append({**first["streams"][1], "qualityLabel": "1080P", "fps": 60})
        self.raw["data"]["videos"].append({"url": "https://cdn.example/second.webm"})
        work = self.map()
        self.assertEqual(len(work.items), 2)
        self.assertEqual([len(item.variants) for item in work.items], [3, 1])
        self.assertEqual(work.items[0].variants[2].fps, 60)
        self.assertEqual(len({v.variant_id for i in work.items for v in i.variants}), 4)

    def test_unknown_tracks_and_explicit_p_labels_only(self):
        streams = self.raw["data"]["videos"][0]["streams"]
        for label, height in (("1080P", 1080), ("2160P", 2160), ("1080P+", 1080),
                              ("4K", None), ("高清", None), ("原画", None)):
            with self.subTest(label=label):
                streams[1]["qualityLabel"] = label
                variant = self.map().items[0].variants[1]
                self.assertEqual(variant.height, height)
                self.assertIs(variant.tracks, Tracks.UNKNOWN)
                self.assertIs(variant.browser_fetchable, Fetchable.UNKNOWN)
                self.assertEqual(variant.merge_with, "")

    def test_explicit_dimensions_and_codecs_are_not_overwritten_by_label(self):
        row = self.raw["data"]["videos"][0]["streams"][1]
        row.update(height=720, width=1280, vcodec="h264", acodec="aac", bitrate=2000000)
        variant = self.map().items[0].variants[1]
        self.assertEqual((variant.height, variant.width, variant.bitrate), (720, 1280, 2000000))
        self.assertIs(variant.tracks, Tracks.AV)

    def test_drm_segmented_and_post_variants_remain_but_are_not_deliverable(self):
        rows = self.raw["data"]["videos"][0]["streams"]
        rows.extend([
            {"url": "https://cdn.example/drm.mp4", "has_drm": True, "qualityLabel": "2160P"},
            {"url": "https://cdn.example/list.m3u8", "qualityLabel": "2160P"},
            {"url": "https://cdn.example/post.mp4", "method": "POST", "body": {"secret": "DO_NOT_PROJECT"}},
            {"url": "https://cdn.example/only-video.mp4", "vcodec": "avc1", "acodec": "none"},
        ])
        item = self.map().items[0]
        self.assertEqual(len(item.variants), 6)
        self.assertTrue(item.default_variant.deliverable)
        self.assertEqual([variant.deliverable for variant in item.variants[2:]], [False] * 4)
        self.assertNotIn("DO_NOT_PROJECT", repr(item))
        self.assertEqual(item.variants[-1].merge_with, "")

    def test_ordinary_header_values_do_not_force_bilibili_delivery(self):
        self.raw["data"]["headers"] = {"User-Agent": "UA", "Accept": "*/*", "Referer": "https://www.bilibili.com/", "Origin": "https://www.bilibili.com"}
        work = self.map(source="https://www.bilibili.com/video/BV13x41117TL/")
        self.assertTrue(all(v.required_headers == () for v in work.items[0].variants))

    def test_credential_header_names_only_with_resource_local_scope(self):
        rows = self.raw["data"]["videos"][0]["streams"]
        rows[1]["headers"] = {"Cookie": "private-value", "X-Access-Key": "private-key"}
        item = self.map().items[0]
        self.assertEqual(item.variants[0].required_headers, ())
        self.assertEqual(item.variants[1].required_headers, ("cookie", "x-access-key"))
        self.assertNotIn("private-", repr(item))

    def test_root_alias_restrictions_cannot_be_cleared_by_format_defaults(self):
        video = self.raw["data"]["videos"][0]
        video.update(has_drm=True, segmented=True, method="POST",
                     headers={"X-Access-Key": "private-key"})
        video["streams"][0].update(has_drm=False, segmented=False, method="GET",
                                    headers={"X-Access-Key": ""})
        restricted, unrelated = self.map().items[0].variants
        self.assertTrue(restricted.drm)
        self.assertTrue(restricted.segmented)
        self.assertEqual(restricted.request_method, "POST")
        self.assertEqual(restricted.required_headers, ("x-access-key",))
        self.assertEqual(unrelated.required_headers, ())
        self.assertFalse(unrelated.drm)
        self.assertFalse(unrelated.segmented)

    def test_malformed_media_and_audio_only_envelope(self):
        self.assertIs(self.map(raw={"code": 200, "data": {"images": [None]}}).kind,
                      FailureKind.INVALID_RESULT)
        work = self.map(raw={"code": 200, "data": {"audio_url": "https://cdn.example/a.mp3"}})
        self.assertIsInstance(work, Work)
        self.assertIs(work.items[0].kind, Kind.AUDIO)

    def test_distinct_labels_share_same_resource_restrictions(self):
        rows = self.raw["data"]["videos"][0]["streams"]
        rows[0].update(qualityLabel="540P", has_drm=True, method="POST",
                       headers={"Cookie": "private"})
        rows.append({"url": rows[0]["url"], "qualityLabel": "原画"})
        item = self.map().items[0]
        self.assertEqual(len(item.variants), 3)
        for variant in (item.variants[0], item.variants[2]):
            self.assertTrue(variant.drm)
            self.assertEqual(variant.request_method, "POST")
            self.assertEqual(variant.required_headers, ("cookie",))
            self.assertFalse(variant.deliverable)
        self.assertIs(item.default_variant, item.variants[1])

    def test_host_policies_reuse_existing_suffixes_otherwise_exact_host(self):
        cases = (
            (SOURCE, DOUYIN_MEDIA_HOST_SUFFIXES),
            ("https://www.bilibili.com/video/BV13x41117TL/", BILIBILI_MEDIA_HOST_SUFFIXES),
            ("https://www.xiaohongshu.com/explore/67abcdef0123456789abcdef", XHS_MEDIA_HOST_SUFFIXES),
            ("https://www.tiktok.com/@example/video/123456789", next(r.media_host_suffixes for r in THIRD_PARTY_PLATFORM_RULES if r.platform_id == "tiktok")),
        )
        for source, suffixes in cases:
            with self.subTest(source=source):
                work = self.map(source=source)
                self.assertEqual(work.items[0].variants[0].host_policy, tuple(suffixes))
        self.raw["data"]["id"] = "work-1"
        work = self.map(source="https://source.example/work/1")
        variant = work.items[0].variants[0]
        self.assertEqual(variant.host_policy, ("cdn.example",))
        self.assertEqual(work.field_sources[f"items.{work.items[0].item_id}.variants.{variant.variant_id}.host_policy"], "adapter:exact_url_host")

    def test_size_estimate_and_missing_facts_are_honest(self):
        row = self.raw["data"]["videos"][0]["streams"][1]
        row.pop("sizeInBytes")
        row.update(size="1.5 MiB", qualityLabel="video")
        variant = self.map().items[0].variants[1]
        self.assertEqual(variant.size_bytes, 1572864)
        self.assertFalse(variant.size_exact)
        self.assertEqual(variant.display_label, "默认清晰度")

    def test_audio_is_an_independent_item_not_an_invented_merge_partner(self):
        self.raw["data"]["audioUrl"] = "https://cdn.example/music.mp3"
        work = self.map()
        self.assertEqual([item.kind for item in work.items], [Kind.VIDEO, Kind.AUDIO])
        self.assertIs(work.items[1].variants[0].tracks, Tracks.UNKNOWN)
        self.assertEqual(work.items[0].variants[0].merge_with, "")

    def test_failure_codes_need_evidence_not_deletion_words_in_message(self):
        for code in (1001, 404, 410, 451, 9999):
            failure = self.map(raw={"code": code, "data": "", "msg": "作品不存在，已删除或私密"})
            self.assertIs(failure.kind, FailureKind.UNAVAILABLE)
        self.assertIs(self.map(raw={"code": 451, "msg": "暂不支持该链接"}).kind, FailureKind.UNSUPPORTED)

    def test_shared_caption_split_keeps_body_and_numeric_topics(self):
        title = "简单几分钟调出高光比通透感"
        body = "先调整画面的整体曝光，再处理高光和阴影，让人物与背景之间保持自然的明暗层次。" * 2
        self.raw["data"].update(title=f"{title} {body} #3分钟学会拍照")
        work = self.map()
        self.assertEqual(work.title, title)
        self.assertEqual(work.description, body)
        self.assertEqual(work.topics, ("3分钟学会拍照",))
