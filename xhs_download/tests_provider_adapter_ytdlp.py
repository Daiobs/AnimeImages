"""P3 synthetic upstream cases; every mapping uses the real, I/O-denied mapper."""
from copy import deepcopy
from dataclasses import fields
from datetime import datetime, timezone
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from xhs_download.services.common import XhsProviderTimeoutError
from xhs_download.services.pipeline import (
    Completeness, Failure, FailureKind, Fetchable, Kind, Tracks, Variant, Work,
    decide_delivery, fact_gaps, field_coverage, validate_work,
)
from xhs_download.services.pipeline.delivery import Method
from xhs_download.services.pipeline.model import DEFAULT_QUALITY_LABEL
from xhs_download.services.provider_adapters.recordings import check, load_recording
from xhs_download.services.provider_adapters.ytdlp import YtDlpAdapter, failure_for_exception
from xhs_download.tests_provider_adapters import deny_map_io


NAME = "yt_dlp_generic"
FIXTURES = Path(__file__).parent / "fixtures" / "provider_raw" / NAME
UPSTREAM = "xhs_download.services.platforms.ytdlp_generic.run_ytdlp_json"


def fixture(case):
    return load_recording(FIXTURES / f"synthetic_{case}.json")


def variant_fields(url, **changes):
    expected = {
        "url": url, "container": "", "tracks": Tracks.UNKNOWN,
        "height": None, "width": None, "fps": None, "bitrate": None,
        "quality_label": "", "is_original": False, "size_bytes": None,
        "size_exact": False, "merge_with": "", "required_headers": (),
        "browser_fetchable": Fetchable.UNKNOWN, "drm": False,
        "segmented": False, "expires_at": None, "host_policy": ("cdn.example",),
    }
    return {**expected, **changes}


class YtDlpMappingTests(TestCase):
    def setUp(self):
        self.adapter = YtDlpAdapter(binary="synthetic-yt-dlp")
        self.document = fixture("youtube_formats")
        self.raw = self.document["raw"]
        self.url = self.document["source_url"]

    def test_duplicate_format_aliases_share_restrictions_without_losing_labels(self):
        first = {"format_id": "v", "url": "https://cdn.example/v.mp4", "format_note": "720P"}
        for restriction in ({"has_drm": True}, {"protocol": "http_dash_segments"},
                            {"http_headers": {"Authorization": "REDACTED"}}):
            with self.subTest(restriction=restriction):
                raw = {"id": "synthetic01", "formats": [first, {**first, **restriction, "format_note": "720P+"}]}
                item = self.map_work(raw).items[0]
                self.assertEqual([v.quality_label for v in item.variants], ["720P", "720P+"])
                for variant in item.variants:
                    self.assertEqual(variant.drm, restriction.get("has_drm", False))
                    self.assertEqual(variant.segmented, "protocol" in restriction)
                    self.assertEqual(variant.required_headers, ("authorization",) if "http_headers" in restriction else ())

    def test_root_selected_credentials_do_not_change_another_resource(self):
        clear = {"format_id": "clear", "url": "https://cdn.example/clear.mp4"}
        for credential in ({"cookies": "session=REDACTED; Domain=private.example; Path=/"},
                           {"http_headers": {"Authorization": "REDACTED"}}):
            with self.subTest(credential=credential):
                private = {"format_id": "private", "url": "https://private.example/v.mp4", **credential}
                raw = {"id": "synthetic01", "formats": [clear, private], **private}
                item = self.map_work(raw).items[0]
                self.assertEqual(item.variants[0].required_headers, ())
                self.assertEqual(item.variants[1].required_headers,
                                 ("cookie",) if "cookies" in credential else ("authorization",))

    def test_description_without_title_is_not_invented_title(self):
        raw = {"id": "synthetic01", "url": "https://cdn.example/v.mp4", "description": "Body only #topic"}
        work = self.map_work(raw)
        self.assertEqual((work.title, work.description, work.topics), ("", "Body only", ("topic",)))

    def map_raw(self, raw, source_url=None):
        before = deepcopy(raw)
        with deny_map_io():
            result = self.adapter.map(raw, source_url or self.url)
            repeated = self.adapter.map(raw, source_url or self.url)
        self.assertEqual(raw, before, "map mutated the upstream input")
        self.assertEqual(result, repeated, "map is not deterministic")
        return result

    def map_work(self, raw=None, source_url=None):
        result = self.map_raw(self.raw if raw is None else raw, source_url)
        self.assertIsInstance(result, Work)
        self.assertEqual(validate_work(result), ())
        self.assertEqual((result.platform, result.provider), ("", ""))
        self.assertFalse(hasattr(result, "raw"))
        self.assertFalse(hasattr(result, "metadata"))
        self.assertEqual(len({item.item_id for item in result.items}), len(result.items))
        all_variants = [variant for item in result.items for variant in item.variants]
        self.assertEqual(len({variant.variant_id for variant in all_variants}), len(all_variants))
        for index, item in enumerate(result.items, 1):
            self.assertTrue(item.item_id)
            self.assertEqual(item.index, index)
            self.assertIn(item.default_variant, item.variants)
            self.assertEqual(set(vars(item)), {"item_id", "kind", "index", "variants"})
        return result

    def assert_work_fields(self, work, **changes):
        expected = {
            "source_url": self.url, "work_id": "synthetic01", "platform": "",
            "title": "Synthetic format demonstration",
            "description": "A complete synthetic info dict.",
            "topics": ("demo", "synthetic"), "author": "Synthetic creator",
            "cover_url": "https://images.example/synthetic01/cover.jpg",
            "duration_seconds": 20.5, "groups": (), "provider": "",
            "completeness": Completeness.FINAL,
        }
        expected.update(changes)
        self.assertEqual(set(expected) | {"items", "field_sources"},
                         {field.name for field in fields(Work)})
        self.assertEqual({name: getattr(work, name) for name in expected}, expected)
        for name in ("work_id", "items"):
            self.assertEqual(work.field_sources[name], NAME)
        self.assertEqual(work.field_sources["source_url"], "request")
        for name, value in (("title", work.title), ("description", work.description),
                            ("topics", work.topics), ("author", work.author),
                            ("cover", work.cover_url), ("duration_seconds", work.duration_seconds)):
            if value:
                self.assertEqual(work.field_sources[name], NAME, name)
            else:
                self.assertNotEqual(work.field_sources.get(name), NAME, name)
        for name in ("platform", "provider", "completeness"):
            self.assertNotEqual(work.field_sources.get(name), NAME)

    def assert_variant_fields(self, variant, expected):
        self.assertTrue(variant.variant_id)
        self.assertEqual(set(expected) | {"variant_id"},
                         {field.name for field in fields(Variant)})
        self.assertEqual({name: getattr(variant, name) for name in expected}, expected)

    def assert_variant_sources(self, work, item, variant, *, native=(), defaults=()):
        prefix = f"items.{item.item_id}.variants.{variant.variant_id}."
        for field in fields(Variant):
            self.assertTrue(work.field_sources.get(prefix + field.name), field.name)
        for name in ("variant_id", "host_policy"):
            self.assertEqual(work.field_sources[prefix + name], "adapter:resource_identity", name)
        for name in native:
            self.assertEqual(work.field_sources[prefix + name], NAME, name)
        for name in defaults:
            expected = "contract:unknown" if name in {"tracks", "browser_fetchable"} else "contract:default"
            self.assertEqual(work.field_sources[prefix + name], expected, name)

    def complete_only(self):
        raw = deepcopy(self.raw)
        raw.pop("requested_formats")
        raw["formats"] = raw["formats"][:2]
        return raw

    def test_synthetic_envelopes_are_complete_and_sanitized(self):
        paths = sorted(FIXTURES.glob("synthetic_*.json"))
        self.assertEqual(len(paths), 3)
        for path in paths:
            with self.subTest(case=path.name):
                document = load_recording(path)
                self.assertEqual(document["provider"], NAME)
                self.assertEqual(document["provenance"], {
                    "kind": "synthetic", "raw_kind": "json", "complete": True,
                })
                check(document)

    def test_youtube_all_work_and_variant_fields(self):
        work = self.map_work()
        self.assert_work_fields(work)
        self.assertEqual(len(work.items), 1)
        item = work.items[0]
        self.assertIs(item.kind, Kind.VIDEO)
        self.assertEqual(len(item.variants), 7)
        low, medium, video, audio, alternate_audio, hls, drm = item.variants
        expected = [
            variant_fields("https://cdn.example/synthetic01/360.mp4", container="mp4",
                           tracks=Tracks.AV, height=360, width=640, fps=30, bitrate=512000,
                           quality_label="360p", size_bytes=1200000, size_exact=True),
            variant_fields("https://cdn.example/synthetic01/720.mp4", container="mp4",
                           tracks=Tracks.AV, height=720, width=1280, fps=30, bitrate=1400000,
                           quality_label="720p", size_bytes=3000000, size_exact=True),
            variant_fields("https://cdn.example/synthetic01/1080.mp4?sig=REDACTED", container="mp4",
                           tracks=Tracks.VIDEO_ONLY, height=1080, width=1920, fps=60,
                           bitrate=4200000, quality_label="1080p", size_bytes=8400000,
                           size_exact=True, merge_with=audio.variant_id, required_headers=("cookie",)),
            variant_fields("https://cdn.example/synthetic01/audio.m4a", container="m4a",
                           tracks=Tracks.AUDIO_ONLY, bitrate=128000, size_bytes=400000),
            variant_fields("https://cdn.example/synthetic01/audio.webm", container="webm",
                           tracks=Tracks.AUDIO_ONLY, bitrate=160000, size_bytes=700000),
            variant_fields("https://cdn.example/synthetic01/master.m3u8", container="mp4",
                           tracks=Tracks.AV, height=2160, width=3840, fps=60, bitrate=8000000,
                           quality_label="2160p", size_bytes=18000000, segmented=True, is_original=True),
            variant_fields("https://cdn.example/synthetic01/protected.mp4", container="mp4",
                           tracks=Tracks.AV, height=4320, width=7680, fps=60, bitrate=20000000,
                           quality_label="4320p", size_bytes=50000000, size_exact=True,
                           drm=True, is_original=True),
        ]
        for variant, expected_fields in zip(item.variants, expected):
            with self.subTest(url=variant.url):
                self.assert_variant_fields(variant, expected_fields)
        self.assertIs(item.default_variant, video)
        self.assertEqual(video.merge_with, audio.variant_id)
        self.assertNotEqual(video.merge_with, alternate_audio.variant_id)
        self.assertEqual(field_coverage(work), dict.fromkeys(("title", "cover", "quality", "size"), NAME))
        self.assertEqual(fact_gaps(work), ())
        self.assertEqual(decide_delivery(item.kind, video).method, Method.SERVER_MERGE)
        self.assertNotIn("REDACTED", repr(video.required_headers))

    def test_sources_distinguish_upstream_facts_from_contract_defaults(self):
        work = self.map_work()
        item = work.items[0]
        for variant in item.variants:
            self.assert_variant_sources(work, item, variant,
                                        native=("url", "container", "tracks", "bitrate", "size_bytes", "drm"),
                                        defaults=("browser_fetchable",))
        self.assert_variant_sources(work, item, item.variants[0],
                                    native=("height", "width", "fps", "quality_label"),
                                    defaults=("is_original",))
        self.assert_variant_sources(work, item, item.variants[2], native=("merge_with", "required_headers"))
        self.assert_variant_sources(work, item, item.variants[5], native=("segmented", "is_original"))
        for name in ("title", "cover", "quality", "size", "description", "topics", "author", "duration_seconds"):
            self.assertEqual(work.field_sources[name], NAME)

    def test_standalone_audio_all_fields_and_default(self):
        document = fixture("audio")
        work = self.map_work(document["raw"], document["source_url"])
        self.assert_work_fields(work, source_url=document["source_url"], work_id="recording",
                                title="Synthetic sound", description="", topics=(),
                                author="Sound creator", cover_url="https://images.example/recording.jpg",
                                duration_seconds=12.5)
        self.assertEqual(len(work.items), 1)
        item = work.items[0]
        self.assertIs(item.kind, Kind.AUDIO)
        self.assertEqual(len(item.variants), 2)
        for variant, expected in zip(item.variants, (
            variant_fields("https://cdn.example/recording.m4a", container="m4a", tracks=Tracks.AUDIO_ONLY,
                           bitrate=128000, size_bytes=200000, size_exact=True),
            variant_fields("https://cdn.example/recording.opus", container="opus", tracks=Tracks.AUDIO_ONLY,
                           bitrate=192000, size_bytes=300000),
        )):
            self.assert_variant_fields(variant, expected)
            self.assert_variant_sources(work, item, variant,
                                        native=("url", "container", "tracks", "bitrate", "size_bytes"),
                                        defaults=("browser_fetchable", "is_original", "drm"))
        self.assertIs(item.default_variant, item.variants[1])
        self.assertEqual(field_coverage(work), {"title": NAME, "cover": NAME, "quality": None, "size": NAME})

    def test_multi_video_all_fields_order_and_missing_facts(self):
        document = fixture("multi_video")
        work = self.map_work(document["raw"], document["source_url"] + "#section")
        self.assert_work_fields(work, source_url=document["source_url"], work_id="chapters",
                                title="Synthetic chapters", description="Two parts in upstream order.",
                                topics=("chapters",), author="Chapter creator",
                                cover_url="https://images.example/chapters.jpg", duration_seconds=30)
        self.assertEqual(len(work.items), 2)
        expected = (
            variant_fields("https://cdn.example/chapters/one.mp4", container="mp4"),
            variant_fields("https://cdn.example/chapters/two.mp4", container="mp4", tracks=Tracks.AV,
                           height=480, width=854, fps=25, bitrate=800000,
                           size_bytes=2000000, size_exact=True),
        )
        for item, expected_fields in zip(work.items, expected):
            self.assertIs(item.kind, Kind.VIDEO)
            self.assertEqual(len(item.variants), 1)
            self.assert_variant_fields(item.default_variant, expected_fields)
            self.assert_variant_sources(work, item, item.default_variant,
                                        native=("url", "container"), defaults=("browser_fetchable", "drm"))
        self.assertEqual(work.items[0].default_variant.display_label, DEFAULT_QUALITY_LABEL)
        self.assertEqual(work.items[1].default_variant.display_label, "480p")
        self.assertEqual(fact_gaps(work), ("TRACKS_UNKNOWN",))
        self.assertEqual(field_coverage(work), {"title": NAME, "cover": NAME, "quality": None, "size": None})

    def test_missing_optional_fields_stay_unknown_without_placeholders(self):
        raw = {"id": "synthetic01", "url": "https://cdn.example/media"}
        work = self.map_work(raw)
        self.assert_work_fields(work, title="", description="", topics=(), author="",
                                cover_url="", duration_seconds=None)
        variant = work.items[0].default_variant
        self.assert_variant_fields(variant, variant_fields(raw["url"]))
        self.assertEqual(field_coverage(work), dict.fromkeys(("title", "cover", "quality", "size")))
        self.assertEqual(fact_gaps(work), ("TRACKS_UNKNOWN",))
        self.assert_variant_sources(work, work.items[0], variant, native=("url",),
                                    defaults=("tracks", "browser_fetchable", "is_original", "drm", "segmented"))
        self.assertEqual(decide_delivery(Kind.VIDEO, variant).method, Method.BROWSER_STREAMING)

    def test_cli_extension_defaults_are_not_track_evidence(self):
        for vcodec, acodec in ((None, None), ("avc1.640028", None), (None, "mp4a.40.2")):
            with self.subTest(vcodec=vcodec, acodec=acodec):
                raw = {"id": "synthetic01", "url": "https://cdn.example/media.mp4",
                       "ext": "mp4", "video_ext": "mp4", "audio_ext": "none",
                       "vcodec": vcodec, "acodec": acodec}
                work = self.map_work(raw)
                variant = work.items[0].default_variant
                self.assertIs(variant.tracks, Tracks.UNKNOWN)
                self.assertEqual(variant.merge_with, "")
                self.assertEqual(fact_gaps(work), ("TRACKS_UNKNOWN",))
                self.assert_variant_sources(work, work.items[0], variant, defaults=("tracks",))

    def test_resolution_labels_only_use_explicit_p_values(self):
        for label, height in (("1080P", 1080), ("2160P", 2160), ("1080P+", 1080),
                              ("4K", None), ("\u539f\u753b", None), ("\u9ad8\u6e05", None)):
            with self.subTest(label=label):
                raw = {"id": "synthetic01", "url": "https://cdn.example/media", "format_note": label}
                variant = self.map_work(raw).items[0].default_variant
                self.assertEqual(variant.height, height)
                self.assertEqual(variant.quality_label, label)
                self.assertFalse(variant.is_original)
                self.assertIs(variant.tracks, Tracks.UNKNOWN)

    def test_placeholder_title_and_labels_are_missing_not_invented(self):
        for value in ("video", "unknown", "\u539f\u59cb\u6587\u4ef6"):
            with self.subTest(value=value):
                raw = {"id": "synthetic01", "url": "https://cdn.example/media",
                       "title": value, "format_note": value, "filesize": 0}
                work = self.map_work(raw)
                self.assertEqual(work.title, "")
                self.assertEqual(work.items[0].default_variant.quality_label, "")
                self.assertIsNone(work.items[0].default_variant.size_bytes)

    def test_invalid_optional_numbers_do_not_become_facts(self):
        for value in (None, 0, -1, True, "1080", float("nan"), float("inf")):
            with self.subTest(value=value):
                raw = {"id": "synthetic01", "url": "https://cdn.example/media"}
                raw.update(dict.fromkeys(("height", "width", "fps", "tbr", "filesize", "duration"), value))
                work = self.map_work(raw)
                variant = work.items[0].default_variant
                self.assertIsNone(work.duration_seconds)
                for name in ("height", "width", "fps", "bitrate", "size_bytes"):
                    self.assertIsNone(getattr(variant, name), name)
                self.assertFalse(variant.size_exact)

    def test_root_url_alias_does_not_add_another_format(self):
        self.raw.update(self.raw["formats"][1])
        work = self.map_work()
        self.assertEqual(len(work.items[0].variants), 7)

    def test_same_url_distinct_formats_are_not_collapsed(self):
        raw = self.complete_only()
        raw["formats"][1]["url"] = raw["formats"][0]["url"]
        raw.update(raw["formats"][1])
        item = self.map_work(raw).items[0]
        self.assertEqual(len(item.variants), 2)
        self.assertEqual([variant.height for variant in item.variants], [360, 720])
        self.assertEqual(item.variants[0].url, item.variants[1].url)
        self.assertIs(item.default_variant, item.variants[1])

    def test_no_requested_formats_means_no_implicit_audio_selection(self):
        self.raw.pop("requested_formats")
        item = self.map_work().items[0]
        self.assertEqual(len(item.variants), 7)
        self.assertTrue(all(not variant.merge_with for variant in item.variants))
        self.assertEqual(item.default_variant.height, 720)
        self.assertIs(item.default_variant.tracks, Tracks.AV)

    def test_requested_downloads_explicit_pair_is_preserved(self):
        self.raw["requested_downloads"] = [{"requested_formats": self.raw.pop("requested_formats")}]
        item = self.map_work().items[0]
        self.assertEqual(item.variants[2].merge_with, item.variants[3].variant_id)
        self.assertIs(item.default_variant, item.variants[2])

    def test_selected_formats_not_in_formats_are_retained(self):
        self.raw["formats"] = self.raw["formats"][:2]
        item = self.map_work().items[0]
        self.assertEqual(len(item.variants), 4)
        self.assertEqual(item.variants[2].merge_with, item.variants[3].variant_id)
        self.assertIs(item.default_variant, item.variants[2])

    def test_ambiguous_requested_audio_is_not_chosen_by_adapter(self):
        self.raw["requested_formats"].append(deepcopy(self.raw["formats"][4]))
        item = self.map_work().items[0]
        self.assertTrue(all(not variant.merge_with for variant in item.variants))
        self.assertEqual(item.default_variant.height, 720)

    def test_unpaired_video_only_result_is_invalid(self):
        self.raw.pop("requested_formats")
        self.raw["formats"] = self.raw["formats"][2:4]
        result = self.map_raw(self.raw)
        self.assertIsInstance(result, Failure)
        self.assertIs(result.kind, FailureKind.INVALID_RESULT)

    def test_d02_hls_and_drm_retained_but_blocked(self):
        item = self.map_work().items[0]
        for variant, reason in ((item.variants[5], "SEGMENTED_STREAM"), (item.variants[6], "DRM_PROTECTED")):
            with self.subTest(reason=reason):
                decision = decide_delivery(item.kind, variant)
                self.assertEqual((decision.rule, decision.method, decision.reason), (0, Method.NONE, reason))
        self.assertTrue(item.default_variant.deliverable)

    def test_d02_all_blocked_formats_still_produce_a_visible_work(self):
        self.raw.pop("requested_formats")
        self.raw["formats"] = self.raw["formats"][5:]
        item = self.map_work().items[0]
        self.assertEqual(len(item.variants), 2)
        self.assertIs(item.default_variant, item.variants[1])
        self.assertEqual(decide_delivery(item.kind, item.default_variant).reason, "DRM_PROTECTED")

    def test_d02_protocol_fragments_and_url_suffix_are_facts(self):
        cases = [
            {"url": "https://cdn.example/live.M3U8?format=mp4"},
            {"url": "https://cdn.example/live.mpd#part"},
            {"url": "https://cdn.example/part.m4s"},
            {"url": "https://cdn.example/manifest", "protocol": "m3u8_native"},
            {"url": "https://cdn.example/manifest", "protocol": "http_dash_segments"},
            {"url": "https://cdn.example/manifest", "fragments": [{"url": "https://cdn.example/part"}]},
        ]
        for row in cases:
            with self.subTest(row=row):
                item = self.map_work({"id": "synthetic01", **row}).items[0]
                self.assertTrue(item.default_variant.segmented)
                self.assertEqual(decide_delivery(item.kind, item.default_variant).reason, "SEGMENTED_STREAM")

    def test_default_order_is_original_height_bitrate_size_then_upstream_order(self):
        cases = (
            ({"is_original": True, "height": 360}, {"height": 2160}, 0),
            ({"height": 1080, "tbr": 10}, {"height": 720, "tbr": 9000}, 0),
            ({"tbr": 800, "filesize": 9000000}, {"tbr": 801, "filesize": 1}, 1),
            ({"filesize": 3000000}, {"filesize": 4000000}, 1),
            ({}, {}, 0),
        )
        for first, second, selected in cases:
            with self.subTest(first=first, second=second):
                raw = self.complete_only()
                base = raw["formats"][0]
                raw["formats"] = [{**base, "format_id": str(index),
                                   "url": f"https://cdn.example/{index}.mp4", **overrides}
                                  for index, overrides in enumerate((first, second))]
                item = self.map_work(raw).items[0]
                self.assertIs(item.default_variant, item.variants[selected])

    def test_required_header_names_not_values_and_no_cors_guess(self):
        raw = self.complete_only()
        raw["http_headers"] = {"User-Agent": "Synthetic fixture", "Referer": self.url, "Cookie": "REDACTED"}
        raw["required_headers"] = ["Referer"]
        work = self.map_work(raw)
        for variant in work.items[0].variants:
            self.assertEqual(variant.required_headers, ("cookie", "referer"))
            self.assertIs(variant.browser_fetchable, Fetchable.UNKNOWN)
            self.assertEqual(variant.host_policy, ("cdn.example",))
            self.assertNotIn("REDACTED", repr(variant.required_headers))

    def test_review_cookie_strings_require_cookie_without_exposing_values(self):
        for scope in ("raw", "format"):
            with self.subTest(scope=scope):
                raw = self.complete_only()
                target = raw if scope == "raw" else raw["formats"][0]
                target["cookies"] = "session=SYNTHETIC_COOKIE_VALUE"
                work = self.map_work(raw)
                self.assertNotIn("SYNTHETIC_COOKIE_VALUE", repr(work))
                self.assertNotIn("session=", repr(work))
                expected = [("cookie",), ("cookie",) if scope == "raw" else ()]
                self.assertEqual([variant.required_headers for variant in work.items[0].variants], expected)
                self.assertEqual(decide_delivery(Kind.VIDEO, work.items[0].variants[0]).method,
                                 Method.SERVER_ATTACHMENT)

    def test_review_duplicate_requested_format_preserves_added_restrictions(self):
        cases = (
            ({"has_drm": True}, (True, False, ())),
            ({"protocol": "http_dash_segments"}, (False, True, ())),
            ({"http_headers": {"Authorization": "Bearer SYNTHETIC_AUTH_VALUE"}},
             (False, False, ("authorization",))),
            ({"has_drm": True, "protocol": "http_dash_segments",
              "http_headers": {"Authorization": "Bearer SYNTHETIC_AUTH_VALUE"}},
             (True, True, ("authorization",))),
        )
        for restrictions, expected in cases:
            with self.subTest(restrictions=tuple(restrictions)):
                raw = self.complete_only()
                selected = {**deepcopy(raw["formats"][1]), **restrictions}
                raw["requested_formats"] = [selected]
                work = self.map_work(raw)
                item = work.items[0]
                self.assertNotIn("SYNTHETIC_AUTH_VALUE", repr(work))
                matches = [variant for variant in item.variants if variant.url == selected["url"]]
                self.assertEqual(len(item.variants), 2)
                self.assertEqual(len(matches), 1)
                variant = matches[0]
                self.assertEqual((variant.drm, variant.segmented, variant.required_headers), expected)
                if expected[0] or expected[1]:
                    self.assertEqual(item.default_variant.height, 360)
                    self.assertEqual(decide_delivery(item.kind, variant).method, Method.NONE)
                else:
                    self.assertIs(item.default_variant, variant)
                    self.assertEqual(decide_delivery(item.kind, variant).method, Method.SERVER_ATTACHMENT)

    def test_review_selected_root_drm_does_not_contaminate_clear_formats(self):
        raw = self.complete_only()
        protected = deepcopy(self.raw["formats"][6])
        raw["formats"].append(protected)
        raw.update(protected)
        item = self.map_work(raw).items[0]
        self.assertEqual(len(item.variants), 3)
        self.assertEqual([variant.drm for variant in item.variants], [False, False, True])
        self.assertEqual(item.default_variant.url, raw["formats"][1]["url"])
        self.assertEqual(item.default_variant.height, 720)
        self.assertTrue(item.default_variant.deliverable)
        self.assertEqual(decide_delivery(item.kind, item.variants[2]).reason, "DRM_PROTECTED")

    def test_review_direct_requested_downloads_retained_without_cross_selection_pairing(self):
        for root_url in (False, True):
            with self.subTest(root_url=root_url):
                raw = deepcopy(self.raw)
                clear, video, audio = (deepcopy(raw["formats"][index]) for index in (1, 2, 3))
                raw.pop("formats")
                raw.pop("requested_formats")
                raw["requested_downloads"] = [clear, video, audio]
                if root_url:
                    raw.update(clear)
                item = self.map_work(raw).items[0]
                self.assertEqual([variant.url for variant in item.variants],
                                 [clear["url"], video["url"], audio["url"]])
                self.assertEqual([variant.tracks for variant in item.variants],
                                 [Tracks.AV, Tracks.VIDEO_ONLY, Tracks.AUDIO_ONLY])
                self.assertEqual([variant.merge_with for variant in item.variants], ["", "", ""])
                self.assertIs(item.default_variant, item.variants[0])
                self.assertEqual(item.default_variant.height, 720)

    def test_expiry_is_only_taken_from_a_known_upstream_expiry_field(self):
        for host, expiry in (("r1.googlevideo.com", datetime(2033, 5, 18, 3, 33, 20, tzinfo=timezone.utc)),
                             ("cdn.example", None)):
            with self.subTest(host=host):
                raw = {"id": "synthetic01", "url": f"https://{host}/media?expire=2000000000"}
                variant = self.map_work(raw).items[0].default_variant
                self.assertEqual(variant.expires_at, expiry)
                self.assertEqual(variant.host_policy, (host,))

    def test_wrong_source_identity_is_rejected(self):
        for url in ("https://www.youtube.com/watch?v=other",
                    "https://youtu.be/other", "https://www.youtube.com/shorts/other",
                    "https://www.youtube.com/embed/other", "https://www.youtube.com/live/other"):
            with self.subTest(url=url):
                result = self.map_raw(self.raw, url)
                self.assertEqual(result, Failure(FailureKind.INVALID_RESULT, "YTDLP_IDENTITY_MISMATCH"))

    def test_invalid_raw_shapes_are_safe_failures(self):
        cases = (None, [], "not raw", {}, {"id": True}, {"id": []},
                 {"id": "synthetic01"}, {"id": "synthetic01", "formats": {}},
                 {"id": "synthetic01", "formats": [None]},
                 {"id": "synthetic01", "url": "javascript:invalid"},
                 {"id": "synthetic01", "url": "https://user:REDACTED@cdn.example/media"},
                 {"id": "synthetic01", "_type": "url", "url": self.url},
                 {"id": "synthetic01", "_type": "url_transparent", "url": self.url},
                 {"id": "synthetic01", "_type": "multi_video", "entries": []},
                 {"id": "synthetic01", "_type": "multi_video", "entries": [None]},
                 {"id": "synthetic01", "_type": "multi_video", "entries": [{"id": "nested", "entries": []}]})
        for raw in cases:
            with self.subTest(raw=raw):
                result = self.map_raw(raw)
                self.assertIsInstance(result, Failure)
                self.assertIs(result.kind, FailureKind.INVALID_RESULT)
                self.assertRegex(result.code, r"^[A-Z][A-Z0-9_]{0,63}$")


class YtDlpFetchAndFailureTests(TestCase):
    def test_fetch_reuses_bounded_existing_client_and_returns_raw_unchanged(self):
        document = fixture("audio")
        adapter = YtDlpAdapter(binary="synthetic-yt-dlp")
        with patch(UPSTREAM, return_value=document["raw"]) as upstream:
            self.assertIs(adapter.fetch(document["source_url"], 2.5), document["raw"])
        upstream.assert_called_once_with(document["source_url"], binary="synthetic-yt-dlp", timeout_seconds=2.5)

    def test_parse_fetches_once_and_runs_real_map_under_io_guard(self):
        document = fixture("youtube_formats")
        before = deepcopy(document["raw"])
        adapter = YtDlpAdapter(binary="synthetic-yt-dlp")
        real_map = adapter.map

        def guarded_map(raw, source_url):
            with deny_map_io():
                return real_map(raw, source_url)

        with patch(UPSTREAM, return_value=document["raw"]) as upstream, \
                patch.object(adapter, "map", side_effect=guarded_map) as mapping:
            result = adapter.parse(document["source_url"], 10)
        upstream.assert_called_once_with(document["source_url"], binary="synthetic-yt-dlp", timeout_seconds=10)
        mapping.assert_called_once_with(document["raw"], document["source_url"])
        self.assertIsInstance(result, Work)
        self.assertEqual(validate_work(result), ())
        self.assertEqual(document["raw"], before)

    def test_exception_classifications_and_diagnostics_do_not_leak_raw_messages(self):
        cases = (
            (TimeoutError("deadline"), FailureKind.TIMEOUT),
            (XhsProviderTimeoutError("yt-dlp \u89e3\u6790\u8d85\u65f6"), FailureKind.TIMEOUT),
            (RuntimeError("connection timed out"), FailureKind.TIMEOUT),
            (RuntimeError("Unsupported URL"), FailureKind.UNSUPPORTED),
            (RuntimeError("Video has been removed"), FailureKind.NOT_FOUND),
            (RuntimeError("Private video"), FailureKind.NOT_FOUND),
            (RuntimeError("Video unavailable"), FailureKind.UNAVAILABLE),
            (RuntimeError("Video unavailable. This content is not available in your country"), FailureKind.UNAVAILABLE),
            (RuntimeError("This video does not exist"), FailureKind.NOT_FOUND),
            (RuntimeError("yt-dlp returned invalid JSON"), FailureKind.INVALID_RESULT),
            (RuntimeError("yt-dlp result format is invalid"), FailureKind.INVALID_RESULT),
            (RuntimeError("No video formats found"), FailureKind.INVALID_RESULT),
            (RuntimeError("Requested format is not available"), FailureKind.INVALID_RESULT),
            (FileNotFoundError("executable not found"), FailureKind.UNAVAILABLE),
            (RuntimeError("upstream refused"), FailureKind.UNAVAILABLE),
        )
        for error, expected in cases:
            with self.subTest(error=type(error).__name__, message=str(error)):
                error.args = (str(error) + " https://source.example/?token=SECRET Cookie: PRIVATE",)
                with deny_map_io():
                    result = failure_for_exception(error)
                self.assertIsInstance(result, Failure)
                self.assertIs(result.kind, expected)
                self.assertRegex(result.code, r"^[A-Z][A-Z0-9_]{0,63}$")
                self.assertNotIn("SECRET", repr(result))
                self.assertNotIn("PRIVATE", repr(result))

    def test_http_failure_mapping(self):
        for status, kind in ((404, FailureKind.NOT_FOUND), (410, FailureKind.NOT_FOUND),
                             (408, FailureKind.TIMEOUT), (412, FailureKind.UNAVAILABLE),
                             (429, FailureKind.UNAVAILABLE), (500, FailureKind.UNAVAILABLE),
                             (503, FailureKind.UNAVAILABLE), (504, FailureKind.UNAVAILABLE),
                             (401, FailureKind.UNAVAILABLE), (403, FailureKind.UNAVAILABLE),
                             (400, FailureKind.INVALID_RESULT)):
            with self.subTest(status=status):
                self.assertEqual(failure_for_exception(RuntimeError(f"HTTP Error {status}")),
                                 Failure(kind, f"HTTP_{status}"))

    def test_fetch_failure_skips_mapping_and_does_not_retry(self):
        adapter = YtDlpAdapter(binary="synthetic-yt-dlp")
        for error, expected in ((RuntimeError("HTTP Error 429"), FailureKind.UNAVAILABLE),
                                (XhsProviderTimeoutError("yt-dlp \u89e3\u6790\u8d85\u65f6"), FailureKind.TIMEOUT),
                                (FileNotFoundError("missing binary"), FailureKind.UNAVAILABLE)):
            with self.subTest(expected=expected), patch(UPSTREAM, side_effect=error) as upstream, \
                    patch.object(adapter, "map", side_effect=AssertionError("must not map a fetch failure")) as mapping:
                result = adapter.parse("https://source.example/work", 10)
                self.assertIs(result.kind, expected)
                upstream.assert_called_once()
                mapping.assert_not_called()
