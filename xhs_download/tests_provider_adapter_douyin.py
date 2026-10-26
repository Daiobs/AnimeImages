"""Controlled native-shaped JSON, with every mapper call isolated from I/O."""
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from .services.pipeline import Failure, FailureKind, Fetchable, Kind, Tracks, Variant, Work, validate_work
from .services.provider_adapters import ProviderAdapter, RawMapper
from .services.provider_adapters.douyin import DouyinMapper, failure_for_exception
from .services.provider_adapters.recordings import check, load_recording
from .services.provider_adapters.service_facts import _host_policy
from .tests_provider_adapters import deny_map_io


FIXTURES = Path(__file__).parent / "fixtures/provider_raw/douyin_native"
SOURCE = "https://www.douyin.com/video/7324602766213598501"
ID = "7324602766213598501"
MEDIA = "https://v3-web.douyinvod.com/video.mp4"
PREFIX = "douyin_native:aweme"


def fixture(name="synthetic_video"):
    document = load_recording(FIXTURES / (name + ".json"))
    check(document)
    assert document["provenance"] == {"kind": "synthetic", "raw_kind": "json", "complete": True}
    return document


class DouyinMappingTests(TestCase):
    def raw(self):
        return fixture()["raw"]

    def small(self, **address):
        return {"aweme_id": ID, "video": {"play_addr": {"url_list": [MEDIA], **address}}}

    def map(self, raw, source=SOURCE):
        before = deepcopy(raw)
        with deny_map_io():
            result = DouyinMapper().map(raw, source)
        self.assertEqual(raw, before)
        if isinstance(result, Work):
            self.assertEqual(validate_work(result), ())
            self.assertEqual((result.platform, result.provider), ("", ""))
            for field in Work.__dataclass_fields__:
                if field != "field_sources":
                    self.assertIn(field, result.field_sources)
            for item in result.items:
                self.assertIn(item.default_variant, item.variants)
                for variant in item.variants:
                    for field in Variant.__dataclass_fields__:
                        self.assertTrue(self.origin(result, item, variant, field))
        return result

    def origin(self, work, item, variant, field):
        return work.field_sources[f"items.{item.item_id}.variants.{variant.variant_id}.{field}"]

    def failure(self, raw, code, kind=FailureKind.INVALID_RESULT, source=SOURCE):
        self.assertEqual(self.map(raw, source), Failure(kind, code))

    def test_pure_mapper_and_no_legacy_helpers(self):
        mapper = DouyinMapper()
        self.assertIsInstance(mapper, RawMapper)
        self.assertNotIsInstance(mapper, ProviderAdapter)
        self.assertFalse(hasattr(mapper, "fetch"))
        self.assertFalse(hasattr(mapper, "parse"))
        with patch("xhs_download.services.platforms.douyin.get_video_media_candidates", side_effect=AssertionError("old candidate helper")), \
             patch("xhs_download.services.platforms.douyin.get_video_cover_urls", side_effect=AssertionError("old cover helper")), \
             patch("xhs_download.services.platforms.douyin.DouyinAdapter.parse_aweme", side_effect=AssertionError("old result")):
            self.assertIsInstance(self.map(self.raw()), Work)

    def test_video_all_addresses_and_original_audio_are_retained(self):
        raw = self.raw()
        work = self.map(raw)
        self.assertEqual([item.kind for item in work.items], [Kind.VIDEO, Kind.AUDIO])
        video, audio = work.items
        expected = [url for row in raw["video"]["bit_rate"] for url in row["play_addr"]["url_list"]]
        for key in ("play_addr", "download_addr", "play_addr_h264", "play_addr_265", "play_addr_bytevc1"):
            expected += raw["video"][key]["url_list"]
        self.assertEqual([v.url for v in video.variants], expected)
        self.assertEqual(audio.default_variant.url, raw["music"]["play_url"]["url_list"][0])
        self.assertIs(audio.default_variant.tracks, Tracks.AUDIO_ONLY)
        self.assertIs(video.default_variant, video.variants[1])
        self.assertTrue(all(v.tracks is Tracks.UNKNOWN for v in video.variants))
        self.assertTrue(all(v.merge_with == "" for item in work.items for v in item.variants))
        self.assertEqual(work.duration_seconds, 12.5)
        self.assertEqual(work.cover_url, raw["video"]["cover"]["url_list"][0])
        self.assertEqual((work.work_id, work.title, work.author, work.description, work.topics),
                         (ID, "Native title", "Creator", "Original body\nSecond paragraph", ("topic", "3minutes")))
        self.assertEqual(work.groups, ())

    def test_video_field_values_and_sources(self):
        work = self.map(self.raw())
        item = work.items[0]
        first = item.variants[0]
        self.assertEqual((first.height, first.width, first.fps, first.bitrate, first.size_bytes, first.size_exact),
                         (720, 1280, 24, 1000000, 12000, True))
        expected = {"height": PREFIX + ".video.bit_rate[0].play_addr.height",
                    "width": PREFIX + ".video.bit_rate[0].play_addr.width",
                    "fps": PREFIX + ".video.bit_rate[0].FPS", "bitrate": PREFIX + ".video.bit_rate[0].bit_rate",
                    "size_bytes": PREFIX + ".video.bit_rate[0].play_addr.data_size",
                    "size_exact": PREFIX + ".video.bit_rate[0].play_addr.data_size",
                    "url": PREFIX + ".video.bit_rate[0].play_addr.url_list",
                    "container": "adapter:url_extension", "tracks": "contract:unknown",
                    "browser_fetchable": "contract:unknown", "host_policy": "existing:DOUYIN_MEDIA_HOST_SUFFIXES"}
        for field, value in expected.items():
            self.assertEqual(self.origin(work, item, first, field), value, field)
        self.assertEqual(first.host_policy, _host_policy(SOURCE, first.url)[0])
        self.assertEqual(work.field_sources["duration_seconds"], PREFIX + ".video.duration:milliseconds")
        for field, path in (("work_id", ".aweme_id"), ("title", ".item_title"), ("description", ".desc"),
                            ("topics", ".desc"), ("author", ".author.nickname"), ("cover_url", ".video.cover.url_list")):
            self.assertEqual(work.field_sources[field], PREFIX + path)

    def test_same_url_formats_keep_own_metadata_and_union_restrictions(self):
        raw = self.small()
        raw["video"]["bit_rate"] = [
            {"FPS": 24, "bit_rate": 1000, "play_addr": {"url_list": [MEDIA], "quality_label": "720P", "data_size": 123,
             "required_headers": ["Referer"], "ip_bound": True}},
            {"FPS": 60, "bit_rate": 2000, "play_addr": {"url_list": [MEDIA], "quality_label": "1080P", "data_size": 456,
             "required_headers": ["Origin"], "ip_bound": False}},
        ]
        work = self.map(raw)
        item = work.items[0]
        self.assertEqual(len(item.variants), 3)
        self.assertEqual([(v.quality_label, v.height, v.fps, v.bitrate, v.size_bytes) for v in item.variants],
                         [("720P", 720, 24, 1000, 123), ("1080P", 1080, 60, 2000, 456), ("", None, None, None, None)])
        for variant in item.variants:
            self.assertEqual(variant.required_headers, ("origin", "referer"))
            self.assertIs(variant.browser_fetchable, Fetchable.FALSE)
        self.assertEqual(self.origin(work, item, item.variants[1], "size_bytes"), PREFIX + ".video.bit_rate[1].play_addr.data_size")

    def test_gallery_audio_candidates_are_not_video_or_dropped(self):
        document = fixture("synthetic_gallery")
        work = self.map(document["raw"], document["source_url"])
        self.assertEqual([item.kind for item in work.items], [Kind.IMAGE, Kind.IMAGE, Kind.AUDIO])
        self.assertEqual([v.url for v in work.items[2].variants], document["raw"]["video"]["play_addr"]["url_list"])
        self.assertEqual([v.container for v in work.items[2].variants], ["m4a", "", "mp3"])
        self.assertTrue(all(v.tracks is Tracks.AUDIO_ONLY for v in work.items[2].variants))
        self.assertEqual([v.url for v in work.items[0].variants], document["raw"]["images"][0]["url_list"])
        self.assertIsNone(work.duration_seconds)
        self.assertEqual(work.field_sources["duration_seconds"], "contract:default")

    def test_native_image_list_aliases_and_strings(self):
        raw = {"aweme_id": ID}
        keys = ("images", "image_infos", "image_list", "imageList")
        for i, key in enumerate(keys):
            raw[key] = [f"https://p3-sign.douyinpic.com/{i}.webp"]
        work = self.map(raw)
        self.assertIsInstance(work, Work)
        self.assertEqual(len(work.items), 4)
        self.assertEqual([item.default_variant.url for item in work.items], [raw[key][0] for key in keys])

    def test_image_all_native_address_shapes_and_formats(self):
        urls = [f"https://p3-sign.douyinpic.com/{i}.webp" for i in range(8)]
        raw = {"aweme_id": ID, "images": [{"url_list": [urls[0]], "download_url_list": [urls[1]],
            "origin_url": urls[2], "large_image": {"url_list": [urls[3]]}, "display_image": urls[4],
            "image": urls[5], "image_url": urls[6], "width": 1200}, urls[7]]}
        work = self.map(raw)
        self.assertIsInstance(work, Work)
        self.assertEqual(set(v.url for v in work.items[0].variants), set(urls[:7]))
        self.assertEqual(work.items[1].default_variant.url, urls[7])
        by_url = {v.url: v for v in work.items[0].variants}
        self.assertEqual(by_url[urls[0]].width, 1200)
        self.assertTrue(all(by_url[url].width is None for url in urls[1:7]))

    def test_image_representation_dimensions_stay_local_and_same_url_can_supply_missing(self):
        original = "https://p3-sign.douyinpic.com/original.webp"
        thumb = "https://p3-sign.douyinpic.com/thumb.webp"
        raw = {"aweme_id": ID, "images": [{"url_list": [original], "width": 1200, "height": 1600,
            "display_image": {"url_list": [thumb], "width": 300},
            "large_image": {"url_list": [original]},
            "required_headers": ["Origin"], "ip_bound": True}]}
        work = self.map(raw)
        original_v, same_url, thumb_v = work.items[0].variants
        self.assertEqual((original_v.width, original_v.height), (1200, 1600))
        self.assertEqual((same_url.width, same_url.height), (1200, 1600))
        self.assertEqual((thumb_v.width, thumb_v.height), (300, None))
        self.assertEqual(thumb_v.required_headers, ())
        self.assertIs(thumb_v.browser_fetchable, Fetchable.UNKNOWN)
        self.assertEqual(same_url.required_headers, ("origin",))
        self.assertIs(same_url.browser_fetchable, Fetchable.FALSE)
        self.assertEqual(self.origin(work, work.items[0], same_url, "width"), PREFIX + ".images[0].width")

    def test_share_title_fallback_and_big_thumbs_keep_raw_cdn(self):
        raw = self.small()
        raw["share_info"] = {"share_title": "Native share title"}
        url = "https://p3-sign.douyinpic.com/thumb~tplv-dy.heic?x=1"
        for field, value in (("img_urls", [url]), ("img_url", url)):
            raw["video"]["big_thumbs"] = [{field: value}]
            work = self.map(raw)
            self.assertEqual((work.title, work.cover_url), ("Native share title", url))
            self.assertEqual(work.field_sources["title"], PREFIX + ".share_info.share_title")
            self.assertEqual(work.field_sources["cover_url"], PREFIX + ".video.big_thumbs[0]." + field)
        raw["desc"] = "Caption wins"
        self.assertEqual(self.map(raw).title, "Caption wins")

    def test_raw_urls_cover_and_protocol_relative_only_resolution(self):
        raw = self.raw()
        raw["video"]["play_addr"]["url_list"].append("//v3-web.douyinvod.com/relative.mp4?ratio=540p")
        work = self.map(raw)
        urls = [v.url for v in work.items[0].variants]
        self.assertIn(raw["video"]["play_addr"]["url_list"][0], urls)
        self.assertIn("https://v3-web.douyinvod.com/relative.mp4?ratio=540p", urls)
        self.assertFalse(any("ratio=4k" in url for url in urls))
        self.assertEqual(work.cover_url, raw["video"]["cover"]["url_list"][0])

    def test_feed_and_detail_envelopes_select_exact_identity(self):
        for key in ("aweme_list", "item_list", "aweme_detail"):
            raw = self.small()
            other = deepcopy(raw)
            other["aweme_id"] = "999"
            envelope = {"status_code": 0, key: raw if key == "aweme_detail" else [other, raw]}
            self.assertEqual(self.map(envelope), self.map(raw))

    def test_identity_mismatch_duplicates_missing_and_conflicting_envelopes(self):
        for raw in ({"aweme_id": "999", "video": self.small()["video"]},
                    {"aweme_list": [self.small(), self.small()]}, {"aweme_detail": None},
                    {"aweme_list": []}, {"aweme_detail": self.small(), "aweme_list": []},
                    {"aweme_id": True, "video": self.small()["video"]}):
            self.assertIs(self.map(raw).kind, FailureKind.INVALID_RESULT)
        self.failure(self.small(), "DOUYIN_IDENTITY_MISMATCH", source=SOURCE + "?modal_id=999")
        self.failure(self.small(), "DOUYIN_IDENTITY_MISMATCH", source=SOURCE + "?modal_id=" + ID + "&modal_id=999")

    def test_native_source_forms_and_unresolved_shortlink(self):
        for source in ("https://www.iesdouyin.com/share/video/" + ID,
                       "https://www.douyin.com/?modal_id=" + ID,
                       "https://www.douyin.com/note/" + ID):
            self.assertIsInstance(self.map(self.small(), source), Work)
        self.failure(self.small(), "DOUYIN_MISSING_SOURCE_IDENTITY", source="https://v.douyin.com/synthetic/")
        self.failure(self.small(), "DOUYIN_UNSUPPORTED_SOURCE", FailureKind.UNSUPPORTED, "https://www.tiktok.com/@x/video/" + ID)

    def test_normalized_and_malformed_results_rejected(self):
        for raw in (None, [], "<html></html>", {"items": []}, {"assets": []}, {"data": self.small()},
                    {"aweme_id": ID}, {"aweme_id": ID, "images": [{}]},
                    {"aweme_id": ID, "video": {"play_addr": {"url_list": ["javascript:secret"]}}}):
            self.assertIs(self.map(raw).kind, FailureKind.INVALID_RESULT)
        for marker in ("_service_raw", "_sidecar", "_retained"):
            raw = self.small()
            raw[marker] = True
            self.assertIs(self.map(raw).kind, FailureKind.INVALID_RESULT)

    def test_header_values_not_required_but_explicit_common_headers_are(self):
        raw = self.small(headers={"Referer": "private-source", "Origin": "private-origin", "User-Agent": "private-agent"})
        variant = self.map(raw).items[0].default_variant
        self.assertEqual(variant.required_headers, ())
        raw["video"]["play_addr"]["required_headers"] = ["Referer", "Origin"]
        work = self.map(raw)
        variant = work.items[0].default_variant
        self.assertEqual(variant.required_headers, ("origin", "referer"))
        self.assertNotIn("contract:", self.origin(work, work.items[0], variant, "required_headers"))
        self.assertNotIn("private-source", repr(work))

    def test_context_flags_false_and_no_facts_unknown(self):
        for field in ("ip_bound", "requires_context", "headers_present"):
            raw = self.small(**{field: True})
            work = self.map(raw)
            variant = work.items[0].default_variant
            self.assertIs(variant.browser_fetchable, Fetchable.FALSE)
            self.assertEqual(self.origin(work, work.items[0], variant, "browser_fetchable"), PREFIX + ".video.play_addr")
        self.assertIs(self.map(self.small()).items[0].default_variant.browser_fetchable, Fetchable.UNKNOWN)

    def test_envelope_api_headers_not_inherited_as_media_requirements(self):
        raw = {"status_code": 0, "aweme_detail": self.small(), "headers": {"Cookie": "private-api-cookie"},
               "required_headers": ["Cookie"], "ip_bound": True}
        variant = self.map(raw).items[0].default_variant
        self.assertEqual(variant.required_headers, ())
        self.assertIs(variant.browser_fetchable, Fetchable.UNKNOWN)

    def test_parent_restrictions_cannot_be_overridden_by_child_false(self):
        raw = self.small(ip_bound=False, requires_context=False, drm=False, segmented=False)
        raw["video"].update(ip_bound=True, requires_context=True, drm=True, segmented=True, required_headers=["Referer"])
        work = self.map(raw)
        variant = work.items[0].default_variant
        self.assertIs(variant.browser_fetchable, Fetchable.FALSE)
        self.assertEqual(variant.required_headers, ("referer",))
        self.assertTrue(variant.drm)
        self.assertTrue(variant.segmented)

    def test_all_explicit_format_facts_and_sources(self):
        raw = self.small(ext="webm", tracks="av", width=1920, height=1080, FPS=60, bitrate=3000000,
                         quality_label="1080P", is_original=True, data_size=12345, size_exact=False,
                         required_headers=["Referer"], headers={"Referer": "REDACTED"}, ip_bound=True,
                         drm=False, segmented=False, method="GET", expires_at=1900000000)
        work = self.map(raw)
        item, variant = work.items[0], work.items[0].default_variant
        expected = {"container": "webm", "tracks": Tracks.AV, "width": 1920, "height": 1080, "fps": 60,
                    "bitrate": 3000000, "quality_label": "1080P", "is_original": True, "size_bytes": 12345,
                    "size_exact": False, "required_headers": ("referer",), "browser_fetchable": Fetchable.FALSE,
                    "drm": False, "segmented": False, "request_method": "GET", "merge_with": "",
                    "expires_at": datetime.fromtimestamp(1900000000, timezone.utc)}
        path = PREFIX + ".video.play_addr"
        origins = {"container": path + ".ext", "tracks": path + ":tracks", "width": path + ".width",
                   "height": path + ".height", "fps": path + ".FPS", "bitrate": path + ".bitrate",
                   "quality_label": path + ".quality_label", "is_original": path, "size_bytes": path + ".data_size",
                   "size_exact": path + ".size_exact", "required_headers": path, "browser_fetchable": path,
                   "drm": path, "segmented": path, "request_method": path, "merge_with": "contract:default",
                   "expires_at": path + ".expires_at"}
        for field, value in expected.items():
            self.assertEqual(getattr(variant, field), value, field)
            self.assertEqual(self.origin(work, item, variant, field), origins[field], field)

    def test_unrecognized_boolean_and_protocol_values_are_not_reported_as_facts(self):
        work = self.map(self.small(is_original=None, drm="unknown", segmented=None, protocol="https"))
        item, variant = work.items[0], work.items[0].default_variant
        for field in ("is_original", "drm", "segmented"):
            self.assertFalse(getattr(variant, field))
            self.assertEqual(self.origin(work, item, variant, field), "contract:default")

    def test_alias_context_does_not_leak_to_other_urls_or_music(self):
        raw = self.raw()
        raw["video"]["bit_rate"][0]["play_addr"].update(required_headers=["Authorization"], ip_bound=True, drm=True)
        work = self.map(raw)
        first, *others = work.items[0].variants
        self.assertEqual(first.required_headers, ("authorization",))
        for variant in [*others, work.items[1].default_variant]:
            self.assertEqual(variant.required_headers, ())
            self.assertIs(variant.browser_fetchable, Fetchable.UNKNOWN)
            self.assertFalse(variant.drm)

    def test_alias_union_drm_method_expiry_cannot_be_relaxed(self):
        raw = self.small()
        raw["video"]["download_addr"] = {"url_list": [MEDIA], "drm": True, "method": "post", "expires_at": 1900000000}
        raw["video"]["play_addr"].update(drm=False, method="GET", expires_at=2000000000)
        work = self.map(raw)
        for variant in work.items[0].variants:
            self.assertTrue(variant.drm)
            self.assertEqual(variant.request_method, "POST")
            self.assertEqual(variant.expires_at, datetime.fromtimestamp(1900000000, timezone.utc))
            self.assertEqual(self.origin(work, work.items[0], variant, "expires_at"), PREFIX + ".video.download_addr.expires_at")

    def test_explicit_tracks_without_pairing_no_invented_merge(self):
        raw = self.small(tracks="av")
        raw["video"]["bit_rate"] = [{"play_addr": {"url_list": [MEDIA + "?separate=1"], "tracks": "video_only", "height": 2160}}]
        raw["music"] = {"play_url": {"url_list": ["https://lf26-music-east.douyinstatic.com/original.mp3"]}}
        work = self.map(raw)
        self.assertIs(work.items[0].variants[0].tracks, Tracks.VIDEO_ONLY)
        self.assertEqual(work.items[0].variants[0].merge_with, "")
        self.assertIs(work.items[0].default_variant.tracks, Tracks.AV)
        self.assertIs(work.items[1].kind, Kind.AUDIO)
        raw["video"].pop("play_addr")
        self.failure(raw, "DOUYIN_ITEM_WITHOUT_DEFAULT")

    def test_explicit_audio_only_in_video_address_becomes_audio_item(self):
        work = self.map(self.small(tracks="audio_only"))
        self.assertIs(work.items[0].kind, Kind.AUDIO)
        self.assertIs(work.items[0].default_variant.tracks, Tracks.AUDIO_ONLY)

    def test_audio_does_not_inherit_video_dimensions(self):
        raw = self.small(url_list=["https://lf26-music-east.douyinstatic.com/obj/ies-music-hj/audio.mp3"])
        raw["video"].update(width=1080, height=1920, FPS=60, ratio="1080P")
        variant = self.map(raw).items[0].default_variant
        self.assertIs(variant.tracks, Tracks.AUDIO_ONLY)
        self.assertEqual((variant.width, variant.height, variant.fps), (None, None, None))

    def test_root_dimensions_do_not_fill_other_renditions(self):
        raw = self.small()
        raw["video"].update(width=1080, height=1920, bit_rate=[{"play_addr": {"url_list": [MEDIA + "?low=1"]}}],
                             download_addr={"url_list": [MEDIA + "?download=1"]})
        variants = self.map(raw).items[0].variants
        self.assertEqual([(v.width, v.height) for v in variants], [(None, None), (1080, 1920), (None, None)])

    def test_alias_track_and_method_conflicts_rejected(self):
        raw = self.small(tracks="av")
        raw["video"]["download_addr"] = {"url_list": [MEDIA], "tracks": "video_only"}
        self.failure(raw, "DOUYIN_CONFLICTING_TRACKS")
        raw = self.small(method="POST")
        raw["video"]["download_addr"] = {"url_list": [MEDIA], "method": "PUT"}
        self.failure(raw, "DOUYIN_CONFLICTING_METHODS")

    def test_quality_uses_native_dimensions_or_explicit_p_not_names(self):
        for label, height in (("1080P+", 1080), ("4K", None), ("HD", None), ("original", None), ("video", None)):
            work = self.map(self.small(quality_label=label))
            self.assertEqual(work.items[0].default_variant.height, height)
        work = self.map(self.small(quality_label="2160P", height=720))
        self.assertEqual(work.items[0].default_variant.height, 720)

    def test_unknown_duration_not_guessed_and_native_units(self):
        raw = self.small()
        raw["duration"] = 20000
        raw["video"]["play_addr"]["duration"] = 12345
        self.assertIsNone(self.map(raw).duration_seconds)
        raw["video"]["download_addr"] = {"url_list": [MEDIA], "duration": 2500}
        self.assertEqual(self.map(raw).duration_seconds, 2.5)
        raw["video"]["duration"] = 1250
        self.assertEqual(self.map(raw).duration_seconds, 1.25)
        raw["video"].pop("duration")
        raw["video"].pop("download_addr")
        raw["music"] = {"duration": 15}
        self.assertIsNone(self.map(raw).duration_seconds)

    def test_music_duration_never_substitutes_for_video_or_gallery_duration(self):
        for with_audio in (False, True):
            raw = self.small()
            raw["music"] = {"duration": 42}
            if with_audio:
                raw["music"]["play_url"] = {"url_list": ["https://lf26-music-east.douyinstatic.com/original.mp3"]}
            work = self.map(raw)
            self.assertIsNone(work.duration_seconds)
            self.assertEqual(work.field_sources["duration_seconds"], "contract:default")
        pure_audio = {"aweme_id": ID, "music": {"duration": 42,
            "play_url": {"url_list": ["https://lf26-music-east.douyinstatic.com/original.mp3"]}}}
        work = self.map(pure_audio)
        self.assertEqual([item.kind for item in work.items], [Kind.AUDIO])
        self.assertEqual(work.duration_seconds, 42)
        self.assertEqual(work.field_sources["duration_seconds"], PREFIX + ".music.duration")

    def test_segmented_and_drm_not_promoted_to_downloadable(self):
        for suffix in ("m3u8", "mpd", "m4s"):
            raw = self.small(url_list=[MEDIA + "." + suffix])
            variant = self.map(raw).items[0].default_variant
            self.assertTrue(variant.segmented)
            self.assertFalse(variant.deliverable)
        work = self.map(self.small(has_drm=True))
        self.assertTrue(work.items[0].default_variant.drm)

    def test_empty_optional_fields_default_sources_no_placeholders(self):
        work = self.map(self.small())
        variant = work.items[0].default_variant
        self.assertEqual((work.title, work.description, work.topics, work.author, work.cover_url), ("", "", (), "", ""))
        self.assertIsNone(work.duration_seconds)
        for field in ("title", "description", "topics", "author", "cover_url", "duration_seconds", "groups"):
            self.assertEqual(work.field_sources[field], "contract:default")
        for field in ("height", "width", "fps", "bitrate", "quality_label", "is_original", "size_bytes", "size_exact",
                      "merge_with", "required_headers", "drm", "segmented", "request_method", "expires_at"):
            self.assertEqual(self.origin(work, work.items[0], variant, field), "contract:default", field)

    def test_long_caption_topics_and_short_title_without_truncating_body(self):
        raw = self.small()
        raw["item_title"] = "Explicit title"
        raw["desc"] = "First paragraph\n" + "Long paragraph. " * 200 + "#3minutes #photo"
        work = self.map(raw)
        self.assertEqual(work.title, "Explicit title")
        self.assertGreater(len(work.description), 2000)
        self.assertEqual(work.topics, ("3minutes", "photo"))
        self.assertNotIn("#", work.description)

    def test_description_derived_from_title_has_title_provenance(self):
        raw = self.small()
        raw["item_title"] = "Lead\nOriginal body #topic"
        work = self.map(raw)
        self.assertEqual((work.title, work.description, work.topics), ("Lead", "Original body", ("topic",)))
        self.assertEqual(work.field_sources["description"], PREFIX + ".item_title")
        self.assertEqual(work.field_sources["topics"], PREFIX + ".item_title")

    def test_nested_normalized_markers_and_empty_wrong_types_rejected(self):
        raw = self.small()
        raw["_service_x"] = True
        self.failure({"aweme_detail": raw}, "DOUYIN_NOT_RAW_AWEME")
        for key in ("images", "image_infos", "image_list", "imageList"):
            raw = self.small()
            raw[key] = {}
            self.failure(raw, "DOUYIN_INVALID_IMAGES")
        raw = self.small()
        raw["video"]["bit_rate"] = {}
        self.failure(raw, "DOUYIN_INVALID_BIT_RATE")

    def test_unpaired_only_is_current_contract_rejection_not_a_real_recording(self):
        self.failure(self.small(tracks="video_only"), "DOUYIN_ITEM_WITHOUT_DEFAULT")

    def test_raw_input_mutation_after_mapping_cannot_change_work(self):
        raw = self.raw()
        work = self.map(raw)
        saved = repr(work)
        raw["video"]["bit_rate"][0]["play_addr"]["url_list"].clear()
        raw["author"]["nickname"] = "changed"
        self.assertEqual(repr(work), saved)

    def test_malformed_numeric_values_not_invented(self):
        for value in (0, -1, True, "unknown", float("nan")):
            raw = self.small(width=value, height=value, data_size=value)
            # NaN is not equal to itself; preserve identity for the raw check.
            work = self.map(raw)
            variant = work.items[0].default_variant
            self.assertEqual((variant.width, variant.height, variant.size_bytes), (None, None, None))

    def test_business_errors_use_safe_unavailable_not_unverified_not_found(self):
        for code in (10216, 999999, -1):
            self.failure({"status_code": code, "status_msg": "Cookie secret URL"}, "UNMAPPED_UPSTREAM_CODE", FailureKind.UNAVAILABLE)
        self.failure({"status_code": "0", "aweme_detail": self.small()}, "DOUYIN_INVALID_STATUS")

    def test_safe_exception_classification(self):
        for status, kind in ((404, FailureKind.NOT_FOUND), (410, FailureKind.NOT_FOUND), (408, FailureKind.TIMEOUT),
                             (401, FailureKind.UNAVAILABLE), (412, FailureKind.UNAVAILABLE), (429, FailureKind.UNAVAILABLE), (503, FailureKind.UNAVAILABLE)):
            error = RuntimeError("private upstream message")
            error.response = SimpleNamespace(status_code=status)
            self.assertEqual(failure_for_exception(error), Failure(kind, f"HTTP_{status}"))
        self.assertEqual(failure_for_exception(TimeoutError("secret")), Failure(FailureKind.TIMEOUT, "DOUYIN_TIMEOUT"))
        self.assertEqual(failure_for_exception(json.JSONDecodeError("secret", "private", 0)), Failure(FailureKind.INVALID_RESULT, "DOUYIN_INVALID_RAW"))
        self.assertEqual(failure_for_exception(RuntimeError("secret")), Failure(FailureKind.UNAVAILABLE, "DOUYIN_UNAVAILABLE"))
