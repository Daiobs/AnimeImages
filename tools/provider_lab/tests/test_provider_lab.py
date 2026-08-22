import ast
import contextlib
import io
import json
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path

from tools.provider_lab.cli import (
    DEFAULT_DELTA,
    DEFAULT_SOURCE_FIXTURE,
    compare_provider_capability,
    inventory,
    main,
    redact_source,
    summarize_delta,
    validate_rich_media,
    validate_source_fixtures,
)


ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = ROOT.parents[1]
RICH_MEDIA_FIXTURES = PROJECT_ROOT / "tools" / "rich_media_contract" / "fixtures"


class ProviderLabCommandTests(unittest.TestCase):
    def test_inventory_is_offline_and_lists_closed_commands(self):
        result = inventory()
        self.assertFalse(result["network_enabled"])
        self.assertFalse(result["parsehub_imported"])
        self.assertFalse(result["production_environment_access"])
        self.assertEqual(len(result["commands"]), 6)

    def test_source_fixture_suite_passes_with_acceptance_and_rejection_cases(self):
        result = validate_source_fixtures()
        self.assertEqual(result["status"], "PASS")
        self.assertGreaterEqual(result["total"], 11)
        self.assertGreater(result["accepted"], 0)
        self.assertGreater(result["rejected"], 0)

    def test_fixture_document_is_fictional_and_contains_no_real_platform_hosts(self):
        rendered = DEFAULT_SOURCE_FIXTURE.read_text(encoding="utf-8")
        document = json.loads(rendered)
        self.assertEqual(document["schema_version"], "1.0")
        self.assertNotIn("xiaohongshu.com/", rendered)
        self.assertNotIn("douyin.com/", rendered)
        self.assertNotIn("bilibili.com/", rendered)
        self.assertNotIn("365yg.com", rendered)

    def test_redact_source_returns_only_structural_fields(self):
        secret = "fictional-sensitive-value"
        result = redact_source(
            Namespace(
                platform_id="xhs",
                input_kind="note_page",
                source=(
                    "https://www.xiaohongshu.example/explore/note_fixture"
                    f"?xsec_token={secret}&utm_source=lab"
                ),
                resolved_url="",
                canonical_identity="xhs_note:note_fixture",
                parameter_policy=json.dumps(
                    {
                        "retain_for_parse": [],
                        "retain_for_identity": [],
                        "remove_after_parse": ["xsec_token"],
                        "always_drop": ["utm_source"],
                    }
                ),
                resolution_duration_ms=3,
            )
        )
        rendered = json.dumps(result, sort_keys=True)
        self.assertNotIn(secret, rendered)
        self.assertNotIn("https://", rendered)
        self.assertNotIn("note_fixture", rendered)
        self.assertEqual(result["canonical_identity_kind"], "xhs_note")
        self.assertEqual(result["removed_parameter_names"], ["utm_source", "xsec_token"])

    def test_cli_rejection_does_not_echo_source_or_parameter_values(self):
        secret = "fictional-value-that-must-not-appear"
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            exit_code = main(
                [
                    "redact-source",
                    "--platform-id",
                    "generic",
                    "--input-kind",
                    "url",
                    "--source",
                    f"http://127.0.0.1/private?token={secret}",
                    "--canonical-identity",
                    "generic_media:private_fixture",
                    "--parameter-policy",
                    json.dumps(
                        {
                            "retain_for_parse": [],
                            "retain_for_identity": [],
                            "remove_after_parse": [],
                            "always_drop": ["token"],
                        }
                    ),
                ]
            )
        output = stdout.getvalue()
        self.assertEqual(exit_code, 2)
        self.assertIn("SOURCE_HOST_PRIVATE", output)
        self.assertNotIn(secret, output)
        self.assertNotIn("127.0.0.1", output)

    def test_compare_and_summary_use_closed_delta_actions(self):
        capability = compare_provider_capability(
            DEFAULT_DELTA,
            "source_url_parameter_lifecycle",
        )
        self.assertEqual(capability["recommended_action"], "ADOPT_NOW")
        self.assertEqual(capability["selected_pr"], "PR-A")
        summary = summarize_delta(DEFAULT_DELTA)
        self.assertGreaterEqual(summary["capability_count"], 8)
        self.assertGreater(summary["action_counts"]["ADOPT_NOW"], 0)

    def test_rich_media_validation_reuses_url_free_contract(self):
        valid = RICH_MEDIA_FIXTURES / "valid" / "live_photo.json"
        invalid = RICH_MEDIA_FIXTURES / "invalid" / "token_key.json"
        self.assertEqual(validate_rich_media([valid])["status"], "PASS")
        result = validate_rich_media([valid, invalid])
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["results"][1]["status"], "SECURITY_REJECTED")

    def test_cli_source_fixture_command_emits_safe_summary(self):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            exit_code = main(["validate-source-fixture"])
        result = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(result["status"], "PASS")
        self.assertNotIn("input_value", result)

    def test_provider_lab_has_no_network_parsehub_or_environment_dependency(self):
        disallowed_imports = {
            "requests",
            "httpx",
            "urllib.request",
            "socket",
            "subprocess",
            "os",
            "dotenv",
            "parsehub",
        }
        for path in sorted(ROOT.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            imports = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imports.update(alias.name.lower() for alias in node.names)
                elif isinstance(node, ast.ImportFrom):
                    imports.add((node.module or "").lower())
            with self.subTest(path=path.name):
                self.assertTrue(imports.isdisjoint(disallowed_imports))

    def test_invalid_delta_path_returns_fixed_cli_error(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "private-delta-name.json"
            path.write_text("{}", encoding="utf-8")
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = main(["summarize-delta", "--delta", str(path)])
        output = stdout.getvalue()
        self.assertEqual(exit_code, 2)
        self.assertIn("INPUT_REJECTED", output)
        self.assertNotIn("private-delta-name", output)


if __name__ == "__main__":
    unittest.main()
