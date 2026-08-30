"""Strict deterministic serialization for upstream observation contracts."""

from __future__ import annotations

import json
from enum import Enum
from typing import Any

from .contracts import (
    MAX_SERIALIZED_BYTES,
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
)


_OBSERVATION_FIELDS = {
    "schema_version",
    "request_id",
    "top_level_provider",
    "provider_version",
    "resolution_mode",
    "resolution_stage",
    "selected_attempt_ordinal",
    "lineage",
    "attempts",
    "evidence_stage",
    "relationship_evidence",
    "candidate_count",
    "video_track_count",
    "audio_track_count",
    "relationship_count",
    "acquisition_requirements",
    "assembly_requirements",
    "warning_codes",
    "error_code",
}
_LINEAGE_FIELDS = {
    "ordinal",
    "parent_ordinal",
    "actor_id",
    "actor_type",
    "backend_type",
    "provider_version",
    "trust_scope",
    "data_shared",
}
_ATTEMPT_FIELDS = {
    "ordinal",
    "stage",
    "backend_id",
    "backend_type",
    "started_offset_ms",
    "elapsed_ms",
    "outcome",
    "failure_class",
    "asset_count",
    "format_count",
    "relationship_count",
    "evidence_stage",
    "relationship_evidence",
    "fallback_reason",
    "lineage_leaf_ordinal",
}


def _reject(code: str, field_name: str) -> None:
    raise UpstreamObservationValidationError(code, field_name)


def _enum_value(value: Enum | None) -> str | None:
    return value.value if value is not None else None


def to_primitive(observation: UpstreamObservationV1) -> dict[str, Any]:
    if type(observation) is not UpstreamObservationV1:
        _reject("INVALID_TYPE", "observation")
    return {
        "schema_version": observation.schema_version,
        "request_id": observation.request_id,
        "top_level_provider": observation.top_level_provider,
        "provider_version": observation.provider_version,
        "resolution_mode": observation.resolution_mode.value,
        "resolution_stage": observation.resolution_stage.value,
        "selected_attempt_ordinal": observation.selected_attempt_ordinal,
        "lineage": [
            {
                "ordinal": node.ordinal,
                "parent_ordinal": node.parent_ordinal,
                "actor_id": node.actor_id,
                "actor_type": node.actor_type.value,
                "backend_type": _enum_value(node.backend_type),
                "provider_version": node.provider_version,
                "trust_scope": node.trust_scope.value,
                "data_shared": node.data_shared.value,
            }
            for node in observation.lineage
        ],
        "attempts": [
            {
                "ordinal": attempt.ordinal,
                "stage": attempt.stage.value,
                "backend_id": attempt.backend_id,
                "backend_type": attempt.backend_type.value,
                "started_offset_ms": attempt.started_offset_ms,
                "elapsed_ms": attempt.elapsed_ms,
                "outcome": attempt.outcome.value,
                "failure_class": _enum_value(attempt.failure_class),
                "asset_count": attempt.asset_count,
                "format_count": attempt.format_count,
                "relationship_count": attempt.relationship_count,
                "evidence_stage": attempt.evidence_stage.value,
                "relationship_evidence": attempt.relationship_evidence.value,
                "fallback_reason": _enum_value(attempt.fallback_reason),
                "lineage_leaf_ordinal": attempt.lineage_leaf_ordinal,
            }
            for attempt in observation.attempts
        ],
        "evidence_stage": observation.evidence_stage.value,
        "relationship_evidence": observation.relationship_evidence.value,
        "candidate_count": observation.candidate_count,
        "video_track_count": observation.video_track_count,
        "audio_track_count": observation.audio_track_count,
        "relationship_count": observation.relationship_count,
        "acquisition_requirements": [
            requirement.value for requirement in observation.acquisition_requirements
        ],
        "assembly_requirements": [
            requirement.value for requirement in observation.assembly_requirements
        ],
        "warning_codes": list(observation.warning_codes),
        "error_code": observation.error_code,
    }


def _canonical_bytes(value: Any) -> bytes:
    try:
        encoded = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError, RecursionError):
        encoded = None
    if encoded is None:
        _reject("INVALID_TYPE", "primitive")
    return encoded


def _check_size(value: Any) -> None:
    if len(_canonical_bytes(value)) > MAX_SERIALIZED_BYTES:
        _reject("SERIALIZED_SIZE_EXCEEDED", "observation")


def _expect_mapping(value: Any, fields: set[str], field_name: str) -> dict[str, Any]:
    if type(value) is not dict:
        _reject("INVALID_TYPE", field_name)
    actual = set(value)
    if actual - fields:
        _reject("UNKNOWN_FIELD", field_name)
    if fields - actual:
        _reject("MISSING_FIELD", field_name)
    return value


def _expect_list(value: Any, field_name: str) -> list[Any]:
    if type(value) is not list:
        _reject("INVALID_TYPE", field_name)
    return value


def _expect_int(value: Any, field_name: str) -> int:
    if type(value) is not int:
        _reject("INVALID_TYPE", field_name)
    return value


def _expect_optional_int(value: Any, field_name: str) -> int | None:
    return None if value is None else _expect_int(value, field_name)


def _expect_str(value: Any, field_name: str) -> str:
    if type(value) is not str:
        _reject("INVALID_TYPE", field_name)
    return value


def _expect_optional_str(value: Any, field_name: str) -> str | None:
    return None if value is None else _expect_str(value, field_name)


def _expect_enum(value: Any, enum_type: type[Enum], field_name: str) -> Enum:
    candidate = _expect_str(value, field_name)
    member = enum_type._value2member_map_.get(candidate)
    if member is None:
        _reject("INVALID_ENUM", field_name)
    return member


def _expect_optional_enum(
    value: Any,
    enum_type: type[Enum],
    field_name: str,
) -> Enum | None:
    return None if value is None else _expect_enum(value, enum_type, field_name)


def _lineage_from_primitive(value: Any) -> BackendLineageNodeV1:
    item = _expect_mapping(value, _LINEAGE_FIELDS, "lineage")
    return BackendLineageNodeV1(
        ordinal=_expect_int(item["ordinal"], "lineage.ordinal"),
        parent_ordinal=_expect_optional_int(
            item["parent_ordinal"], "lineage.parent_ordinal"
        ),
        actor_id=_expect_str(item["actor_id"], "lineage.actor_id"),
        actor_type=_expect_enum(
            item["actor_type"], BackendActorTypeV1, "lineage.actor_type"
        ),
        backend_type=_expect_optional_enum(
            item["backend_type"],
            BackendCapabilityTypeV1,
            "lineage.backend_type",
        ),
        provider_version=_expect_optional_str(
            item["provider_version"], "lineage.provider_version"
        ),
        trust_scope=_expect_enum(
            item["trust_scope"], LineageTrustScopeV1, "lineage.trust_scope"
        ),
        data_shared=_expect_enum(
            item["data_shared"], LineageDataSharedV1, "lineage.data_shared"
        ),
    )


def _attempt_from_primitive(value: Any) -> AttemptTraceV1:
    item = _expect_mapping(value, _ATTEMPT_FIELDS, "attempt")
    return AttemptTraceV1(
        ordinal=_expect_int(item["ordinal"], "attempt.ordinal"),
        stage=_expect_enum(item["stage"], ResolutionStageV1, "attempt.stage"),
        backend_id=_expect_str(item["backend_id"], "attempt.backend_id"),
        backend_type=_expect_enum(
            item["backend_type"],
            BackendCapabilityTypeV1,
            "attempt.backend_type",
        ),
        started_offset_ms=_expect_int(
            item["started_offset_ms"], "attempt.started_offset_ms"
        ),
        elapsed_ms=_expect_int(item["elapsed_ms"], "attempt.elapsed_ms"),
        outcome=_expect_enum(item["outcome"], AttemptOutcomeV1, "attempt.outcome"),
        failure_class=_expect_optional_enum(
            item["failure_class"],
            AttemptFailureClassV1,
            "attempt.failure_class",
        ),
        asset_count=_expect_int(item["asset_count"], "attempt.asset_count"),
        format_count=_expect_int(item["format_count"], "attempt.format_count"),
        relationship_count=_expect_int(
            item["relationship_count"], "attempt.relationship_count"
        ),
        evidence_stage=_expect_enum(
            item["evidence_stage"], EvidenceStageV1, "attempt.evidence_stage"
        ),
        relationship_evidence=_expect_enum(
            item["relationship_evidence"],
            RelationshipEvidenceV1,
            "attempt.relationship_evidence",
        ),
        fallback_reason=_expect_optional_enum(
            item["fallback_reason"],
            AttemptFallbackReasonV1,
            "attempt.fallback_reason",
        ),
        lineage_leaf_ordinal=_expect_optional_int(
            item["lineage_leaf_ordinal"], "attempt.lineage_leaf_ordinal"
        ),
    )


def from_primitive(value: Any) -> UpstreamObservationV1:
    _check_size(value)
    item = _expect_mapping(value, _OBSERVATION_FIELDS, "observation")
    lineage_items = _expect_list(item["lineage"], "lineage")
    attempt_items = _expect_list(item["attempts"], "attempts")
    acquisition_items = _expect_list(
        item["acquisition_requirements"], "acquisition_requirements"
    )
    assembly_items = _expect_list(
        item["assembly_requirements"], "assembly_requirements"
    )
    warning_items = _expect_list(item["warning_codes"], "warning_codes")
    return UpstreamObservationV1(
        schema_version=_expect_str(item["schema_version"], "schema_version"),
        request_id=_expect_str(item["request_id"], "request_id"),
        top_level_provider=_expect_str(
            item["top_level_provider"], "top_level_provider"
        ),
        provider_version=_expect_str(item["provider_version"], "provider_version"),
        resolution_mode=_expect_enum(
            item["resolution_mode"], ResolutionModeV1, "resolution_mode"
        ),
        resolution_stage=_expect_enum(
            item["resolution_stage"], ResolutionStageV1, "resolution_stage"
        ),
        selected_attempt_ordinal=_expect_optional_int(
            item["selected_attempt_ordinal"], "selected_attempt_ordinal"
        ),
        lineage=tuple(_lineage_from_primitive(entry) for entry in lineage_items),
        attempts=tuple(_attempt_from_primitive(entry) for entry in attempt_items),
        evidence_stage=_expect_enum(
            item["evidence_stage"], EvidenceStageV1, "evidence_stage"
        ),
        relationship_evidence=_expect_enum(
            item["relationship_evidence"],
            RelationshipEvidenceV1,
            "relationship_evidence",
        ),
        candidate_count=_expect_int(item["candidate_count"], "candidate_count"),
        video_track_count=_expect_int(
            item["video_track_count"], "video_track_count"
        ),
        audio_track_count=_expect_int(
            item["audio_track_count"], "audio_track_count"
        ),
        relationship_count=_expect_int(
            item["relationship_count"], "relationship_count"
        ),
        acquisition_requirements=tuple(
            _expect_enum(
                entry,
                AcquisitionRequirementV1,
                "acquisition_requirements",
            )
            for entry in acquisition_items
        ),
        assembly_requirements=tuple(
            _expect_enum(entry, AssemblyRequirementV1, "assembly_requirements")
            for entry in assembly_items
        ),
        warning_codes=tuple(
            _expect_str(entry, "warning_codes") for entry in warning_items
        ),
        error_code=_expect_optional_str(item["error_code"], "error_code"),
    )


def to_json(observation: UpstreamObservationV1) -> str:
    encoded = _canonical_bytes(to_primitive(observation))
    if len(encoded) > MAX_SERIALIZED_BYTES:
        _reject("SERIALIZED_SIZE_EXCEEDED", "observation")
    return encoded.decode("utf-8")


def _object_pairs_no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _reject("UNKNOWN_FIELD", "duplicate_field")
        result[key] = value
    return result


def _reject_nonstandard_number(_: str) -> None:
    _reject("INVALID_JSON", "json")


def from_json(value: str | bytes) -> UpstreamObservationV1:
    if type(value) is bytes:
        try:
            text = value.decode("utf-8")
        except UnicodeDecodeError:
            text = None
        if text is None:
            _reject("INVALID_JSON", "json")
    elif type(value) is str:
        text = value
    else:
        _reject("INVALID_TYPE", "json")
    if len(text.encode("utf-8")) > MAX_SERIALIZED_BYTES:
        _reject("SERIALIZED_SIZE_EXCEEDED", "json")
    try:
        primitive = json.loads(
            text,
            object_pairs_hook=_object_pairs_no_duplicates,
            parse_constant=_reject_nonstandard_number,
        )
    except UpstreamObservationValidationError:
        raise
    except (json.JSONDecodeError, ValueError, RecursionError):
        primitive = None
    if primitive is None:
        _reject("INVALID_JSON", "json")
    return from_primitive(primitive)
