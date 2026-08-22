import ast
from dataclasses import fields
from pathlib import Path

from django.test import SimpleTestCase, override_settings

from .services.assets import MediaAsset, ParseResult
from .services.provider_manifest import (
    build_provider_manifest_token,
    provider_manifest_is_valid,
)
from .services.rich_media import (
    RichMediaAssetDescriptorV1,
    RichMediaContractV1,
    RichMediaGroupMemberV1,
    RichMediaGroupV1,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def legacy_assets():
    return (
        MediaAsset(
            media_type="image",
            url="image-location",
            platform="fixture",
            ext="jpg",
        ),
        MediaAsset(
            media_type="video",
            url="video-location",
            platform="fixture",
            cover_url="cover-location",
            ext="mp4",
        ),
        MediaAsset(
            media_type="image",
            url="second-image-location",
            platform="fixture",
            ext="png",
        ),
    )


def live_photo_result():
    still = MediaAsset(
        media_type="image",
        url="still-location",
        platform="fixture",
        ext="jpg",
        asset_id="asset:compat:still",
    )
    motion = MediaAsset(
        media_type="video",
        url="motion-location",
        platform="fixture",
        ext="mp4",
        asset_id="asset:compat:motion",
    )
    contract = RichMediaContractV1(
        schema_version="1.0",
        post_type="live_photo",
        assets=(
            RichMediaAssetDescriptorV1(
                asset_id=still.asset_id,
                media_type="image",
                extension="jpg",
                role="still",
            ),
            RichMediaAssetDescriptorV1(
                asset_id=motion.asset_id,
                media_type="video",
                extension="mp4",
                role="motion",
            ),
        ),
        groups=(
            RichMediaGroupV1(
                group_id="group:compat:one",
                relationship_type="live_photo",
                members=(
                    RichMediaGroupMemberV1(still.asset_id, "still"),
                    RichMediaGroupMemberV1(motion.asset_id, "motion"),
                ),
            ),
        ),
        content_blocks=(),
    )
    return ParseResult(
        platform="fixture",
        post_type="live_photo",
        base_filename="fixture",
        assets=(still, motion),
        metadata={},
        rich_media=contract,
    )


class RichMediaLegacyCompatibilityTests(SimpleTestCase):
    def test_existing_five_argument_positional_constructor_remains_valid(self):
        result = ParseResult("fixture", "video", "name", legacy_assets(), {"key": "value"})
        self.assertIsNone(result.rich_media)
        self.assertEqual(result.metadata, {"key": "value"})

    def test_existing_keyword_constructor_defaults_rich_media_to_none(self):
        result = ParseResult(
            platform="fixture",
            post_type="video",
            base_filename="name",
            assets=legacy_assets(),
            metadata={"key": "value"},
        )
        self.assertIsNone(result.rich_media)

    def test_parse_result_field_order_is_additive_and_repr_stays_legacy_shaped(self):
        self.assertEqual(
            [field.name for field in fields(ParseResult)],
            [
                "platform",
                "post_type",
                "base_filename",
                "assets",
                "metadata",
                "rich_media",
            ],
        )
        result = ParseResult("fixture", "video", "name", (), {})
        self.assertNotIn("rich_media", repr(result))

    def test_legacy_equality_behavior_remains_stable(self):
        first = ParseResult("fixture", "video", "name", legacy_assets(), {"key": "value"})
        second = ParseResult("fixture", "video", "name", legacy_assets(), {"key": "value"})
        self.assertEqual(first, second)
        self.assertNotEqual(first, live_photo_result())

    def test_image_and_video_filters_preserve_existing_relative_order(self):
        result = ParseResult("fixture", "multimedia", "name", legacy_assets(), {})
        self.assertEqual(
            [asset.url for asset in result.image_assets],
            ["image-location", "second-image-location"],
        )
        self.assertEqual(
            [asset.url for asset in result.video_assets],
            ["video-location"],
        )
        self.assertEqual(
            [asset.url for asset in result.assets],
            ["image-location", "video-location", "second-image-location"],
        )

    def test_to_legacy_dict_output_is_exactly_unchanged(self):
        result = ParseResult("fixture", "multimedia", "name", legacy_assets(), {})
        self.assertEqual(
            result.to_legacy_dict(),
            {
                "platform": "fixture",
                "post_type": "multimedia",
                "base_filename": "name",
                "image_urls": ["image-location", "second-image-location"],
                "video_items": [
                    {
                        "video_url": "video-location",
                        "cover_url": "cover-location",
                    }
                ],
            },
        )

    def test_to_download_item_output_is_exactly_unchanged(self):
        asset = MediaAsset(
            media_type="video",
            url="media-location",
            platform="fixture",
            cover_url="cover-location",
            asset_id="asset:download:one",
            provider="fixture-provider",
            provider_resource_id="resource-one",
            provider_cache_key="cache-one",
            provider_manifest_token="manifest-one",
        )
        self.assertEqual(
            asset.to_download_item(),
            {
                "type": "video",
                "url": "media-location",
                "cover_url": "cover-location",
                "asset_id": "asset:download:one",
                "provider": "fixture-provider",
                "provider_resource_id": "resource-one",
                "provider_cache_key": "cache-one",
                "provider_manifest_token": "manifest-one",
            },
        )

    @override_settings(SECRET_KEY="rich-media-test", PROVIDER_MANIFEST_MAX_AGE_SECONDS=60)
    def test_provider_manifest_remains_per_physical_asset(self):
        token = build_provider_manifest_token(
            asset_id="asset:manifest:one",
            provider="fixture-provider",
            provider_resource_id="resource-one",
            provider_cache_key="cache-one",
            url="media-location",
            media_type="video",
        )
        item = {
            "type": "video",
            "url": "media-location",
            "asset_id": "asset:manifest:one",
            "provider": "fixture-provider",
            "provider_resource_id": "resource-one",
            "provider_cache_key": "cache-one",
            "provider_manifest_token": token,
        }
        self.assertTrue(provider_manifest_is_valid(item))
        item["asset_id"] = "asset:manifest:other"
        self.assertFalse(provider_manifest_is_valid(item))

    def test_relationship_does_not_create_download_authority(self):
        result = live_photo_result()
        structural = result.rich_media.to_structural_dict()
        self.assertNotIn("url", str(structural).lower())
        self.assertNotIn("manifest", str(structural).lower())
        self.assertEqual(
            [asset.to_download_item() for asset in result.assets],
            [
                {"type": "image", "url": "still-location", "asset_id": "asset:compat:still"},
                {"type": "video", "url": "motion-location", "asset_id": "asset:compat:motion"},
            ],
        )


class RichMediaCurrentProviderBoundaryTests(SimpleTestCase):
    def test_current_production_parse_result_constructors_do_not_emit_rich_media(self):
        production_files = sorted((PROJECT_ROOT / "xhs_download" / "services").rglob("*.py"))
        constructors = []
        for path in production_files:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                name = None
                if isinstance(node.func, ast.Name):
                    name = node.func.id
                elif isinstance(node.func, ast.Attribute):
                    name = node.func.attr
                if name != "ParseResult":
                    continue
                constructors.append((path, node))
                self.assertNotIn("rich_media", {keyword.arg for keyword in node.keywords})
        self.assertGreaterEqual(len(constructors), 20)

    def test_runtime_module_has_no_tools_parsehub_or_download_import(self):
        path = PROJECT_ROOT / "xhs_download" / "services" / "rich_media.py"
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.append(node.module or "")
        joined = " ".join(imported).lower()
        self.assertNotIn("tools", joined)
        self.assertNotIn("parsehub", joined)
        self.assertNotIn("provider_manifest", joined)
        self.assertNotIn("download", joined)

    def test_runtime_contract_has_no_case_id_or_sequence_index(self):
        self.assertEqual(
            [field.name for field in fields(RichMediaContractV1)],
            ["schema_version", "post_type", "assets", "groups", "content_blocks"],
        )
        self.assertNotIn("case_id", {field.name for field in fields(RichMediaContractV1)})
        self.assertNotIn(
            "sequence_index",
            {field.name for field in fields(RichMediaAssetDescriptorV1)},
        )
