import copy
import json
from pathlib import Path

from django.test import SimpleTestCase

from .services.assets import ParseResult
from .services.platforms.xhs_rich_media import (
    XHS_RICH_MEDIA_MAX_ITEMS,
    XhsRichMediaNormalizationError,
    normalize_xhs_rich_media_payload,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = PROJECT_ROOT / "xhs_download" / "test_fixtures" / "xhs_rich_media_cases.json"


def load_cases():
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


class XhsDormantRichMediaNormalizerTests(SimpleTestCase):
    def setUp(self):
        self.cases = load_cases()

    def normalize(self, name):
        return normalize_xhs_rich_media_payload(copy.deepcopy(self.cases[name]))

    def assert_normalization_error(self, code, payload):
        with self.assertRaises(XhsRichMediaNormalizationError) as raised:
            normalize_xhs_rich_media_payload(payload)
        self.assertEqual(raised.exception.code, code)
        self.assertNotIn("note-fixture", str(raised.exception))
        self.assertNotIn("://", str(raised.exception))

    def test_absent_payload_emits_no_contract_or_assets(self):
        result = normalize_xhs_rich_media_payload(None)
        self.assertEqual(result.assets, ())
        self.assertIsNone(result.rich_media)

    def test_single_image_builds_one_primary_asset(self):
        result = self.normalize("single_image")
        self.assertEqual(result.rich_media.post_type, "image")
        self.assertEqual(result.rich_media.assets[0].role, "primary")
        self.assertEqual(result.assets[0].ext, "jpg")

    def test_gallery_preserves_all_image_extensions(self):
        result = self.normalize("gallery")
        self.assertEqual(result.rich_media.post_type, "gallery")
        self.assertEqual(
            [asset.extension for asset in result.rich_media.assets],
            ["jpeg", "webp", "avif"],
        )
        self.assertEqual(
            [asset.role for asset in result.rich_media.assets],
            ["gallery", "gallery", "gallery"],
        )

    def test_video_builds_one_primary_asset(self):
        result = self.normalize("video")
        self.assertEqual(result.rich_media.post_type, "video")
        self.assertEqual(result.rich_media.assets[0].media_type, "video")
        self.assertEqual(result.rich_media.assets[0].role, "primary")

    def test_one_live_photo_requires_and_preserves_explicit_pair(self):
        result = self.normalize("one_live_photo")
        self.assertEqual(result.rich_media.post_type, "live_photo")
        self.assertEqual(
            [asset.role for asset in result.rich_media.assets],
            ["still", "motion"],
        )
        self.assertEqual(result.rich_media.groups[0].group_id, "xhs:pair:one")
        self.assertEqual(
            [member.role for member in result.rich_media.groups[0].members],
            ["still", "motion"],
        )

    def test_multiple_live_photos_keep_pair_and_asset_order(self):
        result = self.normalize("multiple_live_photos")
        self.assertEqual(result.rich_media.post_type, "live_photo")
        self.assertEqual(
            [group.group_id for group in result.rich_media.groups],
            ["xhs:pair:one", "xhs:pair:two"],
        )
        self.assertEqual(
            [asset.asset_id for asset in result.assets],
            [
                "xhs:still:one",
                "xhs:motion:one",
                "xhs:still:two",
                "xhs:motion:two",
            ],
        )

    def test_mixed_images_and_live_photo_build_multimedia_contract(self):
        result = self.normalize("mixed_image_live_photo")
        self.assertEqual(result.rich_media.post_type, "multimedia")
        self.assertEqual(len(result.rich_media.groups), 1)
        self.assertEqual(
            [asset.media_type for asset in result.assets],
            ["image", "image", "video", "image"],
        )

    def test_requested_and_payload_note_identity_must_match_exactly(self):
        payload = copy.deepcopy(self.cases["single_image"])
        payload["payload_note_id"] = "note-recommendation-2"
        self.assert_normalization_error("XHS_SOURCE_IDENTITY_MISMATCH", payload)

    def test_generic_page_recommendation_payload_fails_closed(self):
        payload = copy.deepcopy(self.cases["gallery"])
        payload["requested_note_id"] = "note-current-1"
        payload["payload_note_id"] = "note-recommended-9"
        self.assert_normalization_error("XHS_SOURCE_IDENTITY_MISMATCH", payload)

    def test_live_photo_owner_must_match_payload_note(self):
        payload = copy.deepcopy(self.cases["one_live_photo"])
        payload["items"][0]["owner_note_id"] = "note-recommendation-2"
        self.assert_normalization_error("XHS_LIVE_PHOTO_OWNER_MISMATCH", payload)

    def test_missing_motion_is_rejected_without_pair_inference(self):
        payload = copy.deepcopy(self.cases["one_live_photo"])
        del payload["items"][0]["motion"]
        self.assert_normalization_error("XHS_LIVE_PHOTO_MOTION_MISSING", payload)

    def test_missing_still_is_rejected_without_pair_inference(self):
        payload = copy.deepcopy(self.cases["one_live_photo"])
        del payload["items"][0]["still"]
        self.assert_normalization_error("XHS_LIVE_PHOTO_STILL_MISSING", payload)

    def test_duplicate_pair_member_is_rejected(self):
        payload = copy.deepcopy(self.cases["one_live_photo"])
        payload["items"][0]["motion"]["asset_id"] = "xhs:still:one"
        self.assert_normalization_error("XHS_LIVE_PHOTO_MEMBER_DUPLICATE", payload)

    def test_duplicate_pair_identity_is_rejected(self):
        payload = copy.deepcopy(self.cases["multiple_live_photos"])
        payload["items"][1]["pair_id"] = "xhs:pair:one"
        self.assert_normalization_error("XHS_LIVE_PHOTO_PAIR_ID_DUPLICATE", payload)

    def test_duplicate_asset_identity_across_items_is_rejected(self):
        payload = copy.deepcopy(self.cases["gallery"])
        payload["items"][1]["asset_id"] = "xhs:gallery:one"
        self.assert_normalization_error("XHS_ASSET_ID_DUPLICATE", payload)

    def test_unknown_extension_is_rejected_instead_of_coerced_to_jpeg(self):
        payload = copy.deepcopy(self.cases["single_image"])
        payload["items"][0]["extension"] = "binary"
        self.assert_normalization_error("XHS_MEDIA_EXTENSION_UNSUPPORTED", payload)

    def test_heic_is_explicitly_deferred_by_contract_v1(self):
        payload = copy.deepcopy(self.cases["single_image"])
        payload["items"][0]["extension"] = "heic"
        self.assert_normalization_error("XHS_HEIC_CONTRACT_GAP", payload)

    def test_url_or_cover_fields_are_not_part_of_sanitized_schema(self):
        for field in ("url", "cover"):
            with self.subTest(field=field):
                payload = copy.deepcopy(self.cases["single_image"])
                payload["items"][0][field] = "rejected-value"
                self.assert_normalization_error(
                    "XHS_MEDIA_ITEM_FIELDS_INVALID",
                    payload,
                )

    def test_order_preservation_flattens_each_pair_still_then_motion(self):
        result = self.normalize("mixed_image_live_photo")
        self.assertEqual(
            [asset.asset_id for asset in result.assets],
            [
                "xhs:mixed:image-one",
                "xhs:mixed:still-one",
                "xhs:mixed:motion-one",
                "xhs:mixed:image-two",
            ],
        )
        self.assertEqual(
            [asset.asset_id for asset in result.rich_media.assets],
            [asset.asset_id for asset in result.assets],
        )

    def test_no_candidate_is_promoted_to_cover_or_primary_in_multimedia(self):
        result = self.normalize("mixed_image_live_photo")
        self.assertTrue(all(asset.cover_url == "" for asset in result.assets))
        self.assertNotIn(
            "primary",
            [asset.role for asset in result.rich_media.assets],
        )

    def test_relationship_metadata_does_not_grant_download_authority(self):
        result = self.normalize("one_live_photo")
        for asset in result.assets:
            self.assertEqual(asset.url, "")
            self.assertEqual(asset.provider, "")
            self.assertEqual(asset.provider_manifest_token, "")
            self.assertNotIn("provider_manifest_token", asset.to_download_item())

    def test_normalized_assets_and_contract_satisfy_parse_result_boundary(self):
        result = self.normalize("mixed_image_live_photo")
        parsed = ParseResult(
            platform="xiaohongshu",
            post_type=result.rich_media.post_type,
            base_filename="fixture",
            assets=result.assets,
            metadata={},
            rich_media=result.rich_media,
        )
        self.assertIs(parsed.rich_media, result.rich_media)

    def test_input_fixture_is_not_mutated(self):
        payload = copy.deepcopy(self.cases["one_live_photo"])
        original = copy.deepcopy(payload)
        normalize_xhs_rich_media_payload(payload)
        self.assertEqual(payload, original)

    def test_item_collection_is_bounded(self):
        payload = copy.deepcopy(self.cases["single_image"])
        payload["items"] = payload["items"] * (XHS_RICH_MEDIA_MAX_ITEMS + 1)
        self.assert_normalization_error("XHS_MEDIA_ITEMS_INVALID", payload)

    def test_fixtures_are_url_free_and_credential_free(self):
        rendered = FIXTURE_PATH.read_text(encoding="utf-8").lower()
        for forbidden in ("://", "token", "cookie", "password", "secret", '"url"'):
            self.assertNotIn(forbidden, rendered)

    def test_current_xhs_adapter_and_registry_do_not_import_dormant_normalizer(self):
        for relative_path in (
            "xhs_download/services/platforms/xiaohongshu.py",
            "xhs_download/services/registry.py",
        ):
            source = (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")
            self.assertNotIn("xhs_rich_media", source)
            self.assertNotIn("normalize_xhs_rich_media_payload", source)
