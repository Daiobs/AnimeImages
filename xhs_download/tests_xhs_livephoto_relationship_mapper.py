import copy
import json
from dataclasses import FrozenInstanceError
from pathlib import Path

from django.test import SimpleTestCase

from .services.platforms.xhs_livephoto_relationship_mapper import (
    XHS_LIVEPHOTO_RELATIONSHIP_MAX_ITEMS,
    XHS_LIVEPHOTO_RELATIONSHIP_SCHEMA_VERSION,
    XhsLivePhotoRelationshipItemV1,
    XhsLivePhotoRelationshipMappingError,
    map_xhs_livephoto_relationships,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MAPPER_PATH = (
    PROJECT_ROOT
    / "xhs_download"
    / "services"
    / "platforms"
    / "xhs_livephoto_relationship_mapper.py"
)
NOTE_ID = "a" * 24
OTHER_NOTE_ID = "b" * 24


def regular_item(label):
    return {
        "livePhoto": False,
        "urlDefault": f"opaque-still-{label}",
        "infoList": [],
        "stream": {},
    }


def livephoto_item(label):
    return {
        "livePhoto": True,
        "urlDefault": f"opaque-default-{label}",
        "infoList": [
            {
                "imageScene": "WB_DFT",
                "url": f"opaque-still-{label}",
            }
        ],
        "stream": {
            "codec-a": [
                {
                    "masterUrl": f"opaque-motion-{label}",
                    "backupUrls": [f"opaque-backup-{label}"],
                    "qualityType": "HD",
                }
            ]
        },
    }


def map_items(items, **overrides):
    arguments = {
        "requested_note_id": NOTE_ID,
        "payload_note_id": NOTE_ID,
        "image_list": items,
    }
    arguments.update(overrides)
    return map_xhs_livephoto_relationships(**arguments)


class XhsLivePhotoRelationshipMapperTests(SimpleTestCase):
    def assert_mapping_error(self, code, **arguments):
        defaults = {
            "requested_note_id": NOTE_ID,
            "payload_note_id": NOTE_ID,
            "image_list": [regular_item("one")],
        }
        defaults.update(arguments)
        with self.assertRaises(XhsLivePhotoRelationshipMappingError) as raised:
            map_xhs_livephoto_relationships(**defaults)
        self.assertEqual(raised.exception.code, code)
        self.assertNotIn(NOTE_ID, str(raised.exception))
        self.assertNotIn(OTHER_NOTE_ID, str(raised.exception))
        return raised.exception

    def test_fourteen_item_all_livephoto_shape(self):
        result = map_items([livephoto_item(str(index)) for index in range(14)])
        self.assertEqual(result.schema_version, XHS_LIVEPHOTO_RELATIONSHIP_SCHEMA_VERSION)
        self.assertTrue(result.identity_confirmed)
        self.assertEqual(result.note_kind, "image")
        self.assertEqual(result.livephoto_count, 14)
        self.assertEqual(result.regular_image_count, 0)
        self.assertEqual(
            [item.source_item_ordinal for item in result.items],
            list(range(14)),
        )

    def test_seventeen_item_mixed_shape_preserves_twelve_pairs_and_five_images(self):
        regular_ordinals = {2, 6, 9, 13, 16}
        items = [
            regular_item(str(index))
            if index in regular_ordinals
            else livephoto_item(str(index))
            for index in range(17)
        ]
        result = map_items(items)
        self.assertEqual(result.livephoto_count, 12)
        self.assertEqual(result.regular_image_count, 5)
        self.assertEqual(
            [item.item_kind for item in result.items],
            [
                "regular_image" if index in regular_ordinals else "live_photo"
                for index in range(17)
            ],
        )

    def test_four_image_gallery_has_no_livephoto_relationship(self):
        result = map_items([regular_item(str(index)) for index in range(4)])
        self.assertEqual(result.livephoto_count, 0)
        self.assertEqual(result.regular_image_count, 4)
        self.assertTrue(all(item.member_order == ("still",) for item in result.items))

    def test_absent_marker_is_a_regular_image_when_motion_is_absent(self):
        item = regular_item("one")
        del item["livePhoto"]
        result = map_items([item])
        self.assertEqual(result.items[0].item_kind, "regular_image")

    def test_note_level_video_control_emits_no_image_relationships(self):
        result = map_items(
            [regular_item("cover")],
            note_level_video_present=True,
        )
        self.assertEqual(result.note_kind, "video")
        self.assertEqual(result.items, ())
        self.assertEqual(result.livephoto_count, 0)

    def test_identity_mismatch_fails_closed(self):
        self.assert_mapping_error(
            "XHS_LIVEPHOTO_SOURCE_IDENTITY_MISMATCH",
            payload_note_id=OTHER_NOTE_ID,
        )

    def test_missing_payload_identity_fails_closed(self):
        self.assert_mapping_error(
            "XHS_LIVEPHOTO_SOURCE_IDENTITY_INVALID",
            payload_note_id=None,
        )

    def test_invalid_identity_shapes_fail_closed(self):
        for invalid in ("", "c" * 65, "a" * 23 + "\n", "not-a-note"):
            with self.subTest(invalid_type=type(invalid).__name__, length=len(invalid)):
                self.assert_mapping_error(
                    "XHS_LIVEPHOTO_SOURCE_IDENTITY_INVALID",
                    requested_note_id=invalid,
                )

    def test_positive_marker_without_still_fails_closed(self):
        item = livephoto_item("one")
        del item["urlDefault"]
        item["infoList"] = []
        self.assert_mapping_error(
            "XHS_LIVEPHOTO_STILL_EVIDENCE_MISSING",
            image_list=[item],
        )

    def test_positive_marker_without_motion_fails_closed(self):
        item = livephoto_item("one")
        item["stream"] = {}
        self.assert_mapping_error(
            "XHS_LIVEPHOTO_MOTION_EVIDENCE_MISSING",
            image_list=[item],
        )

    def test_motion_without_positive_marker_fails_closed(self):
        item = livephoto_item("one")
        item["livePhoto"] = False
        self.assert_mapping_error(
            "XHS_LIVEPHOTO_MOTION_WITHOUT_MARKER",
            image_list=[item],
        )

    def test_url_default_is_reviewed_still_evidence(self):
        item = livephoto_item("one")
        item["infoList"] = []
        result = map_items([item])
        self.assertEqual(result.items[0].still_candidate_count, 1)

    def test_info_list_url_is_reviewed_still_evidence(self):
        item = livephoto_item("one")
        del item["urlDefault"]
        result = map_items([item])
        self.assertEqual(result.items[0].still_candidate_count, 1)

    def test_unreviewed_info_list_field_is_not_still_evidence(self):
        item = livephoto_item("one")
        del item["urlDefault"]
        item["infoList"] = [{"urlDefault": "opaque-unreviewed"}]
        self.assert_mapping_error(
            "XHS_LIVEPHOTO_STILL_EVIDENCE_MISSING",
            image_list=[item],
        )

    def test_master_url_is_reviewed_motion_evidence(self):
        item = livephoto_item("one")
        item["stream"]["codec-a"][0]["backupUrls"] = []
        result = map_items([item])
        self.assertEqual(result.items[0].motion_candidate_count, 1)

    def test_backup_urls_are_reviewed_motion_evidence(self):
        item = livephoto_item("one")
        del item["stream"]["codec-a"][0]["masterUrl"]
        result = map_items([item])
        self.assertEqual(result.items[0].motion_candidate_count, 1)

    def test_multiple_codec_families_count_only_reviewed_motion_fields(self):
        item = livephoto_item("one")
        item["stream"]["codec-b"] = [
            {
                "masterUrl": "opaque-motion-b",
                "backupUrls": ["opaque-backup-b1", "opaque-backup-b2"],
                "ignored": "opaque-noise",
            }
        ]
        result = map_items([item])
        self.assertEqual(result.items[0].motion_candidate_count, 5)

    def test_malformed_stream_shapes_fail_closed(self):
        malformed_streams = (
            "opaque-stream",
            {"codec-a": {}},
            {"codec-a": ["opaque-entry"]},
            {"codec-a": [{"masterUrl": 7}]},
            {"codec-a": [{"backupUrls": "opaque-backup"}]},
            {"codec-a": [{"backupUrls": [7]}]},
        )
        for stream in malformed_streams:
            with self.subTest(stream_type=type(stream).__name__):
                item = livephoto_item("one")
                item["stream"] = stream
                self.assert_mapping_error(
                    "XHS_LIVEPHOTO_STREAM_INVALID",
                    image_list=[item],
                )

    def test_malformed_info_list_shapes_fail_closed(self):
        malformed_info_lists = (
            "opaque-info",
            ["opaque-entry"],
            [{"url": 7}],
        )
        for info_list in malformed_info_lists:
            with self.subTest(info_type=type(info_list).__name__):
                item = regular_item("one")
                item["infoList"] = info_list
                self.assert_mapping_error(
                    "XHS_LIVEPHOTO_INFO_LIST_INVALID",
                    image_list=[item],
                )

    def test_unknown_marker_values_fail_closed(self):
        for marker in (1, "true", None, {}, []):
            with self.subTest(marker_type=type(marker).__name__):
                item = regular_item("one")
                item["livePhoto"] = marker
                self.assert_mapping_error(
                    "XHS_LIVEPHOTO_MARKER_INVALID",
                    image_list=[item],
                )

    def test_empty_image_list_is_rejected_for_image_note(self):
        self.assert_mapping_error(
            "XHS_LIVEPHOTO_IMAGE_LIST_INVALID",
            image_list=[],
        )

    def test_empty_image_list_is_valid_for_note_level_video(self):
        result = map_items([], note_level_video_present=True)
        self.assertEqual(result.note_kind, "video")
        self.assertEqual(result.items, ())

    def test_item_count_above_sixty_four_fails_closed(self):
        self.assert_mapping_error(
            "XHS_LIVEPHOTO_IMAGE_LIST_INVALID",
            image_list=[regular_item("one")]
            * (XHS_LIVEPHOTO_RELATIONSHIP_MAX_ITEMS + 1),
        )

    def test_info_list_count_is_bounded(self):
        item = regular_item("one")
        item["infoList"] = [{"url": "opaque-still"}] * 33
        self.assert_mapping_error(
            "XHS_LIVEPHOTO_INFO_LIST_INVALID",
            image_list=[item],
        )

    def test_codec_family_count_is_bounded(self):
        item = livephoto_item("one")
        item["stream"] = {
            f"codec-{index}": [{"masterUrl": "opaque-motion"}]
            for index in range(17)
        }
        self.assert_mapping_error(
            "XHS_LIVEPHOTO_STREAM_INVALID",
            image_list=[item],
        )

    def test_stream_variant_count_is_bounded(self):
        item = livephoto_item("one")
        item["stream"] = {
            "codec-a": [{"masterUrl": "opaque-motion"}] * 33,
        }
        self.assert_mapping_error(
            "XHS_LIVEPHOTO_STREAM_INVALID",
            image_list=[item],
        )

    def test_backup_candidate_count_is_bounded(self):
        item = livephoto_item("one")
        item["stream"] = {
            "codec-a": [{"backupUrls": ["opaque-motion"] * 65}],
        }
        self.assert_mapping_error(
            "XHS_LIVEPHOTO_STREAM_INVALID",
            image_list=[item],
        )

    def test_source_order_is_preserved_without_kind_sorting(self):
        result = map_items(
            [regular_item("a"), livephoto_item("b"), regular_item("c")]
        )
        self.assertEqual(
            [(item.source_item_ordinal, item.item_kind) for item in result.items],
            [(0, "regular_image"), (1, "live_photo"), (2, "regular_image")],
        )

    def test_unrelated_extra_fields_are_ignored(self):
        item = livephoto_item("one")
        item["firstNoteId"] = OTHER_NOTE_ID
        item["recommendation"] = {"payload": object()}
        item["stream"]["codec-a"][0]["unknown"] = object()
        result = map_items([item])
        self.assertEqual(result.livephoto_count, 1)

    def test_structural_output_contains_no_raw_reference_values(self):
        result = map_items([livephoto_item("sensitive-reference")])
        rendered = json.dumps(result.to_structural_dict(), sort_keys=True)
        self.assertNotIn("opaque-", rendered)
        self.assertNotIn("sensitive-reference", rendered)
        for forbidden in (
            "url",
            "note_id",
            "fingerprint",
            "hostname",
            "extension",
            "provider",
            "manifest",
            "asset_id",
            "group_id",
        ):
            self.assertNotIn(forbidden, rendered.lower())

    def test_repr_contains_no_identity_or_raw_reference_values(self):
        result = map_items([livephoto_item("sensitive-reference")])
        rendered = repr(result)
        self.assertNotIn(NOTE_ID, rendered)
        self.assertNotIn("opaque-", rendered)
        self.assertNotIn("sensitive-reference", rendered)

    def test_error_contains_no_rejected_value(self):
        rejected = "rejected-sensitive-value"
        error = self.assert_mapping_error(
            "XHS_LIVEPHOTO_SOURCE_IDENTITY_INVALID",
            requested_note_id=rejected,
        )
        self.assertNotIn(rejected, str(error))

    def test_direct_error_construction_sanitizes_unknown_code_and_path(self):
        sensitive_value = "opaque-sensitive-reference"
        error = XhsLivePhotoRelationshipMappingError(
            f"UNKNOWN_{sensitive_value}",
            f"image_list[0].{sensitive_value}",
        )
        self.assertEqual(error.code, "XHS_LIVEPHOTO_MAPPING_ERROR")
        self.assertEqual(error.path, "unknown")
        self.assertNotIn(sensitive_value, str(error))

    def test_mapper_source_does_not_access_first_note_id(self):
        source = MAPPER_PATH.read_text(encoding="utf-8")
        self.assertNotIn("firstNoteId", source)
        self.assertNotIn("first_note_id", source)

    def test_source_ordinal_is_not_pairing_evidence(self):
        incomplete = livephoto_item("motion-only")
        del incomplete["urlDefault"]
        incomplete["infoList"] = []
        self.assert_mapping_error(
            "XHS_LIVEPHOTO_STILL_EVIDENCE_MISSING",
            image_list=[regular_item("still-only"), incomplete],
        )

    def test_tuple_stream_containers_are_accepted_without_retention(self):
        item = livephoto_item("one")
        item["infoList"] = tuple(item["infoList"])
        item["stream"]["codec-a"] = tuple(item["stream"]["codec-a"])
        item["stream"]["codec-a"][0]["backupUrls"] = ("opaque-backup",)
        result = map_items((item,))
        self.assertEqual(result.livephoto_count, 1)

    def test_non_mapping_image_item_fails_closed(self):
        self.assert_mapping_error(
            "XHS_LIVEPHOTO_IMAGE_ITEM_INVALID",
            image_list=["opaque-item"],
        )

    def test_regular_image_without_still_evidence_fails_closed(self):
        self.assert_mapping_error(
            "XHS_LIVEPHOTO_STILL_EVIDENCE_MISSING",
            image_list=[{"livePhoto": False, "stream": {}}],
        )

    def test_video_note_with_complete_livephoto_shape_is_blocked(self):
        self.assert_mapping_error(
            "XHS_VIDEO_NOTE_LIVEPHOTO_SHAPE_UNREVIEWED",
            image_list=[livephoto_item("one")],
            note_level_video_present=True,
        )

    def test_video_flag_requires_an_explicit_boolean(self):
        self.assert_mapping_error(
            "XHS_LIVEPHOTO_VIDEO_FLAG_INVALID",
            note_level_video_present=1,
        )

    def test_format_status_is_always_unverified(self):
        result = map_items([regular_item("one"), livephoto_item("two")])
        self.assertEqual({item.format_status for item in result.items}, {"unverified"})
        self.assertNotIn("extension", result.to_structural_dict())

    def test_output_objects_are_frozen(self):
        result = map_items([regular_item("one")])
        with self.assertRaises(FrozenInstanceError):
            result.items[0].item_kind = "live_photo"

    def test_direct_output_construction_enforces_closed_invariants(self):
        with self.assertRaises(XhsLivePhotoRelationshipMappingError) as raised:
            XhsLivePhotoRelationshipItemV1(
                source_item_ordinal=0,
                item_kind="live_photo",
                relationship_basis="none",
                still_candidate_count=1,
                motion_candidate_count=0,
                member_order=("still",),
            )
        self.assertEqual(
            raised.exception.code,
            "XHS_LIVEPHOTO_RELATIONSHIP_ITEM_INVALID",
        )

    def test_mapper_has_no_bridge_to_asset_or_rich_media_contract(self):
        source = MAPPER_PATH.read_text(encoding="utf-8")
        for forbidden in (
            "MediaAsset",
            "XhsRichMediaAssetSpec",
            "RichMediaContractV1",
            "normalize_xhs_rich_media_payload",
        ):
            self.assertNotIn(forbidden, source)

    def test_runtime_mapper_consumption_is_confined_to_staff_builder(self):
        for relative_path in (
            "xhs_download/services/platforms/xiaohongshu.py",
            "xhs_download/services/registry.py",
        ):
            source = (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")
            self.assertNotIn("xhs_livephoto_relationship_mapper", source)
            self.assertNotIn("map_xhs_livephoto_relationships", source)
        staff_builder = (
            PROJECT_ROOT
            / "xhs_download"
            / "services"
            / "platforms"
            / "xhs_livephoto_staff_builder.py"
        ).read_text(encoding="utf-8")
        self.assertIn("map_xhs_livephoto_relationships", staff_builder)

    def test_input_is_not_mutated(self):
        items = [livephoto_item("one"), regular_item("two")]
        original = copy.deepcopy(items)
        map_items(items)
        self.assertEqual(items, original)
