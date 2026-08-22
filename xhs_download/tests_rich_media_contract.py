import json
from dataclasses import FrozenInstanceError, fields
from pathlib import Path

from django.test import SimpleTestCase

from tools.rich_media_contract.validate_contract import VALID, validate_document

from .services.assets import MediaAsset, ParseResult
from .services.rich_media import (
    RICH_MEDIA_MAX_ASSETS,
    RICH_MEDIA_MAX_CONTENT_BLOCKS,
    RICH_MEDIA_MAX_GROUPS,
    RICH_MEDIA_MAX_MEDIA_REFS_PER_BLOCK,
    RICH_MEDIA_MAX_TEXT_BLOCK_CHARS,
    RICH_MEDIA_MAX_TOTAL_TEXT_CHARS,
    RichMediaAssetDescriptorV1,
    RichMediaContractError,
    RichMediaContractV1,
    RichMediaGroupMemberV1,
    RichMediaGroupV1,
    RichMediaMediaBlockV1,
    RichMediaTextBlockV1,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
OFFLINE_FIXTURES = PROJECT_ROOT / "tools" / "rich_media_contract" / "fixtures"
OFFLINE_TOP_LEVEL_KEYS = {
    "schema_version",
    "case_id",
    "post_type",
    "assets",
    "groups",
    "content_blocks",
}


def load_offline_fixture(kind, name):
    return json.loads(
        (OFFLINE_FIXTURES / kind / f"{name}.json").read_text(encoding="utf-8")
    )


def _fixture_fields(value, expected, code, path):
    if type(value) is not dict or set(value) != expected:
        raise RichMediaContractError(code, path)
    return value


def runtime_contract_from_offline_fixture(document):
    document = _fixture_fields(
        document,
        OFFLINE_TOP_LEVEL_KEYS,
        "RICH_MEDIA_FIXTURE_FIELDS_INVALID",
        "document",
    )
    assets = []
    for index, raw_asset in enumerate(document["assets"]):
        asset = _fixture_fields(
            raw_asset,
            {"asset_id", "media_type", "extension", "role"},
            "RICH_MEDIA_FIXTURE_ASSET_FIELDS_INVALID",
            f"assets[{index}]",
        )
        assets.append(RichMediaAssetDescriptorV1(**asset))

    groups = []
    for index, raw_group in enumerate(document["groups"]):
        group = _fixture_fields(
            raw_group,
            {"group_id", "relationship_type", "members"},
            "RICH_MEDIA_FIXTURE_GROUP_FIELDS_INVALID",
            f"groups[{index}]",
        )
        members = []
        for member_index, raw_member in enumerate(group["members"]):
            member = _fixture_fields(
                raw_member,
                {"asset_id", "role"},
                "RICH_MEDIA_FIXTURE_MEMBER_FIELDS_INVALID",
                f"groups[{index}].members[{member_index}]",
            )
            members.append(RichMediaGroupMemberV1(**member))
        groups.append(
            RichMediaGroupV1(
                group_id=group["group_id"],
                relationship_type=group["relationship_type"],
                members=tuple(members),
            )
        )

    blocks = []
    for index, raw_block in enumerate(document["content_blocks"]):
        if type(raw_block) is not dict:
            raise RichMediaContractError(
                "RICH_MEDIA_FIXTURE_BLOCK_FIELDS_INVALID",
                f"content_blocks[{index}]",
            )
        if raw_block.get("type") == "text":
            block = _fixture_fields(
                raw_block,
                {"type", "text"},
                "RICH_MEDIA_FIXTURE_BLOCK_FIELDS_INVALID",
                f"content_blocks[{index}]",
            )
            blocks.append(RichMediaTextBlockV1(text=block["text"]))
        elif raw_block.get("type") == "media":
            block = _fixture_fields(
                raw_block,
                {"type", "asset_ids"},
                "RICH_MEDIA_FIXTURE_BLOCK_FIELDS_INVALID",
                f"content_blocks[{index}]",
            )
            blocks.append(RichMediaMediaBlockV1(asset_ids=tuple(block["asset_ids"])))
        else:
            raise RichMediaContractError(
                "RICH_MEDIA_FIXTURE_BLOCK_TYPE_INVALID",
                f"content_blocks[{index}]",
            )

    return RichMediaContractV1(
        schema_version=document["schema_version"],
        post_type=document["post_type"],
        assets=tuple(assets),
        groups=tuple(groups),
        content_blocks=tuple(blocks),
    )


def physical_assets_for_contract(contract, **overrides):
    assets = []
    for descriptor in contract.assets:
        fields = {
            "media_type": descriptor.media_type,
            "url": "",
            "platform": "fixture",
            "ext": descriptor.extension,
            "asset_id": descriptor.asset_id,
        }
        fields.update(overrides)
        assets.append(MediaAsset(**fields))
    return tuple(assets)


def parse_result_for_contract(contract, assets=None):
    return ParseResult(
        platform="fixture",
        post_type=contract.post_type,
        base_filename="fixture",
        assets=physical_assets_for_contract(contract) if assets is None else assets,
        metadata={},
        rich_media=contract,
    )


def descriptor(asset_id, media_type, extension, role):
    return RichMediaAssetDescriptorV1(
        asset_id=asset_id,
        media_type=media_type,
        extension=extension,
        role=role,
    )


def live_photo_contract():
    still = descriptor("asset:live:still", "image", "jpg", "still")
    motion = descriptor("asset:live:motion", "video", "mp4", "motion")
    return RichMediaContractV1(
        schema_version="1.0",
        post_type="live_photo",
        assets=(still, motion),
        groups=(
            RichMediaGroupV1(
                group_id="group:live:one",
                relationship_type="live_photo",
                members=(
                    RichMediaGroupMemberV1(asset_id=still.asset_id, role="still"),
                    RichMediaGroupMemberV1(asset_id=motion.asset_id, role="motion"),
                ),
            ),
        ),
        content_blocks=(),
    )


class RichMediaOfflineRuntimeParityTests(SimpleTestCase):
    VALID_CASES = (
        "legacy_single_image",
        "legacy_single_video",
        "gallery",
        "animation_image",
        "animation_video",
        "live_photo",
        "multimedia",
        "rich_text_text_only",
        "rich_text_media",
        "rich_text_live_photo",
    )
    INVALID_CASES = (
        "animation_bad_extension",
        "asset_in_two_groups",
        "duplicate_asset_id",
        "duplicate_group_id",
        "duplicate_group_member",
        "gallery_mixed_media",
        "live_photo_missing_motion",
        "live_photo_two_images",
        "missing_asset_ref",
        "raw_html",
        "rich_text_orphan_asset",
        "token_key",
        "unknown_post_type",
        "unknown_relationship_type",
        "unknown_top_field",
        "url_field_injection",
    )

    def test_all_valid_offline_fixtures_construct_runtime_results(self):
        for name in self.VALID_CASES:
            with self.subTest(name=name):
                document = load_offline_fixture("valid", name)
                self.assertEqual(validate_document(document).status, VALID)
                contract = runtime_contract_from_offline_fixture(document)
                result = parse_result_for_contract(contract)
                self.assertIs(result.rich_media, contract)

    def test_all_invalid_offline_fixtures_fail_runtime_construction(self):
        for name in self.INVALID_CASES:
            with self.subTest(name=name):
                document = load_offline_fixture("invalid", name)
                self.assertNotEqual(validate_document(document).status, VALID)
                with self.assertRaises(RichMediaContractError):
                    runtime_contract_from_offline_fixture(document)

    def test_structural_output_is_url_free_and_omits_offline_case_id(self):
        contract = runtime_contract_from_offline_fixture(
            load_offline_fixture("valid", "rich_text_live_photo")
        )
        rendered = json.dumps(contract.to_structural_dict(), ensure_ascii=False)
        self.assertNotIn("case_id", contract.to_structural_dict())
        self.assertNotIn("://", rendered)
        for forbidden in ("token", "cookie", "header", "manifest", "url"):
            self.assertNotIn(forbidden, rendered.lower())


class RichMediaValidRuntimeTests(SimpleTestCase):
    def test_single_image_and_video_contracts_construct(self):
        for post_type, media_type, extension in (
            ("image", "image", "jpg"),
            ("video", "video", "mp4"),
        ):
            with self.subTest(post_type=post_type):
                asset = descriptor(f"asset:{post_type}:one", media_type, extension, "primary")
                contract = RichMediaContractV1("1.0", post_type, (asset,), (), ())
                self.assertEqual(
                    parse_result_for_contract(contract).assets[0].asset_id,
                    asset.asset_id,
                )

    def test_gallery_preserves_descriptor_and_parse_result_order(self):
        assets = tuple(
            descriptor(f"asset:gallery:{index}", "image", "jpg", "gallery")
            for index in range(1, 4)
        )
        contract = RichMediaContractV1("1.0", "gallery", assets, (), ())
        result = parse_result_for_contract(contract)
        self.assertEqual(
            [asset.asset_id for asset in result.assets],
            [asset.asset_id for asset in contract.assets],
        )

    def test_image_and_video_animation_roles_construct(self):
        cases = (
            descriptor("asset:animation:image", "image", "webp", "animation"),
            descriptor("asset:animation:video", "video", "mp4", "animation"),
        )
        for asset in cases:
            with self.subTest(media_type=asset.media_type):
                post_type = asset.media_type
                contract = RichMediaContractV1("1.0", post_type, (asset,), (), ())
                self.assertEqual(parse_result_for_contract(contract).rich_media.assets[0], asset)

    def test_one_live_photo_constructs(self):
        contract = live_photo_contract()
        result = parse_result_for_contract(contract)
        self.assertEqual(len(result.rich_media.groups), 1)
        self.assertEqual(
            [member.role for member in result.rich_media.groups[0].members],
            ["still", "motion"],
        )

    def test_multiple_live_photo_groups_construct(self):
        assets = []
        groups = []
        for index in (1, 2):
            still = descriptor(f"asset:multi:still-{index}", "image", "jpg", "still")
            motion = descriptor(f"asset:multi:motion-{index}", "video", "mp4", "motion")
            assets.extend((still, motion))
            groups.append(
                RichMediaGroupV1(
                    group_id=f"group:multi:pair-{index}",
                    relationship_type="live_photo",
                    members=(
                        RichMediaGroupMemberV1(still.asset_id, "still"),
                        RichMediaGroupMemberV1(motion.asset_id, "motion"),
                    ),
                )
            )
        contract = RichMediaContractV1(
            "1.0", "live_photo", tuple(assets), tuple(groups), ()
        )
        self.assertEqual(len(parse_result_for_contract(contract).rich_media.groups), 2)

    def test_multimedia_preserves_mixed_order(self):
        assets = (
            descriptor("asset:mixed:image-1", "image", "jpg", "inline"),
            descriptor("asset:mixed:video-1", "video", "mp4", "inline"),
            descriptor("asset:mixed:image-2", "image", "png", "inline"),
        )
        contract = RichMediaContractV1("1.0", "multimedia", assets, (), ())
        result = parse_result_for_contract(contract)
        self.assertEqual(
            [asset.media_type for asset in result.assets],
            ["image", "video", "image"],
        )

    def test_rich_text_text_only_media_and_live_photo_construct(self):
        for name in (
            "rich_text_text_only",
            "rich_text_media",
            "rich_text_live_photo",
        ):
            with self.subTest(name=name):
                contract = runtime_contract_from_offline_fixture(
                    load_offline_fixture("valid", name)
                )
                self.assertEqual(parse_result_for_contract(contract).post_type, "rich_text")

    def test_runtime_types_are_frozen(self):
        contract = live_photo_contract()
        with self.assertRaises(FrozenInstanceError):
            contract.post_type = "video"


class RichMediaInvalidRuntimeTests(SimpleTestCase):
    def assert_contract_error(self, code, callback):
        with self.assertRaises(RichMediaContractError) as raised:
            callback()
        self.assertEqual(raised.exception.code, code)
        self.assertNotIn("://", str(raised.exception))

    def test_parse_result_rejects_non_typed_contract(self):
        asset = MediaAsset("image", "", "fixture")
        self.assert_contract_error(
            "RICH_MEDIA_CONTRACT_TYPE_INVALID",
            lambda: ParseResult("fixture", "image", "fixture", (asset,), {}, {}),
        )

    def test_post_type_mismatch_is_rejected(self):
        contract = live_photo_contract()
        self.assert_contract_error(
            "RICH_MEDIA_POST_TYPE_MISMATCH",
            lambda: ParseResult(
                "fixture",
                "multimedia",
                "fixture",
                physical_assets_for_contract(contract),
                {},
                contract,
            ),
        )

    def test_asset_count_mismatch_is_rejected(self):
        contract = live_photo_contract()
        self.assert_contract_error(
            "RICH_MEDIA_ASSET_COUNT_MISMATCH",
            lambda: parse_result_for_contract(
                contract,
                assets=physical_assets_for_contract(contract)[:1],
            ),
        )

    def test_asset_order_and_id_mismatch_are_rejected(self):
        contract = live_photo_contract()
        reversed_assets = tuple(reversed(physical_assets_for_contract(contract)))
        self.assert_contract_error(
            "RICH_MEDIA_ASSET_ID_MISMATCH",
            lambda: parse_result_for_contract(contract, assets=reversed_assets),
        )

    def test_duplicate_and_empty_descriptor_ids_are_rejected(self):
        duplicate = descriptor("asset:duplicate:one", "image", "jpg", "gallery")
        self.assert_contract_error(
            "RICH_MEDIA_ASSET_ID_DUPLICATE",
            lambda: RichMediaContractV1(
                "1.0", "gallery", (duplicate, duplicate), (), ()
            ),
        )
        empty = descriptor("", "image", "jpg", "primary")
        self.assert_contract_error(
            "RICH_MEDIA_ASSET_ID_INVALID",
            lambda: RichMediaContractV1("1.0", "image", (empty,), (), ()),
        )

    def test_missing_actual_asset_id_is_rejected_only_for_rich_media(self):
        contract = RichMediaContractV1(
            "1.0",
            "image",
            (descriptor("asset:identity:one", "image", "jpg", "primary"),),
            (),
            (),
        )
        actual = (MediaAsset("image", "", "fixture", ext="jpg"),)
        self.assert_contract_error(
            "RICH_MEDIA_RESULT_ASSET_ID_INVALID",
            lambda: parse_result_for_contract(contract, assets=actual),
        )

    def test_media_type_and_extension_mismatches_are_rejected(self):
        contract = RichMediaContractV1(
            "1.0",
            "image",
            (descriptor("asset:mismatch:one", "image", "jpg", "primary"),),
            (),
            (),
        )
        wrong_type = (
            MediaAsset("video", "", "fixture", ext="jpg", asset_id="asset:mismatch:one"),
        )
        wrong_extension = (
            MediaAsset("image", "", "fixture", ext="png", asset_id="asset:mismatch:one"),
        )
        self.assert_contract_error(
            "RICH_MEDIA_MEDIA_TYPE_MISMATCH",
            lambda: parse_result_for_contract(contract, assets=wrong_type),
        )
        self.assert_contract_error(
            "RICH_MEDIA_EXTENSION_MISMATCH",
            lambda: parse_result_for_contract(contract, assets=wrong_extension),
        )

    def test_normalized_media_asset_extension_is_accepted(self):
        contract = RichMediaContractV1(
            "1.0",
            "image",
            (descriptor("asset:normalized:one", "image", "jpg", "primary"),),
            (),
            (),
        )
        actual = (
            MediaAsset(
                "image",
                "",
                "fixture",
                ext=".JPG",
                asset_id="asset:normalized:one",
            ),
        )
        self.assertIs(parse_result_for_contract(contract, assets=actual).rich_media, contract)

    def test_missing_or_invalid_live_photo_pair_is_rejected(self):
        still = descriptor("asset:pair:still", "image", "jpg", "still")
        second_still = descriptor("asset:pair:still-2", "image", "jpg", "still")
        missing = RichMediaGroupV1(
            "group:pair:missing",
            "live_photo",
            (RichMediaGroupMemberV1(still.asset_id, "still"),),
        )
        self.assert_contract_error(
            "RICH_MEDIA_LIVE_PHOTO_MEMBER_COUNT",
            lambda: RichMediaContractV1("1.0", "live_photo", (still,), (missing,), ()),
        )
        two_stills = RichMediaGroupV1(
            "group:pair:two-stills",
            "live_photo",
            (
                RichMediaGroupMemberV1(still.asset_id, "still"),
                RichMediaGroupMemberV1(second_still.asset_id, "still"),
            ),
        )
        self.assert_contract_error(
            "RICH_MEDIA_LIVE_PHOTO_PAIR_INCOMPLETE",
            lambda: RichMediaContractV1(
                "1.0", "live_photo", (still, second_still), (two_stills,), ()
            ),
        )

    def test_asset_cannot_appear_in_two_groups(self):
        contract = live_photo_contract()
        repeated = RichMediaGroupV1(
            "group:live:two",
            "live_photo",
            contract.groups[0].members,
        )
        self.assert_contract_error(
            "RICH_MEDIA_ASSET_IN_MULTIPLE_GROUPS",
            lambda: RichMediaContractV1(
                "1.0",
                "live_photo",
                contract.assets,
                (contract.groups[0], repeated),
                (),
            ),
        )

    def test_invalid_animation_extension_is_rejected(self):
        invalid = descriptor("asset:animation:bad", "image", "jpg", "animation")
        self.assert_contract_error(
            "RICH_MEDIA_ANIMATION_EXTENSION_INVALID",
            lambda: RichMediaContractV1("1.0", "image", (invalid,), (), ()),
        )

    def test_unknown_post_relationship_and_block_types_are_rejected(self):
        asset = descriptor("asset:unknown:one", "image", "jpg", "primary")
        self.assert_contract_error(
            "RICH_MEDIA_SCHEMA_VERSION_INVALID",
            lambda: RichMediaContractV1("2.0", "image", (asset,), (), ()),
        )
        self.assert_contract_error(
            "RICH_MEDIA_POST_TYPE_INVALID",
            lambda: RichMediaContractV1("1.0", "unknown", (asset,), (), ()),
        )
        group = RichMediaGroupV1("group:unknown:one", "unknown", ())
        self.assert_contract_error(
            "RICH_MEDIA_RELATIONSHIP_TYPE_INVALID",
            lambda: RichMediaContractV1("1.0", "multimedia", (asset,), (group,), ()),
        )
        self.assert_contract_error(
            "RICH_MEDIA_BLOCK_TYPE_INVALID",
            lambda: RichMediaContractV1("1.0", "rich_text", (), (), (object(),)),
        )

    def test_rich_text_rejects_html_markdown_url_and_credentials(self):
        unsafe = (
            ("RICH_MEDIA_TEXT_HTML_REJECTED", "<script>bad</script>"),
            ("RICH_MEDIA_TEXT_MARKDOWN_REJECTED", "[label](target)"),
            ("RICH_MEDIA_TEXT_URL_REJECTED", "https" + "://fictional.invalid/item"),
            ("RICH_MEDIA_TEXT_CREDENTIAL_REJECTED", "token" + "=redacted"),
        )
        for code, text in unsafe:
            with self.subTest(code=code):
                self.assert_contract_error(
                    code,
                    lambda text=text: RichMediaContractV1(
                        "1.0",
                        "rich_text",
                        (),
                        (),
                        (RichMediaTextBlockV1(text),),
                    ),
                )

    def test_missing_duplicate_and_orphan_media_refs_are_rejected(self):
        asset = descriptor("asset:block:one", "image", "jpg", "inline")
        self.assert_contract_error(
            "RICH_MEDIA_MEDIA_REF_NOT_FOUND",
            lambda: RichMediaContractV1(
                "1.0",
                "rich_text",
                (asset,),
                (),
                (RichMediaMediaBlockV1(("asset:block:missing",)),),
            ),
        )
        self.assert_contract_error(
            "RICH_MEDIA_MEDIA_REF_DUPLICATE",
            lambda: RichMediaContractV1(
                "1.0",
                "rich_text",
                (asset,),
                (),
                (RichMediaMediaBlockV1((asset.asset_id, asset.asset_id)),),
            ),
        )
        self.assert_contract_error(
            "RICH_MEDIA_RICH_TEXT_ASSET_ORPHAN",
            lambda: RichMediaContractV1(
                "1.0",
                "rich_text",
                (asset,),
                (),
                (RichMediaTextBlockV1("Plain text."),),
            ),
        )

    def test_live_photo_pair_cannot_be_split_across_blocks(self):
        contract = live_photo_contract()
        self.assert_contract_error(
            "RICH_MEDIA_LIVE_PHOTO_BLOCK_PAIR_SPLIT",
            lambda: RichMediaContractV1(
                "1.0",
                "rich_text",
                contract.assets,
                contract.groups,
                tuple(
                    RichMediaMediaBlockV1((asset.asset_id,))
                    for asset in contract.assets
                ),
            ),
        )

    def test_realistic_cover_metadata_does_not_enter_structural_contract(self):
        content = descriptor("asset:content:one", "image", "jpg", "primary")
        contract = RichMediaContractV1(
            "1.0",
            "image",
            (content,),
            (),
            (),
        )
        cover_location = "https" + "://fictional.invalid/preview/cover.jpg"
        result = parse_result_for_contract(
            contract,
            assets=(
                MediaAsset(
                    "image",
                    "content-location",
                    "fixture",
                    ext="jpg",
                    asset_id=content.asset_id,
                    cover_url=cover_location,
                    cover_preview_url="cover-preview-location",
                ),
            ),
        )
        structural = result.rich_media.to_structural_dict()
        rendered = json.dumps(structural, ensure_ascii=False).lower()
        self.assertEqual(result.assets[0].cover_url, cover_location)
        for forbidden in ("cover", "preview", "location", "url"):
            self.assertNotIn(forbidden, rendered)

    def test_cover_only_identity_cannot_be_referenced_implicitly(self):
        content = descriptor("asset:content:one", "image", "jpg", "inline")
        canonical_assets = (
            MediaAsset(
                "image",
                "content-location",
                "fixture",
                ext="jpg",
                asset_id=content.asset_id,
                cover_url="cover-only-location",
            ),
        )

        def build_result():
            contract = RichMediaContractV1(
                "1.0",
                "rich_text",
                (content,),
                (),
                (
                    RichMediaMediaBlockV1(
                        (content.asset_id, "asset:cover:only")
                    ),
                ),
            )
            return parse_result_for_contract(contract, assets=canonical_assets)

        self.assert_contract_error(
            "RICH_MEDIA_MEDIA_REF_NOT_FOUND",
            build_result,
        )

    def test_runtime_contract_types_have_no_cover_fields(self):
        runtime_types = (
            RichMediaAssetDescriptorV1,
            RichMediaGroupMemberV1,
            RichMediaGroupV1,
            RichMediaTextBlockV1,
            RichMediaMediaBlockV1,
            RichMediaContractV1,
        )
        forbidden = {"cover", "cover_url", "cover_preview_url", "cover_asset_id"}
        for runtime_type in runtime_types:
            with self.subTest(runtime_type=runtime_type.__name__):
                field_names = {field.name for field in fields(runtime_type)}
                self.assertTrue(field_names.isdisjoint(forbidden))

    def test_real_content_asset_may_reuse_its_cover_location(self):
        content = descriptor("asset:content:one", "image", "jpg", "primary")
        contract = RichMediaContractV1("1.0", "image", (content,), (), ())
        shared_location = "shared-content-and-cover-location"
        result = parse_result_for_contract(
            contract,
            assets=(
                MediaAsset(
                    "image",
                    shared_location,
                    "fixture",
                    ext="jpg",
                    asset_id=content.asset_id,
                    cover_url=shared_location,
                ),
            ),
        )
        self.assertIs(result.rich_media, contract)
        self.assertEqual(result.assets[0].url, result.assets[0].cover_url)

    def test_collection_and_text_limits_are_enforced(self):
        too_many_assets = tuple(
            descriptor(f"asset:limit:{index}", "video", "mp4", "primary")
            for index in range(RICH_MEDIA_MAX_ASSETS + 1)
        )
        self.assert_contract_error(
            "RICH_MEDIA_ASSET_COLLECTION_INVALID",
            lambda: RichMediaContractV1("1.0", "video", too_many_assets, (), ()),
        )
        too_many_groups = tuple(
            RichMediaGroupV1(
                f"group:limit:{index}",
                "live_photo",
                (),
            )
            for index in range(RICH_MEDIA_MAX_GROUPS + 1)
        )
        self.assert_contract_error(
            "RICH_MEDIA_GROUP_COLLECTION_INVALID",
            lambda: RichMediaContractV1(
                "1.0",
                "rich_text",
                (),
                too_many_groups,
                (RichMediaTextBlockV1("Plain text."),),
            ),
        )
        too_many_blocks = tuple(
            RichMediaTextBlockV1("x")
            for _ in range(RICH_MEDIA_MAX_CONTENT_BLOCKS + 1)
        )
        self.assert_contract_error(
            "RICH_MEDIA_BLOCK_COLLECTION_INVALID",
            lambda: RichMediaContractV1("1.0", "rich_text", (), (), too_many_blocks),
        )
        self.assert_contract_error(
            "RICH_MEDIA_TEXT_LENGTH_INVALID",
            lambda: RichMediaContractV1(
                "1.0",
                "rich_text",
                (),
                (),
                (RichMediaTextBlockV1("x" * (RICH_MEDIA_MAX_TEXT_BLOCK_CHARS + 1)),),
            ),
        )
        total_limit_blocks = tuple(
            RichMediaTextBlockV1("x" * RICH_MEDIA_MAX_TEXT_BLOCK_CHARS)
            for _ in range((RICH_MEDIA_MAX_TOTAL_TEXT_CHARS // RICH_MEDIA_MAX_TEXT_BLOCK_CHARS) + 1)
        )
        self.assert_contract_error(
            "RICH_MEDIA_TOTAL_TEXT_LIMIT",
            lambda: RichMediaContractV1(
                "1.0", "rich_text", (), (), total_limit_blocks
            ),
        )
        referenced = descriptor("asset:refs:one", "image", "jpg", "inline")
        too_many_refs = tuple(
            f"asset:refs:{index}" for index in range(RICH_MEDIA_MAX_MEDIA_REFS_PER_BLOCK + 1)
        )
        self.assert_contract_error(
            "RICH_MEDIA_MEDIA_REFS_INVALID",
            lambda: RichMediaContractV1(
                "1.0",
                "rich_text",
                (referenced,),
                (),
                (RichMediaMediaBlockV1(too_many_refs),),
            ),
        )

    def test_error_contract_does_not_echo_rejected_values(self):
        rejected = (
            "https"
            + "://fictional.invalid/private?"
            + "token"
            + "=redacted"
        )
        with self.assertRaises(RichMediaContractError) as raised:
            RichMediaContractV1(
                "1.0",
                "rich_text",
                (),
                (),
                (RichMediaTextBlockV1(rejected),),
            )
        rendered = str(raised.exception)
        self.assertNotIn(rejected, rendered)
        self.assertNotIn("fictional", rendered)
        self.assertNotIn("Traceback", rendered)
