import contextlib
import io
import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from tools.parsehub_lab.compare_results import compare_records, main, validate_record


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def load_fixture(group, name):
    return json.loads((FIXTURES / group / f"{name}.json").read_text(encoding="utf-8"))


class ComparatorFixtureTests(unittest.TestCase):
    def test_single_video_matches(self):
        result = compare_records(load_fixture("single_video", "animeimages"), load_fixture("single_video", "supplier"))
        self.assertEqual(result["classification"], "MATCH")

    def test_gallery_reports_animeimages_richer(self):
        result = compare_records(load_fixture("gallery", "animeimages"), load_fixture("gallery", "supplier"))
        self.assertEqual(result["classification"], "ANIMEIMAGES_RICHER")

    def test_live_photo_pair_matches(self):
        result = compare_records(load_fixture("live_photo", "animeimages"), load_fixture("live_photo", "supplier"))
        self.assertEqual(result["classification"], "MATCH")

    def test_multimedia_reports_supplier_richer(self):
        result = compare_records(load_fixture("multimedia", "animeimages"), load_fixture("multimedia", "supplier"))
        self.assertEqual(result["classification"], "SUPPLIER_RICHER")

    def test_rich_text_reports_supplier_richer(self):
        result = compare_records(load_fixture("rich_text", "animeimages"), load_fixture("rich_text", "supplier"))
        self.assertEqual(result["classification"], "SUPPLIER_RICHER")

    def test_structural_mismatch_is_explicit(self):
        result = compare_records(load_fixture("mismatch", "animeimages"), load_fixture("mismatch", "supplier"))
        self.assertEqual(result["classification"], "STRUCTURAL_MISMATCH")

    def test_malicious_fixture_fails_closed(self):
        result = compare_records(load_fixture("single_video", "animeimages"), load_fixture("malicious", "supplier"))
        self.assertEqual(result["classification"], "INVALID_INPUT")
        self.assertEqual(result["differences"], [])


class RedactionGateTests(unittest.TestCase):
    def setUp(self):
        self.left = load_fixture("single_video", "animeimages")

    def test_full_signed_url_is_rejected_and_not_echoed(self):
        supplier = deepcopy(load_fixture("single_video", "supplier"))
        sensitive = "https" + "://media.example.invalid/file.mp4?signature=fictional"
        supplier["notes"] = sensitive
        rendered = json.dumps(compare_records(self.left, supplier))
        self.assertIn("INVALID_INPUT", rendered)
        self.assertNotIn(sensitive, rendered)

    def test_cookie_like_extra_key_is_rejected_and_not_echoed(self):
        supplier = deepcopy(load_fixture("single_video", "supplier"))
        secret = "fictional-session-material"
        supplier["cookie"] = secret
        rendered = json.dumps(compare_records(self.left, supplier))
        self.assertIn("INVALID_INPUT", rendered)
        self.assertNotIn(secret, rendered)

    def test_raw_error_is_rejected_and_not_echoed(self):
        supplier = deepcopy(load_fixture("single_video", "supplier"))
        raw_error = "provider emitted private request context"
        supplier["raw_error"] = raw_error
        rendered = json.dumps(compare_records(self.left, supplier))
        self.assertIn("INVALID_INPUT", rendered)
        self.assertNotIn(raw_error, rendered)

    def test_query_fragment_without_assignment_is_rejected(self):
        supplier = deepcopy(load_fixture("single_video", "supplier"))
        supplier["notes"] = "fictional path?flag"
        self.assertEqual(compare_records(self.left, supplier)["classification"], "INVALID_INPUT")

    def test_phone_like_text_is_rejected_and_not_echoed(self):
        supplier = deepcopy(load_fixture("single_video", "supplier"))
        phone = "+1 (555) 010-2040"
        supplier["notes"] = phone
        rendered = json.dumps(compare_records(self.left, supplier))
        self.assertIn("INVALID_INPUT", rendered)
        self.assertNotIn(phone, rendered)

    def test_bearer_material_is_rejected_and_not_echoed(self):
        supplier = deepcopy(load_fixture("single_video", "supplier"))
        bearer = "Bearer fictional_material_123456"
        supplier["notes"] = bearer
        rendered = json.dumps(compare_records(self.left, supplier))
        self.assertIn("INVALID_INPUT", rendered)
        self.assertNotIn(bearer, rendered)

    def test_sensitive_assignments_in_allowed_fields_are_rejected(self):
        for key in ("sessionid", "api_key"):
            with self.subTest(key=key):
                supplier = deepcopy(load_fixture("single_video", "supplier"))
                sensitive = f"{key}=fictional_material_123456"
                supplier["media"][0]["quality_label"] = sensitive
                rendered = json.dumps(compare_records(self.left, supplier))
                self.assertIn("INVALID_INPUT", rendered)
                self.assertNotIn(sensitive, rendered)

    def test_basic_auth_is_rejected_and_not_echoed(self):
        supplier = deepcopy(load_fixture("single_video", "supplier"))
        sensitive = "Basic " + "ZmljdGlvbmFsOnJlZGFjdGVk"
        supplier["notes"] = sensitive
        rendered = json.dumps(compare_records(self.left, supplier))
        self.assertIn("INVALID_INPUT", rendered)
        self.assertNotIn(sensitive, rendered)

    def test_bare_jwt_is_rejected_and_not_echoed(self):
        supplier = deepcopy(load_fixture("single_video", "supplier"))
        sensitive = ".".join(("eyJmaWN0aW9uYWw", "cmVkYWN0ZWRwYXlsb2Fk", "cmVkYWN0ZWRzaWduYXR1cmU"))
        supplier["media"][0]["quality_label"] = sensitive
        rendered = json.dumps(compare_records(self.left, supplier))
        self.assertIn("INVALID_INPUT", rendered)
        self.assertNotIn(sensitive, rendered)

    def test_opaque_token_is_rejected_and_not_echoed(self):
        supplier = deepcopy(load_fixture("single_video", "supplier"))
        sensitive = "fictionalopaquecredentialvalue123456"
        supplier["notes"] = sensitive
        rendered = json.dumps(compare_records(self.left, supplier))
        self.assertIn("INVALID_INPUT", rendered)
        self.assertNotIn(sensitive, rendered)

    def test_bare_hostname_is_retained(self):
        supplier = deepcopy(load_fixture("single_video", "supplier"))
        supplier["media"][0]["hostname"] = "other.example.invalid"
        result = compare_records(self.left, supplier)
        self.assertEqual(result["classification"], "SECURITY_REVIEW_REQUIRED")
        self.assertIn("other.example.invalid", json.dumps(result))

    def test_invalid_schema_fails_closed(self):
        supplier = deepcopy(load_fixture("single_video", "supplier"))
        del supplier["media_count"]
        self.assertEqual(compare_records(self.left, supplier)["classification"], "INVALID_INPUT")

    def test_unhashable_enum_values_fail_closed(self):
        mutations = (
            ("input_kind", ["offline_export"]),
            ("content_type", {"value": "video"}),
            ("outcome_class", ["success"]),
            ("error_class", {"value": "none"}),
        )
        for field, value in mutations:
            with self.subTest(field=field):
                supplier = deepcopy(load_fixture("single_video", "supplier"))
                supplier[field] = value
                self.assertEqual(compare_records(self.left, supplier)["classification"], "INVALID_INPUT")

    def test_unhashable_media_enum_values_fail_closed(self):
        for field in ("type", "role"):
            with self.subTest(field=field):
                supplier = deepcopy(load_fixture("single_video", "supplier"))
                supplier["media"][0][field] = ["video"]
                self.assertEqual(compare_records(self.left, supplier)["classification"], "INVALID_INPUT")

    def test_credential_requirement_needs_security_review(self):
        supplier = deepcopy(load_fixture("single_video", "supplier"))
        supplier["credential_required"] = True
        self.assertEqual(compare_records(self.left, supplier)["classification"], "SECURITY_REVIEW_REQUIRED")

    def test_hostname_normalizes_to_lowercase(self):
        supplier = deepcopy(load_fixture("single_video", "supplier"))
        supplier["media"][0]["hostname"] = "MEDIA.EXAMPLE.INVALID"
        self.assertEqual(validate_record(supplier)["media"][0]["hostname"], "media.example.invalid")

    def test_cli_writes_only_safe_comparison(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "comparison.json"
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = main([
                    str(FIXTURES / "single_video" / "animeimages.json"),
                    str(FIXTURES / "single_video" / "supplier.json"),
                    "--output",
                    str(output),
                ])
            self.assertEqual(exit_code, 0)
            self.assertEqual(json.loads(output.read_text(encoding="utf-8"))["classification"], "MATCH")
            self.assertIn("classification: MATCH", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
