import copy
import inspect
import json
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from unittest.mock import patch

from django.test import SimpleTestCase

from .services.image_formats import resolve_image_format
from .services.platforms.xhs_livephoto_asset_spec_mapper import (
    XHS_LIVEPHOTO_ASSET_ID_SCOPE,
    XHS_LIVEPHOTO_ASSET_SCOPE_HEX_LENGTH,
    XHS_LIVEPHOTO_ASSET_SPEC_SCHEMA_VERSION,
    XhsLivePhotoAssetBindingV1,
    XhsLivePhotoAssetSpecMapV1,
    XhsLivePhotoAssetSpecMappingError,
    XhsLivePhotoGroupSpecV1,
    XhsLivePhotoPrivateFormatProofV1,
    XhsLivePhotoPrivateMemberProofV1,
    build_xhs_livephoto_asset_specs,
    build_xhs_livephoto_member_proof,
    build_xhs_livephoto_private_format_proof,
)
from .services.platforms.xhs_livephoto_browser_admission import (
    XHS_LIVEPHOTO_BROWSER_ADMISSION_SCHEMA_VERSION,
    XhsPrivateHttpsMediaCandidateV1,
    normalize_xhs_livephoto_candidate_https,
)
from .services.platforms.xhs_livephoto_candidate_locator import (
    XHS_LIVEPHOTO_CANDIDATE_SCHEMA_VERSION,
    XhsLivePhotoMemberLocatorV1,
    locate_xhs_livephoto_candidates,
    read_candidate_from_locator,
)
from .services.platforms.xhs_livephoto_relationship_mapper import (
    XHS_LIVEPHOTO_RELATIONSHIP_SCHEMA_VERSION,
    map_xhs_livephoto_relationships,
)
from .services.platforms.xhs_rich_media import (
    XhsRichMediaAssetSpec,
    normalize_xhs_rich_media_payload,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MAPPER_PATH = (
    PROJECT_ROOT
    / "xhs_download"
    / "services"
    / "platforms"
    / "xhs_livephoto_asset_spec_mapper.py"
)
NOTE_ID = "a" * 24
OTHER_NOTE_ID = "b" * 24
PRIVATE_MARKER = "private-candidate-marker"
JPEG = b"\xff\xd8\xff\xe0synthetic-jpeg"
WEBP = b"RIFF\x10\x00\x00\x00WEBPsynthetic"
MP4 = b"\x00\x00\x00\x18ftypisom\x00\x00\x00\x00isommp42"


def synthetic_url(host, label, *, query="proof=synthetic"):
    return "https" + "://" + host + "/synthetic/" + label + "?" + query


def regular_item(label, *, query="proof=synthetic"):
    still = synthetic_url(
        "sns-webpic-qc.xhscdn.com",
        f"still-{label}",
        query=query,
    )
    return {
        "livePhoto": False,
        "urlDefault": still,
        "infoList": [{"imageScene": "WB_DFT", "url": still}],
        "stream": {},
    }


def livephoto_item(
    label,
    *,
    motion_host="sns-video-v7.xhscdn.com",
    use_backup=False,
    query="proof=synthetic",
):
    item = regular_item(label, query=query)
    item["livePhoto"] = True
    motion = synthetic_url(motion_host, f"motion-{label}", query=query)
    variant = {"backupUrls": [motion] if use_backup else []}
    if not use_backup:
        variant["masterUrl"] = motion
    item["stream"] = {"h264": [variant]}
    return item


def build_maps(items, *, note_id=NOTE_ID):
    relationships = map_xhs_livephoto_relationships(
        requested_note_id=note_id,
        payload_note_id=note_id,
        image_list=items,
    )
    candidates = locate_xhs_livephoto_candidates(
        relationship_map=relationships,
        image_list=items,
    )
    return relationships, candidates


def private_https_candidate(items, locator):
    private = read_candidate_from_locator(image_list=items, locator=locator)
    return normalize_xhs_livephoto_candidate_https(
        private,
        member_role=locator.member_role,
    )


def format_prefix(role, *, still_format="jpeg"):
    if role == "motion":
        return MP4
    return JPEG if still_format == "jpeg" else WEBP


def private_format_proof(candidate, *, still_format="jpeg"):
    return build_xhs_livephoto_private_format_proof(
        private_https_candidate=candidate,
        prefix=format_prefix(
            candidate.member_role,
            still_format=still_format,
        ),
        content_type="application/octet-stream",
    )


def proof_for_locator(items, locator, *, still_format="jpeg"):
    candidate = private_https_candidate(items, locator)
    return build_xhs_livephoto_member_proof(
        locator=locator,
        private_https_candidate=candidate,
        private_format_proof=private_format_proof(
            candidate,
            still_format=still_format,
        ),
    )


def proofs_for_map(items, candidates, *, webp_ordinals=frozenset()):
    proofs = []
    for item in candidates.items:
        proofs.append(
            proof_for_locator(
                items,
                item.still_locator,
                still_format=(
                    "webp"
                    if item.source_item_ordinal in webp_ordinals
                    else "jpeg"
                ),
            )
        )
        if item.motion_locator is not None:
            proofs.append(proof_for_locator(items, item.motion_locator))
    return tuple(proofs)


def build_result(
    items,
    *,
    note_id=NOTE_ID,
    relationships=None,
    candidates=None,
    proofs=None,
    webp_ordinals=frozenset(),
):
    if relationships is None or candidates is None:
        relationships, candidates = build_maps(items, note_id=note_id)
    if proofs is None:
        proofs = proofs_for_map(
            items,
            candidates,
            webp_ordinals=webp_ordinals,
        )
    return build_xhs_livephoto_asset_specs(
        requested_note_id=note_id,
        payload_note_id=note_id,
        relationship_map=relationships,
        candidate_map=candidates,
        image_list=items,
        member_proofs=proofs,
    )


def rebuild_spec_map(result, *, assets=None, groups=None, post_type=None):
    return XhsLivePhotoAssetSpecMapV1(
        schema_version=result.schema_version,
        relationship_schema_version=result.relationship_schema_version,
        candidate_schema_version=result.candidate_schema_version,
        browser_admission_schema_version=result.browser_admission_schema_version,
        post_type=post_type or result.post_type,
        assets=result.assets if assets is None else tuple(assets),
        groups=result.groups if groups is None else tuple(groups),
    )


def sanitized_bridge_payload(result):
    note_id = "sanitized-fixture-note"
    assets_by_ordinal = {}
    for binding in result.assets:
        assets_by_ordinal.setdefault(binding.source_item_ordinal, []).append(binding)
    groups_by_ordinal = {group.source_item_ordinal: group for group in result.groups}
    items = []
    for ordinal in sorted(assets_by_ordinal):
        bindings = assets_by_ordinal[ordinal]
        group = groups_by_ordinal.get(ordinal)
        if group is None:
            asset = bindings[0].asset_spec
            items.append(
                {
                    "kind": "image",
                    "asset_id": asset.asset_id,
                    "extension": asset.extension,
                }
            )
            continue
        still, motion = (binding.asset_spec for binding in bindings)
        items.append(
            {
                "kind": "live_photo",
                "pair_id": group.group_id,
                "owner_note_id": note_id,
                "still": {
                    "asset_id": still.asset_id,
                    "extension": still.extension,
                },
                "motion": {
                    "asset_id": motion.asset_id,
                    "extension": motion.extension,
                },
            }
        )
    return {
        "requested_note_id": note_id,
        "payload_note_id": note_id,
        "items": items,
    }


class XhsLivePhotoAssetIdentityTests(SimpleTestCase):
    def assert_mapper_error(self, code, callback):
        with self.assertRaises(XhsLivePhotoAssetSpecMappingError) as raised:
            callback()
        self.assertEqual(raised.exception.code, code)
        rendered = str(raised.exception)
        self.assertNotIn(NOTE_ID, rendered)
        self.assertNotIn(OTHER_NOTE_ID, rendered)
        self.assertNotIn(PRIVATE_MARKER, rendered)
        return raised.exception

    def test_same_note_and_order_produce_deterministic_ids(self):
        first = build_result([livephoto_item("one"), regular_item("two")])
        second = build_result([livephoto_item("one"), regular_item("two")])

        self.assertEqual(
            [asset.asset_spec.asset_id for asset in first.assets],
            [asset.asset_spec.asset_id for asset in second.assets],
        )
        self.assertEqual(
            [group.group_id for group in first.groups],
            [group.group_id for group in second.groups],
        )

    def test_different_note_identity_produces_different_scope_and_ids(self):
        items = [regular_item("one")]
        first = build_result(items, note_id=NOTE_ID)
        second = build_result(items, note_id=OTHER_NOTE_ID)

        self.assertNotEqual(
            first.assets[0].asset_spec.asset_id,
            second.assets[0].asset_spec.asset_id,
        )

    def test_changed_source_ordinal_changes_result_scoped_id(self):
        first = build_result([regular_item("target")])
        second = build_result([regular_item("other"), regular_item("target")])

        self.assertNotEqual(
            first.assets[0].asset_spec.asset_id,
            second.assets[1].asset_spec.asset_id,
        )
        self.assertTrue(second.assets[1].asset_spec.asset_id.endswith(":i1:still"))

    def test_ids_are_opaque_and_do_not_contain_raw_note_identity(self):
        result = build_result([livephoto_item("one")])
        rendered = json.dumps(result.to_structural_dict(), sort_keys=True)

        self.assertNotIn(NOTE_ID, rendered)
        self.assertNotIn("payload_note_id", rendered)
        self.assertEqual(
            result.to_structural_dict()["asset_id_scope"],
            XHS_LIVEPHOTO_ASSET_ID_SCOPE,
        )
        scope = result.assets[0].asset_spec.asset_id.split(":")[1]
        self.assertEqual(len(scope), XHS_LIVEPHOTO_ASSET_SCOPE_HEX_LENGTH)

    def test_ids_do_not_depend_on_candidate_url_or_query(self):
        first = build_result([livephoto_item("one", query="proof=first")])
        second = build_result([livephoto_item("one", query="proof=second")])

        self.assertEqual(
            [asset.asset_spec.asset_id for asset in first.assets],
            [asset.asset_spec.asset_id for asset in second.assets],
        )
        self.assertEqual(first.groups[0].group_id, second.groups[0].group_id)

    def test_asset_group_id_forms_use_owned_namespace(self):
        result = build_result([livephoto_item("one")])

        self.assertRegex(
            result.assets[0].asset_spec.asset_id,
            r"^xhs:[0-9a-f]{24}:i0:still$",
        )
        self.assertRegex(
            result.assets[1].asset_spec.asset_id,
            r"^xhs:[0-9a-f]{24}:i0:motion$",
        )
        self.assertRegex(result.groups[0].group_id, r"^xhs:[0-9a-f]{24}:i0:live$")

    def test_output_types_are_frozen(self):
        result = build_result([livephoto_item("one")])
        with self.assertRaises(FrozenInstanceError):
            result.post_type = "gallery"
        with self.assertRaises(FrozenInstanceError):
            result.assets[0].group_id = None


class XhsLivePhotoAssetSpecShapeTests(SimpleTestCase):
    def test_one_regular_image_uses_image_primary(self):
        result = build_result([regular_item("one")])

        self.assertEqual(result.post_type, "image")
        self.assertEqual(len(result.assets), 1)
        self.assertEqual(result.assets[0].asset_spec.role, "primary")
        self.assertEqual(result.groups, ())

    def test_four_regular_images_use_gallery_roles(self):
        result = build_result([regular_item(str(index)) for index in range(4)])

        self.assertEqual(result.post_type, "gallery")
        self.assertEqual(len(result.assets), 4)
        self.assertEqual({asset.asset_spec.role for asset in result.assets}, {"gallery"})
        self.assertTrue(all(asset.group_id is None for asset in result.assets))

    def test_fourteen_livephotos_expand_to_twenty_eight_assets_and_groups(self):
        items = [livephoto_item(str(index)) for index in range(14)]
        result = build_result(items, webp_ordinals=frozenset({1, 3, 5}))

        self.assertEqual(result.post_type, "live_photo")
        self.assertEqual(len(result.assets), 28)
        self.assertEqual(len(result.groups), 14)
        self.assertEqual(
            [asset.asset_spec.role for asset in result.assets],
            [role for _ in range(14) for role in ("still", "motion")],
        )

    def test_mixed_seventeen_item_shape_preserves_physical_expansion_order(self):
        regular_ordinals = {2, 6, 9, 13, 16}
        items = [
            regular_item(str(index))
            if index in regular_ordinals
            else livephoto_item(
                str(index),
                motion_host=(
                    "sns-bak-v8.xhscdn.com"
                    if index % 2
                    else "sns-video-v8.xhscdn.com"
                ),
                use_backup=bool(index % 2),
            )
            for index in range(17)
        ]
        result = build_result(items)

        expected_ordinals = [
            ordinal
            for ordinal in range(17)
            for _ in range(1 if ordinal in regular_ordinals else 2)
        ]
        self.assertEqual(result.post_type, "multimedia")
        self.assertEqual(
            [asset.source_item_ordinal for asset in result.assets],
            expected_ordinals,
        )
        for ordinal in regular_ordinals:
            regular = [
                asset
                for asset in result.assets
                if asset.source_item_ordinal == ordinal
            ]
            self.assertEqual([asset.asset_spec.role for asset in regular], ["inline"])

    def test_each_livephoto_still_immediately_precedes_motion(self):
        result = build_result(
            [livephoto_item("one"), regular_item("two"), livephoto_item("three")]
        )

        for group in result.groups:
            ids = [asset.asset_spec.asset_id for asset in result.assets]
            still_index = ids.index(group.still_asset_id)
            self.assertEqual(ids[still_index + 1], group.motion_asset_id)

    def test_format_mapping_uses_only_closed_magic_classes(self):
        result = build_result(
            [livephoto_item("one"), regular_item("two")],
            webp_ordinals=frozenset({1}),
        )

        self.assertEqual(
            [asset.asset_spec.extension for asset in result.assets],
            ["jpg", "mp4", "webp"],
        )
        self.assertTrue(
            all(type(asset.asset_spec) is XhsRichMediaAssetSpec for asset in result.assets)
        )

    def test_group_ids_are_deterministic_unique_and_exclude_regular_images(self):
        result = build_result(
            [livephoto_item("one"), regular_item("two"), livephoto_item("three")]
        )

        self.assertEqual(len({group.group_id for group in result.groups}), 2)
        self.assertEqual(result.assets[2].group_id, None)
        self.assertEqual(
            {asset.asset_spec.asset_id for asset in result.assets if asset.group_id},
            {
                member
                for group in result.groups
                for member in (group.still_asset_id, group.motion_asset_id)
            },
        )

    def test_asset_binding_rejects_group_membership_inconsistency(self):
        result = build_result([livephoto_item("one")])

        with self.assertRaises(XhsLivePhotoAssetSpecMappingError):
            replace(result.assets[0], group_id=None)

    def test_output_rejects_one_asset_referenced_by_two_groups(self):
        result = build_result([livephoto_item("one"), livephoto_item("two")])
        duplicate_members = replace(
            result.groups[1],
            still_asset_id=result.groups[0].still_asset_id,
            motion_asset_id=result.groups[0].motion_asset_id,
        )

        with self.assertRaises(XhsLivePhotoAssetSpecMappingError):
            XhsLivePhotoAssetSpecMapV1(
                schema_version=result.schema_version,
                relationship_schema_version=result.relationship_schema_version,
                candidate_schema_version=result.candidate_schema_version,
                browser_admission_schema_version=result.browser_admission_schema_version,
                post_type=result.post_type,
                assets=result.assets,
                    groups=(result.groups[0], duplicate_members),
                )


class XhsLivePhotoAssetSpecOutputInvariantTests(SimpleTestCase):
    def assert_output_rejected(self, callback):
        with self.assertRaises(XhsLivePhotoAssetSpecMappingError) as raised:
            callback()
        self.assertEqual(
            raised.exception.code,
            "XHS_LIVEPHOTO_ASSET_SPEC_OUTPUT_INVALID",
        )

    def test_two_groups_cannot_share_one_source_ordinal(self):
        result = build_result([livephoto_item("one"), livephoto_item("two")])
        duplicate_ordinal = replace(result.groups[1], source_item_ordinal=0)

        self.assert_output_rejected(
            lambda: rebuild_spec_map(
                result,
                groups=(result.groups[0], duplicate_ordinal),
            )
        )

    def test_one_ordinary_ordinal_cannot_have_two_assets(self):
        result = build_result([regular_item("one"), regular_item("two")])
        duplicate_ordinal = replace(
            result.assets[1],
            locator=result.assets[0].locator,
            source_item_ordinal=0,
        )

        self.assert_output_rejected(
            lambda: rebuild_spec_map(
                result,
                assets=(result.assets[0], duplicate_ordinal),
            )
        )

    def test_source_ordinal_gap_is_rejected(self):
        result = build_result(
            [regular_item("zero"), regular_item("one"), regular_item("two")]
        )

        self.assert_output_rejected(
            lambda: rebuild_spec_map(
                result,
                assets=(result.assets[0], result.assets[2]),
            )
        )

    def test_source_ordinals_must_start_at_zero(self):
        result = build_result([regular_item("zero"), regular_item("one")])
        ordinal_one = replace(
            result.assets[1],
            asset_spec=replace(result.assets[1].asset_spec, role="primary"),
        )

        self.assert_output_rejected(
            lambda: rebuild_spec_map(
                result,
                assets=(ordinal_one,),
                post_type="image",
            )
        )

    def test_grouped_ordinal_cannot_have_three_assets(self):
        result = build_result([livephoto_item("one")])
        still = result.assets[0]
        scope = still.asset_spec.asset_id.split(":")[1]
        extra_still = replace(
            still,
            asset_spec=replace(
                still.asset_spec,
                asset_id=f"xhs:{scope}:i0:extra",
            ),
        )

        self.assert_output_rejected(
            lambda: rebuild_spec_map(
                result,
                assets=result.assets + (extra_still,),
            )
        )

    def test_grouped_ordinal_cannot_have_only_still(self):
        result = build_result([livephoto_item("one")])

        self.assert_output_rejected(
            lambda: rebuild_spec_map(result, assets=(result.assets[0],))
        )

    def test_grouped_ordinal_requires_still_then_motion_order(self):
        result = build_result([livephoto_item("one")])

        self.assert_output_rejected(
            lambda: rebuild_spec_map(result, assets=tuple(reversed(result.assets)))
        )

    def test_ungrouped_motion_is_rejected(self):
        result = build_result([livephoto_item("one")])

        self.assert_output_rejected(
            lambda: replace(result.assets[1], group_id=None)
        )

    def test_ordinary_images_cannot_be_referenced_by_group(self):
        result = build_result([regular_item("one"), regular_item("two")])
        scope = result.assets[0].asset_spec.asset_id.split(":")[1]
        forged_group = XhsLivePhotoGroupSpecV1(
            group_id=f"xhs:{scope}:i0:live",
            source_item_ordinal=0,
            still_asset_id=result.assets[0].asset_spec.asset_id,
            motion_asset_id=result.assets[1].asset_spec.asset_id,
        )

        self.assert_output_rejected(
            lambda: rebuild_spec_map(result, groups=(forged_group,))
        )

    def test_group_ordinal_must_match_member_ordinal(self):
        result = build_result([livephoto_item("one")])
        mismatched_group = replace(result.groups[0], source_item_ordinal=1)

        self.assert_output_rejected(
            lambda: rebuild_spec_map(result, groups=(mismatched_group,))
        )

    def test_group_specs_follow_source_ordinal_order(self):
        result = build_result([livephoto_item("one"), livephoto_item("two")])

        self.assert_output_rejected(
            lambda: rebuild_spec_map(result, groups=tuple(reversed(result.groups)))
        )


class XhsLivePhotoMemberProofTests(SimpleTestCase):
    def assert_mapper_error(self, code, callback):
        with self.assertRaises(XhsLivePhotoAssetSpecMappingError) as raised:
            callback()
        self.assertEqual(raised.exception.code, code)
        self.assertEqual(str(raised.exception), code)
        return raised.exception

    def test_exact_candidate_with_own_format_proof_passes(self):
        items = [livephoto_item("one")]
        _, candidates = build_maps(items)
        locator = candidates.items[0].motion_locator
        candidate = private_https_candidate(items, locator)
        format_proof = private_format_proof(candidate)

        proof = build_xhs_livephoto_member_proof(
            locator=locator,
            private_https_candidate=candidate,
            private_format_proof=format_proof,
        )

        self.assertEqual(proof.format_class, "mp4")
        self.assertTrue(proof.host_class_browser_verified)

    def test_exact_still_candidate_uses_magic_not_declared_mime(self):
        items = [regular_item("one")]
        _, candidates = build_maps(items)
        locator = candidates.items[0].still_locator
        candidate = private_https_candidate(items, locator)
        format_proof = build_xhs_livephoto_private_format_proof(
            private_https_candidate=candidate,
            prefix=WEBP,
            content_type="image/jpeg",
        )

        proof = build_xhs_livephoto_member_proof(
            locator=locator,
            private_https_candidate=candidate,
            private_format_proof=format_proof,
        )

        self.assertEqual(proof.format_class, "webp")
        self.assertEqual(proof.extension, "webp")

    def test_candidate_proof_from_another_item_fails(self):
        items = [regular_item("one"), regular_item("two")]
        relationships, candidates = build_maps(items)
        proofs = proofs_for_map(items, candidates)
        other_candidate = private_https_candidate(
            items,
            candidates.items[1].still_locator,
        )
        forged = build_xhs_livephoto_member_proof(
            locator=candidates.items[0].still_locator,
            private_https_candidate=other_candidate,
            private_format_proof=private_format_proof(other_candidate),
        )

        self.assert_mapper_error(
            "XHS_LIVEPHOTO_ASSET_SPEC_CANDIDATE_BINDING_MISMATCH",
            lambda: build_result(
                items,
                relationships=relationships,
                candidates=candidates,
                proofs=(forged, proofs[1]),
            ),
        )

    def test_stale_snapshot_candidate_binding_fails(self):
        items = [regular_item("one")]
        relationships, candidates = build_maps(items)
        proofs = proofs_for_map(items, candidates)
        stale_snapshot = copy.deepcopy(items)
        replacement = synthetic_url(
            "sns-webpic-qc.xhscdn.com",
            "replacement-" + PRIVATE_MARKER,
        )
        stale_snapshot[0]["urlDefault"] = replacement
        stale_snapshot[0]["infoList"][0]["url"] = replacement

        self.assert_mapper_error(
            "XHS_LIVEPHOTO_ASSET_SPEC_CANDIDATE_BINDING_MISMATCH",
            lambda: build_result(
                stale_snapshot,
                relationships=relationships,
                candidates=candidates,
                proofs=proofs,
            ),
        )

    def test_master_to_backup_candidate_binding_mismatch_fails(self):
        master_items = [livephoto_item("one")]
        _, master_candidates = build_maps(master_items)
        master_locator = master_candidates.items[0].motion_locator
        master_candidate = private_https_candidate(master_items, master_locator)
        backup_items = [
            livephoto_item(
                "one",
                motion_host="sns-bak-v9.xhscdn.com",
                use_backup=True,
            )
        ]
        relationships, backup_candidates = build_maps(backup_items)
        backup_proofs = proofs_for_map(backup_items, backup_candidates)
        forged_motion = build_xhs_livephoto_member_proof(
            locator=backup_candidates.items[0].motion_locator,
            private_https_candidate=master_candidate,
            private_format_proof=private_format_proof(master_candidate),
        )

        self.assert_mapper_error(
            "XHS_LIVEPHOTO_ASSET_SPEC_CANDIDATE_BINDING_MISMATCH",
            lambda: build_result(
                backup_items,
                relationships=relationships,
                candidates=backup_candidates,
                proofs=(backup_proofs[0], forged_motion),
            ),
        )

    def test_same_host_and_format_cross_candidate_proof_fails(self):
        first_items = [regular_item("one", query="proof=first")]
        second_items = [regular_item("one", query="proof=second")]
        _, first_map = build_maps(first_items)
        _, second_map = build_maps(second_items)
        first_candidate = private_https_candidate(
            first_items,
            first_map.items[0].still_locator,
        )
        second_locator = second_map.items[0].still_locator
        second_candidate = private_https_candidate(second_items, second_locator)

        self.assert_mapper_error(
            "XHS_LIVEPHOTO_ASSET_SPEC_FORMAT_CANDIDATE_BINDING_MISMATCH",
            lambda: build_xhs_livephoto_member_proof(
                locator=second_locator,
                private_https_candidate=second_candidate,
                private_format_proof=private_format_proof(first_candidate),
            ),
        )

    def test_still_format_proof_cannot_be_reused_across_candidates(self):
        first_items = [regular_item("one", query="proof=still-a")]
        second_items = [regular_item("one", query="proof=still-b")]
        _, first_map = build_maps(first_items)
        _, second_map = build_maps(second_items)
        first_candidate = private_https_candidate(
            first_items,
            first_map.items[0].still_locator,
        )
        second_locator = second_map.items[0].still_locator
        second_candidate = private_https_candidate(second_items, second_locator)

        self.assert_mapper_error(
            "XHS_LIVEPHOTO_ASSET_SPEC_FORMAT_CANDIDATE_BINDING_MISMATCH",
            lambda: build_xhs_livephoto_member_proof(
                locator=second_locator,
                private_https_candidate=second_candidate,
                private_format_proof=private_format_proof(
                    first_candidate,
                    still_format="webp",
                ),
            ),
        )

    def test_motion_format_proof_cannot_be_reused_across_candidates(self):
        first_items = [livephoto_item("one", query="proof=motion-a")]
        second_items = [livephoto_item("one", query="proof=motion-b")]
        _, first_map = build_maps(first_items)
        _, second_map = build_maps(second_items)
        first_candidate = private_https_candidate(
            first_items,
            first_map.items[0].motion_locator,
        )
        second_locator = second_map.items[0].motion_locator
        second_candidate = private_https_candidate(second_items, second_locator)

        self.assert_mapper_error(
            "XHS_LIVEPHOTO_ASSET_SPEC_FORMAT_CANDIDATE_BINDING_MISMATCH",
            lambda: build_xhs_livephoto_member_proof(
                locator=second_locator,
                private_https_candidate=second_candidate,
                private_format_proof=private_format_proof(first_candidate),
            ),
        )

    def test_stale_signed_query_format_proof_fails(self):
        first_items = [regular_item("one", query="token=old")]
        fresh_items = [regular_item("one", query="token=fresh")]
        _, first_map = build_maps(first_items)
        _, fresh_map = build_maps(fresh_items)
        first_candidate = private_https_candidate(
            first_items,
            first_map.items[0].still_locator,
        )
        fresh_locator = fresh_map.items[0].still_locator
        fresh_candidate = private_https_candidate(fresh_items, fresh_locator)

        self.assert_mapper_error(
            "XHS_LIVEPHOTO_ASSET_SPEC_FORMAT_CANDIDATE_BINDING_MISMATCH",
            lambda: build_xhs_livephoto_member_proof(
                locator=fresh_locator,
                private_https_candidate=fresh_candidate,
                private_format_proof=private_format_proof(first_candidate),
            ),
        )

    def test_master_format_proof_cannot_authorize_backup_candidate(self):
        master_items = [livephoto_item("one")]
        backup_items = [
            livephoto_item(
                "one",
                motion_host="sns-bak-v9.xhscdn.com",
                use_backup=True,
            )
        ]
        _, master_map = build_maps(master_items)
        _, backup_map = build_maps(backup_items)
        master_candidate = private_https_candidate(
            master_items,
            master_map.items[0].motion_locator,
        )
        backup_locator = backup_map.items[0].motion_locator
        backup_candidate = private_https_candidate(backup_items, backup_locator)

        self.assert_mapper_error(
            "XHS_LIVEPHOTO_ASSET_SPEC_FORMAT_CANDIDATE_BINDING_MISMATCH",
            lambda: build_xhs_livephoto_member_proof(
                locator=backup_locator,
                private_https_candidate=backup_candidate,
                private_format_proof=private_format_proof(master_candidate),
            ),
        )

    def test_mime_only_format_evidence_is_rejected(self):
        items = [regular_item("one")]
        _, candidates = build_maps(items)
        candidate = private_https_candidate(items, candidates.items[0].still_locator)

        self.assert_mapper_error(
            "XHS_LIVEPHOTO_ASSET_SPEC_FORMAT_EVIDENCE_INVALID",
            lambda: build_xhs_livephoto_private_format_proof(
                private_https_candidate=candidate,
                prefix=b"",
                content_type="image/jpeg",
            ),
        )

    def test_raw_or_url_suffix_format_resolution_cannot_authorize_member(self):
        items = [regular_item("one")]
        _, candidates = build_maps(items)
        locator = candidates.items[0].still_locator
        candidate = private_https_candidate(items, locator)
        suffix_only = resolve_image_format(
            b"unknown-binary",
            url="https" + "://invalid.example/synthetic.jpg",
        )

        self.assert_mapper_error(
            "XHS_LIVEPHOTO_ASSET_SPEC_FORMAT_EVIDENCE_INVALID",
            lambda: build_xhs_livephoto_member_proof(
                locator=locator,
                private_https_candidate=candidate,
                private_format_proof=suffix_only,
            ),
        )

    def test_unknown_format_is_rejected(self):
        items = [regular_item("one")]
        _, candidates = build_maps(items)
        candidate = private_https_candidate(items, candidates.items[0].still_locator)

        self.assert_mapper_error(
            "XHS_LIVEPHOTO_ASSET_SPEC_FORMAT_EVIDENCE_INVALID",
            lambda: build_xhs_livephoto_private_format_proof(
                private_https_candidate=candidate,
                prefix=b"unknown-binary",
                content_type="application/octet-stream",
            ),
        )

    def test_motion_zl_and_unknown_hosts_fail_before_format_resolution(self):
        resolver_path = (
            "xhs_download.services.platforms."
            "xhs_livephoto_asset_spec_mapper."
            "resolve_xhs_motion_format_for_research"
        )

        for host_class in ("motion_zl", "motion_unknown"):
            with self.subTest(host_class=host_class):
                forged = object.__new__(XhsPrivateHttpsMediaCandidateV1)
                object.__setattr__(
                    forged,
                    "value",
                    synthetic_url("sns-video-zl.xhscdn.com", PRIVATE_MARKER),
                )
                object.__setattr__(forged, "member_role", "motion")
                object.__setattr__(forged, "host_class", host_class)
                object.__setattr__(forged, "upgrade_applied", False)
                with patch(resolver_path) as resolver:
                    self.assert_mapper_error(
                        "XHS_LIVEPHOTO_ASSET_SPEC_HOST_UNVERIFIED",
                        lambda forged=forged: (
                            build_xhs_livephoto_private_format_proof(
                                private_https_candidate=forged,
                                prefix=MP4,
                                content_type="video/mp4",
                            )
                        ),
                    )
                    resolver.assert_not_called()

    def test_private_binding_is_excluded_from_repr_equality_hash_and_output(self):
        first_items = [regular_item("one", query="proof=first")]
        second_items = [regular_item("one", query="proof=second")]
        _, first_map = build_maps(first_items)
        _, second_map = build_maps(second_items)
        proof = proof_for_locator(first_items, first_map.items[0].still_locator)
        alternate = proof_for_locator(
            second_items,
            second_map.items[0].still_locator,
        )
        rendered = repr(proof) + json.dumps(proof.to_structural_dict())

        self.assertEqual(proof, alternate)
        self.assertEqual(hash(proof), hash(alternate))
        self.assertNotIn(proof.candidate_binding, rendered)
        self.assertNotIn("candidate_binding", rendered)

    def test_format_binding_is_excluded_from_repr_equality_hash_and_output(self):
        first_items = [regular_item("one", query="proof=first")]
        second_items = [regular_item("one", query="proof=second")]
        _, first_map = build_maps(first_items)
        _, second_map = build_maps(second_items)
        first_candidate = private_https_candidate(
            first_items,
            first_map.items[0].still_locator,
        )
        second_candidate = private_https_candidate(
            second_items,
            second_map.items[0].still_locator,
        )
        proof = private_format_proof(first_candidate)
        alternate = private_format_proof(second_candidate)
        rendered = repr(proof) + json.dumps(proof.to_structural_dict())

        self.assertEqual(proof, alternate)
        self.assertEqual(hash(proof), hash(alternate))
        self.assertNotIn(proof.candidate_binding, rendered)
        self.assertNotIn("candidate_binding", rendered)

    def test_host_browser_policy_is_class_level_not_factory_input(self):
        parameters = inspect.signature(build_xhs_livephoto_member_proof).parameters
        items = [regular_item("one")]
        _, candidates = build_maps(items)
        proof = proofs_for_map(items, candidates)[0]

        self.assertNotIn("browser_admission_evidence", parameters)
        self.assertTrue(proof.host_class_browser_verified)
        self.assertNotIn("browser_verified", proof.to_structural_dict())

    def test_output_contains_no_candidate_or_binding_material(self):
        items = [livephoto_item(PRIVATE_MARKER)]
        relationships, candidates = build_maps(items)
        proofs = proofs_for_map(items, candidates)
        result = build_result(
            items,
            relationships=relationships,
            candidates=candidates,
            proofs=proofs,
        )
        rendered = repr(result) + json.dumps(result.to_structural_dict())

        self.assertNotIn(PRIVATE_MARKER, rendered)
        self.assertNotIn(NOTE_ID, rendered)
        for proof in proofs:
            self.assertNotIn(proof.candidate_binding, rendered)

    def test_binding_mismatch_error_contains_no_candidate_material(self):
        first_items = [regular_item(PRIVATE_MARKER, query="token=private-a")]
        second_items = [regular_item("second", query="token=private-b")]
        _, first_map = build_maps(first_items)
        _, second_map = build_maps(second_items)
        first_candidate = private_https_candidate(
            first_items,
            first_map.items[0].still_locator,
        )
        second_locator = second_map.items[0].still_locator
        second_candidate = private_https_candidate(second_items, second_locator)
        first_format_proof = private_format_proof(first_candidate)

        error = self.assert_mapper_error(
            "XHS_LIVEPHOTO_ASSET_SPEC_FORMAT_CANDIDATE_BINDING_MISMATCH",
            lambda: build_xhs_livephoto_member_proof(
                locator=second_locator,
                private_https_candidate=second_candidate,
                private_format_proof=first_format_proof,
            ),
        )
        rendered = repr(error) + str(error)
        self.assertNotIn(PRIVATE_MARKER, rendered)
        self.assertNotIn("private-a", rendered)
        self.assertNotIn(first_candidate.value, rendered)
        self.assertNotIn(first_format_proof.candidate_binding, rendered)


class XhsLivePhotoAssetSpecBoundaryTests(SimpleTestCase):
    def assert_mapper_error(self, code, callback):
        with self.assertRaises(XhsLivePhotoAssetSpecMappingError) as raised:
            callback()
        self.assertEqual(raised.exception.code, code)
        return raised.exception

    def test_cross_contract_asset_and_group_limits(self):
        thirty_two_livephotos = [
            livephoto_item(f"live-{index}") for index in range(32)
        ]
        at_limit = build_result(thirty_two_livephotos)
        self.assertEqual(len(at_limit.assets), 64)
        self.assertEqual(len(at_limit.groups), 32)

        thirty_three_livephotos = [
            livephoto_item(f"overflow-{index}") for index in range(33)
        ]
        relationships, candidates = build_maps(thirty_three_livephotos)
        self.assert_mapper_error(
            "XHS_LIVEPHOTO_ASSET_SPEC_GROUP_LIMIT",
            lambda: build_result(
                thirty_three_livephotos,
                relationships=relationships,
                candidates=candidates,
                proofs=(),
            ),
        )

        mixed_at_limit = [
            livephoto_item(f"mixed-{index}") for index in range(31)
        ] + [regular_item("regular-one"), regular_item("regular-two")]
        mixed_result = build_result(mixed_at_limit)
        self.assertEqual(len(mixed_result.assets), 64)
        self.assertEqual(len(mixed_result.groups), 31)

        mixed_over_limit = [
            livephoto_item(f"mixed-overflow-{index}") for index in range(31)
        ] + [
            regular_item("regular-one"),
            regular_item("regular-two"),
            regular_item("regular-three"),
        ]
        relationships, candidates = build_maps(mixed_over_limit)
        self.assert_mapper_error(
            "XHS_LIVEPHOTO_ASSET_SPEC_PHYSICAL_ASSET_LIMIT",
            lambda: build_result(
                mixed_over_limit,
                relationships=relationships,
                candidates=candidates,
                proofs=(),
            ),
        )

    def test_relationship_and_candidate_count_mismatch_is_rejected(self):
        two_items = [regular_item("one"), regular_item("two")]
        relationships, _ = build_maps(two_items)
        one_item = [regular_item("one")]
        _, candidates = build_maps(one_item)
        proofs = proofs_for_map(one_item, candidates)

        self.assert_mapper_error(
            "XHS_LIVEPHOTO_ASSET_SPEC_CANDIDATE_MAP_MISMATCH",
            lambda: build_result(
                two_items,
                relationships=relationships,
                candidates=candidates,
                proofs=proofs,
            ),
        )

    def test_image_list_snapshot_count_mismatch_is_rejected(self):
        items = [regular_item("one")]
        relationships, candidates = build_maps(items)
        proofs = proofs_for_map(items, candidates)

        self.assert_mapper_error(
            "XHS_LIVEPHOTO_ASSET_SPEC_IMAGE_LIST_SNAPSHOT_MISMATCH",
            lambda: build_result(
                items + [regular_item("two")],
                relationships=relationships,
                candidates=candidates,
                proofs=proofs,
            ),
        )

    def test_missing_duplicate_and_extra_proofs_are_rejected(self):
        items = [livephoto_item("one")]
        relationships, candidates = build_maps(items)
        proofs = proofs_for_map(items, candidates)

        cases = (
            (
                "XHS_LIVEPHOTO_ASSET_SPEC_MEMBER_PROOF_MISSING",
                proofs[:1],
            ),
            (
                "XHS_LIVEPHOTO_ASSET_SPEC_MEMBER_PROOF_DUPLICATE",
                proofs + (proofs[0],),
            ),
        )
        for code, candidate_proofs in cases:
            with self.subTest(code=code):
                self.assert_mapper_error(
                    code,
                    lambda candidate_proofs=candidate_proofs: build_result(
                        items,
                        relationships=relationships,
                        candidates=candidates,
                        proofs=candidate_proofs,
                    ),
                )

    def test_source_identity_mismatch_and_invalid_identity_fail_closed(self):
        items = [regular_item("one")]
        relationships, candidates = build_maps(items)
        proofs = proofs_for_map(items, candidates)

        self.assert_mapper_error(
            "XHS_LIVEPHOTO_ASSET_SPEC_SOURCE_IDENTITY_MISMATCH",
            lambda: build_xhs_livephoto_asset_specs(
                requested_note_id=NOTE_ID,
                payload_note_id=OTHER_NOTE_ID,
                relationship_map=relationships,
                candidate_map=candidates,
                image_list=items,
                member_proofs=proofs,
            ),
        )
        self.assert_mapper_error(
            "XHS_LIVEPHOTO_ASSET_SPEC_SOURCE_IDENTITY_INVALID",
            lambda: build_xhs_livephoto_asset_specs(
                requested_note_id="invalid",
                payload_note_id="invalid",
                relationship_map=relationships,
                candidate_map=candidates,
                image_list=items,
                member_proofs=proofs,
            ),
        )

    def test_rich_media_contract_compatibility_is_proven_but_not_wired(self):
        result = build_result(
            [livephoto_item("one"), regular_item("two")],
            webp_ordinals=frozenset({1}),
        )
        normalized = normalize_xhs_rich_media_payload(
            sanitized_bridge_payload(result)
        )

        self.assertEqual(normalized.rich_media.post_type, result.post_type)
        self.assertEqual(
            [asset.asset_id for asset in normalized.rich_media.assets],
            [asset.asset_spec.asset_id for asset in result.assets],
        )
        self.assertEqual(
            [asset.role for asset in normalized.rich_media.assets],
            [asset.asset_spec.role for asset in result.assets],
        )
        self.assertEqual(
            [group.group_id for group in normalized.rich_media.groups],
            [group.group_id for group in result.groups],
        )

    def test_schema_versions_are_pinned_to_upstream_contracts(self):
        result = build_result([regular_item("one")])

        self.assertEqual(result.schema_version, XHS_LIVEPHOTO_ASSET_SPEC_SCHEMA_VERSION)
        self.assertEqual(
            result.relationship_schema_version,
            XHS_LIVEPHOTO_RELATIONSHIP_SCHEMA_VERSION,
        )
        self.assertEqual(
            result.candidate_schema_version,
            XHS_LIVEPHOTO_CANDIDATE_SCHEMA_VERSION,
        )
        self.assertEqual(
            result.browser_admission_schema_version,
            XHS_LIVEPHOTO_BROWSER_ADMISSION_SCHEMA_VERSION,
        )

    def test_runtime_wiring_is_confined_to_the_xhs_builder_boundary(self):
        adapter = (
            PROJECT_ROOT
            / "xhs_download"
            / "services"
            / "platforms"
            / "xiaohongshu.py"
        ).read_text(encoding="utf-8")
        registry = (
            PROJECT_ROOT / "xhs_download" / "services" / "registry.py"
        ).read_text(encoding="utf-8")
        builder = (
            PROJECT_ROOT
            / "xhs_download"
            / "services"
            / "platforms"
            / "xhs_livephoto_builder.py"
        ).read_text(encoding="utf-8")

        self.assertIn("build_xhs_livephoto_result", adapter)
        self.assertIn("enable_xhs_livephoto_preview", adapter)
        self.assertIn("enable_xhs_livephoto_preview", registry)
        self.assertIn("build_xhs_livephoto_asset_specs", builder)
        self.assertIn("RichMediaContractV1", builder)

    def test_mapper_constructs_no_runtime_asset_manifest_or_provider_result(self):
        source = MAPPER_PATH.read_text(encoding="utf-8")
        for forbidden in (
            "MediaAsset(",
            "ProviderManifest",
            "ParseResult(",
            "RichMediaContractV1(",
            "normalize_xhs_rich_media_payload",
        ):
            self.assertNotIn(forbidden, source)

    def test_mapper_has_no_network_or_runtime_framework_imports(self):
        source = MAPPER_PATH.read_text(encoding="utf-8")
        for forbidden in (
            "import requests",
            "import httpx",
            "urllib.request",
            "urlopen(",
            "django.",
            "xhs_download.services.registry",
        ):
            self.assertNotIn(forbidden, source)

    def test_input_snapshot_is_not_mutated(self):
        items = [livephoto_item("one"), regular_item("two")]
        original = copy.deepcopy(items)

        build_result(items)

        self.assertEqual(items, original)


class XhsLivePhotoPrivateProofInvariantTests(SimpleTestCase):
    def test_direct_format_proof_construction_fails_closed(self):
        with self.assertRaises(XhsLivePhotoAssetSpecMappingError) as raised:
            XhsLivePhotoPrivateFormatProofV1(
                member_role="still",
                format_class="jpeg",
                extension="jpg",
                confidence="magic",
                candidate_binding="a" * 64,
            )
        self.assertEqual(
            raised.exception.code,
            "XHS_LIVEPHOTO_ASSET_SPEC_FORMAT_EVIDENCE_INVALID",
        )

    def test_format_proof_replace_cannot_reseal_private_binding(self):
        items = [regular_item("one")]
        _, candidates = build_maps(items)
        candidate = private_https_candidate(items, candidates.items[0].still_locator)
        proof = private_format_proof(candidate)

        with self.assertRaises(XhsLivePhotoAssetSpecMappingError) as raised:
            replace(proof, candidate_binding="f" * 64)
        self.assertEqual(
            raised.exception.code,
            "XHS_LIVEPHOTO_ASSET_SPEC_FORMAT_EVIDENCE_INVALID",
        )

    def test_direct_member_proof_construction_fails_closed(self):
        items = [regular_item("one")]
        _, candidates = build_maps(items)
        locator = candidates.items[0].still_locator

        with self.assertRaises(XhsLivePhotoAssetSpecMappingError) as raised:
            XhsLivePhotoPrivateMemberProofV1(
                locator=locator,
                member_role="still",
                host_class="still_exact",
                format_class="jpeg",
                extension="jpg",
                host_class_browser_verified=True,
                candidate_binding="a" * 64,
            )
        self.assertEqual(
            raised.exception.code,
            "XHS_LIVEPHOTO_ASSET_SPEC_MEMBER_PROOF_INVALID",
        )

    def test_member_proof_replace_cannot_reseal_assertions(self):
        items = [regular_item("one")]
        _, candidates = build_maps(items)
        proof = proofs_for_map(items, candidates)[0]

        for changes in (
            {"host_class_browser_verified": False},
            {"candidate_binding": "short"},
        ):
            with self.subTest(changes=tuple(changes)):
                with self.assertRaises(XhsLivePhotoAssetSpecMappingError) as raised:
                    replace(proof, **changes)
                self.assertEqual(
                    raised.exception.code,
                    "XHS_LIVEPHOTO_ASSET_SPEC_MEMBER_PROOF_INVALID",
                )
