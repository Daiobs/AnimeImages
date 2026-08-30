"""Pure, URL-free contracts for bounded upstream observations.

The module intentionally owns no runtime integration. It performs no network,
filesystem, process, cache, database, or environment access.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any


SCHEMA_VERSION = "1.0"

# IDs and fixed codes remain compact enough for safe diagnostics.
MAX_ID_LENGTH = 96
MAX_PROVIDER_VERSION_LENGTH = 96
# One request may describe a short delegation tree and bounded retry history.
MAX_LINEAGE_NODES = 32
MAX_ATTEMPTS = 64
MAX_REQUIREMENTS = 16
MAX_WARNING_CODES = 16
# A 64 KiB envelope is ample for the minimal schema without becoming a payload channel.
MAX_SERIALIZED_BYTES = 65_536
MAX_COUNTER_VALUE = 1_000_000
# Attempts describe at most five minutes of relative monotonic time.
MAX_ELAPSED_MS = 300_000
MAX_CODE_LENGTH = 64


class ResolutionStageV1(str, Enum):
    DIRECT_MEDIA = "direct_media"
    DEDICATED_SOURCE = "dedicated_source"
    COMMON_AGGREGATOR = "common_aggregator"
    STATIC_EXTRACTOR = "static_extractor"
    BROWSER_OBSERVER = "browser_observer"


class BackendCapabilityTypeV1(str, Enum):
    DEDICATED_SOURCE_ADAPTER = "DEDICATED_SOURCE_ADAPTER"
    AGGREGATOR_API = "AGGREGATOR_API"
    GENERIC_STATIC_EXTRACTOR = "GENERIC_STATIC_EXTRACTOR"
    BROWSER_NETWORK_OBSERVER = "BROWSER_NETWORK_OBSERVER"
    DIRECT_MEDIA_PROBE = "DIRECT_MEDIA_PROBE"
    FORMAT_INVENTORY_EXTRACTOR = "FORMAT_INVENTORY_EXTRACTOR"
    RICH_MEDIA_RELATIONSHIP_PROVIDER = "RICH_MEDIA_RELATIONSHIP_PROVIDER"
    DELIVERY_TOOL = "DELIVERY_TOOL"


class EvidenceStageV1(str, Enum):
    DETECTED = "DETECTED"
    METADATA_ONLY = "METADATA_ONLY"
    MEDIA_CANDIDATES = "MEDIA_CANDIDATES"
    BROWSER_INSPECTABLE = "BROWSER_INSPECTABLE"
    FULL_MEDIA_VERIFIED = "FULL_MEDIA_VERIFIED"


class RelationshipEvidenceV1(str, Enum):
    NOT_APPLICABLE = "NOT_APPLICABLE"
    CLAIMED = "CLAIMED"
    PRESERVED = "PRESERVED"
    FAILED = "FAILED"


class AcquisitionRequirementV1(str, Enum):
    SPECIAL_HEADERS_REQUIRED = "SPECIAL_HEADERS_REQUIRED"
    REFERER_REQUIRED = "REFERER_REQUIRED"
    COOKIE_REQUIRED = "COOKIE_REQUIRED"
    BROWSER_CONTEXT_REQUIRED = "BROWSER_CONTEXT_REQUIRED"
    RANGE_REQUIRED = "RANGE_REQUIRED"
    EXPIRING_RESOURCE = "EXPIRING_RESOURCE"


class AssemblyRequirementV1(str, Enum):
    DIRECT_SINGLE_FILE = "DIRECT_SINGLE_FILE"
    SEPARATE_VIDEO_AUDIO = "SEPARATE_VIDEO_AUDIO"
    HLS_PLAYLIST = "HLS_PLAYLIST"
    DASH_MANIFEST = "DASH_MANIFEST"
    REQUIRES_REMUX = "REQUIRES_REMUX"
    REQUIRES_TRANSCODE = "REQUIRES_TRANSCODE"


class ResolutionModeV1(str, Enum):
    AUTO = "AUTO"
    DEDICATED_ONLY = "DEDICATED_ONLY"
    COMMON_ONLY = "COMMON_ONLY"
    GRABBER_ONLY = "GRABBER_ONLY"
    SPECIFIC_BACKEND = "SPECIFIC_BACKEND"


class BackendActorTypeV1(str, Enum):
    CONTAINER = "container"
    ADAPTER = "adapter"
    EXTERNAL_SERVICE = "external_service"
    OBSERVER = "observer"
    TOOL = "tool"


class AttemptOutcomeV1(str, Enum):
    SUCCESS = "success"
    FAILED = "failed"
    TIMEOUT = "timeout"
    SKIPPED = "skipped"
    POLICY_REJECTED = "policy_rejected"


class AttemptFailureClassV1(str, Enum):
    NOT_APPLICABLE = "NOT_APPLICABLE"
    UNSUPPORTED_IDENTITY = "UNSUPPORTED_IDENTITY"
    TIMEOUT = "TIMEOUT"
    NETWORK_FAILURE = "NETWORK_FAILURE"
    HTTP_REJECTED = "HTTP_REJECTED"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    RATE_LIMITED = "RATE_LIMITED"
    PARSE_EMPTY = "PARSE_EMPTY"
    RESULT_INVALID = "RESULT_INVALID"
    IDENTITY_MISMATCH = "IDENTITY_MISMATCH"
    POLICY_REJECTED = "POLICY_REJECTED"
    RESOURCE_UNAVAILABLE = "RESOURCE_UNAVAILABLE"
    PROCESS_TERMINATED = "PROCESS_TERMINATED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class AttemptFallbackReasonV1(str, Enum):
    NO_APPLICABLE_BACKEND = "NO_APPLICABLE_BACKEND"
    NO_VALID_MEDIA = "NO_VALID_MEDIA"
    BACKEND_FAILED = "BACKEND_FAILED"
    BACKEND_TIMED_OUT = "BACKEND_TIMED_OUT"
    RESULT_REJECTED = "RESULT_REJECTED"
    MODE_CONTINUATION = "MODE_CONTINUATION"
    HIGHER_AUTHORITY_REQUIRED = "HIGHER_AUTHORITY_REQUIRED"
    NONE = "NONE"


class LineageDataSharedV1(str, Enum):
    NONE = "none"
    SOURCE_REFERENCE = "source_reference"
    METADATA = "metadata"
    PRIVATE_RESOURCE_CONTEXT = "private_resource_context"


class LineageTrustScopeV1(str, Enum):
    INTERNAL = "internal"
    STAFF_LAB = "staff_lab"
    FORMAL_CANDIDATE = "formal_candidate"
    EXTERNAL = "external"


_ERROR_MESSAGES = {
    "INVALID_SCHEMA_VERSION": "upstream observation schema version is invalid",
    "INVALID_IDENTIFIER": "upstream observation identifier is invalid",
    "INVALID_ENUM": "upstream observation enum is invalid",
    "INVALID_LINEAGE": "upstream observation lineage is invalid",
    "INVALID_ATTEMPT": "upstream observation attempt is invalid",
    "INVALID_SELECTION": "upstream observation selection is invalid",
    "INVALID_EVIDENCE": "upstream observation evidence is invalid",
    "INVALID_RELATIONSHIP_EVIDENCE": "relationship evidence is invalid",
    "INVALID_REQUIREMENT": "upstream observation requirement is invalid",
    "NON_CANONICAL_ORDER": (
        "upstream observation collection order is not canonical"
    ),
    "BOUND_EXCEEDED": "upstream observation bound is exceeded",
    "SENSITIVE_VALUE_REJECTED": "sensitive upstream observation value is rejected",
    "UNKNOWN_FIELD": "unknown upstream observation field is rejected",
    "MISSING_FIELD": "required upstream observation field is missing",
    "INVALID_TYPE": "upstream observation value type is invalid",
    "INVALID_JSON": "upstream observation JSON is invalid",
    "SERIALIZED_SIZE_EXCEEDED": "serialized upstream observation is too large",
}
_ERROR_FIELD_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9_.\[\]-]{0,95}\Z")


class UpstreamObservationValidationError(ValueError):
    """A fixed, safely loggable validation failure."""

    def __init__(self, code: str, field_name: str | None = None):
        message = _ERROR_MESSAGES.get(code, _ERROR_MESSAGES["INVALID_TYPE"])
        super().__init__(message)
        self.code = code if code in _ERROR_MESSAGES else "INVALID_TYPE"
        self.field_name = (
            field_name
            if type(field_name) is str and _ERROR_FIELD_PATTERN.fullmatch(field_name)
            else None
        )


_ID_PATTERN = re.compile(
    rf"[A-Za-z0-9][A-Za-z0-9._:@-]{{0,{MAX_ID_LENGTH - 1}}}\Z"
)
_VERSION_PATTERN = re.compile(
    rf"[A-Za-z0-9][A-Za-z0-9._+@:-]{{0,{MAX_PROVIDER_VERSION_LENGTH - 1}}}\Z"
)
_CODE_PATTERN = re.compile(rf"[A-Z][A-Z0-9_]{{0,{MAX_CODE_LENGTH - 1}}}\Z")
_WINDOWS_PATH_PATTERN = re.compile(r"[A-Za-z]:[\\/]")
_SIGNED_QUERY_PATTERN = re.compile(
    r"(?i)(?:[?&]|^)(?:x-amz-[a-z-]+|signature|sig|sign|token|xsec_token|expires)="
)
_CREDENTIAL_PATTERN = re.compile(
    r"(?i)(?:cookie|authorization)\s*[:=]|bearer\s+"
)
_RAW_EXCEPTION_PATTERN = re.compile(
    r"(?:Traceback \(|[A-Za-z][A-Za-z0-9_]*(?:Error|Exception):)"
)


def _reject(code: str, field_name: str) -> None:
    raise UpstreamObservationValidationError(code, field_name)


def _contains_sensitive_value(value: str) -> bool:
    lowered = value.lower()
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        return True
    if "://" in value or lowered.startswith(
        ("file:", "data:", "blob:", "javascript:")
    ):
        return True
    if value.startswith(("/", "~/", "~\\")) or _WINDOWS_PATH_PATTERN.match(value):
        return True
    return bool(
        _SIGNED_QUERY_PATTERN.search(value)
        or _CREDENTIAL_PATTERN.search(value)
        or _RAW_EXCEPTION_PATTERN.search(value)
    )


def _require_string(value: Any, field_name: str) -> str:
    if type(value) is not str:
        _reject("INVALID_TYPE", field_name)
    if _contains_sensitive_value(value):
        _reject("SENSITIVE_VALUE_REJECTED", field_name)
    return value


def _require_identifier(value: Any, field_name: str) -> str:
    candidate = _require_string(value, field_name)
    if not _ID_PATTERN.fullmatch(candidate):
        _reject("INVALID_IDENTIFIER", field_name)
    return candidate


def _require_version(value: Any, field_name: str) -> str:
    candidate = _require_string(value, field_name)
    if not _VERSION_PATTERN.fullmatch(candidate):
        _reject("INVALID_IDENTIFIER", field_name)
    return candidate


def _require_code(value: Any, field_name: str) -> str:
    candidate = _require_string(value, field_name)
    if not _CODE_PATTERN.fullmatch(candidate):
        _reject("INVALID_IDENTIFIER", field_name)
    return candidate


def _require_enum(value: Any, enum_type: type[Enum], field_name: str) -> None:
    if type(value) is not enum_type:
        _reject("INVALID_ENUM", field_name)


def _require_int(
    value: Any,
    field_name: str,
    *,
    minimum: int = 0,
    maximum: int = MAX_COUNTER_VALUE,
) -> int:
    if type(value) is not int:
        _reject("INVALID_TYPE", field_name)
    if value < minimum or value > maximum:
        _reject("BOUND_EXCEEDED", field_name)
    return value


def _require_tuple(value: Any, field_name: str, *, maximum: int) -> tuple[Any, ...]:
    if type(value) is not tuple:
        _reject("INVALID_TYPE", field_name)
    if len(value) > maximum:
        _reject("BOUND_EXCEEDED", field_name)
    return value


def _validate_evidence_counts(
    evidence_stage: EvidenceStageV1,
    candidate_count: int,
    field_name: str,
) -> None:
    if evidence_stage in {EvidenceStageV1.DETECTED, EvidenceStageV1.METADATA_ONLY}:
        if candidate_count != 0:
            _reject("INVALID_EVIDENCE", field_name)
    elif candidate_count == 0:
        _reject("INVALID_EVIDENCE", field_name)


def _validate_relationship_counts(
    relationship_evidence: RelationshipEvidenceV1,
    relationship_count: int,
    field_name: str,
) -> None:
    if relationship_evidence is RelationshipEvidenceV1.NOT_APPLICABLE:
        if relationship_count != 0:
            _reject("INVALID_RELATIONSHIP_EVIDENCE", field_name)
    elif relationship_evidence in {
        RelationshipEvidenceV1.CLAIMED,
        RelationshipEvidenceV1.PRESERVED,
    } and relationship_count == 0:
        _reject("INVALID_RELATIONSHIP_EVIDENCE", field_name)


@dataclass(frozen=True, slots=True)
class BackendLineageNodeV1:
    ordinal: int
    parent_ordinal: int | None
    actor_id: str
    actor_type: BackendActorTypeV1
    backend_type: BackendCapabilityTypeV1 | None
    provider_version: str | None
    trust_scope: LineageTrustScopeV1
    data_shared: LineageDataSharedV1

    def __post_init__(self) -> None:
        _require_int(self.ordinal, "lineage.ordinal", minimum=1)
        if self.parent_ordinal is not None:
            _require_int(self.parent_ordinal, "lineage.parent_ordinal", minimum=1)
            if self.parent_ordinal >= self.ordinal:
                _reject("INVALID_LINEAGE", "lineage.parent_ordinal")
        _require_identifier(self.actor_id, "lineage.actor_id")
        _require_enum(self.actor_type, BackendActorTypeV1, "lineage.actor_type")
        if self.actor_type is BackendActorTypeV1.CONTAINER:
            if self.backend_type is not None:
                _reject("INVALID_LINEAGE", "lineage.backend_type")
        else:
            _require_enum(
                self.backend_type,
                BackendCapabilityTypeV1,
                "lineage.backend_type",
            )
        if self.provider_version is not None:
            _require_version(self.provider_version, "lineage.provider_version")
        _require_enum(self.trust_scope, LineageTrustScopeV1, "lineage.trust_scope")
        _require_enum(self.data_shared, LineageDataSharedV1, "lineage.data_shared")


@dataclass(frozen=True, slots=True)
class AttemptTraceV1:
    ordinal: int
    stage: ResolutionStageV1
    backend_id: str
    backend_type: BackendCapabilityTypeV1
    started_offset_ms: int
    elapsed_ms: int
    outcome: AttemptOutcomeV1
    failure_class: AttemptFailureClassV1 | None
    asset_count: int
    format_count: int
    relationship_count: int
    evidence_stage: EvidenceStageV1
    relationship_evidence: RelationshipEvidenceV1
    fallback_reason: AttemptFallbackReasonV1
    lineage_leaf_ordinal: int

    def __post_init__(self) -> None:
        _require_int(self.ordinal, "attempt.ordinal", minimum=1)
        _require_enum(self.stage, ResolutionStageV1, "attempt.stage")
        _require_identifier(self.backend_id, "attempt.backend_id")
        _require_enum(self.backend_type, BackendCapabilityTypeV1, "attempt.backend_type")
        _require_int(
            self.started_offset_ms,
            "attempt.started_offset_ms",
            maximum=MAX_ELAPSED_MS,
        )
        _require_int(
            self.elapsed_ms,
            "attempt.elapsed_ms",
            maximum=MAX_ELAPSED_MS,
        )
        _require_enum(self.outcome, AttemptOutcomeV1, "attempt.outcome")
        if self.failure_class is not None:
            _require_enum(
                self.failure_class,
                AttemptFailureClassV1,
                "attempt.failure_class",
            )
        for field_name in ("asset_count", "format_count", "relationship_count"):
            _require_int(getattr(self, field_name), f"attempt.{field_name}")
        _require_enum(self.evidence_stage, EvidenceStageV1, "attempt.evidence_stage")
        _require_enum(
            self.relationship_evidence,
            RelationshipEvidenceV1,
            "attempt.relationship_evidence",
        )
        _require_enum(
            self.fallback_reason,
            AttemptFallbackReasonV1,
            "attempt.fallback_reason",
        )
        _require_int(
            self.lineage_leaf_ordinal,
            "attempt.lineage_leaf_ordinal",
            minimum=1,
        )
        self._validate_outcome()
        _validate_evidence_counts(
            self.evidence_stage,
            self.asset_count,
            "attempt.evidence_stage",
        )
        _validate_relationship_counts(
            self.relationship_evidence,
            self.relationship_count,
            "attempt.relationship_evidence",
        )

    def _validate_outcome(self) -> None:
        if self.outcome is AttemptOutcomeV1.SUCCESS:
            if self.failure_class is not None:
                _reject("INVALID_ATTEMPT", "attempt.failure_class")
            return

        if self.asset_count or self.format_count or self.relationship_count:
            _reject("INVALID_ATTEMPT", "attempt.asset_count")

        if self.outcome is AttemptOutcomeV1.FAILED:
            if self.failure_class in {
                None,
                AttemptFailureClassV1.NOT_APPLICABLE,
                AttemptFailureClassV1.TIMEOUT,
                AttemptFailureClassV1.POLICY_REJECTED,
            }:
                _reject("INVALID_ATTEMPT", "attempt.failure_class")
        elif self.outcome is AttemptOutcomeV1.TIMEOUT:
            if self.failure_class is not AttemptFailureClassV1.TIMEOUT:
                _reject("INVALID_ATTEMPT", "attempt.failure_class")
        elif self.outcome is AttemptOutcomeV1.SKIPPED:
            if self.elapsed_ms != 0:
                _reject("INVALID_ATTEMPT", "attempt.elapsed_ms")
            if self.failure_class not in {
                AttemptFailureClassV1.NOT_APPLICABLE,
                AttemptFailureClassV1.UNSUPPORTED_IDENTITY,
                AttemptFailureClassV1.POLICY_REJECTED,
            }:
                _reject("INVALID_ATTEMPT", "attempt.failure_class")
        elif self.outcome is AttemptOutcomeV1.POLICY_REJECTED:
            if self.failure_class is not AttemptFailureClassV1.POLICY_REJECTED:
                _reject("INVALID_ATTEMPT", "attempt.failure_class")


@dataclass(frozen=True, slots=True)
class UpstreamObservationV1:
    schema_version: str
    request_id: str
    top_level_provider: str
    provider_version: str
    resolution_mode: ResolutionModeV1
    resolution_stage: ResolutionStageV1
    selected_attempt_ordinal: int | None
    lineage: tuple[BackendLineageNodeV1, ...]
    attempts: tuple[AttemptTraceV1, ...]
    evidence_stage: EvidenceStageV1
    relationship_evidence: RelationshipEvidenceV1
    candidate_count: int
    video_track_count: int
    audio_track_count: int
    relationship_count: int
    acquisition_requirements: tuple[AcquisitionRequirementV1, ...]
    assembly_requirements: tuple[AssemblyRequirementV1, ...]
    warning_codes: tuple[str, ...]
    error_code: str | None

    def __post_init__(self) -> None:
        if type(self.schema_version) is not str or self.schema_version != SCHEMA_VERSION:
            _reject("INVALID_SCHEMA_VERSION", "schema_version")
        _require_identifier(self.request_id, "request_id")
        _require_identifier(self.top_level_provider, "top_level_provider")
        _require_version(self.provider_version, "provider_version")
        _require_enum(self.resolution_mode, ResolutionModeV1, "resolution_mode")
        _require_enum(self.resolution_stage, ResolutionStageV1, "resolution_stage")
        if self.selected_attempt_ordinal is not None:
            _require_int(
                self.selected_attempt_ordinal,
                "selected_attempt_ordinal",
                minimum=1,
            )
        _require_tuple(self.lineage, "lineage", maximum=MAX_LINEAGE_NODES)
        _require_tuple(self.attempts, "attempts", maximum=MAX_ATTEMPTS)
        _require_enum(self.evidence_stage, EvidenceStageV1, "evidence_stage")
        _require_enum(
            self.relationship_evidence,
            RelationshipEvidenceV1,
            "relationship_evidence",
        )
        for field_name in (
            "candidate_count",
            "video_track_count",
            "audio_track_count",
            "relationship_count",
        ):
            _require_int(getattr(self, field_name), field_name)
        _require_tuple(
            self.acquisition_requirements,
            "acquisition_requirements",
            maximum=MAX_REQUIREMENTS,
        )
        _require_tuple(
            self.assembly_requirements,
            "assembly_requirements",
            maximum=MAX_REQUIREMENTS,
        )
        _require_tuple(
            self.warning_codes,
            "warning_codes",
            maximum=MAX_WARNING_CODES,
        )
        if self.error_code is not None:
            _require_code(self.error_code, "error_code")

        self._validate_lineage()
        self._validate_attempts()
        self._validate_requirements()
        self._validate_facts()
        self._validate_selection()

    def _validate_lineage(self) -> None:
        if not self.lineage:
            _reject("INVALID_LINEAGE", "lineage")
        if any(type(node) is not BackendLineageNodeV1 for node in self.lineage):
            _reject("INVALID_LINEAGE", "lineage")
        ordinals = tuple(node.ordinal for node in self.lineage)
        if len(ordinals) != len(set(ordinals)) or ordinals != tuple(sorted(ordinals)):
            _reject("INVALID_LINEAGE", "lineage.ordinal")
        roots = tuple(node for node in self.lineage if node.parent_ordinal is None)
        if len(roots) != 1:
            _reject("INVALID_LINEAGE", "lineage.parent_ordinal")
        known: set[int] = set()
        for node in self.lineage:
            if node.parent_ordinal is not None and node.parent_ordinal not in known:
                _reject("INVALID_LINEAGE", "lineage.parent_ordinal")
            known.add(node.ordinal)

    def _validate_attempts(self) -> None:
        if any(type(attempt) is not AttemptTraceV1 for attempt in self.attempts):
            _reject("INVALID_ATTEMPT", "attempts")
        ordinals = tuple(attempt.ordinal for attempt in self.attempts)
        if len(ordinals) != len(set(ordinals)) or ordinals != tuple(sorted(ordinals)):
            _reject("INVALID_ATTEMPT", "attempts.ordinal")
        offsets = tuple(attempt.started_offset_ms for attempt in self.attempts)
        if offsets != tuple(sorted(offsets)):
            _reject("INVALID_ATTEMPT", "attempts.started_offset_ms")

        nodes = {node.ordinal: node for node in self.lineage}
        parent_ordinals = {
            node.parent_ordinal
            for node in self.lineage
            if node.parent_ordinal is not None
        }
        leaf_ordinals = set(nodes) - parent_ordinals
        for attempt in self.attempts:
            if attempt.lineage_leaf_ordinal not in leaf_ordinals:
                _reject("INVALID_ATTEMPT", "attempt.lineage_leaf_ordinal")

            # The attempt names the adapter AnimeImages invoked, while the leaf
            # records the terminal actor reached through that adapter. Only the
            # selected leaf's ancestor path is authoritative for this mapping.
            matching_backend_nodes = 0
            current_ordinal: int | None = attempt.lineage_leaf_ordinal
            while current_ordinal is not None:
                node = nodes[current_ordinal]
                if (
                    node.actor_id == attempt.backend_id
                    and node.backend_type is attempt.backend_type
                ):
                    matching_backend_nodes += 1
                current_ordinal = node.parent_ordinal
            if matching_backend_nodes != 1:
                _reject("INVALID_ATTEMPT", "attempt.backend_id")

    def _validate_requirements(self) -> None:
        for requirement in self.acquisition_requirements:
            _require_enum(
                requirement,
                AcquisitionRequirementV1,
                "acquisition_requirements",
            )
        for requirement in self.assembly_requirements:
            _require_enum(
                requirement,
                AssemblyRequirementV1,
                "assembly_requirements",
            )
        if len(set(self.acquisition_requirements)) != len(self.acquisition_requirements):
            _reject("INVALID_REQUIREMENT", "acquisition_requirements")
        if len(set(self.assembly_requirements)) != len(self.assembly_requirements):
            _reject("INVALID_REQUIREMENT", "assembly_requirements")
        acquisition_values = tuple(
            requirement.value for requirement in self.acquisition_requirements
        )
        if acquisition_values != tuple(sorted(acquisition_values)):
            _reject("NON_CANONICAL_ORDER", "acquisition_requirements")
        assembly_values = tuple(
            requirement.value for requirement in self.assembly_requirements
        )
        if assembly_values != tuple(sorted(assembly_values)):
            _reject("NON_CANONICAL_ORDER", "assembly_requirements")
        if (
            AssemblyRequirementV1.DIRECT_SINGLE_FILE in self.assembly_requirements
            and AssemblyRequirementV1.SEPARATE_VIDEO_AUDIO
            in self.assembly_requirements
        ):
            _reject("INVALID_REQUIREMENT", "assembly_requirements")
        if AssemblyRequirementV1.DIRECT_SINGLE_FILE in self.assembly_requirements:
            if self.candidate_count == 0:
                _reject("INVALID_REQUIREMENT", "assembly_requirements")
        if AssemblyRequirementV1.SEPARATE_VIDEO_AUDIO in self.assembly_requirements:
            if (
                self.candidate_count < 2
                or self.video_track_count < 1
                or self.audio_track_count < 1
            ):
                _reject("INVALID_REQUIREMENT", "assembly_requirements")
        if any(
            requirement in self.assembly_requirements
            for requirement in (
                AssemblyRequirementV1.HLS_PLAYLIST,
                AssemblyRequirementV1.DASH_MANIFEST,
                AssemblyRequirementV1.REQUIRES_REMUX,
                AssemblyRequirementV1.REQUIRES_TRANSCODE,
            )
        ) and self.candidate_count == 0:
            _reject("INVALID_REQUIREMENT", "assembly_requirements")

    def _validate_facts(self) -> None:
        _validate_evidence_counts(
            self.evidence_stage,
            self.candidate_count,
            "evidence_stage",
        )
        _validate_relationship_counts(
            self.relationship_evidence,
            self.relationship_count,
            "relationship_evidence",
        )
        if self.candidate_count == 0 and (
            self.video_track_count or self.audio_track_count
        ):
            _reject("INVALID_EVIDENCE", "candidate_count")
        for warning_code in self.warning_codes:
            _require_code(warning_code, "warning_codes")
        if len(set(self.warning_codes)) != len(self.warning_codes):
            _reject("INVALID_IDENTIFIER", "warning_codes")
        if self.warning_codes != tuple(sorted(self.warning_codes)):
            _reject("NON_CANONICAL_ORDER", "warning_codes")

    def _validate_selection(self) -> None:
        if self.selected_attempt_ordinal is None:
            if self.error_code is None:
                _reject("INVALID_SELECTION", "error_code")
            if any(
                attempt.outcome is AttemptOutcomeV1.SUCCESS for attempt in self.attempts
            ):
                _reject("INVALID_SELECTION", "selected_attempt_ordinal")
            if self.candidate_count or self.video_track_count or self.audio_track_count:
                _reject("INVALID_SELECTION", "candidate_count")
            if self.relationship_count:
                _reject("INVALID_SELECTION", "relationship_count")
            return

        if self.error_code is not None:
            _reject("INVALID_SELECTION", "error_code")
        selected = self.selected_attempt
        if selected is None or selected.outcome is not AttemptOutcomeV1.SUCCESS:
            _reject("INVALID_SELECTION", "selected_attempt_ordinal")
        if selected.stage is not self.resolution_stage:
            _reject("INVALID_SELECTION", "resolution_stage")
        if selected.asset_count != self.candidate_count:
            _reject("INVALID_SELECTION", "candidate_count")
        if selected.relationship_count != self.relationship_count:
            _reject("INVALID_SELECTION", "relationship_count")
        if selected.evidence_stage is not self.evidence_stage:
            _reject("INVALID_SELECTION", "evidence_stage")
        if selected.relationship_evidence is not self.relationship_evidence:
            _reject("INVALID_SELECTION", "relationship_evidence")
        if self.evidence_stage is EvidenceStageV1.FULL_MEDIA_VERIFIED and not any(
            attempt.outcome is AttemptOutcomeV1.SUCCESS for attempt in self.attempts
        ):
            _reject("INVALID_EVIDENCE", "evidence_stage")

    @property
    def selected_attempt(self) -> AttemptTraceV1 | None:
        if self.selected_attempt_ordinal is None:
            return None
        return next(
            (
                attempt
                for attempt in self.attempts
                if attempt.ordinal == self.selected_attempt_ordinal
            ),
            None,
        )

    @property
    def selected_backend_id(self) -> str | None:
        return self.selected_attempt.backend_id if self.selected_attempt else None

    @property
    def selected_backend_type(self) -> BackendCapabilityTypeV1 | None:
        return self.selected_attempt.backend_type if self.selected_attempt else None

    def to_primitive(self) -> dict[str, Any]:
        from .serialization import to_primitive

        return to_primitive(self)

    @classmethod
    def from_primitive(cls, value: Any) -> UpstreamObservationV1:
        from .serialization import from_primitive

        return from_primitive(value)

    def to_json(self) -> str:
        from .serialization import to_json

        return to_json(self)

    @classmethod
    def from_json(cls, value: str | bytes) -> UpstreamObservationV1:
        from .serialization import from_json

        return from_json(value)
