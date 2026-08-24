"""Project-local Media Resolve v0.1 Core prototype.

The six public dataclasses in this module are the six accepted Core domain
objects.  Enums, the validation exception, and pure validation helpers are
implementation support; they do not add a seventh consumer-facing object.

This module deliberately owns no Provider, Delivery, transport, persistence,
settings, registry, logging, or network behavior.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, Iterable, Mapping


class MediaResolveValidationError(ValueError):
    """Raised when a value cannot satisfy the accepted Core semantics."""

    def __init__(self, code: str, path: str):
        super().__init__(f"{code}: {path}")
        self.code = code
        self.path = path


class DesiredMediaCapability(str, Enum):
    ANY_RESOLVABLE = "ANY_RESOLVABLE"
    VIDEO = "VIDEO"
    IMAGE = "IMAGE"
    AUDIO = "AUDIO"


class QualityPreference(str, Enum):
    BEST_AVAILABLE = "BEST_AVAILABLE"


class ResultOutcome(str, Enum):
    RESOLVED = "RESOLVED"
    FAILED = "FAILED"


class MediaItemsCompleteness(str, Enum):
    BOUNDED = "BOUNDED"
    EXHAUSTIVE = "EXHAUSTIVE"
    UNKNOWN = "UNKNOWN"


class WarningCode(str, Enum):
    PREFERENCE_NOT_APPLIED = "PREFERENCE_NOT_APPLIED"
    FACTS_UNKNOWN = "FACTS_UNKNOWN"
    MEDIA_ITEMS_OMITTED = "MEDIA_ITEMS_OMITTED"


class MediaType(str, Enum):
    VIDEO = "VIDEO"
    IMAGE = "IMAGE"
    AUDIO = "AUDIO"
    UNKNOWN = "UNKNOWN"


class CandidateCompleteness(str, Enum):
    SELECTED_ONLY = "SELECTED_ONLY"
    BOUNDED = "BOUNDED"
    EXHAUSTIVE = "EXHAUSTIVE"
    UNKNOWN = "UNKNOWN"


class SelectionOwner(str, Enum):
    RESOLVER = "RESOLVER"
    CONSUMER = "CONSUMER"
    PROVIDER_OR_SERVICE = "PROVIDER_OR_SERVICE"


class EvidenceState(str, Enum):
    ASSERTED = "ASSERTED"
    OBSERVED = "OBSERVED"
    MEASURED = "MEASURED"
    UNKNOWN = "UNKNOWN"


class FailureKind(str, Enum):
    UNSUPPORTED_SOURCE = "UNSUPPORTED_SOURCE"
    NO_USABLE_MEDIA = "NO_USABLE_MEDIA"
    RESOLUTION_FAILED = "RESOLUTION_FAILED"
    SECURITY_REJECTED = "SECURITY_REJECTED"


class Retryability(str, Enum):
    RETRYABLE = "RETRYABLE"
    NOT_RETRYABLE = "NOT_RETRYABLE"
    UNKNOWN = "UNKNOWN"


class Fallback(str, Enum):
    AVAILABLE = "AVAILABLE"
    EXHAUSTED = "EXHAUSTED"
    UNKNOWN = "UNKNOWN"


class UserAction(str, Enum):
    NONE = "NONE"
    CHANGE_INPUT = "CHANGE_INPUT"
    REPAIR_CREDENTIAL_ACCESS = "REPAIR_CREDENTIAL_ACCESS"
    UNKNOWN = "UNKNOWN"


_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_PHYSICAL_TOKEN_PATTERN = re.compile(r"[a-z0-9][a-z0-9._+-]{0,63}\Z")
_WARNING_ORDER = {value: index for index, value in enumerate(WarningCode)}


def _reject(code: str, path: str) -> None:
    raise MediaResolveValidationError(code, path)


def _require_enum(value: Any, enum_type: type[Enum], path: str) -> None:
    if type(value) is not enum_type:
        _reject("CORE_ENUM_INVALID", path)


def _require_identifier(value: Any, path: str) -> str:
    if type(value) is not str or not _ID_PATTERN.fullmatch(value):
        _reject("CORE_ID_INVALID", path)
    return value


def _require_non_empty_text(value: Any, path: str, *, limit: int) -> str:
    if type(value) is not str or not value.strip() or len(value) > limit:
        _reject("CORE_TEXT_INVALID", path)
    return value


def _require_int(value: Any, path: str, *, minimum: int) -> int:
    if type(value) is not int or value < minimum:
        _reject("CORE_INTEGER_INVALID", path)
    return value


def _freeze_fact(value: Any, kind: str, path: str) -> Mapping[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        _reject("CORE_FACT_INVALID", path)
    fact = dict(value)
    evidence = fact.get("evidence")
    _require_enum(evidence, EvidenceState, f"{path}.evidence")

    if evidence is EvidenceState.UNKNOWN:
        if set(fact) != {"evidence"}:
            _reject("CORE_UNKNOWN_FACT_HAS_VALUE", path)
        return MappingProxyType(fact)

    if kind == "dimensions":
        if set(fact) != {"width", "height", "evidence"}:
            _reject("CORE_DIMENSIONS_SHAPE_INVALID", path)
        _require_int(fact["width"], f"{path}.width", minimum=1)
        _require_int(fact["height"], f"{path}.height", minimum=1)
    elif kind in {"codec", "container"}:
        if set(fact) != {"value", "evidence"}:
            _reject("CORE_TEXT_FACT_SHAPE_INVALID", path)
        token = fact["value"]
        if type(token) is not str or not _PHYSICAL_TOKEN_PATTERN.fullmatch(token):
            _reject("CORE_PHYSICAL_TOKEN_INVALID", f"{path}.value")
    elif kind == "bitrate":
        if set(fact) != {"bitsPerSecond", "evidence"}:
            _reject("CORE_BITRATE_SHAPE_INVALID", path)
        _require_int(fact["bitsPerSecond"], f"{path}.bitsPerSecond", minimum=1)
    elif kind == "size":
        keys = set(fact)
        value_keys = keys & {"bytes", "estimatedBytes"}
        if keys - {"bytes", "estimatedBytes", "evidence"} or len(value_keys) != 1:
            _reject("CORE_SIZE_SHAPE_INVALID", path)
        value_key = next(iter(value_keys))
        if keys != {value_key, "evidence"}:
            _reject("CORE_SIZE_SHAPE_INVALID", path)
        _require_int(fact[value_key], f"{path}.{value_key}", minimum=0)
        if value_key == "estimatedBytes" and evidence is not EvidenceState.ASSERTED:
            _reject("CORE_ESTIMATE_EVIDENCE_INVALID", f"{path}.evidence")
    else:
        _reject("CORE_FACT_KIND_INVALID", path)
    return MappingProxyType(fact)


def _canonical_warnings(values: Iterable[WarningCode]) -> tuple[WarningCode, ...]:
    warnings = tuple(values)
    for index, warning in enumerate(warnings):
        _require_enum(warning, WarningCode, f"resolution.warnings[{index}]")
    return tuple(sorted(set(warnings), key=_WARNING_ORDER.__getitem__))


@dataclass(frozen=True)
class ResolveRequest:
    """One untrusted reference plus the two accepted optional preferences."""

    source_reference: str = field(repr=False)
    desired_media_capability: DesiredMediaCapability = (
        DesiredMediaCapability.ANY_RESOLVABLE
    )
    quality_preference: QualityPreference | None = None

    def __post_init__(self) -> None:
        _require_non_empty_text(self.source_reference, "sourceReference", limit=4096)
        _require_enum(
            self.desired_media_capability,
            DesiredMediaCapability,
            "desiredMediaCapability",
        )
        if self.quality_preference is not None:
            _require_enum(
                self.quality_preference,
                QualityPreference,
                "qualityPreference",
            )


@dataclass(frozen=True)
class SourceIdentity:
    """Adapter-verified public source identity; presence means verified."""

    source_namespace: str
    item_id: str

    def __post_init__(self) -> None:
        _require_identifier(self.source_namespace, "sourceIdentity.sourceNamespace")
        _require_identifier(self.item_id, "sourceIdentity.itemId")


@dataclass(frozen=True)
class MediaCandidate:
    """One redacted representation description with optional inline facts."""

    candidate_id: str
    dimensions: Mapping[str, Any] | None = None
    codec: Mapping[str, Any] | None = None
    container: Mapping[str, Any] | None = None
    bitrate: Mapping[str, Any] | None = None
    size: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        _require_identifier(self.candidate_id, "candidateId")
        for name in ("dimensions", "codec", "container", "bitrate", "size"):
            object.__setattr__(
                self,
                name,
                _freeze_fact(getattr(self, name), name, f"candidate.{name}"),
            )


@dataclass(frozen=True)
class CandidateProjection:
    """Bounded candidate surface and explicit selection ownership."""

    completeness: CandidateCompleteness
    selection_owner: SelectionOwner
    candidates: tuple[MediaCandidate, ...]
    selected_candidate_id: str | None = None

    def __post_init__(self) -> None:
        _require_enum(self.completeness, CandidateCompleteness, "completeness")
        _require_enum(self.selection_owner, SelectionOwner, "selectionOwner")
        candidates = tuple(self.candidates)
        if not candidates or any(type(value) is not MediaCandidate for value in candidates):
            _reject("CORE_CANDIDATES_EMPTY_OR_INVALID", "candidates")
        candidates = tuple(sorted(candidates, key=lambda value: value.candidate_id))
        candidate_ids = [value.candidate_id for value in candidates]
        if len(candidate_ids) != len(set(candidate_ids)):
            _reject("CORE_CANDIDATE_ID_DUPLICATE", "candidates")
        object.__setattr__(self, "candidates", candidates)

        if self.selection_owner is SelectionOwner.CONSUMER:
            if self.selected_candidate_id is not None:
                _reject("CORE_CONSUMER_SELECTION_PROHIBITED", "selectedCandidateId")
        else:
            if self.selected_candidate_id is None:
                _reject("CORE_SELECTED_CANDIDATE_REQUIRED", "selectedCandidateId")
            _require_identifier(self.selected_candidate_id, "selectedCandidateId")
            if candidate_ids.count(self.selected_candidate_id) != 1:
                _reject("CORE_SELECTED_CANDIDATE_INVALID", "selectedCandidateId")

        if self.completeness is CandidateCompleteness.SELECTED_ONLY:
            if len(candidates) != 1 or self.selected_candidate_id != candidate_ids[0]:
                _reject("CORE_SELECTED_ONLY_INVALID", "candidateProjection")


@dataclass(frozen=True)
class MediaItem:
    """One logical physical media item, not a locator or representation."""

    media_item_id: str
    media_type: MediaType
    candidate_projection: CandidateProjection

    def __post_init__(self) -> None:
        _require_identifier(self.media_item_id, "mediaItemId")
        _require_enum(self.media_type, MediaType, "mediaType")
        if type(self.candidate_projection) is not CandidateProjection:
            _reject("CORE_PROJECTION_INVALID", "candidateProjection")


@dataclass(frozen=True)
class ResolveResult:
    """Resolved or failed result with exactly one conditional payload group."""

    outcome: ResultOutcome
    resolution: Mapping[str, Any] | None = None
    failure: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        _require_enum(self.outcome, ResultOutcome, "outcome")
        if self.outcome is ResultOutcome.RESOLVED:
            if self.resolution is None or self.failure is not None:
                _reject("CORE_RESULT_GROUP_INVALID", "ResolveResult")
            object.__setattr__(self, "resolution", self._validate_resolution())
        else:
            if self.failure is None or self.resolution is not None:
                _reject("CORE_RESULT_GROUP_INVALID", "ResolveResult")
            object.__setattr__(self, "failure", self._validate_failure())

    def _validate_resolution(self) -> Mapping[str, Any]:
        if not isinstance(self.resolution, Mapping):
            _reject("CORE_RESOLUTION_INVALID", "resolution")
        resolution = dict(self.resolution)
        required = {
            "resultId",
            "sourceIdentity",
            "mediaItemsCompleteness",
            "mediaItems",
        }
        if not required.issubset(resolution) or set(resolution) - (required | {"warnings"}):
            _reject("CORE_RESOLUTION_FIELDS_INVALID", "resolution")
        _require_identifier(resolution["resultId"], "resolution.resultId")
        if type(resolution["sourceIdentity"]) is not SourceIdentity:
            _reject("CORE_SOURCE_IDENTITY_INVALID", "resolution.sourceIdentity")
        _require_enum(
            resolution["mediaItemsCompleteness"],
            MediaItemsCompleteness,
            "resolution.mediaItemsCompleteness",
        )
        media_items = tuple(resolution["mediaItems"])
        if not media_items or any(type(value) is not MediaItem for value in media_items):
            _reject("CORE_MEDIA_ITEMS_EMPTY_OR_INVALID", "resolution.mediaItems")
        media_items = tuple(sorted(media_items, key=lambda value: value.media_item_id))
        item_ids = [value.media_item_id for value in media_items]
        if len(item_ids) != len(set(item_ids)):
            _reject("CORE_MEDIA_ITEM_ID_DUPLICATE", "resolution.mediaItems")
        candidate_ids = [
            candidate.candidate_id
            for item in media_items
            for candidate in item.candidate_projection.candidates
        ]
        if len(candidate_ids) != len(set(candidate_ids)):
            _reject("CORE_CANDIDATE_ID_DUPLICATE", "resolution.mediaItems")
        resolution["mediaItems"] = media_items
        warnings = _canonical_warnings(resolution.get("warnings", ()))
        if warnings:
            resolution["warnings"] = warnings
        else:
            resolution.pop("warnings", None)
        return MappingProxyType(resolution)

    def _validate_failure(self) -> Mapping[str, Any]:
        if not isinstance(self.failure, Mapping):
            _reject("CORE_FAILURE_INVALID", "failure")
        failure = dict(self.failure)
        if set(failure) != {"kind", "retryability", "fallback", "userAction"}:
            _reject("CORE_FAILURE_FIELDS_INVALID", "failure")
        _require_enum(failure["kind"], FailureKind, "failure.kind")
        _require_enum(failure["retryability"], Retryability, "failure.retryability")
        _require_enum(failure["fallback"], Fallback, "failure.fallback")
        _require_enum(failure["userAction"], UserAction, "failure.userAction")
        if failure["kind"] is FailureKind.UNSUPPORTED_SOURCE:
            if failure["retryability"] is not Retryability.NOT_RETRYABLE:
                _reject("CORE_UNSUPPORTED_RETRYABILITY_INVALID", "failure.retryability")
            if failure["userAction"] is not UserAction.CHANGE_INPUT:
                _reject("CORE_UNSUPPORTED_ACTION_INVALID", "failure.userAction")
        if failure["kind"] is FailureKind.SECURITY_REJECTED:
            if failure["retryability"] is not Retryability.NOT_RETRYABLE:
                _reject("CORE_SECURITY_RETRYABILITY_INVALID", "failure.retryability")
            if failure["userAction"] is UserAction.REPAIR_CREDENTIAL_ACCESS:
                _reject("CORE_SECURITY_ACTION_INVALID", "failure.userAction")
        return MappingProxyType(failure)

    @classmethod
    def resolved(
        cls,
        *,
        result_id: str,
        source_identity: SourceIdentity,
        media_items_completeness: MediaItemsCompleteness,
        media_items: Iterable[MediaItem],
        warnings: Iterable[WarningCode] = (),
    ) -> "ResolveResult":
        payload: dict[str, Any] = {
            "resultId": result_id,
            "sourceIdentity": source_identity,
            "mediaItemsCompleteness": media_items_completeness,
            "mediaItems": tuple(media_items),
        }
        warnings = tuple(warnings)
        if warnings:
            payload["warnings"] = warnings
        return cls(outcome=ResultOutcome.RESOLVED, resolution=payload)

    @classmethod
    def failed(
        cls,
        *,
        kind: FailureKind,
        retryability: Retryability,
        fallback: Fallback,
        user_action: UserAction,
    ) -> "ResolveResult":
        return cls(
            outcome=ResultOutcome.FAILED,
            failure={
                "kind": kind,
                "retryability": retryability,
                "fallback": fallback,
                "userAction": user_action,
            },
        )

    @property
    def result_id(self) -> str | None:
        return self.resolution["resultId"] if self.resolution is not None else None

    @property
    def source_identity(self) -> SourceIdentity | None:
        return self.resolution["sourceIdentity"] if self.resolution is not None else None

    @property
    def media_items(self) -> tuple[MediaItem, ...]:
        return self.resolution["mediaItems"] if self.resolution is not None else ()

    @property
    def warnings(self) -> tuple[WarningCode, ...]:
        if self.resolution is None:
            return ()
        return self.resolution.get("warnings", ())


def assert_result_id_reuse_safe(results: Iterable[ResolveResult]) -> None:
    """Reject one resultId aliasing two different immutable semantic results."""

    by_id: dict[str, ResolveResult] = {}
    for index, result in enumerate(results):
        if type(result) is not ResolveResult:
            _reject("CORE_RESULT_INVALID", f"results[{index}]")
        if result.outcome is ResultOutcome.FAILED:
            continue
        existing = by_id.setdefault(result.result_id, result)
        if existing != result:
            _reject("CORE_RESULT_ID_ALIAS", f"results[{index}].resultId")


def assert_exhaustive_candidate_set(
    projection: CandidateProjection,
    known_candidate_ids: Iterable[str],
) -> None:
    """Check an EXHAUSTIVE claim against a binding-owned known fixture set."""

    if type(projection) is not CandidateProjection:
        _reject("CORE_PROJECTION_INVALID", "projection")
    if projection.completeness is not CandidateCompleteness.EXHAUSTIVE:
        _reject("CORE_EXHAUSTIVE_CLAIM_REQUIRED", "projection.completeness")
    expected = tuple(sorted(known_candidate_ids))
    for index, value in enumerate(expected):
        _require_identifier(value, f"knownCandidateIds[{index}]")
    actual = tuple(value.candidate_id for value in projection.candidates)
    if actual != expected:
        _reject("CORE_EXHAUSTIVE_SET_MISMATCH", "projection.candidates")


__all__ = (
    "CandidateCompleteness",
    "CandidateProjection",
    "DesiredMediaCapability",
    "EvidenceState",
    "FailureKind",
    "Fallback",
    "MediaCandidate",
    "MediaItem",
    "MediaItemsCompleteness",
    "MediaResolveValidationError",
    "MediaType",
    "QualityPreference",
    "ResolveRequest",
    "ResolveResult",
    "ResultOutcome",
    "Retryability",
    "SelectionOwner",
    "SourceIdentity",
    "UserAction",
    "WarningCode",
    "assert_exhaustive_candidate_set",
    "assert_result_id_reuse_safe",
)
