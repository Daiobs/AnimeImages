"""Synthetic Native API JSON only; no live capture or provider requests."""
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from .services.pipeline import Failure, FailureKind, Fetchable, Kind, Tracks, Variant, Work, validate_work
from .services.provider_adapters import ProviderAdapter, RawMapper
from .services.provider_adapters.bilibili import BilibiliMapper, failure_for_exception
from .services.provider_adapters.recordings import check, load_recording
from .tests_provider_adapters import deny_map_io


FIXTURES = Path(__file__).parent / "fixtures/provider_raw/bilibili_native"
SOURCE = "https://www.bilibili.com/video/BV1J4JX6PEMV/?p=2"
MEDIA = "https://media.bilivideo.com/complete.mp4"


def fixture(name="synthetic_complete_page2"):
    document = load_recording(FIXTURES / (name + ".json"))
    check(document)
    assert document["provenance"] == {"kind": "synthetic", "raw_kind": "json", "complete": True}
    return document


class BilibiliMappingTests(TestCase):
    def raw(self):
        return fixture()["raw"]

    def map(self, raw, source=SOURCE):
        before = deepcopy(raw)
        with deny_map_io():
            work = BilibiliMapper().map(raw, source)
        self.assertEqual(before, raw)
        if isinstance(work, Work):
            self.assertEqual(validate_work(work), ())
            self.assertEqual((work.platform, work.provider), ("", ""))
            for item in work.items:
                for variant in item.variants:
                    for field in Variant.__dataclass_fields__:
                        self.assertIn(f"items.{item.item_id}.variants.{variant.variant_id}.{field}", work.field_sources)
        return work

    def mixed(self):
        raw = self.raw()
        raw["playurl"]["data"]["dash"] = fixture("synthetic_dash_only")["raw"]["playurl"]["data"]["dash"]
        return raw

    def assert_failure(self, raw, code, kind=FailureKind.INVALID_RESULT, source=SOURCE):
        self.assertEqual(self.map(raw, source), Failure(kind, code))

    def test_raw_mapper_only_and_no_legacy_normalizer(self):
        mapper = BilibiliMapper()
        self.assertIsInstance(mapper, RawMapper)
        self.assertNotIsInstance(mapper, ProviderAdapter)
        self.assertFalse(hasattr(mapper, "fetch"))
        self.assertFalse(hasattr(mapper, "parse"))
        with patch("xhs_download.services.platforms.bilibili.get_play_items", side_effect=AssertionError("old path")):
            self.assertIsInstance(self.map(self.raw()), Work)
        for raw in ({"assets": []}, {"items": []}, {"view": {"cid": 222}, "playurl": {"durl": []}}):
            self.assertIs(self.map(raw).kind, FailureKind.INVALID_RESULT)

    def test_complete_native_facts_metadata_and_backup_identity(self):
        raw = self.raw()
        work = self.map(raw)
        self.assertEqual(work.work_id, "BV1J4JX6PEMV")
        self.assertEqual(work.source_url, SOURCE)
        self.assertEqual((work.title, work.author, work.description, work.topics),
                         ("Native title", "Creator", "Original body", ("topic",)))
        self.assertEqual(work.duration_seconds, 24)
        self.assertEqual(work.cover_url, raw["view"]["data"]["pic"])
        self.assertEqual(work.groups, ())
        self.assertEqual(len(work.items), 1)
        item = work.items[0]
        self.assertIs(item.kind, Kind.VIDEO)
        self.assertEqual(len(item.variants), 2)
        self.assertIs(item.default_variant, item.variants[0])
        row = raw["playurl"]["data"]["durl"][0]
        self.assertEqual([v.url for v in item.variants], [row["url"], *row["backup_url"]])
        self.assertTrue(item.variants[0].url.startswith("http://"))
        for variant in item.variants:
            self.assertEqual((variant.container, variant.quality_label, variant.height), ("mp4", "720P", 720))
            self.assertEqual((variant.size_bytes, variant.size_exact), (12345, True))
            self.assertIsNone(variant.width)
            self.assertIsNone(variant.fps)
            self.assertIsNone(variant.bitrate)
            self.assertIs(variant.tracks, Tracks.UNKNOWN)
            self.assertIs(variant.browser_fetchable, Fetchable.UNKNOWN)
            self.assertEqual(variant.required_headers, ())
            self.assertEqual((variant.drm, variant.segmented, variant.is_original), (False, False, False))
            self.assertEqual((variant.request_method, variant.merge_with), ("GET", ""))
        self.assertEqual(item.variants[0].expires_at, datetime.fromtimestamp(1900000000, timezone.utc))
        self.assertEqual(item.variants[1].expires_at, datetime.fromtimestamp(1900000010, timezone.utc))

    def test_bvid_aid_and_actual_cid_page_must_all_agree(self):
        for key, value in (("bvid", "BV1xx411c7mD"), ("avid", 999), ("cid", 111)):
            raw = self.raw()
            raw["selection"]["playurl_params"][key] = value
            self.assertIs(self.map(raw).kind, FailureKind.INVALID_RESULT)
        for field in ("bvid", "aid", "cid", "page"):
            raw = self.raw()
            raw["playurl"]["data"][field] = "BV1xx411c7mD" if field == "bvid" else 999
            self.assertIs(self.map(raw).kind, FailureKind.INVALID_RESULT)
        for page in (1, 3, 0, True, 2.5):
            raw = self.raw()
            raw["selection"]["page"] = page
            self.assertIs(self.map(raw).kind, FailureKind.INVALID_RESULT)
        self.assertIs(self.map(self.raw(), SOURCE.replace("?p=2", "?p=3")).kind, FailureKind.INVALID_RESULT)
        self.assertIs(self.map(self.raw(), SOURCE.replace("?p=2", "?p=2&p=3")).kind, FailureKind.INVALID_RESULT)
        self.assertIs(self.map(self.raw(), SOURCE.replace("BV1J4JX6PEMV", "BV1xx411c7mD")).kind, FailureKind.INVALID_RESULT)
        for field in ("selection",):
            raw = self.raw()
            raw.pop(field)
            self.assert_failure(raw, "BILIBILI_MISSING_SELECTION")

    def test_av_source_matches_native_aid_without_guessing_bv_conversion(self):
        self.assertIsInstance(self.map(self.raw(), "https://www.bilibili.com/video/av123/?p=2"), Work)
        self.assert_failure(self.raw(), "BILIBILI_IDENTITY_MISMATCH", source="https://www.bilibili.com/video/av456/?p=2")
        raw = self.raw()
        raw["view"]["data"].pop("bvid")
        raw["selection"]["playurl_params"].pop("bvid")
        raw["selection"]["playurl_params"]["avid"] = 123
        work = self.map(raw, "https://www.bilibili.com/video/av123/?p=2")
        self.assertEqual(work.work_id, "av123")
        self.assertEqual(work.field_sources["work_id"], "bilibili_native:view.identity")

    def test_resource_identity_cannot_override_selected_page(self):
        for dash in (False, True):
            for key, value in (("cid", 111), ("bvid", "BV1xx411c7mD"), ("aid", 999)):
                raw = self.mixed()
                row = raw["playurl"]["data"]["dash"]["video"][0] if dash else raw["playurl"]["data"]["durl"][0]
                row[key] = value
                self.assertIs(self.map(raw).kind, FailureKind.INVALID_RESULT)

    def test_view_top_cid_only_supports_explicit_single_page_selection(self):
        raw = self.raw()
        raw["view"]["data"].pop("pages")
        self.assert_failure(raw, "BILIBILI_PAGE_MISMATCH")
        raw["selection"]["page"] = 1
        raw["selection"]["playurl_params"]["cid"] = 111
        work = self.map(raw, SOURCE.replace("?p=2", ""))
        self.assertIsInstance(work, Work)
        self.assertEqual(work.duration_seconds, 34)
        self.assertEqual(work.field_sources["duration_seconds"], "bilibili_native:view.duration")

    def test_short_link_requires_executed_resolved_source(self):
        raw = self.raw()
        source = "https://b23.tv/synthetic"
        self.assert_failure(raw, "BILIBILI_MISSING_RESOLVED_SOURCE", source=source)
        raw["selection"]["page_url"] = SOURCE
        self.assertIsInstance(self.map(raw, source), Work)
        raw["selection"]["page_url"] = SOURCE.replace("?p=2", "?p=1")
        self.assert_failure(raw, "BILIBILI_PAGE_MISMATCH", source=source)

    def test_dynamic_and_unrelated_sources_are_not_in_scope(self):
        for source in ("https://t.bilibili.com/123", "https://www.bilibili.com/opus/123", "https://example.com/video/BV1J4JX6PEMV/"):
            self.assert_failure(self.raw(), "BILIBILI_UNSUPPORTED_SOURCE", FailureKind.UNSUPPORTED, source=source)

    def test_multi_durl_is_unsupported_without_constructing_work(self):
        document = fixture("synthetic_multi_durl")
        self.assertEqual(len(document["raw"]["playurl"]["data"]["durl"]), 2)
        with patch("xhs_download.services.provider_adapters.bilibili.Work", side_effect=AssertionError("must not make Work")):
            self.assert_failure(document["raw"], "BILIBILI_SEGMENTED_DURL", FailureKind.UNSUPPORTED, document["source_url"])
        self.assertEqual([row["order"] for row in document["raw"]["playurl"]["data"]["durl"]], [1, 2])

    def test_single_segment_path_is_unsupported(self):
        for suffix in ("m4s", "MPD", "m3u8"):
            raw = self.raw()
            raw["playurl"]["data"]["durl"][0]["url"] = "https://media.bilivideo.com/segment." + suffix
            self.assert_failure(raw, "BILIBILI_SEGMENTED_PATH", FailureKind.UNSUPPORTED)

    def test_dash_only_is_unsupported_without_guessing_an_audio_pair(self):
        document = fixture("synthetic_dash_only")
        for single_audio in (False, True):
            raw = deepcopy(document["raw"])
            if single_audio:
                raw["playurl"]["data"]["dash"]["audio"] = raw["playurl"]["data"]["dash"]["audio"][:1]
                raw["playurl"]["data"]["dash"].pop("dolby")
                raw["playurl"]["data"]["dash"].pop("flac")
            with patch("xhs_download.services.provider_adapters.bilibili.Work", side_effect=AssertionError("must not make Work")):
                self.assert_failure(raw, "BILIBILI_SEPARATE_TRACKS_ONLY", FailureKind.UNSUPPORTED, document["source_url"])

    def test_explicit_split_track_fact_is_not_complete_just_because_it_is_durl(self):
        raw = self.raw()
        raw["playurl"]["data"]["durl"][0].update(vcodec="avc1", acodec="none")
        self.assert_failure(raw, "BILIBILI_SEPARATE_TRACKS_ONLY", FailureKind.UNSUPPORTED)

    def test_complete_default_retains_dash_and_audio_formats_in_two_items(self):
        raw = self.mixed()
        work = self.map(raw)
        self.assertEqual([item.kind for item in work.items], [Kind.VIDEO, Kind.AUDIO])
        video, audio = work.items
        self.assertEqual(len(video.variants), 4)
        self.assertEqual(len(audio.variants), 4)
        self.assertIs(video.default_variant, video.variants[0])
        self.assertEqual(video.default_variant.url, raw["playurl"]["data"]["durl"][0]["url"])
        for variant in video.variants[2:]:
            self.assertIs(variant.tracks, Tracks.VIDEO_ONLY)
            self.assertEqual(variant.merge_with, "")
            self.assertTrue(variant.segmented)
            self.assertFalse(variant.deliverable)
            self.assertAlmostEqual(variant.fps, 30000 / 1001)
            self.assertEqual((variant.width, variant.height, variant.bitrate), (1920, 1080, 1200000))
        for variant in audio.variants:
            self.assertIs(variant.tracks, Tracks.AUDIO_ONLY)
            self.assertEqual(variant.merge_with, "")
            self.assertEqual(variant.quality_label, "")
        self.assertEqual([variant.container for variant in audio.variants], ["m4a", "m4a", "m4a", "flac"])

    def test_same_format_backup_duplicates_do_not_create_new_items_or_labels(self):
        raw = self.raw()
        row = raw["playurl"]["data"]["durl"][0]
        row["backup_url"].extend([row["url"], row["backup_url"][0]])
        work = self.map(raw)
        self.assertEqual(len(work.items), 1)
        self.assertEqual(len(work.items[0].variants), 2)
        self.assertEqual([variant.quality_label for variant in work.items[0].variants], ["720P", "720P"])

    def test_height_is_actual_or_explicit_p_never_quality_id(self):
        for label, height in (("1080P+", 1080), ("4K", None), ("original", None), ("video", None)):
            raw = self.raw()
            play = raw["playurl"]["data"]
            play.update(quality=2160, support_formats=[{"quality": 2160, "new_description": label}], accept_quality=[], accept_description=[])
            variant = self.map(raw).items[0].default_variant
            self.assertEqual(variant.height, height)
            self.assertFalse(variant.is_original)
        raw = self.raw()
        raw["playurl"]["data"].update(support_formats=[], accept_quality=[], accept_description=[])
        self.assertIsNone(self.map(raw).items[0].default_variant.height)
        raw["playurl"]["data"]["durl"][0]["height"] = 360
        self.assertEqual(self.map(raw).items[0].default_variant.height, 360)

    def test_frame_rate_alias_fraction_and_native_size(self):
        raw = self.mixed()
        row = raw["playurl"]["data"]["dash"]["video"][0]
        row.pop("frameRate")
        row.update(frame_rate="60000/1001", size=4567)
        variant = self.map(raw).items[0].variants[2]
        self.assertAlmostEqual(variant.fps, 60000 / 1001)
        self.assertEqual((variant.size_bytes, variant.size_exact), (4567, True))
        row.update(frame_rate="1/0", size=0)
        variant = self.map(raw).items[0].variants[2]
        self.assertIsNone(variant.fps)
        self.assertIsNone(variant.size_bytes)

    def test_customary_headers_and_api_credentials_are_not_required_media_headers(self):
        raw = self.raw()
        raw["selection"]["cookies"] = {"buvid3": "private-api-only"}
        raw["selection"]["headers"] = {"Cookie": "private-api-only"}
        raw["view"]["data"]["headers"] = {"Authorization": "also-api-only"}
        raw["playurl"]["data"]["headers"] = {"Referer": "https://www.bilibili.com/", "User-Agent": "customary"}
        work = self.map(raw)
        self.assertEqual(work.items[0].default_variant.required_headers, ())
        self.assertIs(work.items[0].default_variant.browser_fetchable, Fetchable.UNKNOWN)
        self.assertNotIn("private-api-only", repr(work))
        self.assertNotIn("also-api-only", repr(work))

    def test_explicit_customary_requirements_private_headers_and_context_false(self):
        for facts, headers in (({"required_headers": ["Referer", "Origin"]}, ("origin", "referer")),
                               ({"headers": {"X-Access": "private-value", "Cookie": "private-value"}}, ("cookie", "x-access"))):
            raw = self.raw()
            raw["playurl"]["data"]["durl"][0].update(facts)
            work = self.map(raw)
            self.assertEqual(work.items[0].default_variant.required_headers, headers)
            self.assertNotIn("private-value", repr(work))
        for facts in ({"ip_bound": True}, {"requires_context": True},
                      {"headers_present": True}, {"headers_present": True, "headers": {"X-Access": None}}):
            raw = self.raw()
            raw["playurl"]["data"]["durl"][0].update(facts)
            self.assertIs(self.map(raw).items[0].default_variant.browser_fetchable, Fetchable.FALSE)

    def test_url_scoped_context_does_not_leak_to_other_streams(self):
        raw = self.mixed()
        play = raw["playurl"]["data"]
        play.update(url=play["durl"][0]["url"], required_headers=["Origin"], ip_bound=True)
        work = self.map(raw)
        first = work.items[0].variants[0]
        self.assertEqual(first.required_headers, ("origin",))
        self.assertIs(first.browser_fetchable, Fetchable.FALSE)
        for variant in (*work.items[0].variants[1:], *work.items[1].variants):
            self.assertEqual(variant.required_headers, ())
            self.assertIs(variant.browser_fetchable, Fetchable.UNKNOWN)

    def test_same_url_observations_keep_restrictions_and_earliest_expiry(self):
        for reverse in (False, True):
            raw = self.mixed()
            video = raw["playurl"]["data"]["dash"]["video"]
            first = deepcopy(video[0])
            first.update(headers_present=True, headers={"X-Access": None}, method="POST", drm=True, expires_at=1800000000)
            second = deepcopy(video[0])
            second.update(headers={"Referer": "https://www.bilibili.com/"}, expires_at=1900000000)
            video[:] = [second, first] if reverse else [first, second]
            work = self.map(raw)
            self.assertIsInstance(work, Work)
            for variant in work.items[0].variants[2:]:
                self.assertIs(variant.browser_fetchable, Fetchable.FALSE)
                self.assertEqual(variant.request_method, "POST")
                self.assertTrue(variant.drm)
                self.assertEqual(variant.expires_at, datetime.fromtimestamp(1800000000, timezone.utc))
            self.assertIs(work.items[0].default_variant.browser_fetchable, Fetchable.UNKNOWN)
            self.assertFalse(work.items[0].default_variant.drm)
            self.assertIs(work.items[1].default_variant.browser_fetchable, Fetchable.UNKNOWN)

    def test_field_sources_distinguish_raw_derived_defaults_and_explicit_false(self):
        raw = self.raw()
        row = raw["playurl"]["data"]["durl"][0]
        row.update(is_original=False, drm=False, segmented=False, size_exact=False)
        work = self.map(raw)
        prefix = "items.video:1.variants.video:1:1."
        sources = work.field_sources
        self.assertEqual(sources["work_id"], "bilibili_native:view.identity")
        self.assertEqual(sources["source_url"], "request")
        self.assertEqual(sources["title"], "bilibili_native:view.title")
        self.assertEqual(sources["author"], "bilibili_native:view.owner.name")
        self.assertEqual(sources["duration_seconds"], "bilibili_native:view.pages.duration")
        for field in ("drm", "segmented", "is_original", "size_exact", "size_bytes"):
            self.assertTrue(sources[prefix + field].startswith("bilibili_native:"))
        self.assertEqual(sources[prefix + "height"], "adapter:height_from_label")
        self.assertEqual(sources[prefix + "tracks"], "contract:unknown")
        self.assertEqual(sources[prefix + "browser_fetchable"], "contract:unknown")
        self.assertEqual(sources[prefix + "host_policy"], "existing:BILIBILI_MEDIA_HOST_SUFFIXES")
        self.assertEqual(sources[prefix + "expires_at"], "adapter:url_deadline")
        self.assertEqual(sources[prefix + "request_method"], "contract:default")
        self.assertEqual(sources[prefix + "width"], "contract:default")

    def test_sparse_metadata_stays_missing_not_filename_or_page_title(self):
        raw = self.raw()
        for key in ("title", "desc", "owner", "pic"):
            raw["view"]["data"].pop(key)
        for page in raw["view"]["data"]["pages"]:
            page.pop("duration")
        raw["playurl"]["data"].pop("timelength")
        work = self.map(raw)
        self.assertEqual((work.title, work.description, work.author, work.cover_url, work.topics), ("", "", "", "", ()))
        self.assertIsNone(work.duration_seconds)
        self.assertEqual(work.field_sources["duration_seconds"], "contract:default")

    def test_selected_playurl_milliseconds_are_not_aggregate_view_duration(self):
        raw = self.raw()
        raw["view"]["data"]["pages"][1].pop("duration")
        raw["playurl"]["data"]["timelength"] = 24500
        work = self.map(raw)
        self.assertEqual(work.duration_seconds, 24.5)
        self.assertEqual(work.field_sources["duration_seconds"], "bilibili_native:playurl.timelength_ms")

    def test_no_dimension_size_or_container_guessed_from_bitrate_and_quality_id(self):
        raw = self.raw()
        play = raw["playurl"]["data"]
        play.update(quality=1080, support_formats=[], accept_quality=[], accept_description=[], format="unrecognized")
        row = play["durl"][0]
        row.update(url="https://media.bilivideo.com/opaque?mime_type=video_mp4&br=8000", backup_url=[], size=0)
        variant = self.map(raw).items[0].default_variant
        self.assertEqual(variant.container, "")
        for field in ("height", "width", "size_bytes", "bitrate", "fps"):
            self.assertIsNone(getattr(variant, field))

    def test_api_errors_http_and_malformed_raw_are_safe_failures(self):
        for stage in ("view", "playurl"):
            for code, kind in ((-404, FailureKind.NOT_FOUND), (-403, FailureKind.UNAVAILABLE), (-412, FailureKind.UNAVAILABLE), (-999, FailureKind.UNAVAILABLE)):
                raw = self.raw()
                raw[stage] = {"code": code, "message": "private-upstream-message"}
                result = self.map(raw)
                self.assertIs(result.kind, kind)
                self.assertNotIn("private", repr(result))
            for status, kind in ((404, FailureKind.NOT_FOUND), (408, FailureKind.TIMEOUT), (412, FailureKind.UNAVAILABLE), (429, FailureKind.UNAVAILABLE), (503, FailureKind.UNAVAILABLE)):
                raw = self.raw()
                raw[stage + "_status"] = status
                self.assertIs(self.map(raw).kind, kind)
        for invalid in (None, [], {"code": True}, {"code": "0"}, {"code": 0, "data": []}):
            raw = self.raw()
            raw["playurl"] = invalid
            self.assertIs(self.map(raw).kind, FailureKind.INVALID_RESULT)
        self.assertIs(failure_for_exception(TimeoutError("private")).kind, FailureKind.TIMEOUT)
        error = RuntimeError("private")
        error.response = SimpleNamespace(status_code=429)
        self.assertEqual(failure_for_exception(error).code, "HTTP_429")
