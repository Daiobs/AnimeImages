from __future__ import annotations

import ast
import copy
import json
import unittest
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from unittest.mock import patch

from xhs_download.services.upstream_observation import (
    MAX_ATTEMPTS,
    MAX_COUNTER_VALUE,
    MAX_ELAPSED_MS,
    MAX_ID_LENGTH,
    MAX_LINEAGE_NODES,
    MAX_PROVIDER_VERSION_LENGTH,
    MAX_REQUIREMENTS,
    MAX_SERIALIZED_BYTES,
    MAX_WARNING_CODES,
    AcquisitionRequirementV1,
    AssemblyRequirementV1,
    AttemptFailureClassV1,
    AttemptFallbackReasonV1,
    AttemptOutcomeV1,
    AttemptTraceV1,
    BackendActorTypeV1,
    BackendCapabilityTypeV1,
    BackendLineageNodeV1,
    EvidenceStageV1,
    LineageDataSharedV1,
    LineageTrustScopeV1,
    RelationshipEvidenceV1,
    ResolutionModeV1,
    ResolutionStageV1,
    UpstreamObservationV1,
    UpstreamObservationValidationError,
    from_json,
    from_primitive,
    to_json,
    to_primitive,
)


def make_lineage(
    *,
    backend_id: str = "example.video_adapter",
    backend_type: BackendCapabilityTypeV1 = (
        BackendCapabilityTypeV1.DEDICATED_SOURCE_ADAPTER
    ),
) -> tuple[BackendLineageNodeV1, ...]:
    return (
        BackendLineageNodeV1(
            ordinal=1,
            parent_ordinal=None,
            actor_id="animeimages",
            actor_type=BackendActorTypeV1.CONTAINER,
            backend_type=None,
            provider_version="1.0",
            trust_scope=LineageTrustScopeV1.INTERNAL,
            data_shared=LineageDataSharedV1.NONE,
        ),
        BackendLineageNodeV1(
            ordinal=2,
            parent_ordinal=1,
            actor_id=backend_id,
            actor_type=BackendActorTypeV1.ADAPTER,
            backend_type=backend_type,
            provider_version="commit123",
            trust_scope=LineageTrustScopeV1.STAFF_LAB,
            data_shared=LineageDataSharedV1.SOURCE_REFERENCE,
        ),
    )


def make_attempt(**overrides: object) -> AttemptTraceV1:
    values: dict[str, object] = {
        "ordinal": 1,
        "stage": ResolutionStageV1.DEDICATED_SOURCE,
        "backend_id": "example.video_adapter",
        "backend_type": BackendCapabilityTypeV1.DEDICATED_SOURCE_ADAPTER,
        "started_offset_ms": 0,
        "elapsed_ms": 25,
        "outcome": AttemptOutcomeV1.SUCCESS,
        "failure_class": None,
        "asset_count": 1,
        "format_count": 1,
        "relationship_count": 0,
        "evidence_stage": EvidenceStageV1.MEDIA_CANDIDATES,
        "relationship_evidence": RelationshipEvidenceV1.NOT_APPLICABLE,
        "fallback_reason": AttemptFallbackReasonV1.NONE,
        "lineage_leaf_ordinal": 2,
    }
    values.update(overrides)
    return AttemptTraceV1(**values)


def make_failed_attempt(**overrides: object) -> AttemptTraceV1:
    values: dict[str, object] = {
        "ordinal": 1,
        "stage": ResolutionStageV1.DEDICATED_SOURCE,
        "backend_id": "example.video_adapter",
        "backend_type": BackendCapabilityTypeV1.DEDICATED_SOURCE_ADAPTER,
        "started_offset_ms": 0,
        "elapsed_ms": 25,
        "outcome": AttemptOutcomeV1.FAILED,
        "failure_class": AttemptFailureClassV1.NETWORK_FAILURE,
        "asset_count": 0,
        "format_count": 0,
        "relationship_count": 0,
        "evidence_stage": EvidenceStageV1.METADATA_ONLY,
        "relationship_evidence": RelationshipEvidenceV1.NOT_APPLICABLE,
        "fallback_reason": AttemptFallbackReasonV1.BACKEND_FAILED,
        "lineage_leaf_ordinal": 2,
    }
    values.update(overrides)
    return AttemptTraceV1(**values)


def make_skipped_attempt(**overrides: object) -> AttemptTraceV1:
    values: dict[str, object] = {
        "ordinal": 1,
        "stage": ResolutionStageV1.DEDICATED_SOURCE,
        "backend_id": "example.video_adapter",
        "backend_type": BackendCapabilityTypeV1.DEDICATED_SOURCE_ADAPTER,
        "started_offset_ms": 0,
        "elapsed_ms": 0,
        "outcome": AttemptOutcomeV1.SKIPPED,
        "failure_class": AttemptFailureClassV1.NOT_APPLICABLE,
        "asset_count": 0,
        "format_count": 0,
        "relationship_count": 0,
        "evidence_stage": EvidenceStageV1.DETECTED,
        "relationship_evidence": RelationshipEvidenceV1.NOT_APPLICABLE,
        "fallback_reason": AttemptFallbackReasonV1.NO_APPLICABLE_BACKEND,
        "lineage_leaf_ordinal": 2,
    }
    values.update(overrides)
    return AttemptTraceV1(**values)


def make_observation(**overrides: object) -> UpstreamObservationV1:
    values: dict[str, object] = {
        "schema_version": "1.0",
        "request_id": "request_1",
        "top_level_provider": "example_repo",
        "provider_version": "commit123",
        "resolution_mode": ResolutionModeV1.SPECIFIC_BACKEND,
        "resolution_stage": ResolutionStageV1.DEDICATED_SOURCE,
        "selected_attempt_ordinal": 1,
        "lineage": make_lineage(),
        "attempts": (make_attempt(),),
        "evidence_stage": EvidenceStageV1.MEDIA_CANDIDATES,
        "relationship_evidence": RelationshipEvidenceV1.NOT_APPLICABLE,
        "candidate_count": 1,
        "video_track_count": 1,
        "audio_track_count": 1,
        "relationship_count": 0,
        "acquisition_requirements": (),
        "assembly_requirements": (AssemblyRequirementV1.DIRECT_SINGLE_FILE,),
        "warning_codes": (),
        "error_code": None,
    }
    values.update(overrides)
    return UpstreamObservationV1(**values)


def make_failure_observation(**overrides: object) -> UpstreamObservationV1:
    values: dict[str, object] = {
        "schema_version": "1.0",
        "request_id": "request_failed",
        "top_level_provider": "example_repo",
        "provider_version": "commit123",
        "resolution_mode": ResolutionModeV1.AUTO,
        "resolution_stage": ResolutionStageV1.DEDICATED_SOURCE,
        "selected_attempt_ordinal": None,
        "lineage": (make_lineage()[0],),
        "attempts": (),
        "evidence_stage": EvidenceStageV1.DETECTED,
        "relationship_evidence": RelationshipEvidenceV1.NOT_APPLICABLE,
        "candidate_count": 0,
        "video_track_count": 0,
        "audio_track_count": 0,
        "relationship_count": 0,
        "acquisition_requirements": (),
        "assembly_requirements": (),
        "warning_codes": (),
        "error_code": "NO_VALID_MEDIA",
    }
    values.update(overrides)
    return UpstreamObservationV1(**values)


def complete_mp4_fixture() -> UpstreamObservationV1:
    return make_observation()


def separate_av_fixture() -> UpstreamObservationV1:
    backend_id = "example.track_adapter"
    backend_type = BackendCapabilityTypeV1.FORMAT_INVENTORY_EXTRACTOR
    attempt = make_attempt(
        backend_id=backend_id,
        backend_type=backend_type,
        asset_count=2,
        format_count=2,
        relationship_count=1,
        relationship_evidence=RelationshipEvidenceV1.PRESERVED,
    )
    return make_observation(
        lineage=make_lineage(backend_id=backend_id, backend_type=backend_type),
        attempts=(attempt,),
        relationship_evidence=RelationshipEvidenceV1.PRESERVED,
        candidate_count=2,
        video_track_count=1,
        audio_track_count=1,
        relationship_count=1,
        assembly_requirements=(AssemblyRequirementV1.SEPARATE_VIDEO_AUDIO,),
    )


def livephoto_fixture() -> UpstreamObservationV1:
    backend_id = "example.rich_media_adapter"
    backend_type = BackendCapabilityTypeV1.RICH_MEDIA_RELATIONSHIP_PROVIDER
    attempt = make_attempt(
        backend_id=backend_id,
        backend_type=backend_type,
        asset_count=2,
        format_count=2,
        relationship_count=1,
        relationship_evidence=RelationshipEvidenceV1.PRESERVED,
    )
    return make_observation(
        lineage=make_lineage(backend_id=backend_id, backend_type=backend_type),
        attempts=(attempt,),
        relationship_evidence=RelationshipEvidenceV1.PRESERVED,
        candidate_count=2,
        video_track_count=0,
        audio_track_count=0,
        relationship_count=1,
        assembly_requirements=(),
    )


def nested_aggregator_lineage_fixture() -> UpstreamObservationV1:
    backend_id = "videodl.KuKuToolVideoClient"
    backend_type = BackendCapabilityTypeV1.AGGREGATOR_API
    lineage = (
        make_lineage()[0],
        BackendLineageNodeV1(
            ordinal=2,
            parent_ordinal=1,
            actor_id="videodl_repo",
            actor_type=BackendActorTypeV1.CONTAINER,
            backend_type=None,
            provider_version="commit123",
            trust_scope=LineageTrustScopeV1.STAFF_LAB,
            data_shared=LineageDataSharedV1.SOURCE_REFERENCE,
        ),
        BackendLineageNodeV1(
            ordinal=3,
            parent_ordinal=2,
            actor_id=backend_id,
            actor_type=BackendActorTypeV1.ADAPTER,
            backend_type=backend_type,
            provider_version="commit123",
            trust_scope=LineageTrustScopeV1.STAFF_LAB,
            data_shared=LineageDataSharedV1.SOURCE_REFERENCE,
        ),
        BackendLineageNodeV1(
            ordinal=4,
            parent_ordinal=3,
            actor_id="kukutool.service",
            actor_type=BackendActorTypeV1.EXTERNAL_SERVICE,
            backend_type=backend_type,
            provider_version="1.0",
            trust_scope=LineageTrustScopeV1.EXTERNAL,
            data_shared=LineageDataSharedV1.SOURCE_REFERENCE,
        ),
    )
    attempt = make_attempt(
        stage=ResolutionStageV1.COMMON_AGGREGATOR,
        backend_id=backend_id,
        backend_type=backend_type,
        lineage_leaf_ordinal=4,
    )
    return make_observation(
        top_level_provider="videodl_repo",
        resolution_stage=ResolutionStageV1.COMMON_AGGREGATOR,
        lineage=lineage,
        attempts=(attempt,),
    )


class ValidationTestCase(unittest.TestCase):
    def assert_invalid(self, code: str, callback) -> UpstreamObservationValidationError:
        with self.assertRaises(UpstreamObservationValidationError) as raised:
            callback()
        self.assertEqual(raised.exception.code, code)
        return raised.exception


class EnumAndBoundTests(ValidationTestCase):
    def test_core_enum_values_are_fixed(self):
        self.assertEqual(
            [item.value for item in ResolutionStageV1],
            [
                "direct_media",
                "dedicated_source",
                "common_aggregator",
                "static_extractor",
                "browser_observer",
            ],
        )
        self.assertEqual(
            [item.value for item in EvidenceStageV1],
            [
                "DETECTED",
                "METADATA_ONLY",
                "MEDIA_CANDIDATES",
                "BROWSER_INSPECTABLE",
                "FULL_MEDIA_VERIFIED",
            ],
        )

    def test_supporting_enum_values_are_fixed(self):
        self.assertEqual(ResolutionModeV1.GRABBER_ONLY.value, "GRABBER_ONLY")
        self.assertEqual(BackendActorTypeV1.EXTERNAL_SERVICE.value, "external_service")
        self.assertEqual(AttemptOutcomeV1.POLICY_REJECTED.value, "policy_rejected")
        self.assertEqual(LineageDataSharedV1.PRIVATE_RESOURCE_CONTEXT.value, "private_resource_context")

    def test_unknown_enum_fails_closed(self):
        primitive = make_observation().to_primitive()
        primitive["evidence_stage"] = "UNKNOWN"
        self.assert_invalid("INVALID_ENUM", lambda: from_primitive(primitive))

    def test_enum_case_mismatch_fails_closed(self):
        primitive = make_observation().to_primitive()
        primitive["resolution_stage"] = "DEDICATED_SOURCE"
        self.assert_invalid("INVALID_ENUM", lambda: from_primitive(primitive))

    def test_empty_enum_fails_closed(self):
        primitive = make_observation().to_primitive()
        primitive["resolution_mode"] = ""
        self.assert_invalid("INVALID_ENUM", lambda: from_primitive(primitive))

    def test_bounds_are_named_and_positive(self):
        bounds = (
            MAX_ID_LENGTH,
            MAX_PROVIDER_VERSION_LENGTH,
            MAX_LINEAGE_NODES,
            MAX_ATTEMPTS,
            MAX_REQUIREMENTS,
            MAX_WARNING_CODES,
            MAX_SERIALIZED_BYTES,
            MAX_COUNTER_VALUE,
            MAX_ELAPSED_MS,
        )
        self.assertTrue(all(type(value) is int and value > 0 for value in bounds))

    def test_legal_identifier_forms_are_preserved(self):
        for actor_id in (
            "videodl_repo",
            "videodl.BilibiliVideoClient",
            "provider@commit",
            "backend:adapter",
        ):
            with self.subTest(actor_id=actor_id):
                self.assertEqual(
                    replace(make_lineage()[1], actor_id=actor_id).actor_id,
                    actor_id,
                )

    def test_empty_identifier_is_rejected(self):
        self.assert_invalid(
            "INVALID_IDENTIFIER",
            lambda: replace(make_lineage()[1], actor_id=""),
        )

    def test_oversized_identifier_is_rejected(self):
        self.assert_invalid(
            "INVALID_IDENTIFIER",
            lambda: replace(make_lineage()[1], actor_id="a" * (MAX_ID_LENGTH + 1)),
        )

    def test_identifier_spaces_and_slashes_are_rejected(self):
        for value in ("bad id", "bad/id", "bad\\id"):
            with self.subTest(value=value):
                self.assert_invalid(
                    "INVALID_IDENTIFIER",
                    lambda value=value: replace(make_lineage()[1], actor_id=value),
                )

    def test_oversized_provider_version_is_rejected(self):
        self.assert_invalid(
            "INVALID_IDENTIFIER",
            lambda: replace(
                make_lineage()[1],
                provider_version="v" * (MAX_PROVIDER_VERSION_LENGTH + 1),
            ),
        )

    def test_bool_does_not_masquerade_as_integer(self):
        self.assert_invalid(
            "INVALID_TYPE",
            lambda: replace(make_attempt(), elapsed_ms=True),
        )

    def test_counter_bound_is_enforced(self):
        self.assert_invalid(
            "BOUND_EXCEEDED",
            lambda: replace(make_attempt(), format_count=MAX_COUNTER_VALUE + 1),
        )

    def test_elapsed_bound_is_enforced(self):
        self.assert_invalid(
            "BOUND_EXCEEDED",
            lambda: replace(make_attempt(), elapsed_ms=MAX_ELAPSED_MS + 1),
        )

    def test_error_is_fixed_and_does_not_echo_input(self):
        error = UpstreamObservationValidationError(
            "INVALID_IDENTIFIER",
            "https://private.invalid/value",
        )
        self.assertEqual(error.code, "INVALID_IDENTIFIER")
        self.assertIsNone(error.field_name)
        self.assertNotIn("private", str(error))

    def test_contracts_are_frozen_slotted_and_hashable(self):
        values = (make_lineage()[0], make_attempt(), make_observation())
        for value in values:
            with self.subTest(value=type(value).__name__):
                self.assertFalse(hasattr(value, "__dict__"))
                self.assertIsInstance(hash(value), int)
                with self.assertRaises(FrozenInstanceError):
                    value.ordinal = 99


class LineageTests(ValidationTestCase):
    def test_valid_lineage_has_one_root_and_one_leaf(self):
        observation = make_observation()
        self.assertEqual(sum(node.parent_ordinal is None for node in observation.lineage), 1)
        self.assertEqual(observation.attempts[0].lineage_leaf_ordinal, 2)

    def test_empty_lineage_is_rejected(self):
        self.assert_invalid("INVALID_LINEAGE", lambda: make_observation(lineage=()))

    def test_lineage_without_root_is_rejected(self):
        child = replace(make_lineage()[1], ordinal=2, parent_ordinal=1)
        self.assert_invalid(
            "INVALID_LINEAGE",
            lambda: make_failure_observation(lineage=(child,)),
        )

    def test_multiple_roots_are_rejected(self):
        second_root = replace(
            make_lineage()[0],
            ordinal=2,
            actor_id="another.container",
        )
        self.assert_invalid(
            "INVALID_LINEAGE",
            lambda: make_failure_observation(
                lineage=(make_lineage()[0], second_root)
            ),
        )

    def test_duplicate_ordinal_is_rejected(self):
        duplicate = replace(make_lineage()[0], actor_id="duplicate.container")
        self.assert_invalid(
            "INVALID_LINEAGE",
            lambda: make_failure_observation(
                lineage=(make_lineage()[0], duplicate)
            ),
        )

    def test_missing_parent_is_rejected(self):
        child = replace(make_lineage()[1], ordinal=3, parent_ordinal=2)
        self.assert_invalid(
            "INVALID_LINEAGE",
            lambda: make_failure_observation(lineage=(make_lineage()[0], child)),
        )

    def test_self_parent_is_rejected(self):
        self.assert_invalid(
            "INVALID_LINEAGE",
            lambda: replace(make_lineage()[1], parent_ordinal=2),
        )

    def test_parent_after_child_is_rejected(self):
        self.assert_invalid(
            "INVALID_LINEAGE",
            lambda: replace(make_lineage()[1], parent_ordinal=3),
        )

    def test_cycles_are_impossible_under_parent_order(self):
        self.assert_invalid(
            "INVALID_LINEAGE",
            lambda: BackendLineageNodeV1(
                ordinal=1,
                parent_ordinal=2,
                actor_id="cycle.adapter",
                actor_type=BackendActorTypeV1.ADAPTER,
                backend_type=BackendCapabilityTypeV1.DEDICATED_SOURCE_ADAPTER,
                provider_version=None,
                trust_scope=LineageTrustScopeV1.STAFF_LAB,
                data_shared=LineageDataSharedV1.NONE,
            ),
        )

    def test_repeated_actor_ids_across_branches_are_valid(self):
        root = make_lineage()[0]
        dedicated_adapter = replace(
            make_lineage()[1],
            actor_id="dedicated.adapter",
        )
        dedicated_service = BackendLineageNodeV1(
            ordinal=3,
            parent_ordinal=2,
            actor_id="anyfetcher.service",
            actor_type=BackendActorTypeV1.EXTERNAL_SERVICE,
            backend_type=BackendCapabilityTypeV1.AGGREGATOR_API,
            provider_version="1.0",
            trust_scope=LineageTrustScopeV1.EXTERNAL,
            data_shared=LineageDataSharedV1.SOURCE_REFERENCE,
        )
        common_adapter = BackendLineageNodeV1(
            ordinal=4,
            parent_ordinal=1,
            actor_id="common.adapter",
            actor_type=BackendActorTypeV1.ADAPTER,
            backend_type=BackendCapabilityTypeV1.AGGREGATOR_API,
            provider_version="1.0",
            trust_scope=LineageTrustScopeV1.STAFF_LAB,
            data_shared=LineageDataSharedV1.SOURCE_REFERENCE,
        )
        common_service = replace(
            dedicated_service,
            ordinal=5,
            parent_ordinal=4,
        )
        attempt = make_attempt(
            backend_id="dedicated.adapter",
            lineage_leaf_ordinal=3,
        )
        observation = make_observation(
            lineage=(
                root,
                dedicated_adapter,
                dedicated_service,
                common_adapter,
                common_service,
            ),
            attempts=(attempt,),
        )
        repeated = [
            node
            for node in observation.lineage
            if node.actor_id == "anyfetcher.service"
        ]
        self.assertEqual([node.ordinal for node in repeated], [3, 5])

    def test_nested_adapter_backend_maps_to_terminal_service_leaf(self):
        observation = nested_aggregator_lineage_fixture()
        attempt = observation.attempts[0]
        self.assertEqual(attempt.backend_id, "videodl.KuKuToolVideoClient")
        self.assertEqual(attempt.lineage_leaf_ordinal, 4)
        self.assertEqual(observation.lineage[-1].actor_id, "kukutool.service")

    def test_backend_outside_selected_leaf_ancestor_path_is_rejected(self):
        observation = nested_aggregator_lineage_fixture()
        sibling = BackendLineageNodeV1(
            ordinal=5,
            parent_ordinal=1,
            actor_id="sibling.adapter",
            actor_type=BackendActorTypeV1.ADAPTER,
            backend_type=BackendCapabilityTypeV1.AGGREGATOR_API,
            provider_version="1.0",
            trust_scope=LineageTrustScopeV1.STAFF_LAB,
            data_shared=LineageDataSharedV1.SOURCE_REFERENCE,
        )
        attempt = replace(observation.attempts[0], backend_id="sibling.adapter")
        self.assert_invalid(
            "INVALID_ATTEMPT",
            lambda: make_observation(
                resolution_stage=ResolutionStageV1.COMMON_AGGREGATOR,
                lineage=observation.lineage + (sibling,),
                attempts=(attempt,),
            ),
        )

    def test_backend_type_must_match_selected_ancestor(self):
        observation = nested_aggregator_lineage_fixture()
        attempt = replace(
            observation.attempts[0],
            backend_type=BackendCapabilityTypeV1.DEDICATED_SOURCE_ADAPTER,
        )
        self.assert_invalid(
            "INVALID_ATTEMPT",
            lambda: make_observation(
                resolution_stage=ResolutionStageV1.COMMON_AGGREGATOR,
                lineage=observation.lineage,
                attempts=(attempt,),
            ),
        )

    def test_duplicate_backend_on_selected_ancestor_path_is_ambiguous(self):
        observation = nested_aggregator_lineage_fixture()
        duplicate_backend_leaf = replace(
            observation.lineage[-1],
            actor_id=observation.attempts[0].backend_id,
        )
        self.assert_invalid(
            "INVALID_ATTEMPT",
            lambda: make_observation(
                resolution_stage=ResolutionStageV1.COMMON_AGGREGATOR,
                lineage=observation.lineage[:-1] + (duplicate_backend_leaf,),
                attempts=observation.attempts,
            ),
        )

    def test_missing_selected_leaf_is_rejected(self):
        attempt = replace(make_attempt(), lineage_leaf_ordinal=3)
        self.assert_invalid(
            "INVALID_ATTEMPT",
            lambda: make_observation(attempts=(attempt,)),
        )

    def test_non_leaf_attempt_link_is_rejected(self):
        external = BackendLineageNodeV1(
            ordinal=3,
            parent_ordinal=2,
            actor_id="example.external",
            actor_type=BackendActorTypeV1.EXTERNAL_SERVICE,
            backend_type=BackendCapabilityTypeV1.AGGREGATOR_API,
            provider_version="1.0",
            trust_scope=LineageTrustScopeV1.EXTERNAL,
            data_shared=LineageDataSharedV1.SOURCE_REFERENCE,
        )
        self.assert_invalid(
            "INVALID_ATTEMPT",
            lambda: make_observation(lineage=make_lineage() + (external,)),
        )

    def test_attempt_backend_id_must_match_selected_lineage_path(self):
        attempt = replace(make_attempt(), backend_id="other.adapter")
        self.assert_invalid(
            "INVALID_ATTEMPT",
            lambda: make_observation(attempts=(attempt,)),
        )

    def test_attempt_backend_type_must_match_selected_lineage_path(self):
        attempt = replace(
            make_attempt(),
            backend_type=BackendCapabilityTypeV1.AGGREGATOR_API,
        )
        self.assert_invalid(
            "INVALID_ATTEMPT",
            lambda: make_observation(attempts=(attempt,)),
        )

    def test_container_cannot_claim_backend_capability(self):
        self.assert_invalid(
            "INVALID_LINEAGE",
            lambda: replace(
                make_lineage()[0],
                backend_type=BackendCapabilityTypeV1.DEDICATED_SOURCE_ADAPTER,
            ),
        )

    def test_non_container_requires_backend_capability(self):
        self.assert_invalid(
            "INVALID_ENUM",
            lambda: replace(make_lineage()[1], backend_type=None),
        )

    def test_lineage_node_bound_is_enforced(self):
        nodes = [make_lineage()[0]]
        for ordinal in range(2, MAX_LINEAGE_NODES + 2):
            nodes.append(
                replace(
                    make_lineage()[1],
                    ordinal=ordinal,
                    parent_ordinal=ordinal - 1,
                    actor_id=f"adapter.{ordinal}",
                )
            )
        self.assert_invalid(
            "BOUND_EXCEEDED",
            lambda: make_failure_observation(lineage=tuple(nodes)),
        )


class AttemptTests(ValidationTestCase):
    def test_success_attempt_is_valid(self):
        self.assertEqual(make_attempt().outcome, AttemptOutcomeV1.SUCCESS)

    def test_fallback_reason_is_always_a_non_null_enum(self):
        attempts = (
            make_attempt(),
            make_failed_attempt(),
            replace(
                make_failed_attempt(),
                outcome=AttemptOutcomeV1.TIMEOUT,
                failure_class=AttemptFailureClassV1.TIMEOUT,
                fallback_reason=AttemptFallbackReasonV1.BACKEND_TIMED_OUT,
            ),
            make_skipped_attempt(),
            replace(
                make_failed_attempt(),
                outcome=AttemptOutcomeV1.POLICY_REJECTED,
                failure_class=AttemptFailureClassV1.POLICY_REJECTED,
                fallback_reason=AttemptFallbackReasonV1.RESULT_REJECTED,
            ),
        )
        self.assertTrue(
            all(
                type(attempt.fallback_reason) is AttemptFallbackReasonV1
                for attempt in attempts
            )
        )

    def test_null_fallback_reason_is_rejected(self):
        self.assert_invalid(
            "INVALID_ENUM",
            lambda: replace(make_attempt(), fallback_reason=None),
        )

    def test_negative_elapsed_is_rejected(self):
        self.assert_invalid(
            "BOUND_EXCEEDED",
            lambda: replace(make_attempt(), elapsed_ms=-1),
        )

    def test_success_with_failure_class_is_rejected(self):
        self.assert_invalid(
            "INVALID_ATTEMPT",
            lambda: replace(
                make_attempt(),
                failure_class=AttemptFailureClassV1.NETWORK_FAILURE,
            ),
        )

    def test_failed_without_failure_class_is_rejected(self):
        self.assert_invalid(
            "INVALID_ATTEMPT",
            lambda: replace(make_failed_attempt(), failure_class=None),
        )

    def test_failed_with_assets_is_rejected(self):
        self.assert_invalid(
            "INVALID_ATTEMPT",
            lambda: replace(make_failed_attempt(), asset_count=1),
        )

    def test_timeout_requires_timeout_class(self):
        self.assert_invalid(
            "INVALID_ATTEMPT",
            lambda: replace(
                make_failed_attempt(),
                outcome=AttemptOutcomeV1.TIMEOUT,
            ),
        )

    def test_timeout_with_fixed_class_is_valid(self):
        attempt = replace(
            make_failed_attempt(),
            outcome=AttemptOutcomeV1.TIMEOUT,
            failure_class=AttemptFailureClassV1.TIMEOUT,
            fallback_reason=AttemptFallbackReasonV1.BACKEND_TIMED_OUT,
        )
        self.assertEqual(attempt.outcome, AttemptOutcomeV1.TIMEOUT)

    def test_skipped_requires_zero_elapsed(self):
        self.assert_invalid(
            "INVALID_ATTEMPT",
            lambda: replace(make_skipped_attempt(), elapsed_ms=1),
        )

    def test_skipped_cannot_report_assets(self):
        self.assert_invalid(
            "INVALID_ATTEMPT",
            lambda: replace(make_skipped_attempt(), asset_count=1),
        )

    def test_skipped_requires_policy_safe_class(self):
        self.assert_invalid(
            "INVALID_ATTEMPT",
            lambda: replace(
                make_skipped_attempt(),
                failure_class=AttemptFailureClassV1.NETWORK_FAILURE,
            ),
        )

    def test_policy_rejected_requires_fixed_class(self):
        self.assert_invalid(
            "INVALID_ATTEMPT",
            lambda: replace(
                make_failed_attempt(),
                outcome=AttemptOutcomeV1.POLICY_REJECTED,
            ),
        )

    def test_duplicate_attempt_ordinal_is_rejected(self):
        second = replace(make_attempt(), started_offset_ms=30)
        self.assert_invalid(
            "INVALID_ATTEMPT",
            lambda: make_observation(attempts=(make_attempt(), second)),
        )

    def test_attempt_ordinal_out_of_order_is_rejected(self):
        first = replace(make_attempt(), ordinal=2)
        second = replace(make_attempt(), ordinal=1, started_offset_ms=30)
        self.assert_invalid(
            "INVALID_ATTEMPT",
            lambda: make_observation(attempts=(first, second)),
        )

    def test_attempt_offsets_must_be_monotonic(self):
        first = replace(make_attempt(), ordinal=1, started_offset_ms=20)
        second = replace(make_attempt(), ordinal=2, started_offset_ms=10)
        self.assert_invalid(
            "INVALID_ATTEMPT",
            lambda: make_observation(attempts=(first, second)),
        )

    def test_attempt_requires_existing_lineage_leaf(self):
        self.assert_invalid(
            "INVALID_TYPE",
            lambda: replace(make_attempt(), lineage_leaf_ordinal=None),
        )

    def test_attempt_bound_is_enforced(self):
        self.assert_invalid(
            "BOUND_EXCEEDED",
            lambda: make_observation(attempts=(make_attempt(),) * (MAX_ATTEMPTS + 1)),
        )

    def test_attempt_media_evidence_requires_assets(self):
        self.assert_invalid(
            "INVALID_EVIDENCE",
            lambda: replace(make_attempt(), asset_count=0),
        )

    def test_attempt_preserved_relationship_requires_count(self):
        self.assert_invalid(
            "INVALID_RELATIONSHIP_EVIDENCE",
            lambda: replace(
                make_attempt(),
                relationship_evidence=RelationshipEvidenceV1.PRESERVED,
            ),
        )


class ObservationAndFixtureTests(ValidationTestCase):
    def test_complete_mp4_fixture(self):
        observation = complete_mp4_fixture()
        self.assertEqual(observation.candidate_count, 1)
        self.assertGreaterEqual(observation.video_track_count, 1)
        self.assertGreaterEqual(observation.audio_track_count, 1)
        self.assertEqual(observation.relationship_count, 0)
        self.assertIn(
            AssemblyRequirementV1.DIRECT_SINGLE_FILE,
            observation.assembly_requirements,
        )

    def test_separate_video_audio_fixture(self):
        observation = separate_av_fixture()
        self.assertEqual(observation.candidate_count, 2)
        self.assertEqual(observation.video_track_count, 1)
        self.assertEqual(observation.audio_track_count, 1)
        self.assertEqual(observation.relationship_count, 1)
        self.assertEqual(
            observation.assembly_requirements,
            (AssemblyRequirementV1.SEPARATE_VIDEO_AUDIO,),
        )

    def test_livephoto_still_motion_fixture(self):
        observation = livephoto_fixture()
        self.assertEqual(observation.candidate_count, 2)
        self.assertEqual(observation.relationship_count, 1)
        self.assertEqual(
            observation.relationship_evidence,
            RelationshipEvidenceV1.PRESERVED,
        )
        self.assertEqual(observation.assembly_requirements, ())

    def test_selected_backend_is_derived_without_duplicate_state(self):
        observation = make_observation()
        self.assertIs(observation.selected_attempt, observation.attempts[0])
        self.assertEqual(observation.selected_backend_id, "example.video_adapter")
        self.assertIs(
            observation.selected_backend_type,
            BackendCapabilityTypeV1.DEDICATED_SOURCE_ADAPTER,
        )

    def test_empty_failed_observation_is_explicitly_supported(self):
        observation = make_failure_observation()
        self.assertIsNone(observation.selected_attempt)
        self.assertEqual(observation.error_code, "NO_VALID_MEDIA")

    def test_failed_observation_requires_error_code(self):
        self.assert_invalid(
            "INVALID_SELECTION",
            lambda: make_failure_observation(error_code=None),
        )

    def test_selected_attempt_must_exist(self):
        self.assert_invalid(
            "INVALID_SELECTION",
            lambda: make_observation(selected_attempt_ordinal=2),
        )

    def test_selected_attempt_must_succeed(self):
        attempt = make_failed_attempt()
        self.assert_invalid(
            "INVALID_SELECTION",
            lambda: make_observation(
                attempts=(attempt,),
                evidence_stage=EvidenceStageV1.METADATA_ONLY,
                candidate_count=0,
                video_track_count=0,
                audio_track_count=0,
                assembly_requirements=(),
            ),
        )

    def test_selected_attempt_cannot_be_skipped(self):
        attempt = make_skipped_attempt()
        self.assert_invalid(
            "INVALID_SELECTION",
            lambda: make_observation(
                attempts=(attempt,),
                evidence_stage=EvidenceStageV1.DETECTED,
                candidate_count=0,
                video_track_count=0,
                audio_track_count=0,
                assembly_requirements=(),
            ),
        )

    def test_selected_stage_must_match_observation(self):
        self.assert_invalid(
            "INVALID_SELECTION",
            lambda: make_observation(resolution_stage=ResolutionStageV1.DIRECT_MEDIA),
        )

    def test_selected_counts_must_match_observation(self):
        self.assert_invalid(
            "INVALID_SELECTION",
            lambda: make_observation(candidate_count=2),
        )

    def test_selected_evidence_must_match_observation(self):
        attempt = replace(
            make_attempt(),
            evidence_stage=EvidenceStageV1.BROWSER_INSPECTABLE,
        )
        self.assert_invalid(
            "INVALID_SELECTION",
            lambda: make_observation(attempts=(attempt,)),
        )

    def test_metadata_only_cannot_report_candidates(self):
        self.assert_invalid(
            "INVALID_EVIDENCE",
            lambda: make_observation(evidence_stage=EvidenceStageV1.METADATA_ONLY),
        )

    def test_media_candidates_requires_candidate(self):
        self.assert_invalid(
            "INVALID_EVIDENCE",
            lambda: make_observation(candidate_count=0, assembly_requirements=()),
        )

    def test_full_media_verified_requires_successful_candidate(self):
        self.assert_invalid(
            "INVALID_SELECTION",
            lambda: make_failure_observation(
                evidence_stage=EvidenceStageV1.FULL_MEDIA_VERIFIED,
                candidate_count=1,
            ),
        )

    def test_not_applicable_relationship_requires_zero_count(self):
        self.assert_invalid(
            "INVALID_RELATIONSHIP_EVIDENCE",
            lambda: make_observation(relationship_count=1),
        )

    def test_preserved_relationship_requires_count(self):
        self.assert_invalid(
            "INVALID_RELATIONSHIP_EVIDENCE",
            lambda: make_observation(
                relationship_evidence=RelationshipEvidenceV1.PRESERVED
            ),
        )

    def test_claimed_relationship_requires_count(self):
        self.assert_invalid(
            "INVALID_RELATIONSHIP_EVIDENCE",
            lambda: make_observation(
                relationship_evidence=RelationshipEvidenceV1.CLAIMED
            ),
        )

    def test_tracks_require_candidate(self):
        self.assert_invalid(
            "INVALID_EVIDENCE",
            lambda: make_failure_observation(video_track_count=1),
        )

    def test_duplicate_acquisition_requirement_is_rejected(self):
        requirement = AcquisitionRequirementV1.RANGE_REQUIRED
        self.assert_invalid(
            "INVALID_REQUIREMENT",
            lambda: make_observation(acquisition_requirements=(requirement, requirement)),
        )

    def test_duplicate_assembly_requirement_is_rejected(self):
        requirement = AssemblyRequirementV1.DIRECT_SINGLE_FILE
        self.assert_invalid(
            "INVALID_REQUIREMENT",
            lambda: make_observation(assembly_requirements=(requirement, requirement)),
        )

    def test_reversed_acquisition_requirements_are_rejected(self):
        self.assert_invalid(
            "NON_CANONICAL_ORDER",
            lambda: make_observation(
                acquisition_requirements=(
                    AcquisitionRequirementV1.RANGE_REQUIRED,
                    AcquisitionRequirementV1.COOKIE_REQUIRED,
                )
            ),
        )

    def test_reversed_assembly_requirements_are_rejected(self):
        self.assert_invalid(
            "NON_CANONICAL_ORDER",
            lambda: make_observation(
                assembly_requirements=(
                    AssemblyRequirementV1.REQUIRES_REMUX,
                    AssemblyRequirementV1.DASH_MANIFEST,
                )
            ),
        )

    def test_reversed_warning_codes_are_rejected(self):
        error = self.assert_invalid(
            "NON_CANONICAL_ORDER",
            lambda: make_observation(
                warning_codes=("PARTIAL_METADATA", "FORMAT_UNKNOWN")
            ),
        )
        self.assertNotIn("PARTIAL_METADATA", str(error))

    def test_canonical_collections_round_trip_unchanged(self):
        observation = make_observation(
            acquisition_requirements=(
                AcquisitionRequirementV1.COOKIE_REQUIRED,
                AcquisitionRequirementV1.RANGE_REQUIRED,
            ),
            assembly_requirements=(
                AssemblyRequirementV1.DASH_MANIFEST,
                AssemblyRequirementV1.REQUIRES_REMUX,
            ),
            warning_codes=("FORMAT_UNKNOWN", "PARTIAL_METADATA"),
        )
        restored = UpstreamObservationV1.from_json(observation.to_json())
        self.assertEqual(
            restored.acquisition_requirements,
            observation.acquisition_requirements,
        )
        self.assertEqual(
            restored.assembly_requirements,
            observation.assembly_requirements,
        )
        self.assertEqual(restored.warning_codes, observation.warning_codes)
        self.assertEqual(restored.to_json(), observation.to_json())

    def test_separate_av_requires_audio_track(self):
        self.assert_invalid(
            "INVALID_REQUIREMENT",
            lambda: make_observation(
                candidate_count=2,
                audio_track_count=0,
                assembly_requirements=(AssemblyRequirementV1.SEPARATE_VIDEO_AUDIO,),
            ),
        )

    def test_separate_av_requires_video_track(self):
        self.assert_invalid(
            "INVALID_REQUIREMENT",
            lambda: make_observation(
                candidate_count=2,
                video_track_count=0,
                assembly_requirements=(AssemblyRequirementV1.SEPARATE_VIDEO_AUDIO,),
            ),
        )

    def test_separate_av_requires_two_candidates(self):
        self.assert_invalid(
            "INVALID_REQUIREMENT",
            lambda: make_observation(
                assembly_requirements=(AssemblyRequirementV1.SEPARATE_VIDEO_AUDIO,)
            ),
        )

    def test_direct_single_file_requires_candidate(self):
        self.assert_invalid(
            "INVALID_REQUIREMENT",
            lambda: make_failure_observation(
                assembly_requirements=(AssemblyRequirementV1.DIRECT_SINGLE_FILE,)
            ),
        )

    def test_playlist_requirements_require_candidate(self):
        for requirement in (
            AssemblyRequirementV1.HLS_PLAYLIST,
            AssemblyRequirementV1.DASH_MANIFEST,
            AssemblyRequirementV1.REQUIRES_REMUX,
            AssemblyRequirementV1.REQUIRES_TRANSCODE,
        ):
            with self.subTest(requirement=requirement):
                self.assert_invalid(
                    "INVALID_REQUIREMENT",
                    lambda requirement=requirement: make_failure_observation(
                        assembly_requirements=(requirement,)
                    ),
                )

    def test_direct_and_separate_facts_are_mutually_exclusive(self):
        self.assert_invalid(
            "INVALID_REQUIREMENT",
            lambda: make_observation(
                candidate_count=2,
                assembly_requirements=(
                    AssemblyRequirementV1.DIRECT_SINGLE_FILE,
                    AssemblyRequirementV1.SEPARATE_VIDEO_AUDIO,
                ),
            ),
        )

    def test_warning_codes_are_unique_and_fixed(self):
        self.assert_invalid(
            "INVALID_IDENTIFIER",
            lambda: make_observation(warning_codes=("FORMAT_UNKNOWN", "FORMAT_UNKNOWN")),
        )
        self.assert_invalid(
            "INVALID_IDENTIFIER",
            lambda: make_observation(warning_codes=("free text",)),
        )

    def test_requirement_bound_is_enforced(self):
        values = (AcquisitionRequirementV1.RANGE_REQUIRED,) * (MAX_REQUIREMENTS + 1)
        self.assert_invalid(
            "BOUND_EXCEEDED",
            lambda: make_observation(acquisition_requirements=values),
        )

    def test_warning_bound_is_enforced(self):
        values = tuple(f"WARNING_{index}" for index in range(MAX_WARNING_CODES + 1))
        self.assert_invalid(
            "BOUND_EXCEEDED",
            lambda: make_observation(warning_codes=values),
        )

    def test_collection_fields_must_be_tuples(self):
        for field_name, value in (
            ("lineage", list(make_lineage())),
            ("attempts", [make_attempt()]),
            ("acquisition_requirements", []),
            ("assembly_requirements", []),
            ("warning_codes", []),
        ):
            with self.subTest(field_name=field_name):
                self.assert_invalid(
                    "INVALID_TYPE",
                    lambda field_name=field_name, value=value: make_observation(
                        **{field_name: value}
                    ),
                )


class SerializationTests(ValidationTestCase):
    def test_primitive_round_trip_preserves_tuples_and_equality(self):
        for fixture in (
            complete_mp4_fixture(),
            separate_av_fixture(),
            livephoto_fixture(),
            nested_aggregator_lineage_fixture(),
            make_failure_observation(),
        ):
            with self.subTest(request_id=fixture.request_id):
                restored = from_primitive(to_primitive(fixture))
                self.assertEqual(restored, fixture)
                self.assertIsInstance(restored.lineage, tuple)
                self.assertIsInstance(restored.attempts, tuple)

    def test_json_round_trip_is_exact(self):
        observation = separate_av_fixture()
        self.assertEqual(from_json(to_json(observation)), observation)
        self.assertEqual(UpstreamObservationV1.from_json(observation.to_json()), observation)

    def test_json_is_deterministic_and_canonical(self):
        observation = livephoto_fixture()
        first = observation.to_json()
        second = observation.to_json()
        self.assertEqual(first, second)
        self.assertEqual(first, json.dumps(json.loads(first), sort_keys=True, separators=(",", ":")))

    def test_unknown_root_field_is_rejected(self):
        primitive = make_observation().to_primitive()
        primitive["unexpected"] = "value"
        self.assert_invalid("UNKNOWN_FIELD", lambda: from_primitive(primitive))

    def test_unknown_nested_field_is_rejected(self):
        primitive = make_observation().to_primitive()
        primitive["lineage"][0]["unexpected"] = "value"
        self.assert_invalid("UNKNOWN_FIELD", lambda: from_primitive(primitive))

    def test_invalid_relationship_enum_is_rejected(self):
        primitive = make_observation().to_primitive()
        primitive["relationship_evidence"] = "preserved"
        self.assert_invalid("INVALID_ENUM", lambda: from_primitive(primitive))

    def test_missing_required_field_is_rejected(self):
        primitive = make_observation().to_primitive()
        primitive.pop("request_id")
        self.assert_invalid("MISSING_FIELD", lambda: from_primitive(primitive))

    def test_wrong_field_type_is_rejected(self):
        primitive = make_observation().to_primitive()
        primitive["warning_codes"] = "NOT_A_LIST"
        self.assert_invalid("INVALID_TYPE", lambda: from_primitive(primitive))

    def test_attempt_fallback_and_leaf_are_always_serialized(self):
        attempt = make_observation().to_primitive()["attempts"][0]
        self.assertEqual(attempt["fallback_reason"], "NONE")
        self.assertEqual(attempt["lineage_leaf_ordinal"], 2)
        self.assertIs(type(attempt["lineage_leaf_ordinal"]), int)

    def test_null_attempt_fallback_reason_is_rejected(self):
        primitive = make_observation().to_primitive()
        primitive["attempts"][0]["fallback_reason"] = None
        self.assert_invalid("INVALID_TYPE", lambda: from_primitive(primitive))

    def test_null_attempt_lineage_leaf_is_rejected(self):
        primitive = make_observation().to_primitive()
        primitive["attempts"][0]["lineage_leaf_ordinal"] = None
        self.assert_invalid("INVALID_TYPE", lambda: from_primitive(primitive))

    def test_deserializer_does_not_sort_non_canonical_collections(self):
        observation = make_observation(
            acquisition_requirements=(
                AcquisitionRequirementV1.COOKIE_REQUIRED,
                AcquisitionRequirementV1.RANGE_REQUIRED,
            ),
            assembly_requirements=(
                AssemblyRequirementV1.DASH_MANIFEST,
                AssemblyRequirementV1.REQUIRES_REMUX,
            ),
            warning_codes=("FORMAT_UNKNOWN", "PARTIAL_METADATA"),
        )
        for field_name in (
            "acquisition_requirements",
            "assembly_requirements",
            "warning_codes",
        ):
            with self.subTest(field_name=field_name):
                primitive = observation.to_primitive()
                primitive[field_name].reverse()
                self.assert_invalid(
                    "NON_CANONICAL_ORDER",
                    lambda primitive=primitive: from_primitive(primitive),
                )

    def test_top_level_provider_version_is_required_text(self):
        primitive = make_observation().to_primitive()
        primitive["provider_version"] = None
        self.assert_invalid("INVALID_TYPE", lambda: from_primitive(primitive))

    def test_bool_in_integer_field_is_rejected(self):
        primitive = make_observation().to_primitive()
        primitive["candidate_count"] = True
        self.assert_invalid("INVALID_TYPE", lambda: from_primitive(primitive))

    def test_oversized_json_is_rejected_before_parse(self):
        value = "{" + (" " * MAX_SERIALIZED_BYTES) + "}"
        self.assert_invalid("SERIALIZED_SIZE_EXCEEDED", lambda: from_json(value))

    def test_oversized_primitive_is_rejected(self):
        value = {"padding": "x" * MAX_SERIALIZED_BYTES}
        self.assert_invalid(
            "SERIALIZED_SIZE_EXCEEDED",
            lambda: from_primitive(value),
        )

    def test_output_size_limit_is_enforced(self):
        with patch(
            "xhs_download.services.upstream_observation.serialization.MAX_SERIALIZED_BYTES",
            64,
        ):
            self.assert_invalid(
                "SERIALIZED_SIZE_EXCEEDED",
                lambda: to_json(make_observation()),
            )

    def test_malformed_json_is_rejected(self):
        error = self.assert_invalid("INVALID_JSON", lambda: from_json("{"))
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)

    def test_excessive_json_nesting_fails_closed(self):
        value: object = "leaf"
        for _ in range(1100):
            value = [value]
        self.assert_invalid("INVALID_TYPE", lambda: from_primitive(value))

    def test_wrong_schema_version_is_rejected(self):
        primitive = make_observation().to_primitive()
        primitive["schema_version"] = "2.0"
        self.assert_invalid(
            "INVALID_SCHEMA_VERSION",
            lambda: from_primitive(primitive),
        )

    def test_duplicate_json_key_is_rejected(self):
        value = '{"schema_version":"1.0","schema_version":"1.0"}'
        self.assert_invalid("UNKNOWN_FIELD", lambda: from_json(value))

    def test_nonstandard_json_numbers_are_rejected(self):
        self.assert_invalid("INVALID_JSON", lambda: from_json('{"value":NaN}'))

    def test_invalid_utf8_is_rejected(self):
        error = self.assert_invalid("INVALID_JSON", lambda: from_json(b"\xff"))
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)

    def test_invalid_enum_does_not_retain_original_value(self):
        primitive = make_observation().to_primitive()
        primitive["resolution_stage"] = "PRIVATE_VALUE"
        error = self.assert_invalid("INVALID_ENUM", lambda: from_primitive(primitive))
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)
        self.assertNotIn("PRIVATE_VALUE", str(error))

    def test_json_methods_and_module_functions_are_equivalent(self):
        observation = make_observation()
        self.assertEqual(observation.to_primitive(), to_primitive(observation))
        self.assertEqual(observation.to_json(), to_json(observation))
        self.assertEqual(
            UpstreamObservationV1.from_primitive(observation.to_primitive()),
            observation,
        )


class SecurityAndPurityTests(ValidationTestCase):
    def test_sensitive_identifier_values_are_rejected(self):
        values = (
            "https://private.invalid/media",
            "file:private",
            "data:private",
            "blob:private",
            "javascript:private",
            "/Users/private/file",
            "~/private/file",
            "C:\\private\\file",
            "Cookie:secret",
            "Authorization:Bearer",
            "Bearer secret",
            "?signature=secret",
            "?xsec_token=secret",
            "line\nbreak",
        )
        for value in values:
            with self.subTest(value=value):
                self.assert_invalid(
                    "SENSITIVE_VALUE_REJECTED",
                    lambda value=value: make_observation(top_level_provider=value),
                )

    def test_raw_exception_text_is_rejected(self):
        for value in ("ValueError:boom", "CustomException:private", "Traceback (last)"):
            with self.subTest(value=value):
                self.assert_invalid(
                    "SENSITIVE_VALUE_REJECTED",
                    lambda value=value: replace(make_lineage()[1], actor_id=value),
                )

    def test_normal_serialization_has_no_private_field_names(self):
        forbidden = {
            "url",
            "host",
            "header",
            "cookie",
            "token",
            "path",
            "exception",
            "payload",
            "body",
            "private_resource_ref",
            "delivery_disposition",
            "route_admission_state",
            "deployment_state",
        }

        def walk(value):
            if isinstance(value, dict):
                for key, nested in value.items():
                    self.assertNotIn(key.lower(), forbidden)
                    walk(nested)
            elif isinstance(value, list):
                for nested in value:
                    walk(nested)
            elif isinstance(value, str):
                self.assertNotIn("://", value)
                self.assertNotIn("Cookie:", value)
                self.assertNotIn("Authorization:", value)

        for fixture in (
            complete_mp4_fixture(),
            separate_av_fixture(),
            livephoto_fixture(),
            nested_aggregator_lineage_fixture(),
        ):
            walk(fixture.to_primitive())

    def test_fixture_schema_has_no_deferred_authority(self):
        serialized = complete_mp4_fixture().to_json()
        for deferred in (
            "RouteAdmissionState",
            "DeploymentState",
            "LicenseScope",
            "DeliveryDisposition",
            "PrivateResourceChannel",
            "FormatVariant",
            "VideoTrack",
            "AudioTrack",
        ):
            self.assertNotIn(deferred, serialized)

    def test_package_imports_only_standard_pure_modules(self):
        package = Path(__file__).parent / "services" / "upstream_observation"
        banned_roots = {
            "django",
            "requests",
            "httpx",
            "urllib",
            "socket",
            "subprocess",
            "multiprocessing",
            "pathlib",
            "os",
            "threading",
        }
        for path in package.glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            imported: set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.update(alias.name.split(".")[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module.split(".")[0])
            self.assertFalse(imported & banned_roots, path.name)

    def test_package_contains_no_io_network_process_or_dynamic_execution_calls(self):
        package = Path(__file__).parent / "services" / "upstream_observation"
        banned_calls = {
            "open",
            "exec",
            "eval",
            "getenv",
            "read_text",
            "write_text",
            "read_bytes",
            "write_bytes",
            "Popen",
            "run",
            "system",
            "start",
            "connect",
            "request",
        }
        for path in package.glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            calls = {
                node.func.id
                if isinstance(node.func, ast.Name)
                else node.func.attr
                for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and isinstance(node.func, (ast.Name, ast.Attribute))
            }
            self.assertFalse(calls & banned_calls, path.name)

    def test_no_runtime_module_imports_the_kernel(self):
        app_root = Path(__file__).parent
        package = app_root / "services" / "upstream_observation"
        consumers = []
        for path in app_root.rglob("*.py"):
            if package in path.parents or path.name.startswith("tests_"):
                continue
            text = path.read_text(encoding="utf-8")
            if "upstream_observation" in text:
                consumers.append(path.relative_to(app_root).as_posix())
        self.assertEqual(consumers, [])

    def test_production_package_stays_within_three_modules(self):
        package = Path(__file__).parent / "services" / "upstream_observation"
        self.assertEqual(
            {path.name for path in package.glob("*.py")},
            {"__init__.py", "contracts.py", "serialization.py"},
        )


if __name__ == "__main__":
    unittest.main()
