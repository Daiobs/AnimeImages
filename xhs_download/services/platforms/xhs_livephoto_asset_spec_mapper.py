"""Dormant, URL-free XHS LivePhoto asset identity and spec mapping."""

from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass, field
from typing import Any

from ..image_formats import resolve_image_format
from ..rich_media import RICH_MEDIA_MAX_ASSETS, RICH_MEDIA_MAX_GROUPS
from .xhs_livephoto_browser_admission import (
    XHS_LIVEPHOTO_BROWSER_ADMISSION_SCHEMA_VERSION,
    XHS_LIVEPHOTO_BROWSER_VERIFIED_HOST_CLASSES,
    XhsLivePhotoBrowserAdmissionError,
    XhsPrivateHttpsMediaCandidateV1,
    normalize_xhs_livephoto_candidate_https,
)
from .xhs_livephoto_candidate_locator import (
    XHS_LIVEPHOTO_CANDIDATE_SCHEMA_VERSION,
    XhsLivePhotoCandidateError,
    XhsLivePhotoCandidateMapV1,
    XhsLivePhotoMemberLocatorV1,
    read_candidate_from_locator,
    resolve_xhs_motion_format_for_research,
)
from .xhs_livephoto_relationship_mapper import (
    XHS_LIVEPHOTO_RELATIONSHIP_SCHEMA_VERSION,
    XhsLivePhotoRelationshipMapV1,
)
from .xhs_rich_media import XhsRichMediaAssetSpec


XHS_LIVEPHOTO_ASSET_SPEC_SCHEMA_VERSION = "1.0"
XHS_LIVEPHOTO_ASSET_ID_SCOPE = "result_and_order_scoped"
XHS_LIVEPHOTO_ASSET_SCOPE_HEX_LENGTH = 24

_NOTE_ID_RE = re.compile(r"^[0-9a-f]{24}$")
_BINDING_RE = re.compile(r"^[0-9a-f]{64}$")
_DOMAIN_ID_RE = re.compile(
    r"^[a-z][a-z0-9_-]{0,31}(?::[a-z0-9][a-z0-9_-]{0,31}){1,5}$"
)
_FORMAT_EXTENSIONS = {
    "jpeg": "jpg",
    "webp": "webp",
    "mp4": "mp4",
}
_PRIVATE_FORMAT_PROOF_SEAL = object()
_PRIVATE_MEMBER_PROOF_SEAL = object()
_POST_TYPES = frozenset({"image", "gallery", "live_photo", "multimedia"})
_GENERIC_ERROR_CODE = "XHS_LIVEPHOTO_ASSET_SPEC_ERROR"
_ERROR_CODES = frozenset(
    {
        _GENERIC_ERROR_CODE,
        "XHS_LIVEPHOTO_ASSET_SPEC_CANDIDATE_BINDING_MISMATCH",
        "XHS_LIVEPHOTO_ASSET_SPEC_CANDIDATE_MAP_MISMATCH",
        "XHS_LIVEPHOTO_ASSET_SPEC_FORMAT_CANDIDATE_BINDING_MISMATCH",
        "XHS_LIVEPHOTO_ASSET_SPEC_FORMAT_EVIDENCE_INVALID",
        "XHS_LIVEPHOTO_ASSET_SPEC_HOST_UNVERIFIED",
        "XHS_LIVEPHOTO_ASSET_SPEC_IMAGE_LIST_SNAPSHOT_MISMATCH",
        "XHS_LIVEPHOTO_ASSET_SPEC_INPUT_INVALID",
        "XHS_LIVEPHOTO_ASSET_SPEC_MEMBER_PROOF_DUPLICATE",
        "XHS_LIVEPHOTO_ASSET_SPEC_MEMBER_PROOF_INVALID",
        "XHS_LIVEPHOTO_ASSET_SPEC_MEMBER_PROOF_MISSING",
        "XHS_LIVEPHOTO_ASSET_SPEC_OUTPUT_INVALID",
        "XHS_LIVEPHOTO_ASSET_SPEC_PHYSICAL_ASSET_LIMIT",
        "XHS_LIVEPHOTO_ASSET_SPEC_RELATIONSHIP_MISMATCH",
        "XHS_LIVEPHOTO_ASSET_SPEC_ROLE_MISMATCH",
        "XHS_LIVEPHOTO_ASSET_SPEC_GROUP_LIMIT",
        "XHS_LIVEPHOTO_ASSET_SPEC_SOURCE_IDENTITY_INVALID",
        "XHS_LIVEPHOTO_ASSET_SPEC_SOURCE_IDENTITY_MISMATCH",
    }
)


class XhsLivePhotoAssetSpecMappingError(ValueError):
    """Fixed-code failure that never renders identity or candidate material."""

    def __init__(self, code: str):
        if type(code) is not str or code not in _ERROR_CODES:
            code = _GENERIC_ERROR_CODE
        self.code = code
        super().__init__(code)


def _reject(code: str) -> None:
    raise XhsLivePhotoAssetSpecMappingError(code)


def _require_domain_id(value: Any) -> str:
    if type(value) is not str or not _DOMAIN_ID_RE.fullmatch(value):
        _reject("XHS_LIVEPHOTO_ASSET_SPEC_OUTPUT_INVALID")
    return value


def _require_verified_host_class(host_class: Any, *, member_role: str) -> str:
    if (
        type(host_class) is not str
        or host_class not in XHS_LIVEPHOTO_BROWSER_VERIFIED_HOST_CLASSES
        or (member_role == "still" and host_class != "still_exact")
        or (
            member_role == "motion"
            and host_class not in {"motion_versioned", "motion_backup"}
        )
    ):
        _reject("XHS_LIVEPHOTO_ASSET_SPEC_HOST_UNVERIFIED")
    return host_class


def _candidate_binding(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _resolve_format_and_extension(
    prefix: Any,
    *,
    member_role: str,
    content_type: Any,
) -> tuple[str, str]:
    if not isinstance(prefix, (bytes, bytearray, memoryview)) or type(
        content_type
    ) is not str:
        _reject("XHS_LIVEPHOTO_ASSET_SPEC_FORMAT_EVIDENCE_INVALID")
    prefix = bytes(prefix)
    if member_role == "still":
        format_evidence = resolve_image_format(
            prefix,
            content_type=content_type,
        )
        if (
            format_evidence.confidence != "magic"
            or not format_evidence.valid
            or format_evidence.format not in {"jpeg", "webp"}
        ):
            _reject("XHS_LIVEPHOTO_ASSET_SPEC_FORMAT_EVIDENCE_INVALID")
        format_class = format_evidence.format
    elif member_role == "motion":
        format_evidence = resolve_xhs_motion_format_for_research(
            prefix,
            content_type=content_type,
        )
        if (
            format_evidence.confidence != "magic"
            or not format_evidence.valid
            or format_evidence.format_class != "mp4"
        ):
            _reject("XHS_LIVEPHOTO_ASSET_SPEC_FORMAT_EVIDENCE_INVALID")
        format_class = format_evidence.format_class
    else:
        _reject("XHS_LIVEPHOTO_ASSET_SPEC_ROLE_MISMATCH")
    return format_class, _FORMAT_EXTENSIONS[format_class]


@dataclass(frozen=True)
class XhsLivePhotoPrivateFormatProofV1:
    member_role: str
    format_class: str
    extension: str
    confidence: str
    candidate_binding: str = field(repr=False, compare=False, hash=False)
    _private_factory_seal: object | None = field(
        default=None,
        repr=False,
        compare=False,
        hash=False,
    )

    def __post_init__(self) -> None:
        if self._private_factory_seal is not _PRIVATE_FORMAT_PROOF_SEAL:
            _reject("XHS_LIVEPHOTO_ASSET_SPEC_FORMAT_EVIDENCE_INVALID")
        if (
            self.member_role not in {"still", "motion"}
            or self.format_class not in _FORMAT_EXTENSIONS
            or self.extension != _FORMAT_EXTENSIONS[self.format_class]
            or (
                self.member_role == "still"
                and self.format_class not in {"jpeg", "webp"}
            )
            or (self.member_role == "motion" and self.format_class != "mp4")
            or self.confidence != "magic"
            or type(self.candidate_binding) is not str
            or not _BINDING_RE.fullmatch(self.candidate_binding)
        ):
            _reject("XHS_LIVEPHOTO_ASSET_SPEC_FORMAT_EVIDENCE_INVALID")
        object.__setattr__(self, "_private_factory_seal", None)

    def to_structural_dict(self) -> dict[str, Any]:
        return {
            "member_role": self.member_role,
            "format_class": self.format_class,
            "extension": self.extension,
            "confidence": self.confidence,
        }


@dataclass(frozen=True)
class XhsLivePhotoPrivateMemberProofV1:
    locator: XhsLivePhotoMemberLocatorV1
    member_role: str
    host_class: str
    format_class: str
    extension: str
    host_class_browser_verified: bool
    candidate_binding: str = field(repr=False, compare=False, hash=False)
    _private_factory_seal: object | None = field(
        default=None,
        repr=False,
        compare=False,
        hash=False,
    )

    def __post_init__(self) -> None:
        if self._private_factory_seal is not _PRIVATE_MEMBER_PROOF_SEAL:
            _reject("XHS_LIVEPHOTO_ASSET_SPEC_MEMBER_PROOF_INVALID")
        _require_verified_host_class(
            self.host_class,
            member_role=self.member_role,
        )
        if (
            type(self.locator) is not XhsLivePhotoMemberLocatorV1
            or self.member_role != self.locator.member_role
            or self.format_class not in _FORMAT_EXTENSIONS
            or self.extension != _FORMAT_EXTENSIONS[self.format_class]
            or (
                self.member_role == "still"
                and self.format_class not in {"jpeg", "webp"}
            )
            or (self.member_role == "motion" and self.format_class != "mp4")
            or self.host_class_browser_verified is not True
            or type(self.candidate_binding) is not str
            or not _BINDING_RE.fullmatch(self.candidate_binding)
        ):
            _reject("XHS_LIVEPHOTO_ASSET_SPEC_MEMBER_PROOF_INVALID")
        object.__setattr__(self, "_private_factory_seal", None)

    def to_structural_dict(self) -> dict[str, Any]:
        return {
            "locator": self.locator.to_structural_dict(),
            "member_role": self.member_role,
            "host_class": self.host_class,
            "format_class": self.format_class,
            "extension": self.extension,
            "host_class_browser_verified": self.host_class_browser_verified,
        }


@dataclass(frozen=True)
class XhsLivePhotoAssetBindingV1:
    asset_spec: XhsRichMediaAssetSpec
    locator: XhsLivePhotoMemberLocatorV1
    source_item_ordinal: int
    host_class: str
    format_class: str
    group_id: str | None

    def __post_init__(self) -> None:
        if type(self.locator) is not XhsLivePhotoMemberLocatorV1:
            _reject("XHS_LIVEPHOTO_ASSET_SPEC_OUTPUT_INVALID")
        _require_verified_host_class(
            self.host_class,
            member_role=self.locator.member_role,
        )
        expected_media_type = (
            "image" if self.locator.member_role == "still" else "video"
        )
        expected_extension = _FORMAT_EXTENSIONS.get(self.format_class, "")
        if (
            type(self.asset_spec) is not XhsRichMediaAssetSpec
            or self.source_item_ordinal != self.locator.source_item_ordinal
            or not expected_extension
            or self.asset_spec.media_type != expected_media_type
            or self.asset_spec.extension != expected_extension
            or (
                self.locator.member_role == "motion"
                and self.asset_spec.role != "motion"
            )
            or (
                self.locator.member_role == "still"
                and self.asset_spec.role not in {"primary", "gallery", "inline", "still"}
            )
            or (
                self.group_id is not None
                and not _DOMAIN_ID_RE.fullmatch(self.group_id)
            )
            or (
                self.group_id is None
                and self.asset_spec.role in {"still", "motion"}
            )
            or (
                self.group_id is not None
                and self.asset_spec.role not in {"still", "motion"}
            )
        ):
            _reject("XHS_LIVEPHOTO_ASSET_SPEC_OUTPUT_INVALID")
        _require_domain_id(self.asset_spec.asset_id)

    def to_structural_dict(self) -> dict[str, Any]:
        return {
            "asset_spec": {
                "asset_id": self.asset_spec.asset_id,
                "media_type": self.asset_spec.media_type,
                "extension": self.asset_spec.extension,
                "role": self.asset_spec.role,
            },
            "locator": self.locator.to_structural_dict(),
            "source_item_ordinal": self.source_item_ordinal,
            "host_class": self.host_class,
            "format_class": self.format_class,
            "group_id": self.group_id,
        }


@dataclass(frozen=True)
class XhsLivePhotoGroupSpecV1:
    group_id: str
    source_item_ordinal: int
    still_asset_id: str
    motion_asset_id: str

    def __post_init__(self) -> None:
        if (
            type(self.source_item_ordinal) is not int
            or self.source_item_ordinal < 0
            or self.still_asset_id == self.motion_asset_id
        ):
            _reject("XHS_LIVEPHOTO_ASSET_SPEC_OUTPUT_INVALID")
        _require_domain_id(self.group_id)
        _require_domain_id(self.still_asset_id)
        _require_domain_id(self.motion_asset_id)

    def to_structural_dict(self) -> dict[str, Any]:
        return {
            "group_id": self.group_id,
            "source_item_ordinal": self.source_item_ordinal,
            "still_asset_id": self.still_asset_id,
            "motion_asset_id": self.motion_asset_id,
        }


@dataclass(frozen=True)
class XhsLivePhotoAssetSpecMapV1:
    schema_version: str
    relationship_schema_version: str
    candidate_schema_version: str
    browser_admission_schema_version: str
    post_type: str
    assets: tuple[XhsLivePhotoAssetBindingV1, ...]
    groups: tuple[XhsLivePhotoGroupSpecV1, ...]

    def __post_init__(self) -> None:
        if type(self.assets) is tuple and len(self.assets) > RICH_MEDIA_MAX_ASSETS:
            _reject("XHS_LIVEPHOTO_ASSET_SPEC_PHYSICAL_ASSET_LIMIT")
        if type(self.groups) is tuple and len(self.groups) > RICH_MEDIA_MAX_GROUPS:
            _reject("XHS_LIVEPHOTO_ASSET_SPEC_GROUP_LIMIT")
        if (
            self.schema_version != XHS_LIVEPHOTO_ASSET_SPEC_SCHEMA_VERSION
            or self.relationship_schema_version
            != XHS_LIVEPHOTO_RELATIONSHIP_SCHEMA_VERSION
            or self.candidate_schema_version != XHS_LIVEPHOTO_CANDIDATE_SCHEMA_VERSION
            or self.browser_admission_schema_version
            != XHS_LIVEPHOTO_BROWSER_ADMISSION_SCHEMA_VERSION
            or self.post_type not in _POST_TYPES
            or type(self.assets) is not tuple
            or not self.assets
            or type(self.groups) is not tuple
            or any(
                type(asset) is not XhsLivePhotoAssetBindingV1
                for asset in self.assets
            )
            or any(
                type(group) is not XhsLivePhotoGroupSpecV1
                for group in self.groups
            )
        ):
            _reject("XHS_LIVEPHOTO_ASSET_SPEC_OUTPUT_INVALID")

        asset_ids = [asset.asset_spec.asset_id for asset in self.assets]
        group_ids = [group.group_id for group in self.groups]
        if len(asset_ids) != len(set(asset_ids)) or len(group_ids) != len(
            set(group_ids)
        ):
            _reject("XHS_LIVEPHOTO_ASSET_SPEC_OUTPUT_INVALID")

        asset_by_id = {
            asset.asset_spec.asset_id: asset
            for asset in self.assets
        }
        asset_index_by_id = {
            asset.asset_spec.asset_id: index
            for index, asset in enumerate(self.assets)
        }
        assets_by_ordinal: dict[int, list[XhsLivePhotoAssetBindingV1]] = {}
        for asset in self.assets:
            assets_by_ordinal.setdefault(asset.source_item_ordinal, []).append(asset)

        group_by_ordinal: dict[int, XhsLivePhotoGroupSpecV1] = {}
        grouped_asset_ids: list[str] = []
        group_ordinals: list[int] = []
        for group in self.groups:
            if group.source_item_ordinal in group_by_ordinal:
                _reject("XHS_LIVEPHOTO_ASSET_SPEC_OUTPUT_INVALID")
            group_by_ordinal[group.source_item_ordinal] = group
            group_ordinals.append(group.source_item_ordinal)
            grouped_asset_ids.extend((group.still_asset_id, group.motion_asset_id))

        source_ordinals = [asset.source_item_ordinal for asset in self.assets]
        expected_ordinals = list(range(len(assets_by_ordinal)))
        grouped_asset_id_set = set(grouped_asset_ids)
        if (
            source_ordinals != sorted(source_ordinals)
            or group_ordinals != sorted(group_ordinals)
            or sorted(assets_by_ordinal) != expected_ordinals
            or not set(group_by_ordinal).issubset(assets_by_ordinal)
            or len(grouped_asset_ids) != len(grouped_asset_id_set)
            or any(asset_id not in asset_by_id for asset_id in grouped_asset_id_set)
            or any(
                (asset.group_id is None)
                != (asset.asset_spec.asset_id not in grouped_asset_id_set)
                for asset in self.assets
            )
        ):
            _reject("XHS_LIVEPHOTO_ASSET_SPEC_OUTPUT_INVALID")

        for ordinal in expected_ordinals:
            ordinal_assets = assets_by_ordinal[ordinal]
            group = group_by_ordinal.get(ordinal)
            if group is None:
                if (
                    len(ordinal_assets) != 1
                    or ordinal_assets[0].asset_spec.media_type != "image"
                    or ordinal_assets[0].group_id is not None
                ):
                    _reject("XHS_LIVEPHOTO_ASSET_SPEC_OUTPUT_INVALID")
                continue

            if len(ordinal_assets) != 2:
                _reject("XHS_LIVEPHOTO_ASSET_SPEC_OUTPUT_INVALID")
            still, motion = ordinal_assets
            if (
                still.asset_spec.asset_id != group.still_asset_id
                or motion.asset_spec.asset_id != group.motion_asset_id
                or still.group_id != group.group_id
                or motion.group_id != group.group_id
                or still.source_item_ordinal != group.source_item_ordinal
                or motion.source_item_ordinal != group.source_item_ordinal
                or still.asset_spec.role != "still"
                or motion.asset_spec.role != "motion"
                or asset_index_by_id[still.asset_spec.asset_id] + 1
                != asset_index_by_id[motion.asset_spec.asset_id]
            ):
                _reject("XHS_LIVEPHOTO_ASSET_SPEC_OUTPUT_INVALID")

        grouped_all = bool(self.groups) and len(grouped_asset_id_set) == len(
            self.assets
        )
        expected_post_type = (
            "live_photo"
            if grouped_all
            else "multimedia"
            if self.groups
            else "image"
            if len(self.assets) == 1
            else "gallery"
        )
        if self.post_type != expected_post_type:
            _reject("XHS_LIVEPHOTO_ASSET_SPEC_OUTPUT_INVALID")
        expected_regular_role = _regular_role(post_type=expected_post_type)
        if any(
            asset.group_id is None
            and asset.asset_spec.role != expected_regular_role
            for asset in self.assets
        ):
            _reject("XHS_LIVEPHOTO_ASSET_SPEC_OUTPUT_INVALID")

    def to_structural_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "relationship_schema_version": self.relationship_schema_version,
            "candidate_schema_version": self.candidate_schema_version,
            "browser_admission_schema_version": self.browser_admission_schema_version,
            "post_type": self.post_type,
            "asset_id_scope": XHS_LIVEPHOTO_ASSET_ID_SCOPE,
            "assets": [asset.to_structural_dict() for asset in self.assets],
            "groups": [group.to_structural_dict() for group in self.groups],
        }


def build_xhs_livephoto_private_format_proof(
    *,
    private_https_candidate: XhsPrivateHttpsMediaCandidateV1,
    prefix: bytes,
    content_type: str,
) -> XhsLivePhotoPrivateFormatProofV1:
    """Bind magic-derived format evidence to one exact private candidate."""

    if type(private_https_candidate) is not XhsPrivateHttpsMediaCandidateV1:
        _reject("XHS_LIVEPHOTO_ASSET_SPEC_FORMAT_EVIDENCE_INVALID")
    _require_verified_host_class(
        private_https_candidate.host_class,
        member_role=private_https_candidate.member_role,
    )
    if type(private_https_candidate.value) is not str:
        _reject("XHS_LIVEPHOTO_ASSET_SPEC_FORMAT_EVIDENCE_INVALID")
    format_class, extension = _resolve_format_and_extension(
        prefix,
        member_role=private_https_candidate.member_role,
        content_type=content_type,
    )
    return XhsLivePhotoPrivateFormatProofV1(
        member_role=private_https_candidate.member_role,
        format_class=format_class,
        extension=extension,
        confidence="magic",
        candidate_binding=_candidate_binding(private_https_candidate.value),
        _private_factory_seal=_PRIVATE_FORMAT_PROOF_SEAL,
    )


def build_xhs_livephoto_member_proof(
    *,
    locator: XhsLivePhotoMemberLocatorV1,
    private_https_candidate: XhsPrivateHttpsMediaCandidateV1,
    private_format_proof: XhsLivePhotoPrivateFormatProofV1,
) -> XhsLivePhotoPrivateMemberProofV1:
    """Combine class-level transport policy with exact-candidate format proof."""

    if type(locator) is not XhsLivePhotoMemberLocatorV1:
        _reject("XHS_LIVEPHOTO_ASSET_SPEC_MEMBER_PROOF_INVALID")
    if type(private_https_candidate) is not XhsPrivateHttpsMediaCandidateV1:
        _reject("XHS_LIVEPHOTO_ASSET_SPEC_MEMBER_PROOF_INVALID")
    if type(private_https_candidate.value) is not str:
        _reject("XHS_LIVEPHOTO_ASSET_SPEC_MEMBER_PROOF_INVALID")
    if private_https_candidate.member_role != locator.member_role:
        _reject("XHS_LIVEPHOTO_ASSET_SPEC_ROLE_MISMATCH")
    _require_verified_host_class(
        private_https_candidate.host_class,
        member_role=private_https_candidate.member_role,
    )
    if type(private_format_proof) is not XhsLivePhotoPrivateFormatProofV1:
        _reject("XHS_LIVEPHOTO_ASSET_SPEC_FORMAT_EVIDENCE_INVALID")
    if private_format_proof.member_role != locator.member_role:
        _reject("XHS_LIVEPHOTO_ASSET_SPEC_ROLE_MISMATCH")
    current_binding = _candidate_binding(private_https_candidate.value)
    if not hmac.compare_digest(
        current_binding,
        private_format_proof.candidate_binding,
    ):
        _reject("XHS_LIVEPHOTO_ASSET_SPEC_FORMAT_CANDIDATE_BINDING_MISMATCH")
    return XhsLivePhotoPrivateMemberProofV1(
        locator=locator,
        member_role=locator.member_role,
        host_class=private_https_candidate.host_class,
        format_class=private_format_proof.format_class,
        extension=private_format_proof.extension,
        host_class_browser_verified=True,
        candidate_binding=current_binding,
        _private_factory_seal=_PRIVATE_MEMBER_PROOF_SEAL,
    )


def _scope_key(payload_note_id: str) -> str:
    digest = hashlib.sha256(
        ("xhs-livephoto-asset-spec-v1:" + payload_note_id).encode("utf-8")
    ).hexdigest()
    return digest[:XHS_LIVEPHOTO_ASSET_SCOPE_HEX_LENGTH]


def _asset_id(scope_key: str, source_item_ordinal: int, member_role: str) -> str:
    return f"xhs:{scope_key}:i{source_item_ordinal}:{member_role}"


def _group_id(scope_key: str, source_item_ordinal: int) -> str:
    return f"xhs:{scope_key}:i{source_item_ordinal}:live"


def _validate_current_candidate(
    *,
    image_list: Any,
    proof: XhsLivePhotoPrivateMemberProofV1,
) -> None:
    try:
        private_candidate = read_candidate_from_locator(
            image_list=image_list,
            locator=proof.locator,
        )
        private_https_candidate = normalize_xhs_livephoto_candidate_https(
            private_candidate,
            member_role=proof.member_role,
        )
    except XhsLivePhotoBrowserAdmissionError as exc:
        if exc.code in {
            "XHS_LIVEPHOTO_BROWSER_HOST_CLASS_UNVERIFIED",
            "XHS_LIVEPHOTO_BROWSER_ADMISSION_HOST_REJECTED",
        }:
            _reject("XHS_LIVEPHOTO_ASSET_SPEC_HOST_UNVERIFIED")
        _reject("XHS_LIVEPHOTO_ASSET_SPEC_IMAGE_LIST_SNAPSHOT_MISMATCH")
    except XhsLivePhotoCandidateError:
        _reject("XHS_LIVEPHOTO_ASSET_SPEC_IMAGE_LIST_SNAPSHOT_MISMATCH")

    current_binding = _candidate_binding(private_https_candidate.value)
    if not hmac.compare_digest(current_binding, proof.candidate_binding):
        _reject("XHS_LIVEPHOTO_ASSET_SPEC_CANDIDATE_BINDING_MISMATCH")
    if (
        private_https_candidate.member_role != proof.member_role
        or private_https_candidate.host_class != proof.host_class
    ):
        _reject("XHS_LIVEPHOTO_ASSET_SPEC_MEMBER_PROOF_INVALID")


def _regular_role(*, post_type: str) -> str:
    if post_type == "image":
        return "primary"
    if post_type == "gallery":
        return "gallery"
    return "inline"


def validate_xhs_livephoto_asset_spec_limits(
    relationship_map: XhsLivePhotoRelationshipMapV1,
) -> None:
    if type(relationship_map) is not XhsLivePhotoRelationshipMapV1:
        _reject("XHS_LIVEPHOTO_ASSET_SPEC_RELATIONSHIP_MISMATCH")
    group_count = relationship_map.livephoto_count
    physical_asset_count = (
        relationship_map.regular_image_count + (group_count * 2)
    )
    if group_count > RICH_MEDIA_MAX_GROUPS:
        _reject("XHS_LIVEPHOTO_ASSET_SPEC_GROUP_LIMIT")
    if physical_asset_count > RICH_MEDIA_MAX_ASSETS:
        _reject("XHS_LIVEPHOTO_ASSET_SPEC_PHYSICAL_ASSET_LIMIT")


def build_xhs_livephoto_asset_specs(
    *,
    requested_note_id: str,
    payload_note_id: str,
    relationship_map: XhsLivePhotoRelationshipMapV1,
    candidate_map: XhsLivePhotoCandidateMapV1,
    image_list: Any,
    member_proofs: tuple[XhsLivePhotoPrivateMemberProofV1, ...],
) -> XhsLivePhotoAssetSpecMapV1:
    """Build deterministic, URL-free specs from one exact selected snapshot."""

    if (
        type(requested_note_id) is not str
        or type(payload_note_id) is not str
        or not _NOTE_ID_RE.fullmatch(requested_note_id)
        or not _NOTE_ID_RE.fullmatch(payload_note_id)
    ):
        _reject("XHS_LIVEPHOTO_ASSET_SPEC_SOURCE_IDENTITY_INVALID")
    if requested_note_id != payload_note_id:
        _reject("XHS_LIVEPHOTO_ASSET_SPEC_SOURCE_IDENTITY_MISMATCH")
    if type(relationship_map) is not XhsLivePhotoRelationshipMapV1:
        _reject("XHS_LIVEPHOTO_ASSET_SPEC_RELATIONSHIP_MISMATCH")
    if type(candidate_map) is not XhsLivePhotoCandidateMapV1:
        _reject("XHS_LIVEPHOTO_ASSET_SPEC_CANDIDATE_MAP_MISMATCH")
    if relationship_map.note_kind != "image" or not relationship_map.identity_confirmed:
        _reject("XHS_LIVEPHOTO_ASSET_SPEC_RELATIONSHIP_MISMATCH")
    if (
        relationship_map.schema_version
        != candidate_map.relationship_schema_version
        or len(relationship_map.items) != len(candidate_map.items)
    ):
        _reject("XHS_LIVEPHOTO_ASSET_SPEC_CANDIDATE_MAP_MISMATCH")
    if (
        not isinstance(image_list, (list, tuple))
        or len(image_list) != len(relationship_map.items)
    ):
        _reject("XHS_LIVEPHOTO_ASSET_SPEC_IMAGE_LIST_SNAPSHOT_MISMATCH")
    if type(member_proofs) is not tuple or any(
        type(proof) is not XhsLivePhotoPrivateMemberProofV1
        for proof in member_proofs
    ):
        _reject("XHS_LIVEPHOTO_ASSET_SPEC_MEMBER_PROOF_INVALID")

    validate_xhs_livephoto_asset_spec_limits(relationship_map)

    proofs_by_locator: dict[
        XhsLivePhotoMemberLocatorV1,
        XhsLivePhotoPrivateMemberProofV1,
    ] = {}
    for proof in member_proofs:
        if proof.locator in proofs_by_locator:
            _reject("XHS_LIVEPHOTO_ASSET_SPEC_MEMBER_PROOF_DUPLICATE")
        proofs_by_locator[proof.locator] = proof

    all_live = all(item.item_kind == "live_photo" for item in relationship_map.items)
    all_regular = all(
        item.item_kind == "regular_image" for item in relationship_map.items
    )
    post_type = (
        "live_photo"
        if all_live
        else "image"
        if all_regular and len(relationship_map.items) == 1
        else "gallery"
        if all_regular
        else "multimedia"
    )
    scope_key = _scope_key(payload_note_id)
    assets: list[XhsLivePhotoAssetBindingV1] = []
    groups: list[XhsLivePhotoGroupSpecV1] = []
    expected_locators: set[XhsLivePhotoMemberLocatorV1] = set()

    for relationship_item, candidate_item in zip(
        relationship_map.items,
        candidate_map.items,
    ):
        ordinal = relationship_item.source_item_ordinal
        if (
            candidate_item.source_item_ordinal != ordinal
            or candidate_item.item_kind != relationship_item.item_kind
        ):
            _reject("XHS_LIVEPHOTO_ASSET_SPEC_CANDIDATE_MAP_MISMATCH")

        locators = [candidate_item.still_locator]
        if candidate_item.motion_locator is not None:
            locators.append(candidate_item.motion_locator)
        for locator in locators:
            expected_locators.add(locator)
            proof = proofs_by_locator.get(locator)
            if proof is None:
                _reject("XHS_LIVEPHOTO_ASSET_SPEC_MEMBER_PROOF_MISSING")
            _validate_current_candidate(image_list=image_list, proof=proof)

        if relationship_item.item_kind == "regular_image":
            proof = proofs_by_locator[candidate_item.still_locator]
            asset = XhsRichMediaAssetSpec(
                asset_id=_asset_id(scope_key, ordinal, "still"),
                media_type="image",
                extension=proof.extension,
                role=_regular_role(post_type=post_type),
            )
            assets.append(
                XhsLivePhotoAssetBindingV1(
                    asset_spec=asset,
                    locator=proof.locator,
                    source_item_ordinal=ordinal,
                    host_class=proof.host_class,
                    format_class=proof.format_class,
                    group_id=None,
                )
            )
            continue

        if candidate_item.motion_locator is None:
            _reject("XHS_LIVEPHOTO_ASSET_SPEC_CANDIDATE_MAP_MISMATCH")
        still_proof = proofs_by_locator[candidate_item.still_locator]
        motion_proof = proofs_by_locator[candidate_item.motion_locator]
        group_id = _group_id(scope_key, ordinal)
        still_id = _asset_id(scope_key, ordinal, "still")
        motion_id = _asset_id(scope_key, ordinal, "motion")
        assets.extend(
            (
                XhsLivePhotoAssetBindingV1(
                    asset_spec=XhsRichMediaAssetSpec(
                        asset_id=still_id,
                        media_type="image",
                        extension=still_proof.extension,
                        role="still",
                    ),
                    locator=still_proof.locator,
                    source_item_ordinal=ordinal,
                    host_class=still_proof.host_class,
                    format_class=still_proof.format_class,
                    group_id=group_id,
                ),
                XhsLivePhotoAssetBindingV1(
                    asset_spec=XhsRichMediaAssetSpec(
                        asset_id=motion_id,
                        media_type="video",
                        extension=motion_proof.extension,
                        role="motion",
                    ),
                    locator=motion_proof.locator,
                    source_item_ordinal=ordinal,
                    host_class=motion_proof.host_class,
                    format_class=motion_proof.format_class,
                    group_id=group_id,
                ),
            )
        )
        groups.append(
            XhsLivePhotoGroupSpecV1(
                group_id=group_id,
                source_item_ordinal=ordinal,
                still_asset_id=still_id,
                motion_asset_id=motion_id,
            )
        )

    if set(proofs_by_locator) != expected_locators:
        _reject("XHS_LIVEPHOTO_ASSET_SPEC_MEMBER_PROOF_INVALID")
    return XhsLivePhotoAssetSpecMapV1(
        schema_version=XHS_LIVEPHOTO_ASSET_SPEC_SCHEMA_VERSION,
        relationship_schema_version=relationship_map.schema_version,
        candidate_schema_version=candidate_map.schema_version,
        browser_admission_schema_version=XHS_LIVEPHOTO_BROWSER_ADMISSION_SCHEMA_VERSION,
        post_type=post_type,
        assets=tuple(assets),
        groups=tuple(groups),
    )


__all__ = (
    "XHS_LIVEPHOTO_ASSET_ID_SCOPE",
    "XHS_LIVEPHOTO_ASSET_SCOPE_HEX_LENGTH",
    "XHS_LIVEPHOTO_ASSET_SPEC_SCHEMA_VERSION",
    "XhsLivePhotoAssetBindingV1",
    "XhsLivePhotoAssetSpecMapV1",
    "XhsLivePhotoAssetSpecMappingError",
    "XhsLivePhotoGroupSpecV1",
    "XhsLivePhotoPrivateFormatProofV1",
    "XhsLivePhotoPrivateMemberProofV1",
    "build_xhs_livephoto_asset_specs",
    "build_xhs_livephoto_member_proof",
    "build_xhs_livephoto_private_format_proof",
    "validate_xhs_livephoto_asset_spec_limits",
)
