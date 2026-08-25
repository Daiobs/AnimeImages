import copy
import json
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

from django.test import SimpleTestCase

from .services.image_formats import ImageFormatResolution, resolve_image_format
from .services.platforms.xhs_livephoto_asset_spec_mapper import (
    XHS_LIVEPHOTO_ASSET_ID_SCOPE,
    XHS_LIVEPHOTO_ASSET_SCOPE_HEX_LENGTH,
    XHS_LIVEPHOTO_ASSET_SPEC_SCHEMA_VERSION,
    XhsLivePhotoAssetSpecMapV1,
    XhsLivePhotoAssetSpecMappingError,
    XhsLivePhotoPrivateMemberProofV1,
    build_xhs_livephoto_asset_specs,
    build_xhs_livephoto_member_proof,
)
from .services.platforms.xhs_livephoto_browser_admission import (
    XHS_LIVEPHOTO_BROWSER_ADMISSION_SCHEMA_VERSION,
    XhsLivePhotoBrowserAdmissionEvidenceV1,
    XhsPrivateHttpsMediaCandidateV1,
    normalize_xhs_livephoto_candidate_https,
)
from .services.platforms.xhs_livephoto_candidate_locator import (
    XHS_LIVEPHOTO_CANDIDATE_SCHEMA_VERSION,
    XhsLivePhotoMemberLocatorV1,
    locate_xhs_livephoto_candidates,
    read_candidate_from_locator,
    resolve_xhs_motion_format_for_research,
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


def format_evidence(role, *, still_format="jpeg"):
    if role == "motion":
        return resolve_xhs_motion_format_for_research(
            MP4,
            content_type="application/octet-stream",
        )
    prefix = JPEG if still_format == "jpeg" else WEBP
    return resolve_image_format(
        prefix,
        content_type="application/octet-stream",
    )


def browser_evidence(candidate, *, format_class):
    role = candidate.member_role
    return XhsLivePhotoBrowserAdmissionEvidenceV1(
        member_role=role,
        host_class=candidate.host_class,
        format_class=format_class,
        response_type="cors",
        body_readable=True,
        final_host_class=candidate.host_class,
        range_result="partial_206" if role == "motion" else "ignored_bounded_200",
        redirect_class="none",
        upgrade_applied=candidate.upgrade_applied,
    )


def proof_for_locator(items, locator, *, still_format="jpeg"):
    candidate = private_https_candidate(items, locator)
    evidence = format_evidence(locator.member_role, still_format=still_format)
    evidence_format = (
        evidence.format if locator.member_role == "still" else evidence.format_class
    )
    return build_xhs_livephoto_member_proof(
        locator=locator,
        private_https_candidate=candidate,
        browser_admission_evidence=browser_evidence(
            candidate,
            format_class=evidence_format,
        ),
        format_evidence=evidence,
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


class XhsLivePhotoMemberProofTests(SimpleTestCase):
    def assert_mapper_error(self, code, callback):
        with self.assertRaises(XhsLivePhotoAssetSpecMappingError) as raised:
            callback()
        self.assertEqual(raised.exception.code, code)
        self.assertEqual(str(raised.exception), code)
        return raised.exception

    def test_candidate_binding_match_passes(self):
        items = [livephoto_item("one")]
        result = build_result(items)

        self.assertEqual(len(result.assets), 2)

    def test_candidate_proof_from_another_item_fails(self):
        items = [regular_item("one"), regular_item("two")]
        relationships, candidates = build_maps(items)
        proofs = proofs_for_map(items, candidates)
        forged = replace(proofs[1], locator=proofs[0].locator)

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
        master_proofs = proofs_for_map(master_items, master_candidates)
        backup_items = [
            livephoto_item(
                "one",
                motion_host="sns-bak-v9.xhscdn.com",
                use_backup=True,
            )
        ]
        relationships, backup_candidates = build_maps(backup_items)
        backup_proofs = proofs_for_map(backup_items, backup_candidates)
        forged_motion = replace(
            master_proofs[1],
            locator=backup_candidates.items[0].motion_locator,
            host_class="motion_backup",
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

    def test_host_class_mismatch_fails(self):
        items = [livephoto_item("one")]
        _, candidates = build_maps(items)
        locator = candidates.items[0].motion_locator
        candidate = private_https_candidate(items, locator)
        mismatched_evidence = XhsLivePhotoBrowserAdmissionEvidenceV1(
            member_role="motion",
            host_class="motion_backup",
            format_class="mp4",
            response_type="cors",
            body_readable=True,
            final_host_class="motion_backup",
            range_result="partial_206",
            redirect_class="none",
            upgrade_applied=False,
        )

        self.assert_mapper_error(
            "XHS_LIVEPHOTO_ASSET_SPEC_HOST_UNVERIFIED",
            lambda: build_xhs_livephoto_member_proof(
                locator=locator,
                private_https_candidate=candidate,
                browser_admission_evidence=mismatched_evidence,
                format_evidence=format_evidence("motion"),
            ),
        )

    def test_format_evidence_mismatch_fails(self):
        items = [regular_item("one")]
        _, candidates = build_maps(items)
        locator = candidates.items[0].still_locator
        candidate = private_https_candidate(items, locator)

        self.assert_mapper_error(
            "XHS_LIVEPHOTO_ASSET_SPEC_FORMAT_MISMATCH",
            lambda: build_xhs_livephoto_member_proof(
                locator=locator,
                private_https_candidate=candidate,
                browser_admission_evidence=browser_evidence(
                    candidate,
                    format_class="jpeg",
                ),
                format_evidence=format_evidence("still", still_format="webp"),
            ),
        )

    def test_mime_only_format_evidence_is_rejected(self):
        items = [regular_item("one")]
        _, candidates = build_maps(items)
        locator = candidates.items[0].still_locator
        candidate = private_https_candidate(items, locator)
        mime_only = resolve_image_format(b"", content_type="image/jpeg")

        self.assert_mapper_error(
            "XHS_LIVEPHOTO_ASSET_SPEC_FORMAT_EVIDENCE_INVALID",
            lambda: build_xhs_livephoto_member_proof(
                locator=locator,
                private_https_candidate=candidate,
                browser_admission_evidence=browser_evidence(
                    candidate,
                    format_class="jpeg",
                ),
                format_evidence=mime_only,
            ),
        )

    def test_url_suffix_only_format_evidence_is_rejected(self):
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
                browser_admission_evidence=browser_evidence(
                    candidate,
                    format_class="jpeg",
                ),
                format_evidence=suffix_only,
            ),
        )

    def test_unknown_format_is_rejected(self):
        items = [regular_item("one")]
        _, candidates = build_maps(items)
        locator = candidates.items[0].still_locator
        candidate = private_https_candidate(items, locator)

        self.assert_mapper_error(
            "XHS_LIVEPHOTO_ASSET_SPEC_FORMAT_EVIDENCE_INVALID",
            lambda: build_xhs_livephoto_member_proof(
                locator=locator,
                private_https_candidate=candidate,
                browser_admission_evidence=browser_evidence(
                    candidate,
                    format_class="jpeg",
                ),
                format_evidence=ImageFormatResolution(),
            ),
        )

    def test_motion_zl_and_unknown_hosts_are_rejected(self):
        items = [livephoto_item("one")]
        _, candidates = build_maps(items)
        locator = candidates.items[0].motion_locator

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
                self.assert_mapper_error(
                    "XHS_LIVEPHOTO_ASSET_SPEC_HOST_UNVERIFIED",
                    lambda forged=forged: build_xhs_livephoto_member_proof(
                        locator=locator,
                        private_https_candidate=forged,
                        browser_admission_evidence=object(),
                        format_evidence=format_evidence("motion"),
                    ),
                )

    def test_private_binding_is_excluded_from_repr_equality_hash_and_output(self):
        items = [regular_item("one")]
        _, candidates = build_maps(items)
        proof = proofs_for_map(items, candidates)[0]
        alternate = replace(proof, candidate_binding="f" * 64)
        rendered = repr(proof) + json.dumps(proof.to_structural_dict())

        self.assertEqual(proof, alternate)
        self.assertEqual(hash(proof), hash(alternate))
        self.assertNotIn(proof.candidate_binding, rendered)
        self.assertNotIn("candidate_binding", rendered)

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


class XhsLivePhotoAssetSpecBoundaryTests(SimpleTestCase):
    def assert_mapper_error(self, code, callback):
        with self.assertRaises(XhsLivePhotoAssetSpecMappingError) as raised:
            callback()
        self.assertEqual(raised.exception.code, code)
        return raised.exception

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

    def test_current_adapter_registry_and_provider_emission_remain_disconnected(self):
        for relative_path in (
            "xhs_download/services/platforms/xiaohongshu.py",
            "xhs_download/services/registry.py",
            "xhs_download/services/parser.py",
            "xhs_download/views.py",
        ):
            source = (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")
            self.assertNotIn("xhs_livephoto_asset_spec_mapper", source)
            self.assertNotIn("build_xhs_livephoto_asset_specs", source)
        adapter = (
            PROJECT_ROOT
            / "xhs_download"
            / "services"
            / "platforms"
            / "xiaohongshu.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("rich_media=", adapter)

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
    def test_direct_motion_zl_proof_construction_fails_closed(self):
        locator = XhsLivePhotoMemberLocatorV1(
            source_item_ordinal=0,
            member_role="motion",
            source_kind="master_url",
            codec_family_ordinal=0,
            codec_family_class="avc",
            stream_variant_index=0,
        )
        with self.assertRaises(XhsLivePhotoAssetSpecMappingError) as raised:
            XhsLivePhotoPrivateMemberProofV1(
                locator=locator,
                member_role="motion",
                host_class="motion_zl",
                format_class="mp4",
                extension="mp4",
                browser_verified=True,
                candidate_binding="a" * 64,
            )
        self.assertEqual(
            raised.exception.code,
            "XHS_LIVEPHOTO_ASSET_SPEC_HOST_UNVERIFIED",
        )

    def test_browser_verified_flag_and_binding_shape_are_mandatory(self):
        items = [regular_item("one")]
        _, candidates = build_maps(items)
        proof = proofs_for_map(items, candidates)[0]

        for changes in (
            {"browser_verified": False},
            {"candidate_binding": "short"},
        ):
            with self.subTest(changes=tuple(changes)):
                with self.assertRaises(XhsLivePhotoAssetSpecMappingError):
                    replace(proof, **changes)
