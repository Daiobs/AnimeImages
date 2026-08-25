import json
import re
from pathlib import Path

from django.test import SimpleTestCase


PROJECT_ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_PATH = (
    PROJECT_ROOT
    / "research"
    / "evidence"
    / "xhs_livephoto_browser_transport_v1.json"
)
RUNTIME_FILES = (
    PROJECT_ROOT / "xhs_download" / "services" / "platforms" / "xiaohongshu.py",
    PROJECT_ROOT / "xhs_download" / "services" / "registry.py",
    PROJECT_ROOT / "xhs_download" / "views.py",
)


def load_evidence():
    return json.loads(EVIDENCE_PATH.read_text(encoding="utf-8"))


class XhsLivePhotoBrowserTransportEvidenceTests(SimpleTestCase):
    def setUp(self):
        self.evidence = load_evidence()
        self.samples = {
            sample["sample_id"]: sample
            for sample in self.evidence["samples"]
        }
        self.hosts = {
            row["host_class"]: row
            for row in self.evidence["host_matrix"]
        }

    def test_schema_and_reviewed_inventory_are_fixed(self):
        self.assertEqual(self.evidence["schema_version"], 1)
        self.assertEqual(self.evidence["collection"]["sample_count"], 5)
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
        self.assertEqual(
            set(self.hosts),
            {
                "still_exact",
                "motion_versioned",
                "motion_zl",
                "motion_backup",
            },
        )

    def test_evidence_contains_no_url_identity_or_signed_value(self):
        rendered = EVIDENCE_PATH.read_text(encoding="utf-8")
        lowered = rendered.lower()
        self.assertNotIn("://", rendered)
        self.assertNotIn("xhslink", lowered)
        self.assertNotIn("xsec_", lowered)
        self.assertNotIn("cookie", lowered)
        self.assertNotIn("authorization", lowered)
        self.assertIsNone(re.search(r"\b[0-9a-f]{24}\b", rendered, re.I))

    def test_collection_is_bounded_and_retains_no_private_media(self):
        collection = self.evidence["collection"]
        self.assertEqual(collection["bounded_prefix_bytes"], 4096)
        self.assertEqual(collection["retained_equivalence_candidate_count"], 6)
        self.assertEqual(collection["retained_browser_request_count"], 12)
        self.assertFalse(collection["full_media_saved"])
        self.assertFalse(collection["raw_payload_saved"])
        self.assertFalse(collection["candidate_values_saved"])
        self.assertFalse(collection["response_headers_saved"])
        self.assertFalse(collection["server_relay_used"])

    def test_normalization_policy_is_fail_closed_before_network(self):
        policy = self.evidence["normalization_policy"]
        self.assertEqual(policy["output_scheme"], "https")
        self.assertTrue(policy["signed_parameter_bytes_preserved"])
        self.assertTrue(policy["fragment_removed"])
        for key in (
            "userinfo_rejected",
            "ip_literal_rejected",
            "localhost_rejected",
            "trailing_dot_rejected",
            "lookalike_rejected",
            "unreviewed_host_rejected",
        ):
            self.assertTrue(policy[key])
        self.assertEqual(policy["unknown_host_network_requests"], 0)

    def test_discovered_and_browser_verified_host_inventories_are_distinct(self):
        policy = self.evidence["host_class_policy"]
        self.assertEqual(
            policy["discovered_host_classes"],
            [
                "still_exact",
                "motion_versioned",
                "motion_zl",
                "motion_backup",
            ],
        )
        self.assertEqual(
            policy["browser_verified_host_classes"],
            ["still_exact", "motion_versioned", "motion_backup"],
        )
        self.assertEqual(policy["motion_zl_status"], "discovered_unverified")

    def test_three_observed_host_classes_pass_equivalence_and_browser_body(self):
        for host_class in (
            "still_exact",
            "motion_versioned",
            "motion_backup",
        ):
            with self.subTest(host_class=host_class):
                row = self.hosts[host_class]
                self.assertEqual(row["discovery_status"], "discovered_verified")
                self.assertEqual(row["browser_admission"], "admitted")
                self.assertEqual(row["representative_count"], 2)
                self.assertEqual(row["https_normalization"], "pass")
                self.assertEqual(row["http_https_equivalence"], "pass")
                self.assertEqual(row["prefix_sha256_match_count"], 2)
                self.assertEqual(row["reported_total_match_count"], 2)
                self.assertEqual(row["browser_response_types"], ["cors"])
                self.assertEqual(row["browser_body_readable_count"], 2)
                self.assertEqual(row["final_host_classes"], [host_class])
                self.assertEqual(row["status"], "pass")

    def test_numeric_host_classes_have_two_observed_labels_where_available(self):
        self.assertEqual(
            self.hosts["motion_versioned"]["distinct_numeric_label_count"],
            2,
        )
        self.assertEqual(
            self.hosts["motion_backup"]["distinct_numeric_label_count"],
            2,
        )

    def test_motion_range_body_is_readable_without_content_range_exposure(self):
        for host_class in ("motion_versioned", "motion_backup"):
            with self.subTest(host_class=host_class):
                row = self.hosts[host_class]
                self.assertEqual(row["browser_range_fetch_pass_count"], 2)
                self.assertEqual(row["range_result"], "partial_206")
                self.assertEqual(row["content_range_exposed_count"], 0)

    def test_still_range_ignore_remains_bounded_and_magic_proven(self):
        still = self.hosts["still_exact"]
        self.assertEqual(still["format_classes"], ["jpeg"])
        self.assertEqual(still["range_result"], "ignored_bounded_200")
        self.assertEqual(still["browser_normal_fetch_pass_count"], 2)

    def test_motion_zl_gap_is_not_inferred_from_other_host_classes(self):
        zl = self.hosts["motion_zl"]
        self.assertTrue(zl["payload_observed"])
        self.assertEqual(zl["discovery_status"], "discovered_unverified")
        self.assertEqual(zl["browser_admission"], "rejected")
        self.assertEqual(zl["representative_count"], 0)
        self.assertEqual(
            zl["https_normalization"],
            "rejected_unverified_host_class",
        )
        self.assertEqual(
            zl["http_https_equivalence"],
            "not_run_fresh_candidate_unavailable",
        )
        self.assertEqual(zl["browser_body_readable_count"], 0)
        self.assertEqual(
            zl["status"],
            "discovered_unverified",
        )

    def test_gallery_video_and_invalid_controls_remain_fail_closed(self):
        controls = self.evidence["negative_controls"]
        video = self.samples["xhs-video-01"]
        self.assertTrue(controls["gallery_still_fetch_passed"])
        self.assertEqual(controls["gallery_motion_fetch_count"], 0)
        self.assertTrue(controls["video_cover_fetch_passed"])
        self.assertEqual(controls["video_livephoto_fetch_count"], 0)
        self.assertEqual(controls["invalid_media_request_count"], 0)
        self.assertFalse(self.samples["xhs-gallery-01"]["negative_false_positive"])
        self.assertEqual(video["regular_image_item_count"], 0)
        self.assertEqual(video["selected_still_count"], 0)
        self.assertEqual(video["selected_motion_count"], 0)
        self.assertFalse(video["negative_false_positive"])
        self.assertTrue(self.samples["xhs-invalid-01"]["invalid_fail_closed"])

    def test_verified_subset_is_ready_while_full_observed_coverage_is_closed(self):
        readiness = self.evidence["readiness"]
        for key in (
            "relationship_mapper_ready",
            "candidate_selection_ready",
            "still_format_ready",
            "motion_format_ready",
            "https_normalization_ready",
            "verified_browser_host_subset_ready",
            "http_https_equivalence_ready_for_verified_subset",
            "browser_cors_ready_for_verified_subset",
            "browser_body_readable_ready_for_verified_subset",
            "host_review_ready_for_verified_subset",
            "asset_spec_mapper_ready_for_verified_subset",
        ):
            self.assertTrue(readiness[key])
        for key in (
            "full_observed_host_coverage_ready",
            "http_https_equivalence_ready_for_all_discovered_hosts",
            "browser_cors_ready_for_all_discovered_hosts",
            "browser_body_readable_ready_for_all_discovered_hosts",
            "host_review_ready_for_all_discovered_hosts",
            "raw_http_browser_ready",
            "provider_integration_ready",
        ):
            self.assertFalse(readiness[key])
        self.assertEqual(readiness["motion_zl_status"], "discovered_unverified")
        self.assertEqual(
            readiness["full_observed_host_coverage_blocker"],
            "required_motion_zl_browser_evidence_unavailable",
        )

    def test_runtime_remains_disconnected_from_dormant_admission(self):
        self.assertEqual(set(self.evidence["runtime_changes"].values()), {False})
        module_name = "xhs_livephoto_browser_admission"
        for path in RUNTIME_FILES:
            self.assertNotIn(module_name, path.read_text(encoding="utf-8"))
