import json
import re
from pathlib import Path

from django.test import SimpleTestCase


PROJECT_ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_PATH = (
    PROJECT_ROOT
    / "research"
    / "evidence"
    / "xhs_livephoto_payload_evidence_v1.json"
)
SHA256_FINGERPRINT_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
HOST_FAMILY_RE = re.compile(
    r"^[a-z0-9-]+(?:\*?[a-z0-9-]*)?(?:\.[a-z0-9-]+)+$"
)
SAMPLE_FIELDS = {
    "sample_id",
    "sample_type",
    "source_short_link",
    "target_identity_confirmed",
    "target_note_id",
    "payload_note_id",
    "item_count",
    "livephoto_item_count",
    "regular_image_item_count",
    "item_local_pair_evidence",
    "pair_evidence_basis",
    "negative_false_positive",
    "invalid_fail_closed",
    "still_host_family",
    "motion_host_family",
    "format_family",
    "mapper_ready_signal",
    "notes",
}


def load_evidence():
    return json.loads(EVIDENCE_PATH.read_text(encoding="utf-8"))


def has_direct_pair_evidence(item):
    return all(
        item.get(field) is True
        for field in (
            "same_item_owner",
            "livephoto_marker_present",
            "still_field_present",
            "motion_field_present",
        )
    )


class XhsLivePhotoRealPayloadEvidenceTests(SimpleTestCase):
    def setUp(self):
        self.evidence = load_evidence()
        self.samples = {
            sample["sample_id"]: sample
            for sample in self.evidence["samples"]
        }

    def test_evidence_schema_and_sample_inventory_are_fixed(self):
        self.assertEqual(self.evidence["schema_version"], 1)
        self.assertEqual(self.evidence["sample_count"], 5)
        self.assertEqual(
            set(self.samples),
            {
                "xhs-livephoto-01",
                "xhs-livephoto-02",
                "xhs-gallery-01",
                "xhs-video-01",
                "xhs-invalid-01",
            },
        )
        for sample in self.samples.values():
            self.assertEqual(set(sample), SAMPLE_FIELDS)

    def test_source_and_note_identity_values_are_fingerprints_only(self):
        for sample in self.samples.values():
            self.assertRegex(sample["source_short_link"], SHA256_FINGERPRINT_RE)
            self.assertRegex(sample["target_note_id"], SHA256_FINGERPRINT_RE)
            if sample["target_identity_confirmed"]:
                self.assertEqual(sample["payload_note_id"], sample["target_note_id"])
            else:
                self.assertIsNone(sample["payload_note_id"])

    def test_evidence_file_contains_no_url_query_or_secret_material(self):
        rendered = EVIDENCE_PATH.read_text(encoding="utf-8").lower()
        for forbidden in (
            "://",
            "xsec_",
            "cookie",
            "authorization",
            "signed_url",
            "masterurl\": \"http",
            "?",
        ):
            self.assertNotIn(forbidden, rendered)

    def test_two_positive_samples_have_direct_item_local_pairs(self):
        first = self.samples["xhs-livephoto-01"]
        second = self.samples["xhs-livephoto-02"]
        for sample in (first, second):
            self.assertTrue(sample["target_identity_confirmed"])
            self.assertTrue(sample["item_local_pair_evidence"])
            self.assertGreater(sample["livephoto_item_count"], 0)
            self.assertEqual(
                sample["pair_evidence_basis"],
                "same_item_live_marker_still_and_motion",
            )
            self.assertTrue(sample["mapper_ready_signal"])
        self.assertEqual(first["livephoto_item_count"], 14)
        self.assertEqual(second["livephoto_item_count"], 12)
        self.assertEqual(second["regular_image_item_count"], 5)

    def test_array_position_alone_is_not_pair_evidence(self):
        guessed = {
            "same_item_owner": False,
            "livephoto_marker_present": False,
            "still_field_present": True,
            "motion_field_present": True,
            "array_positions_equal": True,
        }
        self.assertFalse(has_direct_pair_evidence(guessed))
        self.assertFalse(self.evidence["pairing_policy"]["array_position_allowed"])

    def test_url_similarity_alone_is_not_pair_evidence(self):
        guessed = {
            "same_item_owner": False,
            "livephoto_marker_present": True,
            "still_field_present": True,
            "motion_field_present": True,
            "url_paths_similar": True,
        }
        self.assertFalse(has_direct_pair_evidence(guessed))
        self.assertFalse(self.evidence["pairing_policy"]["url_similarity_allowed"])

    def test_first_note_id_and_recommendations_have_no_authority(self):
        policy = self.evidence["identity_policy"]
        self.assertTrue(policy["requested_payload_identity_match_required"])
        self.assertFalse(policy["first_note_id_allowed"])
        self.assertFalse(policy["recommendation_payload_allowed"])

    def test_gallery_control_has_no_livephoto_false_positive(self):
        sample = self.samples["xhs-gallery-01"]
        self.assertEqual(sample["sample_type"], "image_gallery")
        self.assertEqual(sample["item_count"], 4)
        self.assertEqual(sample["livephoto_item_count"], 0)
        self.assertFalse(sample["item_local_pair_evidence"])
        self.assertFalse(sample["negative_false_positive"])

    def test_video_control_has_no_livephoto_false_positive(self):
        sample = self.samples["xhs-video-01"]
        self.assertEqual(sample["sample_type"], "video")
        self.assertEqual(sample["livephoto_item_count"], 0)
        self.assertFalse(sample["item_local_pair_evidence"])
        self.assertFalse(sample["negative_false_positive"])

    def test_invalid_sample_fails_closed_without_payload_identity(self):
        sample = self.samples["xhs-invalid-01"]
        self.assertEqual(sample["sample_type"], "invalid")
        self.assertFalse(sample["target_identity_confirmed"])
        self.assertIsNone(sample["payload_note_id"])
        self.assertEqual(sample["item_count"], 0)
        self.assertEqual(sample["livephoto_item_count"], 0)
        self.assertFalse(sample["item_local_pair_evidence"])
        self.assertTrue(sample["invalid_fail_closed"])

    def test_host_evidence_contains_hostname_families_only(self):
        host_groups = self.evidence["host_review"]
        for key in ("still_host_families", "motion_host_families"):
            for host in host_groups[key]:
                self.assertRegex(host, HOST_FAMILY_RE)
                self.assertNotIn("/", host)
                self.assertNotIn(":", host)
                self.assertNotIn("?", host)
        self.assertFalse(host_groups["allowlist_changed"])
        self.assertFalse(host_groups["download_authority_granted"])

    def test_format_evidence_is_bounded_and_does_not_save_media(self):
        review = self.evidence["format_review"]
        self.assertEqual(review["representative_still_signature_family"], "jpeg")
        self.assertEqual(
            review["representative_motion_signature_family"],
            "iso_bmff_mp4",
        )
        self.assertEqual(review["sample_method"], "bounded_first_128_bytes")
        self.assertFalse(review["media_saved"])
        self.assertFalse(review["payload_native_still_format_field_confirmed"])
        self.assertFalse(review["mapper_extension_population_authorized"])

    def test_mapper_readiness_is_derived_from_all_required_gates(self):
        gates = self.evidence["gate_results"]
        required = (
            gates["two_livephoto_samples_with_direct_pairs"],
            gates["gallery_false_positive_zero"],
            gates["video_false_positive_zero"],
            gates["invalid_sample_fail_closed"],
            not gates["heuristic_pairing_used"],
        )
        self.assertTrue(all(required))
        self.assertEqual(gates["mapper_implementation_ready"], all(required))
        self.assertFalse(gates["provider_integration_ready"])

    def test_runtime_capabilities_remain_disconnected(self):
        self.assertEqual(
            set(self.evidence["runtime_changes"].values()),
            {False},
        )
