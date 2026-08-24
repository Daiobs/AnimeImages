import copy
import json
from dataclasses import fields
from pathlib import Path

from django.test import SimpleTestCase

from .services.assets import MediaAsset
from .services.platforms.bilibili_dynamic_normalizer import (
    BILIBILI_DYNAMIC_MAX_ITEMS,
    BilibiliDynamicAssetSpec,
    BilibiliDynamicNormalizationError,
    classify_bilibili_source_shape,
    normalize_bilibili_dynamic_payload,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = (
    PROJECT_ROOT
    / "xhs_download"
    / "test_fixtures"
    / "bilibili_dynamic_cases.json"
)
HOST_MATRIX_PATH = (
    PROJECT_ROOT
    / "docs"
    / "research"
    / "parsehub"
    / "bilibili_dynamic_host_matrix.json"
)


def load_document():
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


class BilibiliDormantSourceShapeTests(SimpleTestCase):
    def test_fictional_source_matrix_classifies_expected_shapes(self):
        for case in load_document()["source_cases"]:
            with self.subTest(case=case["case_id"]):
                result = classify_bilibili_source_shape(case["input"])
                self.assertEqual(result.target_kind, case["target_kind"])
                self.assertEqual(
                    result.identity.canonical if result.identity else None,
                    case["identity"],
                )
                self.assertEqual(
                    result.requires_resolution,
                    case["requires_resolution"],
                )

    def test_direct_dynamic_and_opus_keep_distinct_input_kinds(self):
        dynamic = classify_bilibili_source_shape(
            "https://t.bilibili.com/123456789012345678"
        )
        opus = classify_bilibili_source_shape(
            "https://www.bilibili.com/opus/123456789012345678"
        )
        self.assertEqual(dynamic.input_kind, "dynamic_page")
        self.assertEqual(opus.input_kind, "opus_page")

    def test_share_text_is_distinct_from_direct_opus_input(self):
        result = classify_bilibili_source_shape(
            "离线示例 https://www.bilibili.com/opus/123456789012345678，后续文字"
        )
        self.assertEqual(result.input_kind, "share_text")
        self.assertEqual(result.target_kind, "opus")

    def test_short_link_has_no_guessed_identity(self):
        result = classify_bilibili_source_shape("https://b23.tv/fixture-only")
        self.assertEqual(result.input_kind, "short_url")
        self.assertEqual(result.target_kind, "short")
        self.assertIsNone(result.identity)
        self.assertTrue(result.requires_resolution)

    def test_generic_numeric_page_remains_unverified_without_identity(self):
        for source in (
            "https://www.bilibili.com/223456789012345678",
            "https://space.bilibili.com/223456789012345678",
        ):
            with self.subTest(source=source):
                result = classify_bilibili_source_shape(source)
                self.assertEqual(result.input_kind, "url")
                self.assertEqual(result.target_kind, "numeric_unverified")
                self.assertIsNone(result.identity)
                self.assertTrue(result.requires_resolution)

    def test_unknown_bilibili_page_is_not_classified_as_video(self):
        result = classify_bilibili_source_shape(
            "https://www.bilibili.com/read/fixture-only"
        )
        self.assertEqual(result.target_kind, "unknown")
        self.assertIsNone(result.identity)

    def test_non_bilibili_page_is_unknown(self):
        result = classify_bilibili_source_shape(
            "https://media.invalid.example/opus/123456789012345678"
        )
        self.assertEqual(result.target_kind, "unknown")

    def test_private_or_malformed_source_fails_with_fixed_error(self):
        for source in (
            "http://127.0.0.1/123456789012345678",
            "https://user@example.com/123456789012345678",
            "not-a-url",
        ):
            with self.subTest(source=source):
                with self.assertRaises(BilibiliDynamicNormalizationError) as raised:
                    classify_bilibili_source_shape(source)
                self.assertEqual(raised.exception.code, "BILIBILI_SOURCE_INVALID")
                self.assertNotIn(source, str(raised.exception))


class BilibiliDormantDynamicNormalizerTests(SimpleTestCase):
    def setUp(self):
        self.cases = load_document()["normalization_cases"]

    def normalize(self, name):
        return normalize_bilibili_dynamic_payload(copy.deepcopy(self.cases[name]))

    def assert_normalization_error(self, code, payload):
        with self.assertRaises(BilibiliDynamicNormalizationError) as raised:
            normalize_bilibili_dynamic_payload(payload)
        self.assertEqual(raised.exception.code, code)
        self.assertNotIn("123456789012345678", str(raised.exception))
        self.assertNotIn("://", str(raised.exception))

    def test_one_image_maps_to_image(self):
        result = self.normalize("one_image")
        self.assertEqual(result.rich_media.post_type, "image")
        self.assertEqual(result.rich_media.assets[0].role, "primary")

    def test_gallery_preserves_payload_order(self):
        result = self.normalize("gallery")
        self.assertEqual(result.rich_media.post_type, "gallery")
        self.assertEqual(
            [asset.asset_id for asset in result.asset_specs],
            ["bili:gallery:one", "bili:gallery:two", "bili:gallery:three"],
        )

    def test_explicit_same_item_pair_maps_to_live_photo(self):
        result = self.normalize("one_live_photo")
        self.assertEqual(result.rich_media.post_type, "live_photo")
        self.assertEqual(
            [asset.role for asset in result.rich_media.assets],
            ["still", "motion"],
        )
        self.assertEqual(result.rich_media.groups[0].group_id, "bili:pair:one")

    def test_multimedia_keeps_image_pair_video_source_order(self):
        result = self.normalize("multimedia")
        self.assertEqual(result.rich_media.post_type, "multimedia")
        self.assertEqual(
            [asset.asset_id for asset in result.asset_specs],
            [
                "bili:mixed:image-one",
                "bili:mixed:still-one",
                "bili:mixed:motion-one",
                "bili:mixed:video-one",
                "bili:mixed:image-two",
            ],
        )

    def test_title_and_author_are_candidates_not_rich_text_blocks(self):
        result = self.normalize("one_image")
        self.assertEqual(result.title_candidate, "Fictional image post")
        self.assertEqual(result.author_candidate, "Fixture author")
        self.assertEqual(result.rich_media.content_blocks, ())

    def test_same_numeric_value_with_different_identity_kind_fails_closed(self):
        payload = copy.deepcopy(self.cases["one_image"])
        payload["payload_identity"]["kind"] = "bilibili_opus"
        self.assert_normalization_error("BILIBILI_SOURCE_IDENTITY_MISMATCH", payload)

    def test_different_payload_identity_fails_closed(self):
        payload = copy.deepcopy(self.cases["one_image"])
        payload["payload_identity"]["id"] = "223456789012345678"
        self.assert_normalization_error("BILIBILI_SOURCE_IDENTITY_MISMATCH", payload)

    def test_live_photo_owner_must_match_exact_payload_identity(self):
        payload = copy.deepcopy(self.cases["one_live_photo"])
        payload["items"][0]["owner_identity"]["kind"] = "bilibili_dynamic"
        self.assert_normalization_error("BILIBILI_LIVE_PHOTO_OWNER_MISMATCH", payload)

    def test_missing_still_or_motion_is_rejected_without_inference(self):
        for member, code in (
            ("still", "BILIBILI_LIVE_PHOTO_STILL_MISSING"),
            ("motion", "BILIBILI_LIVE_PHOTO_MOTION_MISSING"),
        ):
            with self.subTest(member=member):
                payload = copy.deepcopy(self.cases["one_live_photo"])
                del payload["items"][0][member]
                self.assert_normalization_error(code, payload)

    def test_duplicate_pair_member_is_rejected(self):
        payload = copy.deepcopy(self.cases["one_live_photo"])
        payload["items"][0]["motion"]["asset_id"] = "bili:still:one"
        self.assert_normalization_error(
            "BILIBILI_LIVE_PHOTO_MEMBER_DUPLICATE",
            payload,
        )

    def test_duplicate_asset_identity_is_rejected(self):
        payload = copy.deepcopy(self.cases["gallery"])
        payload["items"][1]["asset_id"] = "bili:gallery:one"
        self.assert_normalization_error("BILIBILI_ASSET_ID_DUPLICATE", payload)

    def test_duplicate_pair_identity_is_rejected(self):
        payload = copy.deepcopy(self.cases["one_live_photo"])
        payload["items"].append(copy.deepcopy(payload["items"][0]))
        payload["items"][1]["still"]["asset_id"] = "bili:still:two"
        payload["items"][1]["motion"]["asset_id"] = "bili:motion:two"
        self.assert_normalization_error(
            "BILIBILI_LIVE_PHOTO_PAIR_ID_DUPLICATE",
            payload,
        )

    def test_unknown_extension_is_rejected_not_coerced_to_jpeg(self):
        payload = copy.deepcopy(self.cases["one_image"])
        payload["items"][0]["extension"] = "binary"
        self.assert_normalization_error(
            "BILIBILI_MEDIA_EXTENSION_UNSUPPORTED",
            payload,
        )

    def test_url_cover_and_text_slice_fields_are_outside_schema(self):
        for field in ("url", "cover", "text_slice"):
            with self.subTest(field=field):
                payload = copy.deepcopy(self.cases["one_image"])
                payload["items"][0][field] = "rejected"
                self.assert_normalization_error(
                    "BILIBILI_MEDIA_ITEM_FIELDS_INVALID",
                    payload,
                )

    def test_unsafe_metadata_is_rejected(self):
        payload = copy.deepcopy(self.cases["one_image"])
        payload["title"] = "token=fictional"
        self.assert_normalization_error("BILIBILI_METADATA_UNSAFE", payload)

    def test_terminal_target_cannot_be_overridden_by_recommendation_items(self):
        for state in ("deleted", "private", "not_found"):
            with self.subTest(state=state):
                payload = copy.deepcopy(self.cases["gallery"])
                payload["target_state"] = state
                self.assert_normalization_error("BILIBILI_TARGET_TERMINAL", payload)

    def test_empty_target_with_unrelated_payload_identity_fails_closed(self):
        payload = copy.deepcopy(self.cases["gallery"])
        payload["requested_identity"] = {
            "kind": "bilibili_opus",
            "id": "223456789012345678",
        }
        self.assert_normalization_error("BILIBILI_SOURCE_IDENTITY_MISMATCH", payload)

    def test_relationships_do_not_grant_download_authority(self):
        result = self.normalize("one_live_photo")
        self.assertEqual(
            {field.name for field in fields(BilibiliDynamicAssetSpec)},
            {"asset_id", "media_type", "extension", "role"},
        )
        for asset in result.asset_specs:
            self.assertIs(type(asset), BilibiliDynamicAssetSpec)
            self.assertNotIsInstance(asset, MediaAsset)
            for delivery_field in (
                "url",
                "preview_url",
                "cover_url",
                "headers",
                "provider",
                "provider_manifest_token",
                "to_download_item",
            ):
                self.assertFalse(hasattr(asset, delivery_field))

    def test_no_asset_spec_contains_cover_or_delivery_fields(self):
        result = self.normalize("multimedia")
        self.assertNotIn("primary", [asset.role for asset in result.asset_specs])
        self.assertTrue(
            all(not hasattr(asset, "cover_url") for asset in result.asset_specs)
        )

    def test_asset_specs_and_contract_descriptors_remain_aligned(self):
        result = self.normalize("multimedia")
        self.assertEqual(
            [
                (asset.asset_id, asset.media_type, asset.extension, asset.role)
                for asset in result.asset_specs
            ],
            [
                (asset.asset_id, asset.media_type, asset.extension, asset.role)
                for asset in result.rich_media.assets
            ],
        )

    def test_input_is_not_mutated(self):
        payload = copy.deepcopy(self.cases["one_live_photo"])
        original = copy.deepcopy(payload)
        normalize_bilibili_dynamic_payload(payload)
        self.assertEqual(payload, original)

    def test_item_collection_is_bounded(self):
        payload = copy.deepcopy(self.cases["one_image"])
        payload["items"] = payload["items"] * (BILIBILI_DYNAMIC_MAX_ITEMS + 1)
        self.assert_normalization_error("BILIBILI_MEDIA_ITEMS_INVALID", payload)

    def test_payload_fixtures_are_url_and_credential_free(self):
        rendered = json.dumps(
            load_document()["normalization_cases"],
            ensure_ascii=False,
        ).lower()
        for forbidden in ("://", '"url"', "cookie", "password", "secret"):
            self.assertNotIn(forbidden, rendered)

    def test_host_matrix_never_marks_dynamic_media_admitted(self):
        matrix = json.loads(HOST_MATRIX_PATH.read_text(encoding="utf-8"))
        self.assertEqual(matrix["review_scope"], "static evidence only; no host admission")
        self.assertNotIn(
            "HOST_ADMITTED",
            {item["status"] for item in matrix["hosts"]},
        )
        for category in ("image", "motion_video", "cover"):
            item = next(row for row in matrix["hosts"] if row["category"] == category)
            self.assertEqual(item["status"], "HOST_REVIEW_REQUIRED")

    def test_current_adapter_and_registry_do_not_import_dormant_module(self):
        for relative_path in (
            "xhs_download/services/platforms/bilibili.py",
            "xhs_download/services/registry.py",
        ):
            source = (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")
            self.assertNotIn("bilibili_dynamic_normalizer", source)
            self.assertNotIn("normalize_bilibili_dynamic_payload", source)
            self.assertNotIn("rich_media=", source)
