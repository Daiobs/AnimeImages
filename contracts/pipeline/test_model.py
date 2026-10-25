"""Unified pipeline §1.4: result model invariants (A2, INV-03)."""
from unittest import TestCase

from xhs_download.services.pipeline import (
    DEFAULT_QUALITY_LABEL,
    Group,
    Item,
    Kind,
    Tracks,
    Work,
    field_coverage,
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
