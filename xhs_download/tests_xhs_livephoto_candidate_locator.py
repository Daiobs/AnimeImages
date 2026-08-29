from dataclasses import FrozenInstanceError, replace
from pathlib import Path

from django.test import SimpleTestCase

from .services.image_formats import resolve_image_format
from .services.platforms.xhs_livephoto_candidate_locator import (
    XHS_LIVEPHOTO_CANDIDATE_SCHEMA_VERSION,
    XHS_LIVEPHOTO_LOCATOR_SCOPE,
    XhsLivePhotoCandidateError,
    XhsLivePhotoMemberLocatorV1,
    XhsPrivateMediaCandidateV1,
    classify_xhs_livephoto_host_for_research,
    locate_xhs_livephoto_candidates,
    read_candidate_from_locator,
    resolve_xhs_motion_format_for_research,
)
from .services.platforms.xhs_livephoto_relationship_mapper import (
    map_xhs_livephoto_relationships,
)


NOTE_ID = "a" * 24
PRIVATE_VALUE = "private-candidate-value"
PROJECT_ROOT = Path(__file__).resolve().parents[1]

JPEG = b"\xff\xd8\xff\xe0jpeg"
PNG = b"\x89PNG\r\n\x1a\npng"
WEBP = b"RIFF\x04\x00\x00\x00WEBPwebp"
AVIF = b"\x00\x00\x00\x18ftypavif\x00\x00\x00\x00avifmif1"
MP4 = b"\x00\x00\x00\x18ftypisom\x00\x00\x00\x00isommp42"
MOV = b"\x00\x00\x00\x18ftypqt  \x00\x00\x00\x00qt  "
WEBM = b"\x1aE\xdf\xa3webm"


def secure_url(host, path="resource"):
    return "https" + "://" + host + "/" + path


def regular_item(label):
    default = f"private-default-{label}"
    return {
        "livePhoto": False,
        "urlDefault": default,
        "infoList": [
            {"imageScene": "WB_PRV", "url": f"private-preview-{label}"},
            {"imageScene": "WB_DFT", "url": default},
        ],
        "stream": {},
    }


def livephoto_item(label, *, codec_before=True):
    item = regular_item(label)
    item["livePhoto"] = True
    h264 = [
        {
            "masterUrl": f"private-motion-{label}",
            "backupUrls": [
                f"private-backup-a-{label}",
                f"private-backup-b-{label}",
            ],
            "qualityType": "HD",
        }
    ]
    item["stream"] = (
        {"h265": [], "h264": h264, "av1": [], "h266": []}
        if codec_before
        else {"h264": h264, "h265": [], "h266": [], "av1": []}
    )
    return item


def relationship_map(items, *, video=False):
    return map_xhs_livephoto_relationships(
        requested_note_id=NOTE_ID,
        payload_note_id=NOTE_ID,
        image_list=items,
        note_level_video_present=video,
    )


def candidate_map(items):
    return locate_xhs_livephoto_candidates(
        relationship_map=relationship_map(items),
        image_list=items,
    )


class XhsLivePhotoCandidateSelectionTests(SimpleTestCase):
    def assert_candidate_error(self, code, callback):
        with self.assertRaises(XhsLivePhotoCandidateError) as raised:
            callback()
        self.assertEqual(raised.exception.code, code)
        self.assertNotIn(PRIVATE_VALUE, str(raised.exception))
        return raised.exception

    def test_fourteen_livephoto_items_produce_ordered_member_locators(self):
        items = [livephoto_item(str(index)) for index in range(14)]
        result = candidate_map(items)

        self.assertEqual(result.schema_version, XHS_LIVEPHOTO_CANDIDATE_SCHEMA_VERSION)
        self.assertEqual(len(result.items), 14)
        self.assertEqual(
            [item.source_item_ordinal for item in result.items],
            list(range(14)),
        )
        self.assertTrue(all(item.still_locator for item in result.items))
        self.assertTrue(all(item.motion_locator for item in result.items))

    def test_seventeen_mixed_items_preserve_kind_and_order(self):
        regular_ordinals = {2, 6, 9, 13, 16}
        items = [
            regular_item(str(index))
            if index in regular_ordinals
            else livephoto_item(str(index), codec_before=index % 2 == 0)
            for index in range(17)
        ]

        result = candidate_map(items)

        self.assertEqual(
            [item.item_kind for item in result.items],
            [
                "regular_image" if index in regular_ordinals else "live_photo"
                for index in range(17)
            ],
        )
        self.assertEqual(
            [item.source_item_ordinal for item in result.items],
            list(range(17)),
        )

    def test_gallery_produces_still_only_locators(self):
        result = candidate_map([regular_item(str(index)) for index in range(4)])

        self.assertEqual(len(result.items), 4)
        self.assertTrue(all(item.still_locator for item in result.items))
        self.assertTrue(all(item.motion_locator is None for item in result.items))

    def test_video_relationship_is_not_a_candidate_map(self):
        items = [regular_item("cover")]
        relationships = relationship_map(items, video=True)

        self.assert_candidate_error(
            "XHS_LIVEPHOTO_CANDIDATE_UNSUPPORTED_NOTE_KIND",
            lambda: locate_xhs_livephoto_candidates(
                relationship_map=relationships,
                image_list=items,
            ),
        )

    def test_relationship_and_image_list_count_mismatch_fails_closed(self):
        items = [regular_item("one")]
        relationships = relationship_map(items)

        self.assert_candidate_error(
            "XHS_LIVEPHOTO_CANDIDATE_COUNT_MISMATCH",
            lambda: locate_xhs_livephoto_candidates(
                relationship_map=relationships,
                image_list=items + [regular_item("two")],
            ),
        )

    def test_relationship_item_kind_mismatch_fails_closed(self):
        items = [livephoto_item("one")]
        relationships = relationship_map(items)
        items[0]["livePhoto"] = False
        items[0]["stream"] = {}

        self.assert_candidate_error(
            "XHS_LIVEPHOTO_CANDIDATE_ITEM_KIND_MISMATCH",
            lambda: locate_xhs_livephoto_candidates(
                relationship_map=relationships,
                image_list=items,
            ),
        )

    def test_still_candidate_count_mismatch_fails_closed(self):
        items = [regular_item("one")]
        relationships = relationship_map(items)
        items[0]["infoList"].append(
            {"imageScene": "WB_PRV", "url": "private-new-preview"}
        )

        self.assert_candidate_error(
            "XHS_LIVEPHOTO_CANDIDATE_COUNT_MISMATCH",
            lambda: locate_xhs_livephoto_candidates(
                relationship_map=relationships,
                image_list=items,
            ),
        )

    def test_motion_candidate_count_mismatch_fails_closed(self):
        items = [livephoto_item("one")]
        relationships = relationship_map(items)
        items[0]["stream"]["h264"][0]["backupUrls"].append(
            "private-new-backup"
        )

        self.assert_candidate_error(
            "XHS_LIVEPHOTO_CANDIDATE_COUNT_MISMATCH",
            lambda: locate_xhs_livephoto_candidates(
                relationship_map=relationships,
                image_list=items,
            ),
        )

    def test_url_default_is_the_primary_still_locator(self):
        result = candidate_map([regular_item("one")])

        locator = result.items[0].still_locator
        self.assertEqual(locator.source_kind, "url_default")
        self.assertIsNone(locator.info_list_index)

    def test_url_default_and_reviewed_default_scene_must_match(self):
        items = [regular_item("one")]
        items[0]["infoList"][1]["url"] = PRIVATE_VALUE

        self.assert_candidate_error(
            "XHS_LIVEPHOTO_CANDIDATE_STILL_POLICY_DRIFT",
            lambda: candidate_map(items),
        )

    def test_multiple_inconsistent_reviewed_defaults_fail_closed(self):
        items = [regular_item("one")]
        items[0]["infoList"].append(
            {"imageScene": "WB_DFT", "url": PRIVATE_VALUE}
        )

        self.assert_candidate_error(
            "XHS_LIVEPHOTO_CANDIDATE_STILL_POLICY_DRIFT",
            lambda: candidate_map(items),
        )

    def test_url_default_without_reviewed_default_scene_is_supported(self):
        items = [regular_item("one")]
        items[0]["infoList"] = items[0]["infoList"][:1]

        locator = candidate_map(items).items[0].still_locator

        self.assertEqual(locator.source_kind, "url_default")

    def test_reviewed_default_scene_is_the_still_fallback(self):
        items = [regular_item("one")]
        del items[0]["urlDefault"]

        result = candidate_map(items)

        locator = result.items[0].still_locator
        self.assertEqual(locator.source_kind, "info_list_url")
        self.assertEqual(locator.info_list_index, 1)

    def test_preview_scene_alone_is_not_a_full_still_fallback(self):
        items = [regular_item("one")]
        del items[0]["urlDefault"]
        items[0]["infoList"] = items[0]["infoList"][:1]
        relationships = relationship_map(items)

        self.assert_candidate_error(
            "XHS_LIVEPHOTO_CANDIDATE_MEMBER_MISSING",
            lambda: locate_xhs_livephoto_candidates(
                relationship_map=relationships,
                image_list=items,
            ),
        )

    def test_multiple_default_scene_fallbacks_are_ambiguous(self):
        items = [regular_item("one")]
        del items[0]["urlDefault"]
        items[0]["infoList"].append(
            {"imageScene": "WB_DFT", "url": "private-other-default"}
        )
        relationships = relationship_map(items)

        self.assert_candidate_error(
            "XHS_LIVEPHOTO_CANDIDATE_STILL_POLICY_AMBIGUOUS",
            lambda: locate_xhs_livephoto_candidates(
                relationship_map=relationships,
                image_list=items,
            ),
        )

    def test_duplicate_equal_default_scene_fallbacks_are_ambiguous(self):
        items = [regular_item("one")]
        del items[0]["urlDefault"]
        items[0]["infoList"].append(
            {"imageScene": "WB_DFT", "url": "private-default-one"}
        )

        self.assert_candidate_error(
            "XHS_LIVEPHOTO_CANDIDATE_STILL_POLICY_AMBIGUOUS",
            lambda: candidate_map(items),
        )

    def test_master_url_is_the_primary_motion_locator(self):
        result = candidate_map([livephoto_item("one")])

        locator = result.items[0].motion_locator
        self.assertEqual(locator.source_kind, "master_url")
        self.assertEqual(locator.codec_family_class, "avc")
        self.assertEqual(locator.codec_family_ordinal, 1)
        self.assertEqual(locator.stream_variant_index, 0)

    def test_first_backup_is_used_when_master_field_is_absent(self):
        items = [livephoto_item("one")]
        del items[0]["stream"]["h264"][0]["masterUrl"]

        result = candidate_map(items)

        locator = result.items[0].motion_locator
        self.assertEqual(locator.source_kind, "backup_url")
        self.assertEqual(locator.backup_index, 0)

    def test_empty_master_field_fails_closed(self):
        items = [livephoto_item("one")]
        items[0]["stream"]["h264"][0]["masterUrl"] = ""

        self.assert_candidate_error(
            "XHS_LIVEPHOTO_CANDIDATE_MASTER_FIELD_INVALID",
            lambda: candidate_map(items),
        )

    def test_whitespace_master_field_fails_closed(self):
        items = [livephoto_item("one")]
        items[0]["stream"]["h264"][0]["masterUrl"] = "   "

        self.assert_candidate_error(
            "XHS_LIVEPHOTO_CANDIDATE_MASTER_FIELD_INVALID",
            lambda: candidate_map(items),
        )

    def test_none_master_field_fails_closed(self):
        items = [livephoto_item("one")]
        items[0]["stream"]["h264"][0]["masterUrl"] = None

        self.assert_candidate_error(
            "XHS_LIVEPHOTO_CANDIDATE_MASTER_FIELD_INVALID",
            lambda: candidate_map(items),
        )

    def test_non_string_master_field_fails_closed(self):
        items = [livephoto_item("one")]
        relationships = relationship_map(items)
        items[0]["stream"]["h264"][0]["masterUrl"] = {"value": PRIVATE_VALUE}

        self.assert_candidate_error(
            "XHS_LIVEPHOTO_CANDIDATE_MASTER_FIELD_INVALID",
            lambda: locate_xhs_livephoto_candidates(
                relationship_map=relationships,
                image_list=items,
            ),
        )

    def test_codec_ordinal_is_recorded_but_h264_class_is_authoritative(self):
        first = candidate_map([livephoto_item("one", codec_before=True)])
        second = candidate_map([livephoto_item("two", codec_before=False)])

        self.assertEqual(first.items[0].motion_locator.codec_family_ordinal, 1)
        self.assertEqual(second.items[0].motion_locator.codec_family_ordinal, 0)
        self.assertEqual(first.items[0].motion_locator.codec_family_class, "avc")
        self.assertEqual(second.items[0].motion_locator.codec_family_class, "avc")

    def test_empty_unreviewed_codec_families_are_compatible(self):
        result = candidate_map([livephoto_item("one")])
        self.assertEqual(result.items[0].motion_locator.codec_family_class, "avc")

    def test_nonempty_unreviewed_codec_family_fails_closed(self):
        items = [livephoto_item("one")]
        items[0]["stream"]["h265"] = [
            {"masterUrl": "private-h265", "backupUrls": []}
        ]
        relationships = relationship_map(items)

        self.assert_candidate_error(
            "XHS_LIVEPHOTO_CANDIDATE_UNSUPPORTED_CODEC",
            lambda: locate_xhs_livephoto_candidates(
                relationship_map=relationships,
                image_list=items,
            ),
        )

    def test_multiple_viable_h264_variants_fail_closed(self):
        items = [livephoto_item("one")]
        items[0]["stream"]["h264"].append(
            {"masterUrl": "private-second-motion", "backupUrls": []}
        )
        relationships = relationship_map(items)

        self.assert_candidate_error(
            "XHS_LIVEPHOTO_CANDIDATE_MOTION_POLICY_AMBIGUOUS",
            lambda: locate_xhs_livephoto_candidates(
                relationship_map=relationships,
                image_list=items,
            ),
        )

    def test_livephoto_without_reviewed_h264_candidate_fails_closed(self):
        items = [livephoto_item("one")]
        items[0]["stream"] = {
            "h265": [{"masterUrl": "private-h265", "backupUrls": []}]
        }
        relationships = relationship_map(items)

        self.assert_candidate_error(
            "XHS_LIVEPHOTO_CANDIDATE_UNSUPPORTED_CODEC",
            lambda: locate_xhs_livephoto_candidates(
                relationship_map=relationships,
                image_list=items,
            ),
        )

    def test_malformed_image_list_is_rejected(self):
        relationships = relationship_map([regular_item("one")])
        self.assert_candidate_error(
            "XHS_LIVEPHOTO_CANDIDATE_IMAGE_LIST_INVALID",
            lambda: locate_xhs_livephoto_candidates(
                relationship_map=relationships,
                image_list="private-list",
            ),
        )

    def test_malformed_info_list_is_rejected(self):
        items = [regular_item("one")]
        relationships = relationship_map(items)
        items[0]["infoList"] = "private-info"
        self.assert_candidate_error(
            "XHS_LIVEPHOTO_CANDIDATE_ITEM_INVALID",
            lambda: locate_xhs_livephoto_candidates(
                relationship_map=relationships,
                image_list=items,
            ),
        )

    def test_malformed_stream_is_rejected(self):
        items = [livephoto_item("one")]
        relationships = relationship_map(items)
        items[0]["stream"] = "private-stream"
        self.assert_candidate_error(
            "XHS_LIVEPHOTO_CANDIDATE_ITEM_INVALID",
            lambda: locate_xhs_livephoto_candidates(
                relationship_map=relationships,
                image_list=items,
            ),
        )


class XhsLivePhotoCandidateReadSafetyTests(SimpleTestCase):
    def test_locator_scope_is_the_same_selected_payload_snapshot(self):
        self.assertEqual(
            XHS_LIVEPHOTO_LOCATOR_SCOPE,
            "same_selected_payload_snapshot",
        )

    def test_same_snapshot_reread_passes(self):
        items = [livephoto_item("one")]
        locators = candidate_map(items).items[0]

        still = read_candidate_from_locator(
            image_list=items,
            locator=locators.still_locator,
        )
        motion = read_candidate_from_locator(
            image_list=items,
            locator=locators.motion_locator,
        )

        self.assertEqual(still.value, "private-default-one")
        self.assertEqual(motion.value, "private-motion-one")

    def test_read_url_default_returns_a_private_wrapper(self):
        items = [regular_item("one")]
        locator = candidate_map(items).items[0].still_locator

        candidate = read_candidate_from_locator(image_list=items, locator=locator)

        self.assertIsInstance(candidate, XhsPrivateMediaCandidateV1)
        self.assertEqual(candidate.value, "private-default-one")
        self.assertEqual(candidate.member_role, "still")

    def test_read_info_list_fallback_checks_reviewed_scene(self):
        items = [regular_item("one")]
        del items[0]["urlDefault"]
        locator = candidate_map(items).items[0].still_locator

        candidate = read_candidate_from_locator(image_list=items, locator=locator)

        self.assertEqual(candidate.value, "private-default-one")

    def test_read_master_url_uses_the_recorded_codec_location(self):
        items = [livephoto_item("one")]
        locator = candidate_map(items).items[0].motion_locator

        candidate = read_candidate_from_locator(image_list=items, locator=locator)

        self.assertEqual(candidate.value, "private-motion-one")

    def test_read_backup_url_uses_the_recorded_backup_index(self):
        items = [livephoto_item("one")]
        del items[0]["stream"]["h264"][0]["masterUrl"]
        locator = candidate_map(items).items[0].motion_locator

        candidate = read_candidate_from_locator(image_list=items, locator=locator)

        self.assertEqual(candidate.value, "private-backup-a-one")

    def test_reordered_codec_mapping_invalidates_an_old_locator(self):
        items = [livephoto_item("one", codec_before=True)]
        locator = candidate_map(items).items[0].motion_locator
        items[0]["stream"] = {
            "h264": items[0]["stream"]["h264"],
            "h265": [],
            "av1": [],
            "h266": [],
        }

        with self.assertRaises(XhsLivePhotoCandidateError) as raised:
            read_candidate_from_locator(image_list=items, locator=locator)
        self.assertEqual(
            raised.exception.code,
            "XHS_LIVEPHOTO_CANDIDATE_LOCATOR_INVALID",
        )

    def test_reordered_info_list_invalidates_an_old_locator(self):
        items = [regular_item("one")]
        del items[0]["urlDefault"]
        locator = candidate_map(items).items[0].still_locator
        items[0]["infoList"] = list(reversed(items[0]["infoList"]))

        with self.assertRaises(XhsLivePhotoCandidateError) as raised:
            read_candidate_from_locator(image_list=items, locator=locator)

        self.assertEqual(
            raised.exception.code,
            "XHS_LIVEPHOTO_CANDIDATE_LOCATOR_INVALID",
        )

    def test_same_variant_backup_locator_remains_valid_after_master_appears(self):
        items = [livephoto_item("one")]
        del items[0]["stream"]["h264"][0]["masterUrl"]
        locator = candidate_map(items).items[0].motion_locator
        items[0]["stream"]["h264"][0]["masterUrl"] = "private-new-master"

        candidate = read_candidate_from_locator(
            image_list=items,
            locator=locator,
        )

        self.assertEqual(candidate.value, "private-backup-a-one")

    def test_locator_out_of_range_fails_without_candidate_value(self):
        locator = XhsLivePhotoMemberLocatorV1(
            source_item_ordinal=1,
            member_role="still",
            source_kind="url_default",
        )
        with self.assertRaises(XhsLivePhotoCandidateError) as raised:
            read_candidate_from_locator(
                image_list=[{"urlDefault": PRIVATE_VALUE}],
                locator=locator,
            )
        self.assertNotIn(PRIVATE_VALUE, str(raised.exception))

    def test_locator_pointing_to_wrong_field_type_fails_safely(self):
        locator = XhsLivePhotoMemberLocatorV1(
            source_item_ordinal=0,
            member_role="still",
            source_kind="url_default",
        )
        with self.assertRaises(XhsLivePhotoCandidateError) as raised:
            read_candidate_from_locator(
                image_list=[{"urlDefault": {"private": PRIVATE_VALUE}}],
                locator=locator,
            )
        self.assertNotIn(PRIVATE_VALUE, str(raised.exception))

    def test_candidate_value_is_hidden_from_repr(self):
        items = [regular_item("one")]
        locator = candidate_map(items).items[0].still_locator
        candidate = XhsPrivateMediaCandidateV1(
            value=PRIVATE_VALUE,
            locator=locator,
            member_role="still",
        )

        self.assertNotIn(PRIVATE_VALUE, repr(candidate))

    def test_candidate_value_is_absent_from_structural_output(self):
        items = [regular_item("one")]
        locator = candidate_map(items).items[0].still_locator
        candidate = XhsPrivateMediaCandidateV1(
            value=PRIVATE_VALUE,
            locator=locator,
            member_role="still",
        )

        self.assertNotIn(PRIVATE_VALUE, repr(candidate.to_structural_dict()))
        self.assertEqual(
            set(candidate.to_structural_dict()),
            {"locator", "member_role"},
        )

    def test_candidate_comparison_and_hash_do_not_depend_on_value(self):
        locator = XhsLivePhotoMemberLocatorV1(
            source_item_ordinal=0,
            member_role="still",
            source_kind="url_default",
        )
        first = XhsPrivateMediaCandidateV1("private-one", locator, "still")
        second = XhsPrivateMediaCandidateV1("private-two", locator, "still")

        self.assertEqual(first, second)
        self.assertEqual(hash(first), hash(second))

    def test_structural_candidate_map_contains_no_private_values(self):
        items = [livephoto_item("one")]
        result = candidate_map(items)
        rendered = repr(result.to_structural_dict())

        for private in (
            "private-default-one",
            "private-motion-one",
            "private-backup-a-one",
        ):
            self.assertNotIn(private, rendered)

    def test_structural_output_contains_no_cross_snapshot_identity(self):
        rendered = repr(candidate_map([livephoto_item("one")]).to_structural_dict())

        for forbidden in ("snapshot", "payload_identity", "note_id", "fingerprint"):
            self.assertNotIn(forbidden, rendered.lower())

    def test_locator_and_candidate_objects_are_frozen(self):
        items = [regular_item("one")]
        locator = candidate_map(items).items[0].still_locator
        candidate = read_candidate_from_locator(image_list=items, locator=locator)

        with self.assertRaises(FrozenInstanceError):
            locator.source_kind = "info_list_url"
        with self.assertRaises(FrozenInstanceError):
            candidate.member_role = "motion"

    def test_invalid_locator_shape_fails_closed(self):
        with self.assertRaises(XhsLivePhotoCandidateError):
            XhsLivePhotoMemberLocatorV1(
                source_item_ordinal=0,
                member_role="still",
                source_kind="backup_url",
                codec_family_ordinal=0,
                codec_family_class="avc",
                stream_variant_index=0,
                backup_index=0,
            )

    def test_replace_cannot_turn_still_locator_into_motion_without_indexes(self):
        locator = XhsLivePhotoMemberLocatorV1(
            source_item_ordinal=0,
            member_role="still",
            source_kind="url_default",
        )
        with self.assertRaises(XhsLivePhotoCandidateError):
            replace(locator, member_role="motion", source_kind="master_url")


class XhsLivePhotoCandidateFormatEvidenceTests(SimpleTestCase):
    def test_still_magic_covers_jpeg_png_webp_and_avif(self):
        for prefix, expected in (
            (JPEG, "jpeg"),
            (PNG, "png"),
            (WEBP, "webp"),
            (AVIF, "avif"),
        ):
            with self.subTest(expected=expected):
                result = resolve_image_format(
                    prefix,
                    content_type="application/octet-stream",
                )
                self.assertEqual(result.format, expected)
                self.assertEqual(result.confidence, "magic")

    def test_mp4_magic_is_proven_without_a_url_suffix(self):
        result = resolve_xhs_motion_format_for_research(
            MP4,
            content_type="video/mp4",
        )
        self.assertTrue(result.valid)
        self.assertEqual(result.format_class, "mp4")
        self.assertEqual(result.confidence, "magic")

    def test_quicktime_magic_is_classified_as_mov(self):
        result = resolve_xhs_motion_format_for_research(
            MOV,
            content_type="video/quicktime",
        )
        self.assertEqual(result.format_class, "mov")

    def test_webm_magic_is_classified_from_ebml(self):
        result = resolve_xhs_motion_format_for_research(
            WEBM,
            content_type="video/webm",
        )
        self.assertEqual(result.format_class, "webm")

    def test_html_json_xml_and_svg_motion_bodies_are_invalid(self):
        for prefix in (
            b"<!doctype html>",
            b'{"error":true}',
            b"<?xml version='1.0'?><error/>",
            b"<svg/>",
        ):
            with self.subTest(prefix=prefix[:12]):
                result = resolve_xhs_motion_format_for_research(
                    prefix,
                    content_type="video/mp4",
                )
                self.assertFalse(result.valid)
                self.assertEqual(result.error_code, "MOTION_CONTENT_INVALID")

    def test_motion_magic_wins_and_records_content_type_mismatch(self):
        result = resolve_xhs_motion_format_for_research(
            WEBM,
            content_type="video/mp4",
        )
        self.assertEqual(result.format_class, "webm")
        self.assertTrue(result.metadata_mismatch)

    def test_content_type_alone_does_not_prove_motion_format(self):
        result = resolve_xhs_motion_format_for_research(
            b"unknown-binary",
            content_type="video/mp4",
        )
        self.assertFalse(result.valid)
        self.assertEqual(result.error_code, "MOTION_FORMAT_UNDETERMINED")

    def test_avif_ftyp_is_not_misclassified_as_motion_mp4(self):
        result = resolve_xhs_motion_format_for_research(
            AVIF,
            content_type="video/mp4",
        )
        self.assertFalse(result.valid)
        self.assertEqual(result.error_code, "MOTION_FORMAT_UNDETERMINED")


class XhsLivePhotoCandidateHostResearchTests(SimpleTestCase):
    def test_reviewed_still_host_is_classified(self):
        self.assertEqual(
            classify_xhs_livephoto_host_for_research(
                secure_url("sns-webpic-qc.xhscdn.com"),
                member_role="still",
            ),
            "still_exact",
        )

    def test_reviewed_motion_host_families_are_classified(self):
        cases = (
            ("sns-video-v6.xhscdn.com", "motion_versioned"),
            ("sns-video-zl.xhscdn.com", "motion_zl"),
            ("sns-bak-v3.xhscdn.com", "motion_backup"),
        )
        for host, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(
                    classify_xhs_livephoto_host_for_research(
                        secure_url(host),
                        member_role="motion",
                    ),
                    expected,
                )

    def test_host_lookalikes_are_rejected(self):
        for host in (
            "sns-video-v6.xhscdn.com.example.test",
            "evil-sns-video-v6.xhscdn.com",
            "sns-bak-v.xhscdn.com",
            "sns-webpic-qc.xhscdn.com.example.test",
        ):
            with self.subTest(host=host):
                self.assertEqual(
                    classify_xhs_livephoto_host_for_research(
                        secure_url(host),
                        member_role="motion",
                    ),
                    "",
                )

    def test_non_https_userinfo_custom_port_and_empty_path_are_rejected(self):
        values = (
            "http" + "://sns-video-v6.xhscdn.com/resource",
            "https" + "://user@sns-video-v6.xhscdn.com/resource",
            "https" + "://sns-video-v6.xhscdn.com:444/resource",
            "https" + "://sns-video-v6.xhscdn.com/",
        )
        for value in values:
            with self.subTest(value_type=value.split(":", 1)[0]):
                self.assertEqual(
                    classify_xhs_livephoto_host_for_research(
                        value,
                        member_role="motion",
                    ),
                    "",
                )

    def test_still_host_is_not_accepted_as_motion(self):
        self.assertEqual(
            classify_xhs_livephoto_host_for_research(
                secure_url("sns-webpic-qc.xhscdn.com"),
                member_role="motion",
            ),
            "",
        )


class XhsLivePhotoCandidateRuntimeBoundaryTests(SimpleTestCase):
    def test_current_xhs_adapter_and_registry_do_not_import_locator(self):
        consumers = (
            PROJECT_ROOT / "xhs_download" / "services" / "platforms" / "xiaohongshu.py",
            PROJECT_ROOT / "xhs_download" / "services" / "registry.py",
            PROJECT_ROOT / "xhs_download" / "services" / "parser.py",
            PROJECT_ROOT / "xhs_download" / "views.py",
        )
        module_name = "xhs_livephoto_candidate_locator"
        for path in consumers:
            with self.subTest(path=path.name):
                self.assertNotIn(module_name, path.read_text(encoding="utf-8"))

    def test_candidate_module_constructs_no_runtime_asset_or_manifest(self):
        source = (
            PROJECT_ROOT
            / "xhs_download"
            / "services"
            / "platforms"
            / "xhs_livephoto_candidate_locator.py"
        ).read_text(encoding="utf-8")
        for forbidden in (
            "MediaAsset(",
            "XhsRichMediaAssetSpec(",
            "RichMediaContractV1(",
            "ProviderManifest",
        ):
            self.assertNotIn(forbidden, source)
