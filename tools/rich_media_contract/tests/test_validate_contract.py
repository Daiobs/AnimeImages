import contextlib
import io
import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from tools.rich_media_contract.validate_contract import (
    INVALID_ASSET,
    INVALID_BLOCK,
    INVALID_GROUP,
    INVALID_SCHEMA,
    MAX_TEXT_BLOCK_CHARS,
    SECURITY_REJECTED,
    VALID,
    main,
    normalize_document,
    validate_document,
)


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures"


def load_fixture(kind, name):
    return json.loads((FIXTURES / kind / f"{name}.json").read_text(encoding="utf-8"))


class ValidFixtureTests(unittest.TestCase):
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

    def test_all_valid_fixtures_pass(self):
        for name in self.VALID_CASES:
            with self.subTest(name=name):
                self.assertEqual(validate_document(load_fixture("valid", name)).status, VALID)

    def test_asset_tuple_order_is_preserved_by_normalization(self):
        document = load_fixture("valid", "multimedia")
        expected = [asset["asset_id"] for asset in document["assets"]]
        normalized = normalize_document(document)
        self.assertEqual([asset["asset_id"] for asset in normalized["assets"]], expected)

    def test_live_photo_has_exact_still_motion_pair(self):
        normalized = normalize_document(load_fixture("valid", "live_photo"))
        self.assertEqual(
            [member["role"] for member in normalized["groups"][0]["members"]],
            ["still", "motion"],
        )

    def test_rich_text_live_photo_references_pair_together(self):
        normalized = normalize_document(load_fixture("valid", "rich_text_live_photo"))
        self.assertEqual(len(normalized["content_blocks"][1]["asset_ids"]), 2)


class InvalidFixtureTests(unittest.TestCase):
    EXPECTED = {
        "missing_asset_ref": INVALID_BLOCK,
        "duplicate_asset_id": INVALID_ASSET,
        "duplicate_group_id": INVALID_GROUP,
        "live_photo_two_images": INVALID_GROUP,
        "live_photo_missing_motion": INVALID_GROUP,
        "asset_in_two_groups": INVALID_GROUP,
        "raw_html": SECURITY_REJECTED,
        "url_field_injection": SECURITY_REJECTED,
        "token_key": SECURITY_REJECTED,
        "unknown_post_type": INVALID_SCHEMA,
        "unknown_relationship_type": INVALID_GROUP,
        "animation_bad_extension": INVALID_ASSET,
        "unknown_top_field": INVALID_SCHEMA,
        "duplicate_group_member": INVALID_GROUP,
        "rich_text_orphan_asset": INVALID_BLOCK,
        "gallery_mixed_media": INVALID_SCHEMA,
    }

    def test_all_invalid_fixtures_fail_closed(self):
        for name, expected_status in self.EXPECTED.items():
            with self.subTest(name=name):
                result = validate_document(load_fixture("invalid", name))
                self.assertEqual(result.status, expected_status)
                self.assertFalse(result.is_valid)

    def test_oversized_text_is_rejected(self):
        document = load_fixture("valid", "rich_text_text_only")
        document["content_blocks"][0]["text"] = "x" * (MAX_TEXT_BLOCK_CHARS + 1)
        result = validate_document(document)
        self.assertEqual(result.status, INVALID_BLOCK)
        self.assertEqual(result.code, "TEXT_BLOCK_LENGTH_INVALID")

    def test_url_like_text_is_rejected_without_echoing_value(self):
        document = load_fixture("valid", "rich_text_text_only")
        sensitive = "https" + "://fictional.invalid/media?signature=redacted"
        document["content_blocks"][0]["text"] = sensitive
        result = validate_document(document)
        self.assertEqual(result.status, SECURITY_REJECTED)
        self.assertNotIn(sensitive, result.render())

    def test_markdown_link_is_rejected(self):
        document = load_fixture("valid", "rich_text_text_only")
        document["content_blocks"][0]["text"] = "[label]" + "(target)"
        self.assertEqual(validate_document(document).status, SECURITY_REJECTED)

    def test_deep_unknown_structure_is_rejected_without_traceback(self):
        document = load_fixture("valid", "legacy_single_image")
        document["unexpected"] = [[[[[[["value"]]]]]]]
        result = validate_document(document)
        self.assertEqual(result.status, SECURITY_REJECTED)
        self.assertEqual(result.code, "DOCUMENT_DEPTH_LIMIT")


class ContractArtifactTests(unittest.TestCase):
    def test_schema_is_valid_json_and_contains_no_network_location(self):
        schema_text = (ROOT / "schema_v1.json").read_text(encoding="utf-8")
        schema = json.loads(schema_text)
        self.assertEqual(schema["additionalProperties"], False)
        self.assertNotIn("://", schema_text)

    def test_all_fixture_json_is_fictional_and_url_free(self):
        fixtures = sorted(FIXTURES.rglob("*.json"))
        self.assertGreaterEqual(len(fixtures), 20)
        for path in fixtures:
            with self.subTest(path=path.name):
                text = path.read_text(encoding="utf-8")
                json.loads(text)
                self.assertNotIn("://", text)
                self.assertNotIn("www.", text.lower())

    def test_cli_returns_fixed_safe_output(self):
        invalid = load_fixture("invalid", "token_key")
        secret = invalid["assets"][0]["provider_token"]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "private-name.json"
            path.write_text(json.dumps(invalid), encoding="utf-8")
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = main([str(path)])
        output = stdout.getvalue()
        self.assertEqual(exit_code, 2)
        self.assertIn("SECURITY_REJECTED", output)
        self.assertNotIn(secret, output)
        self.assertNotIn("private-name", output)


if __name__ == "__main__":
    unittest.main()
