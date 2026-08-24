import json
import re
from pathlib import Path

from django.test import SimpleTestCase


PROJECT_ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_PATH = (
    PROJECT_ROOT
    / "research"
    / "evidence"
    / "xhs_livephoto_candidate_format_host_v1.json"
)
HOST_FAMILY_RE = re.compile(r"^[a-z0-9-]+(?:[.*A-Z0-9-]*)?(?:\.[a-z0-9-]+)+$")


def load_evidence():
    return json.loads(EVIDENCE_PATH.read_text(encoding="utf-8"))


class XhsLivePhotoCandidateEvidenceTests(SimpleTestCase):
    def setUp(self):
        self.evidence = load_evidence()
        self.samples = {
            sample["sample_id"]: sample
            for sample in self.evidence["samples"]
        }

    def test_schema_and_sample_inventory_are_fixed(self):
        self.assertEqual(self.evidence["schema_version"], 1)
        self.assertEqual(
            self.evidence["locator_scope"],
            "same_selected_payload_snapshot",
        )
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

    def test_evidence_contains_no_complete_link_or_raw_note_identity(self):
        rendered = EVIDENCE_PATH.read_text(encoding="utf-8")
        self.assertNotIn("://", rendered)
        self.assertNotIn("xhslink", rendered.lower())
        self.assertIsNone(re.search(r"\b[0-9a-f]{24}\b", rendered, re.IGNORECASE))

    def test_evidence_contains_no_candidate_or_payload_values(self):
        collection = self.evidence["collection"]
        self.assertFalse(collection["raw_payload_saved"])
        self.assertFalse(collection["candidate_values_saved"])
        self.assertFalse(collection["full_media_saved"])
        self.assertFalse(collection["credentials_used"])

    def test_two_positive_samples_preserve_relationship_counts(self):
        first = self.samples["xhs-livephoto-01"]
        second = self.samples["xhs-livephoto-02"]

        self.assertEqual(
            (first["item_count"], first["livephoto_item_count"]),
            (14, 14),
        )
        self.assertEqual(
            (
                second["item_count"],
                second["livephoto_item_count"],
                second["regular_image_item_count"],
            ),
            (17, 12, 5),
        )
        self.assertTrue(first["candidate_selection_passed"])
        self.assertTrue(second["candidate_selection_passed"])

    def test_positive_selected_member_counts_are_item_local(self):
        positives = (
            self.samples["xhs-livephoto-01"],
            self.samples["xhs-livephoto-02"],
        )
        for sample in positives:
            self.assertEqual(
                sample["selected_still_count"],
                sample["item_count"],
            )
            self.assertEqual(
                sample["selected_motion_count"],
                sample["livephoto_item_count"],
            )

    def test_gallery_is_still_only_without_false_positive(self):
        gallery = self.samples["xhs-gallery-01"]

        self.assertEqual(gallery["selected_still_count"], 4)
        self.assertEqual(gallery["selected_motion_count"], 0)
        self.assertFalse(gallery["negative_false_positive"])

    def test_video_produces_no_livephoto_candidate_map(self):
        video = self.samples["xhs-video-01"]

        self.assertEqual(video["livephoto_item_count"], 0)
        self.assertEqual(video["selected_still_count"], 0)
        self.assertEqual(video["selected_motion_count"], 0)
        self.assertFalse(video["negative_false_positive"])

    def test_invalid_sample_has_no_locator_or_probe(self):
        invalid = self.samples["xhs-invalid-01"]

        self.assertFalse(invalid["target_identity_confirmed"])
        self.assertEqual(invalid["item_count"], 0)
        self.assertEqual(invalid["selected_still_count"], 0)
        self.assertEqual(invalid["selected_motion_count"], 0)
        self.assertTrue(invalid["invalid_fail_closed"])

    def test_still_policy_is_supported_by_full_reviewed_population(self):
        still = self.evidence["still_evidence"]

        self.assertEqual(still["image_note_item_count"], 35)
        self.assertEqual(still["url_default_coverage_count"], 35)
        self.assertEqual(still["info_list_coverage_count"], 35)
        self.assertEqual(still["url_default_equals_wb_dft_count"], 35)
        self.assertEqual(still["url_default_larger_than_wb_prv_count"], 35)
        self.assertEqual(still["selected_policy"], "url_default")
        self.assertEqual(
            still["fallback_policy"],
            "single_wb_dft_info_list_url",
        )
        self.assertEqual(
            still["drift_policy"],
            "all_non_empty_wb_dft_equal_url_default",
        )
        self.assertTrue(still["selection_ready"])

    def test_still_scene_values_are_closed_and_order_independent(self):
        self.assertEqual(
            set(self.evidence["still_evidence"]["reviewed_scene_values"]),
            {"WB_DFT", "WB_PRV"},
        )

    def test_motion_policy_does_not_use_first_codec_family(self):
        motion = self.evidence["motion_evidence"]

        self.assertEqual(
            set(motion["h264_codec_ordinal_values"]),
            {0, 1, 2, 3},
        )
        self.assertEqual(motion["codec_families_with_candidates"], ["h264"])
        self.assertEqual(
            motion["selected_codec_policy"],
            "closed_h264_to_avc_class",
        )

    def test_motion_variant_and_source_policies_are_deterministic(self):
        motion = self.evidence["motion_evidence"]

        self.assertEqual(motion["livephoto_item_count"], 26)
        self.assertEqual(motion["viable_variant_count_per_item"], 1)
        self.assertEqual(motion["master_coverage_count"], 26)
        self.assertEqual(motion["backup_candidate_count"], 52)
        self.assertEqual(
            motion["bounded_master_backup_equivalent_count"],
            52,
        )
        self.assertEqual(motion["selected_source_policy"], "master_url")
        self.assertEqual(
            motion["fallback_policy"],
            "first_backup_only_when_master_field_absent",
        )
        self.assertEqual(
            motion["present_invalid_master_policy"],
            "fail_closed",
        )

    def test_all_selected_still_and_motion_formats_are_magic_proven(self):
        still = self.evidence["still_evidence"]
        motion = self.evidence["motion_evidence"]
        review = self.evidence["format_review"]

        self.assertEqual(still["selected_format_counts"]["webp"], 35)
        self.assertEqual(motion["selected_format_counts"]["mp4"], 26)
        self.assertEqual(review["unknown_count"], 0)
        self.assertEqual(review["invalid_count"], 0)
        self.assertFalse(review["url_suffix_used_as_proof"])
        self.assertTrue(review["per_selected_candidate_format_proven"])

    def test_probe_accounting_matches_bounded_collection(self):
        collection = self.evidence["collection"]
        still = self.evidence["still_evidence"]
        review = self.evidence["format_review"]

        self.assertEqual(still["image_note_unique_candidate_probe_count"], 70)
        self.assertEqual(still["video_control_cover_probe_count"], 2)
        self.assertEqual(
            still["image_note_unique_candidate_probe_count"]
            + still["video_control_cover_probe_count"],
            review["still_probe_count"],
        )
        self.assertEqual(review["still_probe_count"], 72)
        self.assertEqual(review["motion_probe_count"], 78)
        self.assertEqual(
            review["still_probe_count"] + review["motion_probe_count"],
            collection["unique_candidate_probe_count"],
        )
        self.assertEqual(collection["bounded_prefix_bytes"], 4096)

    def test_probe_transport_observations_are_complete_or_explicitly_unretained(self):
        collection = self.evidence["collection"]
        probe = self.evidence["probe_observations"]
        total = collection["unique_candidate_probe_count"]

        self.assertEqual(sum(probe["http_status_counts"].values()), total)
        self.assertEqual(probe["response_total_size_observed_count"], total)
        self.assertEqual(
            probe["content_range_total_present_count"]
            + probe["content_length_total_used_count"],
            total,
        )
        self.assertEqual(
            sum(probe["first_byte_latency_bucket_counts"].values()),
            total,
        )
        self.assertEqual(
            probe["size_bucket_retained_count"]
            + probe["size_bucket_not_retained_count"],
            total,
        )
        self.assertEqual(
            sum(probe["size_bucket_counts"].values()),
            total,
        )
        self.assertEqual(probe["body_prefix_readable_count"], total)
        self.assertEqual(probe["server_observed_cors_wildcard_count"], total)
        self.assertEqual(probe["redirected_count"], 0)
        self.assertEqual(
            sum(probe["final_host_family_counts"].values()),
            total,
        )

    def test_host_values_are_family_labels_only(self):
        families = (
            self.evidence["still_evidence"]["host_families"]
            + self.evidence["motion_evidence"]["host_families"]
        )
        for family in families:
            self.assertRegex(family, HOST_FAMILY_RE)
            self.assertNotIn("/", family)
            self.assertNotIn(":", family)

    def test_host_review_records_http_payload_and_https_gap(self):
        host = self.evidence["host_review"]

        self.assertEqual(host["payload_scheme"], "http")
        self.assertTrue(host["https_upgrade_required_before_browser_use"])
        self.assertTrue(host["dns_public"])
        self.assertTrue(host["tls_valid"])
        self.assertFalse(host["production_allowlist_changed"])

    def test_browser_cors_is_not_falsely_marked_ready(self):
        host = self.evidence["host_review"]

        self.assertTrue(host["browser_fetch_attempted"])
        self.assertFalse(host["browser_fetch_completed"])
        self.assertEqual(
            host["browser_fetch_fixed_failure"],
            "browser_control_timeout",
        )
        self.assertFalse(host["browser_direct_download_ready"])
        self.assertEqual(
            host["host_review_status"],
            "partial_browser_cors_not_proven",
        )

    def test_asset_spec_and_provider_gates_remain_closed(self):
        readiness = self.evidence["readiness"]

        self.assertEqual(readiness["relationship_mapper"], "merged")
        self.assertTrue(readiness["candidate_selection_ready"])
        self.assertTrue(readiness["still_format_ready"])
        self.assertTrue(readiness["motion_format_ready"])
        self.assertFalse(readiness["host_review_ready"])
        self.assertFalse(readiness["asset_spec_mapper_ready"])
        self.assertFalse(readiness["provider_integration_ready"])
        self.assertEqual(readiness["current_provider_emission_count"], 0)

    def test_runtime_changes_are_all_false(self):
        self.assertEqual(set(self.evidence["runtime_changes"].values()), {False})
