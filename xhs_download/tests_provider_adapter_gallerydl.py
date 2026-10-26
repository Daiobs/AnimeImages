"""P4 synthetic native message streams, mapped offline without fetching.

Directory is work/display context, not downloader context. Native HTTP reads
Url metadata's _http_headers; None removes a name, and custom defaults are real
instructions. No session-cookie absence or browser-access claim follows from
missing private metadata. No invented private cookie message protocol is used.

These are complete synthetic inputs, not captures. Optional codec, quality,
size and DRM facts exercise explicitly supplied metadata, not a claim that all
gallery-dl extractors emit it. Only explicit file identities permit variants;
nested media, thumbnails and fallbacks do not create extra download resources.
"""
from copy import deepcopy
from dataclasses import fields
from pathlib import Path
import re
from unittest import TestCase
from urllib.parse import urlsplit

from xhs_download.services.pipeline import (
    Completeness, Failure, FailureKind, Fetchable, Kind, Method, Tracks, Variant,
    Work, decide_delivery, fact_gaps, field_coverage, validate_work,
)
from xhs_download.services.pipeline.model import DEFAULT_QUALITY_LABEL
from xhs_download.services.provider_adapters import ProviderAdapter, RawMapper
from xhs_download.services.provider_adapters.gallerydl import GalleryDlMapper
from xhs_download.services.provider_adapters.recordings import check, load_recording
from xhs_download.tests_provider_adapters import deny_map_io


NAME = "gallery_dl_generic"
FIXTURES = Path(__file__).parent / "fixtures" / "provider_raw" / NAME
SOURCE = "https://source.example/posts/synthetic-work"
MEDIA = "https://cdn.example/media.mp4"


def fixture(case):
    return load_recording(FIXTURES / f"synthetic_{case}.json")


def stream(*rows, directory=None):
    context = {"post_id": "synthetic-work"} if directory is None else directory
    return [[2, deepcopy(context)], *[[3, url, deepcopy(metadata)] for url, metadata in rows]]


def variant_fields(url, **changes):
    return {
        "url": url, "container": "", "tracks": Tracks.UNKNOWN,
        "height": None, "width": None, "fps": None, "bitrate": None,
        "quality_label": "", "is_original": False, "size_bytes": None,
        "size_exact": False, "merge_with": "", "required_headers": (),
        "browser_fetchable": Fetchable.UNKNOWN, "drm": False,
        "segmented": False, "request_method": "GET", "expires_at": None,
        "host_policy": (urlsplit(url).hostname,), **changes,
    }


class GalleryDlSyntheticFixtureTests(TestCase):
    def test_complete_synthetic_envelopes_are_sanitized_and_use_reserved_hosts(self):
        paths = sorted(FIXTURES.glob("synthetic_*.json"))
        self.assertTrue(paths)
        for path in paths:
            with self.subTest(case=path.name):
                document = load_recording(path)
                check(document)
                self.assertEqual(document["provider"], NAME)
                self.assertEqual(document["platform"], "synthetic")
                self.assertEqual(document["provenance"], {
                    "kind": "synthetic", "raw_kind": "json", "complete": True,
                })
                self.assertIsInstance(document["raw"], list)
                for match in re.finditer(r"https?://[^\s'\"<>]+", repr(document)):
                    self.assertTrue(urlsplit(match[0]).hostname.endswith(".example"))


class GalleryDlMappingTests(TestCase):
    def setUp(self):
        self.mapper = GalleryDlMapper()

    def map_raw(self, raw, source_url=SOURCE):
        before = deepcopy(raw)
        with deny_map_io():
            first = self.mapper.map(raw, source_url)
            repeated = self.mapper.map(raw, source_url)
        self.assertEqual(raw, before, "map mutated native upstream messages")
        self.assertEqual(first, repeated, "map IDs or facts are not stable")
        return first

    def map_work(self, raw, source_url=SOURCE):
        work = self.map_raw(raw, source_url)
        self.assertIsInstance(work, Work)
        self.assertEqual(validate_work(work), ())
        self.assertEqual((work.platform, work.provider), ("", ""))
        self.assertFalse(hasattr(work, "raw"))
        self.assertFalse(hasattr(work, "metadata"))
        self.assertEqual(len({item.item_id for item in work.items}), len(work.items))
        variants = [v for item in work.items for v in item.variants]
        self.assertEqual(len({v.variant_id for v in variants}), len(variants))
        for index, item in enumerate(work.items, 1):
            self.assertTrue(item.item_id)
            self.assertEqual(item.index, index)
            self.assertIn(item.default_variant, item.variants)
            self.assertEqual(set(vars(item)), {"item_id", "kind", "index", "variants"})
            for variant in item.variants:
                self.assertTrue(variant.variant_id)
                self.assertIs(variant.browser_fetchable, Fetchable.UNKNOWN)
                self.assertEqual(variant.host_policy, (urlsplit(variant.url).hostname,))
                prefix = f"items.{item.item_id}.variants.{variant.variant_id}."
                for field in fields(Variant):
                    self.assertTrue(work.field_sources.get(prefix + field.name), prefix + field.name)
        return work

    def map_fixture(self, case):
        document = fixture(case)
        return self.map_work(document["raw"], document["source_url"])

    def assert_failure(self, raw, kind=FailureKind.INVALID_RESULT, source_url=SOURCE):
        result = self.map_raw(raw, source_url)
        self.assertIsInstance(result, Failure)
        self.assertIs(result.kind, kind)
        self.assertRegex(result.code, r"^[A-Z][A-Z0-9_]{0,63}$")
        self.assertEqual(set(vars(result)), {"kind", "code"})
        return result

    def assert_variant(self, variant, expected):
        self.assertEqual(set(expected) | {"variant_id"}, {field.name for field in fields(Variant)})
        self.assertEqual({name: getattr(variant, name) for name in expected}, expected)

    def test_public_api_is_map_only_and_wildcard_platform(self):
        self.assertEqual((self.mapper.name, self.mapper.platforms), (NAME, ("*",)))
        self.assertIsInstance(self.mapper, RawMapper)
        self.assertNotIsInstance(self.mapper, ProviderAdapter)
        self.assertFalse(hasattr(self.mapper, "fetch"))
        self.assertFalse(hasattr(self.mapper, "parse"))

    def test_all_work_fields_and_mixed_image_video_audio(self):
        work = self.map_fixture("mixed_media")
        expected = {
            "source_url": fixture("mixed_media")["source_url"],
            "work_id": "synthetic-mixed", "platform": "", "provider": "",
            "title": "Synthetic mixed media", "description": "Explicitly supplied metadata.",
            "topics": ("mixed", "synthetic"), "author": "Synthetic creator",
            "cover_url": "https://images.example/mixed/cover.jpg",
            "duration_seconds": 12.5, "groups": (), "completeness": Completeness.FINAL,
        }
        self.assertEqual(set(expected) | {"items", "field_sources"}, {f.name for f in fields(Work)})
        self.assertEqual({name: getattr(work, name) for name in expected}, expected)
        self.assertEqual([item.kind for item in work.items], [Kind.IMAGE, Kind.VIDEO, Kind.AUDIO])
        self.assertEqual([len(item.variants) for item in work.items], [1, 1, 1])
        expected_variants = (
            variant_fields("https://cdn.example/mixed/photo.jpg", container="jpg",
                           width=1600, height=1200, size_bytes=240000, size_exact=True),
            variant_fields("https://cdn.example/mixed/video.mp4", container="mp4",
                           tracks=Tracks.AV, width=1920, height=1080, quality_label="1080P+",
                           fps=30, bitrate=2500000, size_bytes=3750000, size_exact=True,
                           required_headers=("accept", "cookie", "referer", "user-agent")),
            variant_fields("https://cdn.example/mixed/audio", container="mp3", size_bytes=200000),
        )
        for item, expected_variant in zip(work.items, expected_variants):
            self.assert_variant(item.default_variant, expected_variant)
        self.assertEqual(fact_gaps(work), ("TRACKS_UNKNOWN",))
        self.assertEqual(field_coverage(work), dict.fromkeys(("title", "cover", "quality", "size"), NAME))
        for name in ("work_id", "title", "description", "topics", "author", "cover", "duration_seconds", "items"):
            self.assertEqual(work.field_sources[name], NAME, name)
        self.assertEqual(work.field_sources["source_url"], "request")
        for name in ("provider", "platform", "completeness"):
            self.assertNotEqual(work.field_sources.get(name), NAME)
        self.assertNotIn("REDACTED", repr(work))
        self.assertNotIn("private-context", repr(work))

    def test_album_identity_and_title_survive_directory_per_image(self):
        work = self.map_fixture("album")
        self.assertEqual((work.work_id, work.title), ("synthetic-album", "Synthetic album"))
        self.assertEqual([item.kind for item in work.items], [Kind.IMAGE, Kind.IMAGE])
        self.assertEqual([item.default_variant.url for item in work.items], [
            "https://cdn.example/album/first.jpg", "https://cdn.example/album/second.webp",
        ])
        self.assertEqual([item.default_variant.required_headers for item in work.items], [(), ()])
        self.assertEqual(fact_gaps(work), ())

    def test_sparse_native_image_preserves_tags_and_does_not_invent_measurements(self):
        work = self.map_fixture("sparse_image")
        self.assertEqual((work.work_id, work.title, work.description), (
            "synthetic-image", "Synthetic illustration", "An image with no measured download facts.",
        ))
        self.assertEqual(work.topics, ("illustration", "synthetic"))
        self.assertIn(work.author, ("Synthetic Creator", "synthetic_creator"))
        self.assertIn(work.cover_url, ("https://images.example/preview.png", "https://images.example/preview-xl.png"))
        self.assertEqual(len(work.items), 1)
        item = work.items[0]
        self.assertIs(item.kind, Kind.IMAGE)
        self.assertEqual(len(item.variants), 1)
        self.assert_variant(item.default_variant, variant_fields(
            "https://cdn.example/images/original.png", container="png"))
        self.assertEqual(field_coverage(work), {"title": NAME, "cover": NAME, "quality": None, "size": None})

    def test_list_and_tuple_native_message_shapes(self):
        metadata = {"post_id": "synthetic-work", "title": "Directory title"}
        for directory in ([2, metadata], [2, "", metadata]):
            for tuples in (False, True):
                with self.subTest(directory_size=len(directory), tuples=tuples):
                    raw = [deepcopy(directory), [3, MEDIA, {"extension": "mp4"}]]
                    if tuples:
                        raw = tuple(tuple(message) for message in raw)
                    work = self.map_work(raw)
                    self.assertEqual((work.work_id, work.title), ("synthetic-work", "Directory title"))

    def test_work_identity_supported_keys_and_integer_ids(self):
        for key in ("post_id", "post_shortcode", "tweet_id", "status_id", "pageid", "id", "shortcode", "slug"):
            for identity in ("synthetic-work", 12345):
                with self.subTest(key=key, identity=identity):
                    work = self.map_work(stream((MEDIA, {}), directory={key: identity}))
                    self.assertEqual(work.work_id, str(identity))

    def test_without_directory_uses_first_url_metadata_as_work_context(self):
        raw = [
            [3, "https://cdn.example/1.jpg", {"post_id": "synthetic-work", "num": 1,
                                              "title": "First title", "tags": ["explicit"]}],
            [3, "https://cdn.example/2.jpg", {"post_id": "synthetic-work", "num": 2,
                                              "title": "Second title"}],
        ]
        work = self.map_work(raw)
        self.assertEqual((work.work_id, work.title, work.topics), ("synthetic-work", "First title", ("explicit",)))
        self.assertEqual(len(work.items), 2)

    def test_instagram_work_identity_is_not_media_identity(self):
        for directory in ({"post_id": "synthetic-work", "post_shortcode": "post-short"},
                          {"post_shortcode": "post-short"}):
            with self.subTest(directory=directory):
                raw = stream(
                    ("https://cdn.example/first.jpg", {**directory, "media_id": "one", "num": 1, "shortcode": "file-one"}),
                    ("https://cdn.example/second.jpg", {**directory, "media_id": "two", "num": 2, "shortcode": "file-two"}),
                    directory=directory,
                )
                work = self.map_work(raw)
                self.assertEqual(work.work_id, directory.get("post_id", directory["post_shortcode"]))
                self.assertEqual(len(work.items), 2)

    def test_multiple_distinct_work_identities_are_invalid_not_a_mixed_work(self):
        first = stream((MEDIA, {"post_id": "synthetic-work"}))
        cases = (
            first + [[2, {"post_id": "another-work"}], [3, "https://cdn.example/other.mp4", {}]],
            first + [[3, "https://cdn.example/other.mp4", {"post_id": "another-work"}]],
            first[1:] + [[3, "https://cdn.example/other.mp4", {"post_id": "another-work"}]],
            [[2, {"id": "first"}], [3, MEDIA, {}], [2, {"id": "second"}], [3, MEDIA, {}]],
            [[2, {"id": "image", "album": {"id": "album-one"}}], [3, MEDIA, {}],
             [2, {"id": "image", "album": {"id": "album-two"}}], [3, MEDIA, {}]],
        )
        for raw in cases:
            with self.subTest(raw=raw):
                self.assert_failure(raw)

    def test_missing_or_invalid_work_identity_is_not_invented_from_url(self):
        for value in (None, "", "  ", True, False, [], {}, 1.5):
            with self.subTest(value=value):
                self.assert_failure(stream((MEDIA, {}), directory={"post_id": value}))
        self.assert_failure([[3, MEDIA, {"media_id": "file-not-work", "num": 1}]])

    def test_only_emitted_url_messages_create_media(self):
        metadata = {
            "post_id": "synthetic-work", "extension": "mp4",
            "thumbnail": "https://images.example/cover.jpg",
            "thumbnails": [{"url": "https://images.example/thumb.jpg"}],
            "avatar": {"url": "https://images.example/avatar.jpg"},
            "_fallback": ["https://fallback.example/backup.mp4"],
            "video_url": "https://cdn.example/not-emitted.mp4",
            "files": [{"url": "https://cdn.example/sibling.mp4"}],
            "nested": {"formats": [{"url": "https://cdn.example/recursive.mp4"}]},
        }
        work = self.map_work(stream((MEDIA, metadata)))
        self.assertEqual([v.url for item in work.items for v in item.variants], [MEDIA])
        self.assert_failure([[2, metadata]])
        self.assert_failure([[2, metadata], [3, "", metadata]])

    def test_repeated_explicit_file_identity_groups_variants(self):
        for key in ("media_id", "file_id", "image_id", "video_id", "num"):
            with self.subTest(key=key):
                raw = stream((MEDIA, {key: 1, "quality": "720P"}),
                             ("https://cdn.example/high.mp4", {key: 1, "quality": "1080P"}))
                work = self.map_work(raw)
                self.assertEqual(len(work.items), 1)
                self.assertEqual([v.url for v in work.items[0].variants], [MEDIA, "https://cdn.example/high.mp4"])
                self.assertIs(work.items[0].default_variant, work.items[0].variants[1])

    def test_distinct_file_ids_or_numbers_never_collapse_same_url_and_label(self):
        for key in ("media_id", "file_id", "image_id", "video_id", "num"):
            with self.subTest(key=key):
                work = self.map_work(stream((MEDIA, {key: 1, "quality": "720P"}),
                                            (MEDIA, {key: 2, "quality": "720P"})))
                self.assertEqual(len(work.items), 2)
                self.assertEqual([len(item.variants) for item in work.items], [1, 1])

    def test_image_identity_used_as_work_context_does_not_override_file_number(self):
        raw = stream((MEDIA, {"image_id": "work-image", "num": 1}),
                     (MEDIA, {"image_id": "work-image", "num": 2}))
        self.assertEqual(len(self.map_work(raw).items), 2)

    def test_missing_reliable_file_identity_keeps_every_emission_separate(self):
        for metadata in ({}, {"filename": "same", "quality": "720P"},
                         {"num": None}, {"media_id": ""}, {"media_id": True}):
            with self.subTest(metadata=metadata):
                work = self.map_work(stream((MEDIA, metadata), (MEDIA, metadata)))
                self.assertEqual(len(work.items), 2)
                self.assertEqual([item.default_variant.url for item in work.items], [MEDIA, MEDIA])

    def test_duplicate_variant_labels_and_urls_keep_ordinal_unique_ids(self):
        raw = stream(*[(MEDIA, {"media_id": "clip", "quality": label})
                       for label in ("1080P", "1080P", "1080P HDR")])
        item = self.map_work(raw).items[0]
        self.assertEqual(len(item.variants), 3)
        self.assertEqual([v.url for v in item.variants], [MEDIA] * 3)
        self.assertEqual([v.quality_label for v in item.variants], ["1080P", "1080P", "1080P HDR"])

    def test_same_file_same_url_aliases_union_all_restrictions(self):
        raw = stream(
            (MEDIA, {"media_id": "clip", "quality": "720P", "_http_headers": {"Referer": "REDACTED"}}),
            (MEDIA, {"media_id": "clip", "quality": "720P HDR", "has_drm": True,
                     "protocol": "http_dash_segments", "http_headers": {"Authorization": "REDACTED"}}),
        )
        item = self.map_work(raw).items[0]
        self.assertEqual(len(item.variants), 2)
        for variant in item.variants:
            self.assertTrue(variant.drm)
            self.assertTrue(variant.segmented)
            self.assertEqual(variant.required_headers, ("authorization", "referer"))
            self.assertEqual(decide_delivery(item.kind, variant).reason, "DRM_PROTECTED")

    def test_alias_restrictions_do_not_contaminate_another_url_or_file(self):
        for other in (("https://cdn.example/other.mp4", {"media_id": "clip"}),
                      (MEDIA, {"media_id": "another-file"})):
            with self.subTest(other=other):
                raw = stream((MEDIA, {"media_id": "clip", "drm": True,
                                      "_http_headers": {"Cookie": "REDACTED"}}), other)
                work = self.map_work(raw)
                variants = [v for item in work.items for v in item.variants]
                self.assertEqual(len(variants), 2)
                self.assertTrue(variants[0].drm)
                self.assertFalse(variants[1].drm)
                self.assertEqual(variants[1].required_headers, ())

    def test_hdr_and_blocked_variants_are_retained_and_best_downloadable_is_default(self):
        document = fixture("video_variants")
        work = self.map_work(document["raw"], document["source_url"])
        self.assertEqual(len(work.items), 1)
        item = work.items[0]
        self.assertEqual([v.url for v in item.variants], [row[1] for row in document["raw"] if row[0] == 3])
        self.assertEqual(len(item.variants), 5)
        self.assertIs(item.default_variant, item.variants[2])
        self.assertEqual(item.default_variant.quality_label, "2160P HDR")
        self.assertTrue(item.default_variant.deliverable)
        self.assertEqual(decide_delivery(item.kind, item.variants[3]).reason, "DRM_PROTECTED")
        self.assertEqual(decide_delivery(item.kind, item.variants[4]).reason, "SEGMENTED_STREAM")

    def test_all_blocked_variants_still_form_visible_work(self):
        raw = fixture("video_variants")["raw"]
        item = self.map_work([raw[0], *raw[-2:]]).items[0]
        self.assertEqual(len(item.variants), 2)
        self.assertIs(item.default_variant, item.variants[0])
        self.assertFalse(item.default_variant.deliverable)

    def test_default_ranking_uses_original_height_bitrate_size_then_emission_order(self):
        cases = (
            ({"is_original": True, "height": 360}, {"height": 2160}, 0),
            ({"height": 1080, "bitrate": 1}, {"height": 720, "bitrate": 9000}, 0),
            ({"bitrate": 800, "filesize": 9000000}, {"bitrate": 801, "filesize": 1}, 1),
            ({"filesize": 3000000}, {"filesize": 4000000}, 1),
            ({}, {}, 0),
        )
        for first, second, selected in cases:
            with self.subTest(first=first, second=second):
                item = self.map_work(stream(
                    (MEDIA, {"media_id": "clip", **first}),
                    ("https://cdn.example/other.mp4", {"media_id": "clip", **second}),
                )).items[0]
                self.assertIs(item.default_variant, item.variants[selected])

    def test_d02_protocol_fragments_suffix_and_drm_facts(self):
        segmented = (
            ("https://cdn.example/live.M3U8?format=mp4", {}),
            ("https://cdn.example/live.mpd#part", {}),
            ("https://cdn.example/part.m4s", {}),
            (MEDIA, {"protocol": "m3u8_native"}),
            (MEDIA, {"protocol": "http_dash_segments"}),
            (MEDIA, {"fragments": [{"url": "https://cdn.example/part"}]}),
        )
        for url, metadata in segmented:
            with self.subTest(url=url, metadata=metadata):
                item = self.map_work(stream((url, {"extension": "mp4", **metadata}))).items[0]
                self.assertTrue(item.default_variant.segmented)
                self.assertEqual(decide_delivery(item.kind, item.default_variant).reason, "SEGMENTED_STREAM")
        for key in ("drm", "has_drm"):
            with self.subTest(key=key):
                item = self.map_work(stream((MEDIA, {key: True}))).items[0]
                self.assertTrue(item.default_variant.drm)
                self.assertEqual(decide_delivery(item.kind, item.default_variant).method, Method.NONE)

    def test_matching_nested_file_descriptor_supplies_facts_only_for_emitted_url(self):
        descriptor = {"url": MEDIA, "extension": "mp4", "width": 1920, "height": 1080,
                      "filesize": 123456, "vcodec": "h264", "acodec": "aac"}
        for matches in (False, True):
            with self.subTest(matches=matches):
                nested = {**descriptor, "url": MEDIA if matches else "https://cdn.example/sibling.mp4"}
                item = self.map_work(stream((MEDIA, {"file": nested}))).items[0]
                self.assertEqual(len(item.variants), 1)
                expected = variant_fields(MEDIA, container="mp4")
                if matches:
                    expected.update(width=1920, height=1080, size_bytes=123456, size_exact=True, tracks=Tracks.AV)
                self.assert_variant(item.default_variant, expected)

    def test_directory_is_display_context_not_header_or_media_fact_inheritance(self):
        directory = {
            "post_id": "synthetic-work", "title": "Context title",
            "required_headers": ["Referer"], "headers": {"Cookie": "REDACTED"},
            "http_headers": {"Authorization": "REDACTED"},
            "_http_headers": {"User-Agent": "REDACTED"},
            "height": 2160, "filesize": 999999, "vcodec": "h264", "acodec": "aac",
        }
        work = self.map_work(stream((MEDIA, {}), directory=directory))
        self.assertEqual(work.title, "Context title")
        self.assert_variant(work.items[0].default_variant, variant_fields(MEDIA, container="mp4"))

    def test_native_http_headers_include_customary_names_but_not_none(self):
        metadata = {"_http_headers": {
            "Referer": "https://source.example/private-header-value", "User-Agent": "REDACTED",
            "Accept": "video/*", "Cookie": "REDACTED", "X-Empty": "", "Authorization": None,
        }}
        work = self.map_work(stream((MEDIA, metadata)))
        variant = work.items[0].default_variant
        self.assertEqual(variant.required_headers, ("accept", "cookie", "referer", "user-agent", "x-empty"))
        self.assertEqual(decide_delivery(Kind.VIDEO, variant).method, Method.SERVER_ATTACHMENT)
        for value in ("REDACTED", "private-header-value", "video/*"):
            self.assertNotIn(value, repr(work))

    def test_native_http_header_context_is_url_local_not_sibling_or_nested(self):
        raw = stream(
            (MEDIA, {"num": 1, "_http_headers": {"Cookie": "REDACTED"}}),
            ("https://cdn.example/second.mp4", {"num": 2}),
            ("https://cdn.example/third.mp4", {"num": 3, "file": {
                "url": "https://cdn.example/third.mp4", "_http_headers": {"Authorization": "REDACTED"},
            }}),
        )
        self.assertEqual([item.default_variant.required_headers for item in self.map_work(raw).items],
                         [("cookie",), (), ()])

    def test_generic_header_aliases_follow_p3_defaults_and_explicit_requirements(self):
        for key in ("headers", "http_headers"):
            for required in ([], ["Referer", "User-Agent", "referer"]):
                with self.subTest(key=key, required=required):
                    metadata = {key: {"User-Agent": "REDACTED", "Referer": SOURCE, "Accept": "*/*",
                                      "Cookie": "REDACTED", "Authorization": "REDACTED", "X-Trace": "REDACTED"},
                                "required_headers": required}
                    variant = self.map_work(stream((MEDIA, metadata))).items[0].default_variant
                    self.assertEqual(variant.required_headers,
                                     tuple(sorted({"cookie", "authorization", "x-trace", *[v.lower() for v in required]})))

    def test_absent_facts_stay_unknown_and_have_contract_field_sources(self):
        work = self.map_work(stream((MEDIA, {})))
        self.assertEqual((work.title, work.description, work.topics, work.author, work.cover_url), ("", "", (), "", ""))
        self.assertIsNone(work.duration_seconds)
        self.assertEqual(work.groups, ())
        self.assertIs(work.completeness, Completeness.FINAL)
        item = work.items[0]
        self.assert_variant(item.default_variant, variant_fields(MEDIA, container="mp4"))
        self.assertEqual(item.default_variant.display_label, DEFAULT_QUALITY_LABEL)
        self.assertEqual(fact_gaps(work), ("TRACKS_UNKNOWN",))
        self.assertEqual(field_coverage(work), dict.fromkeys(("title", "cover", "quality", "size")))
        prefix = f"items.{item.item_id}.variants.{item.default_variant.variant_id}."
        for name in ("tracks", "browser_fetchable"):
            self.assertEqual(work.field_sources[prefix + name], "contract:unknown")
        for name in ("title", "cover", "quality", "size"):
            self.assertNotEqual(work.field_sources.get(name), NAME)

    def test_video_codecs_are_explicit_not_inferred_from_extension_or_partial_hints(self):
        cases = (
            ({}, Tracks.UNKNOWN), ({"vcodec": "h264"}, Tracks.UNKNOWN),
            ({"acodec": "aac"}, Tracks.UNKNOWN),
            ({"vcodec": "unknown", "acodec": "none"}, Tracks.UNKNOWN),
            ({"vcodec": None, "acodec": None, "audio_ext": "none"}, Tracks.UNKNOWN),
            ({"vcodec": "h264", "acodec": "aac"}, Tracks.AV),
        )
        for metadata, tracks in cases:
            with self.subTest(metadata=metadata):
                item = self.map_work(stream((MEDIA, metadata))).items[0]
                self.assertIs(item.kind, Kind.VIDEO)
                self.assertIs(item.default_variant.tracks, tracks)
                self.assertEqual(item.default_variant.merge_with, "")

    def test_explicit_video_only_is_retained_but_never_implicitly_paired(self):
        raw = stream(
            (MEDIA, {"media_id": "clip", "height": 720, "vcodec": "h264", "acodec": "aac"}),
            ("https://cdn.example/video-only.mp4", {"media_id": "clip", "height": 2160,
                                                    "vcodec": "h264", "acodec": "none"}),
            ("https://cdn.example/audio.m4a", {"media_id": "sound", "vcodec": "none", "acodec": "aac"}),
        )
        work = self.map_work(raw)
        self.assertEqual([item.kind for item in work.items], [Kind.VIDEO, Kind.AUDIO])
        video = work.items[0]
        self.assertIs(video.variants[1].tracks, Tracks.VIDEO_ONLY)
        self.assertEqual(video.variants[1].merge_with, "")
        self.assertIs(video.default_variant, video.variants[0])
        self.assertIs(work.items[1].default_variant.tracks, Tracks.AUDIO_ONLY)
        self.assert_failure(stream((MEDIA, {"vcodec": "h264", "acodec": "none"})))

    def test_audio_kind_from_content_type_or_container_does_not_invent_tracks(self):
        for url, metadata, container in (
            ("https://cdn.example/audio.mp3", {}, "mp3"),
            ("https://cdn.example/audio", {"extension": "m4a"}, "m4a"),
            ("https://cdn.example/audio", {"content_type": "audio/mpeg"}, "mp3"),
            ("https://cdn.example/audio", {"mime_type": "audio/ogg"}, "ogg"),
        ):
            with self.subTest(metadata=metadata, container=container):
                item = self.map_work(stream((url, metadata))).items[0]
                self.assertIs(item.kind, Kind.AUDIO)
                self.assertEqual(item.default_variant.container, container)
                self.assertIs(item.default_variant.tracks, Tracks.UNKNOWN)

    def test_container_sources_and_dimensions_are_independent(self):
        for url, metadata, container, kind in (
            ("https://cdn.example/file", {"extension": "jpg"}, "jpg", Kind.IMAGE),
            ("https://cdn.example/file", {"ext": "webp"}, "webp", Kind.IMAGE),
            ("https://cdn.example/file", {"mime": "image/png"}, "png", Kind.IMAGE),
            ("https://cdn.example/file", {"mime_type": "video/mp4"}, "mp4", Kind.VIDEO),
            ("https://cdn.example/file.JPG?token=REDACTED", {}, "jpg", Kind.IMAGE),
        ):
            with self.subTest(metadata=metadata, container=container):
                item = self.map_work(stream((url, metadata))).items[0]
                self.assertIs(item.kind, kind)
                self.assertEqual(item.default_variant.container, container)
                self.assertIsNone(item.default_variant.width)
                self.assertIsNone(item.default_variant.height)

    def test_only_explicit_p_labels_supply_height(self):
        for label, height in (("1080P", 1080), ("2160P", 2160), ("1080P+", 1080),
                              ("2160P HDR", 2160), ("4K", None), ("\u539f\u753b", None), ("\u9ad8\u6e05", None)):
            with self.subTest(label=label):
                variant = self.map_work(stream((MEDIA, {"quality": label}))).items[0].default_variant
                self.assertEqual((variant.quality_label, variant.height), (label, height))
                self.assertFalse(variant.is_original)

    def test_placeholder_titles_and_labels_are_missing_and_description_not_title(self):
        for placeholder in ("video", "unknown", "\u539f\u59cb\u6587\u4ef6"):
            with self.subTest(placeholder=placeholder):
                raw = stream((MEDIA, {"quality": placeholder, "filesize": 0}),
                             directory={"post_id": "synthetic-work", "title": placeholder,
                                        "description": "Body only"})
                work = self.map_work(raw)
                self.assertEqual((work.title, work.description), ("", "Body only"))
                self.assertEqual(work.items[0].default_variant.quality_label, "")
                self.assertIsNone(work.items[0].default_variant.size_bytes)

    def test_invalid_optional_numbers_are_not_facts_and_uncompressed_size_is_ignored(self):
        for value in (None, 0, -1, True, "1080", float("nan"), float("inf")):
            with self.subTest(value=value):
                metadata = dict.fromkeys(("height", "width", "fps", "bitrate", "filesize", "filesize_approx"), value)
                metadata["uncompressed_filesize"] = 9876543
                variant = self.map_work(stream((MEDIA, metadata))).items[0].default_variant
                for name in ("height", "width", "fps", "bitrate", "size_bytes"):
                    self.assertIsNone(getattr(variant, name), name)
                self.assertFalse(variant.size_exact)

    def test_generic_query_expiry_and_access_flags_do_not_create_observed_facts(self):
        url = "https://shard.cdn.example/file.mp4?expire=2000000000&token=REDACTED"
        metadata = {"browser_fetchable": True, "cors": True, "public": True}
        variant = self.map_work(stream((url, metadata))).items[0].default_variant
        self.assertIsNone(variant.expires_at)
        self.assertIs(variant.browser_fetchable, Fetchable.UNKNOWN)
        self.assertEqual(variant.host_policy, ("shard.cdn.example",))

    def test_unexpanded_queue_invalidates_even_otherwise_valid_media(self):
        queue = [6, "https://source.example/delegated", {"post_id": "synthetic-work"}]
        for raw in ([queue], [queue, *stream((MEDIA, {}))], [*stream((MEDIA, {})), queue]):
            with self.subTest(raw=raw):
                self.assert_failure(raw)

    def test_terminal_error_prevents_partial_success(self):
        document = fixture("terminal_error")
        self.assert_failure(document["raw"], FailureKind.UNAVAILABLE, document["source_url"])

    def test_native_error_classes_and_http_statuses_are_safe_failures(self):
        cases = (
            ("NotFoundError", "content not found", FailureKind.NOT_FOUND),
            ("NoExtractorError", "no extractor", FailureKind.UNSUPPORTED),
            ("Timeout", "request timed out", FailureKind.TIMEOUT),
            ("TimeoutError", "deadline", FailureKind.TIMEOUT),
            ("HttpError", "HTTP Error 412", FailureKind.UNAVAILABLE),
            ("HttpError", "HTTP Error 429", FailureKind.UNAVAILABLE),
            ("HttpError", "HTTP Error 500", FailureKind.UNAVAILABLE),
            ("HttpError", "HTTP Error 503", FailureKind.UNAVAILABLE),
            ("HttpError", "HTTP Error 504", FailureKind.UNAVAILABLE),
            ("HttpError", "HTTP Error 400", FailureKind.INVALID_RESULT),
            ("ValueError", "invalid JSON", FailureKind.INVALID_RESULT),
        )
        for error, message, kind in cases:
            for partial in (False, True):
                with self.subTest(error=error, message=message, partial=partial):
                    raw = stream((MEDIA, {})) if partial else []
                    raw.append([-1, {"error": error, "message": message +
                                     " https://source.example/private?token=REDACTED Cookie: REDACTED"}])
                    result = self.assert_failure(raw, kind)
                    for private in ("REDACTED", "source.example", "Cookie", message):
                        self.assertNotIn(private, repr(result))

    def test_malformed_and_reduced_legacy_shells_are_rejected(self):
        cases = (
            None, {}, "not raw", [], [None], [[]], [[3]], [[2, None]],
            [[2, "", []]], [[3, MEDIA]], [[3, MEDIA, None]], [[3, MEDIA, []]],
            [["3", MEDIA, {"post_id": "synthetic-work"}]], [[7, [MEDIA], {}]],
            [[-1]], [[-1, None]], [[-1, {"message": "no error class"}]],
            {"id": "synthetic-work", "raw_shape": {"list": 2}},
            {"media_assets": [{"url": MEDIA}]}, {"candidates": [{"url": MEDIA}]},
            {"raw": stream((MEDIA, {}))},
            [[2, {"post_id": "synthetic-work", "raw_shape": {"list": 2}}]],
        )
        for raw in cases:
            with self.subTest(raw=raw):
                self.assert_failure(raw)

    def test_non_http_delegated_or_credential_urls_do_not_get_rescued_by_nested_media(self):
        for url in ("", "not-a-url", "javascript:invalid", "file:///tmp/media.mp4",
                    "ytdl:https://source.example/video", "https://REDACTED:REDACTED@cdn.example/file.mp4"):
            with self.subTest(url=url):
                self.assert_failure(stream((url, {"extension": "mp4", "file": {"url": MEDIA}, "_fallback": [MEDIA]})))

    def test_invalid_source_url_is_a_safe_failure(self):
        for source_url in ("", "javascript:invalid", "https://REDACTED:REDACTED@source.example/post"):
            with self.subTest(source_url=source_url):
                self.assert_failure(stream((MEDIA, {})), source_url=source_url)
