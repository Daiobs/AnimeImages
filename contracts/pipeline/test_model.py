"""Unified pipeline §1.4: result model invariants (A2, INV-03)."""
from dataclasses import replace
from unittest import TestCase

from xhs_download.services.pipeline import (
    DEFAULT_QUALITY_LABEL,
    Group,
    Item,
    Kind,
    Tracks,
    Variant,
    Work,
    fact_gaps,
    field_coverage,
    height_from_label,
    validate_work,
)

from .fakes import SOURCE, variant, video_work


def item(*variants, kind=Kind.VIDEO, item_id="i1", index=1):
    return Item(item_id=item_id, kind=kind, index=index, variants=tuple(variants))


class DefaultVariantContract(TestCase):
    def test_default_is_original_then_height_then_bitrate_then_size(self):
        cases = [
            ("original beats taller",
             [variant("tall", height=2160), variant("orig", height=1080, is_original=True)], "orig"),
            ("taller beats higher bitrate",
             [variant("rate", height=720, bitrate=9_000_000), variant("tall", height=1080)], "tall"),
            ("bitrate beats size",
             [variant("big", height=1080, size_bytes=900), variant("rate", height=1080, bitrate=5)],
             "rate"),
            ("size decides last",
             [variant("small", height=1080, size_bytes=10), variant("big", height=1080, size_bytes=99)],
             "big"),
            ("ties keep adapter order",
             [variant("first", height=1080), variant("second", height=1080)], "first"),
        ]
        for name, variants, expected in cases:
            with self.subTest(name):
                self.assertEqual(item(*variants).default_variant.variant_id, expected)

    def test_video_only_without_merge_partner_is_never_the_default(self):
        silent = variant("silent", height=2160, tracks=Tracks.VIDEO_ONLY)
        complete = variant("av", height=720)
        self.assertEqual(item(silent, complete).default_variant.variant_id, "av")
        self.assertIsNone(item(silent).default_variant)

    def test_video_only_with_merge_partner_can_be_the_default(self):
        split = variant("v", height=2160, tracks=Tracks.VIDEO_ONLY, merge_with="a")
        audio = variant("a", tracks=Tracks.AUDIO_ONLY, container="m4a")
        self.assertEqual(item(split, audio, variant("av", height=720)).default_variant.variant_id, "v")

    def test_audio_only_is_not_the_default_of_a_video_item_but_is_of_an_audio_item(self):
        audio = variant("a", tracks=Tracks.AUDIO_ONLY, container="m4a", bitrate=320_000)
        self.assertEqual(item(audio, variant("av", height=360)).default_variant.variant_id, "av")
        self.assertEqual(item(audio, kind=Kind.AUDIO).default_variant.variant_id, "a")

    def test_display_label_uses_label_then_height_then_default_text(self):
        self.assertEqual(variant(quality_label="原画").display_label, "原画")
        self.assertEqual(variant(height=1080).display_label, "1080p")
        self.assertEqual(variant().display_label, DEFAULT_QUALITY_LABEL)


class ValidationContract(TestCase):
    def test_a_complete_work_has_no_violations(self):
        self.assertEqual(validate_work(video_work(platform="example")), ())

    def test_each_invariant_breach_is_reported(self):
        audio = variant("a", tracks=Tracks.AUDIO_ONLY, container="m4a")
        cases = {
            "missing_work_id": video_work(work_id=""),
            "no_items": Work(source_url=SOURCE, work_id="w", items=()),
            "item_without_variants": Work(source_url=SOURCE, work_id="w", items=(item(),)),
            "item_without_default": video_work(
                variants=(variant("v", tracks=Tracks.VIDEO_ONLY),)),
            "duplicate_variant_id": video_work(variants=(variant("v"), variant("v"))),
            "duplicate_item_id": Work(source_url=SOURCE, work_id="w",
                                      items=(item(variant("a")), item(variant("b"), index=2))),
            "item_index_below_one": Work(source_url=SOURCE, work_id="w",
                                         items=(item(variant("a"), index=0),)),
            "merge_partner_not_audio": video_work(variants=(
                variant("v", tracks=Tracks.VIDEO_ONLY, merge_with="missing"), audio)),
            "group_unknown_item": video_work(groups=(Group("live_photo", ("i1", "ghost")),)),
            "placeholder_size": video_work(variants=(variant("v", size_bytes=0),)),
        }
        for code, work in cases.items():
            with self.subTest(code):
                self.assertIn(code, validate_work(work))

    def test_placeholder_values_are_rejected_not_shown(self):
        for label in ("video", " Video ", "原始文件", "unknown"):
            with self.subTest(label=label):
                work = video_work(variants=(variant("v", quality_label=label),))
                self.assertIn("placeholder_quality_label", validate_work(work))
        for title in ("example", "Example Site", "视频", "untitled"):
            with self.subTest(title=title):
                work = video_work(title=title, platform="example")
                self.assertIn("placeholder_title",
                              validate_work(work, platform_names=("Example Site",)))

    def test_missing_fields_stay_missing(self):
        work = video_work(title="", variants=(variant("v", height=None),))
        self.assertEqual(validate_work(work), ())
        self.assertEqual(work.items[0].default_variant.display_label, DEFAULT_QUALITY_LABEL)


class FieldCoverageContract(TestCase):
    def test_coverage_names_who_supplied_each_field_and_none_when_missing(self):
        work = video_work(
            variants=(variant("v", height=1080, size_bytes=1234),),
            cover_url="",
            field_sources={"size": "enrichment"},
        )
        self.assertEqual(field_coverage(work), {
            "title": "provider", "cover": None, "quality": "provider", "size": "enrichment",
        })


class UnknownTracksContract(TestCase):
    """D-01: tracks nobody stated stay unknown and are delivered as complete files."""

    def test_tracks_default_to_unknown(self):
        self.assertIs(Variant(variant_id="v", url="https://media.example.test/v").tracks,
                      Tracks.UNKNOWN)

    def test_unknown_tracks_can_be_the_default_and_rank_by_quality(self):
        unknown = variant("u", height=1080, tracks=Tracks.UNKNOWN)
        complete = variant("av", height=720, tracks=Tracks.AV)
        self.assertEqual(item(complete, unknown).default_variant.variant_id, "u")
        only_unknown = video_work(variants=(variant("u", tracks=Tracks.UNKNOWN),))
        self.assertEqual(validate_work(only_unknown), ())
        self.assertEqual(only_unknown.items[0].default_variant.variant_id, "u")

    def test_fact_gaps_count_unknown_tracks_of_video_and_audio_only(self):
        unknown = variant("u", tracks=Tracks.UNKNOWN)
        cases = [
            ("unknown video", video_work(variants=(unknown,)), ("TRACKS_UNKNOWN",)),
            ("complete video", video_work(variants=(variant("av", tracks=Tracks.AV),)), ()),
            ("split video", video_work(variants=(
                variant("v", tracks=Tracks.VIDEO_ONLY, merge_with="a"),
                variant("a", tracks=Tracks.AUDIO_ONLY, container="m4a"))), ()),
            ("unknown audio", Work(source_url=SOURCE, work_id="w",
                                   items=(item(unknown, kind=Kind.AUDIO),)), ("TRACKS_UNKNOWN",)),
            ("image", Work(source_url=SOURCE, work_id="w",
                           items=(item(variant("p", container="jpg", tracks=Tracks.UNKNOWN),
                                       kind=Kind.IMAGE),)), ()),
        ]
        for name, work, expected in cases:
            with self.subTest(name):
                self.assertEqual(fact_gaps(work), expected)


class ResolutionLabelContract(TestCase):
    """D-01: an explicit P label from the Provider may set height; nothing else may."""

    def test_explicit_p_labels_give_their_number(self):
        for label, height in (("1080P", 1080), ("2160P", 2160), ("1080P+", 1080),
                              ("720p60", 720), (" 540P", 540), ("360p", 360)):
            with self.subTest(label=label):
                self.assertEqual(height_from_label(label), height)

    def test_other_labels_give_no_height(self):
        for label in ("原画", "高清", "4K", "1080", "HD", "1080Pro", "100P", "9999P", "", None):
            with self.subTest(label=label):
                self.assertIsNone(height_from_label(label))


class DeliverabilityContract(TestCase):
    """D-02: DRM and segmented streams stay listed but lose the default to files."""

    def test_drm_and_segmented_default_to_false(self):
        plain = Variant(variant_id="v", url="https://media.example.test/v.mp4")
        self.assertEqual((plain.drm, plain.segmented, plain.deliverable), (False, False, True))

    def test_a_downloadable_variant_wins_the_default(self):
        cases = [
            ("segmented taller", variant("hls", height=1080, segmented=True), variant("file", height=720)),
            ("drm original", variant("drm", height=2160, drm=True, is_original=True),
             variant("file", height=480)),
        ]
        for name, blocked, deliverable in cases:
            with self.subTest(name):
                self.assertEqual(item(blocked, deliverable).default_variant.variant_id, "file")

    def test_an_item_with_only_undeliverable_variants_stays_valid_and_visible(self):
        hls = replace(variant("hls", height=1080, segmented=True),
                      url="https://media.example.test/master.m3u8")
        drm = variant("drm", height=720, drm=True)
        work = video_work(variants=(drm, hls))
        self.assertEqual(validate_work(work), ())
        self.assertEqual(work.items[0].default_variant.variant_id, "hls")

    def test_a_manifest_or_segment_url_must_be_marked_segmented(self):
        for path in ("master.m3u8", "manifest.MPD", "seg-1.m4s", "index.m3u8?sig=REDACTED"):
            with self.subTest(path=path):
                unmarked = replace(variant("v"), url=f"https://media.example.test/{path}")
                self.assertIn("segmented_url_not_marked", validate_work(video_work(variants=(unmarked,))))
                marked = replace(unmarked, segmented=True)
                self.assertNotIn("segmented_url_not_marked", validate_work(video_work(variants=(marked,))))
        for url in ("https://media.example.test/m3u8/video.mp4",
                    "https://media.example.test/video.mp4?list=a.m3u8"):
            with self.subTest(url=url):
                self.assertEqual(validate_work(video_work(variants=(replace(variant("v"), url=url),))), ())


class RequestMethodContract(TestCase):
    """D-03: only GET is downloadable; the method must be a real upper-case name."""

    def test_method_defaults_to_get(self):
        self.assertEqual(Variant(variant_id="v", url="https://media.example.test/v").request_method, "GET")

    def test_non_get_is_listed_but_loses_the_default(self):
        post = variant("post", height=2160, request_method="POST")
        get = variant("get", height=720)
        self.assertFalse(post.deliverable)
        self.assertEqual(item(post, get).default_variant.variant_id, "get")
        self.assertEqual(validate_work(video_work(variants=(post,))), ())

    def test_method_must_be_an_upper_case_name(self):
        for method in ("post", "Get", "", "GET ", "P0ST", "X" * 11):
            with self.subTest(method=method):
                work = video_work(variants=(variant("v", request_method=method),))
                self.assertIn("invalid_request_method", validate_work(work))


class UnpairedVideoContract(TestCase):
    """D-04: a video-only variant without an audio partner is listed, not downloaded."""

    def test_unpaired_video_only_is_not_deliverable_but_paired_is(self):
        self.assertFalse(variant("v", tracks=Tracks.VIDEO_ONLY).deliverable)
        self.assertTrue(variant("v", tracks=Tracks.VIDEO_ONLY, merge_with="a").deliverable)
