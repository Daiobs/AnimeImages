import json
import unittest
from copy import deepcopy
from pathlib import Path

from tools.rich_media_contract.map_parsehub_result import MappingRejected, map_record
from tools.rich_media_contract.validate_contract import VALID, validate_document


PROJECT_ROOT = Path(__file__).resolve().parents[3]
PARSEHUB_FIXTURES = PROJECT_ROOT / "tools" / "parsehub_lab" / "fixtures"


def load_parsehub_fixture(group, name="supplier"):
    return json.loads(
        (PARSEHUB_FIXTURES / group / f"{name}.json").read_text(encoding="utf-8")
    )


class ParserLabMappingTests(unittest.TestCase):
    def test_live_photo_maps_to_exact_relation_group(self):
        result = map_record(load_parsehub_fixture("live_photo"))
        self.assertEqual(result["post_type"], "live_photo")
        self.assertEqual(result["groups"][0]["relationship_type"], "live_photo")
        self.assertEqual(
            [member["role"] for member in result["groups"][0]["members"]],
            ["still", "motion"],
        )
        self.assertEqual(validate_document(result).status, VALID)

    def test_multimedia_preserves_sanitized_media_order(self):
        result = map_record(load_parsehub_fixture("multimedia"))
        self.assertEqual(result["post_type"], "multimedia")
        self.assertEqual(
            [asset["media_type"] for asset in result["assets"]],
            ["image", "video"],
        )

    def test_rich_text_maps_to_inert_text_block(self):
        result = map_record(load_parsehub_fixture("rich_text"))
        self.assertEqual(result["post_type"], "rich_text")
        self.assertEqual(result["content_blocks"][0]["type"], "text")
        self.assertNotIn("notes", result)

    def test_rich_text_live_photo_members_share_one_media_block(self):
        source = deepcopy(load_parsehub_fixture("live_photo"))
        source["content_type"] = "rich_text"
        source["description_present"] = True
        result = map_record(source)
        media_blocks = [
            block for block in result["content_blocks"] if block["type"] == "media"
        ]
        self.assertEqual(len(media_blocks), 1)
        self.assertEqual(len(media_blocks[0]["asset_ids"]), 2)

    def test_animation_maps_to_physical_image(self):
        source = deepcopy(load_parsehub_fixture("gallery"))
        source["case_id"] = "fictional_animation"
        source["media_count"] = 2
        source["media"] = [deepcopy(source["media"][0]), deepcopy(source["media"][0])]
        for index, media in enumerate(source["media"]):
            media["index"] = index
            media["type"] = "animation"
            media["role"] = "gallery"
            media["extension"] = "webp"
        result = map_record(source)
        self.assertEqual(result["assets"][0]["media_type"], "image")
        self.assertEqual(result["assets"][0]["role"], "animation")

    def test_cover_is_not_promoted_to_canonical_asset(self):
        source = deepcopy(load_parsehub_fixture("single_video"))
        source["media"][0]["role"] = "cover"
        with self.assertRaisesRegex(
            MappingRejected, "MAPPING_COVER_IS_NOT_CANONICAL_ASSET"
        ):
            map_record(source)

    def test_malicious_parser_lab_record_fails_closed(self):
        with self.assertRaisesRegex(MappingRejected, "MAPPING_INPUT_REJECTED"):
            map_record(load_parsehub_fixture("malicious"))


if __name__ == "__main__":
    unittest.main()
