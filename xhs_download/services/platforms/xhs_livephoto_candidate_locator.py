"""Dormant, URL-free XHS LivePhoto candidate selection."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from ..image_formats import normalize_image_content_type, prefix_looks_like_non_image
from .xhs_livephoto_relationship_mapper import (
    XHS_LIVEPHOTO_MAX_CANDIDATES_PER_ITEM,
    XHS_LIVEPHOTO_MAX_CODEC_FAMILIES,
    XHS_LIVEPHOTO_MAX_INFO_ITEMS,
    XHS_LIVEPHOTO_MAX_STREAM_VARIANTS,
    XHS_LIVEPHOTO_RELATIONSHIP_MAX_ITEMS,
    XHS_LIVEPHOTO_RELATIONSHIP_SCHEMA_VERSION,
    XhsLivePhotoRelationshipMapV1,
)


XHS_LIVEPHOTO_CANDIDATE_SCHEMA_VERSION = "1.0"
XHS_LIVEPHOTO_LOCATOR_SCOPE = "same_selected_payload_snapshot"

_MEMBER_ROLES = frozenset({"still", "motion"})
_SOURCE_KINDS = frozenset(
    {"url_default", "info_list_url", "master_url", "backup_url"}
)
_STILL_SOURCE_KINDS = frozenset({"url_default", "info_list_url"})
_MOTION_SOURCE_KINDS = frozenset({"master_url", "backup_url"})
_REVIEWED_STILL_SCENE = "WB_DFT"
_REVIEWED_MOTION_CODEC_KEY = "h264"
_REVIEWED_MOTION_CODEC_CLASS = "avc"

_GENERIC_ERROR_CODE = "XHS_LIVEPHOTO_CANDIDATE_ERROR"
_ERROR_CODES = frozenset(
    {
        _GENERIC_ERROR_CODE,
        "XHS_LIVEPHOTO_CANDIDATE_COUNT_MISMATCH",
        "XHS_LIVEPHOTO_CANDIDATE_IMAGE_LIST_INVALID",
        "XHS_LIVEPHOTO_CANDIDATE_ITEM_INVALID",
        "XHS_LIVEPHOTO_CANDIDATE_ITEM_KIND_MISMATCH",
        "XHS_LIVEPHOTO_CANDIDATE_LOCATOR_INVALID",
        "XHS_LIVEPHOTO_CANDIDATE_MAP_INVALID",
        "XHS_LIVEPHOTO_CANDIDATE_MASTER_FIELD_INVALID",
        "XHS_LIVEPHOTO_CANDIDATE_MEMBER_MISSING",
        "XHS_LIVEPHOTO_CANDIDATE_MOTION_POLICY_AMBIGUOUS",
        "XHS_LIVEPHOTO_CANDIDATE_RELATIONSHIP_INVALID",
        "XHS_LIVEPHOTO_CANDIDATE_STILL_POLICY_AMBIGUOUS",
        "XHS_LIVEPHOTO_CANDIDATE_STILL_POLICY_DRIFT",
        "XHS_LIVEPHOTO_CANDIDATE_UNSUPPORTED_CODEC",
        "XHS_LIVEPHOTO_CANDIDATE_UNSUPPORTED_NOTE_KIND",
    }
)
_SAFE_ERROR_PATH_RE = re.compile(
    r"^(?:relationship_map|candidate_map|image_list|locator|unknown|"
    r"image_list\[\d+\](?:\.livePhoto|\.urlDefault|\.infoList|\.stream)?|"
    r"image_list\[\d+\]\.infoList\[\d+\](?:\.url|\.imageScene)?|"
    r"image_list\[\d+\]\.stream\[\d+\](?:\[\d+\](?:\.masterUrl|"
    r"\.backupUrls(?:\[\d+\])?)?)?)$"
)

_STILL_HOST = "sns-webpic-qc.xhscdn.com"
_MOTION_VERSIONED_HOST_RE = re.compile(r"^sns-video-v[0-9]+\.xhscdn\.com$")
_MOTION_BACKUP_HOST_RE = re.compile(r"^sns-bak-v[0-9]+\.xhscdn\.com$")
_MOTION_ZL_HOST = "sns-video-zl.xhscdn.com"

_MOTION_CONTENT_TYPES = {
    "video/mp4": "mp4",
    "video/quicktime": "mov",
    "video/webm": "webm",
}
_MP4_BRANDS = frozenset(
    {
        b"avc1",
        b"dash",
        b"hev1",
        b"hvc1",
        b"iso2",
        b"iso3",
        b"iso4",
        b"iso5",
        b"iso6",
        b"isom",
        b"M4V ",
        b"mp41",
        b"mp42",
        b"MSNV",
    }
)


class XhsLivePhotoCandidateError(ValueError):
    """Fixed-code candidate error that never includes rejected values."""

    def __init__(self, code: str, path: str):
        if (
            type(code) is not str
            or code not in _ERROR_CODES
            or type(path) is not str
            or not _SAFE_ERROR_PATH_RE.fullmatch(path)
        ):
            code = _GENERIC_ERROR_CODE
            path = "unknown"
        self.code = code
        self.path = path
        super().__init__(f"{code} path={path}")


def _reject(code: str, path: str) -> None:
    raise XhsLivePhotoCandidateError(code, path)


def _valid_index(value: Any, upper_bound: int) -> bool:
    return type(value) is int and 0 <= value < upper_bound


@dataclass(frozen=True)
class XhsLivePhotoMemberLocatorV1:
    source_item_ordinal: int
    member_role: str
    source_kind: str
    info_list_index: int | None = None
    codec_family_ordinal: int | None = None
    codec_family_class: str | None = None
    stream_variant_index: int | None = None
    backup_index: int | None = None

    def __post_init__(self) -> None:
        if (
            not _valid_index(
                self.source_item_ordinal,
                XHS_LIVEPHOTO_RELATIONSHIP_MAX_ITEMS,
            )
            or self.member_role not in _MEMBER_ROLES
            or self.source_kind not in _SOURCE_KINDS
        ):
            _reject("XHS_LIVEPHOTO_CANDIDATE_LOCATOR_INVALID", "locator")

        if self.member_role == "still":
            valid = (
                self.source_kind in _STILL_SOURCE_KINDS
                and self.codec_family_ordinal is None
                and self.codec_family_class is None
                and self.stream_variant_index is None
                and self.backup_index is None
                and (
                    (self.source_kind == "url_default" and self.info_list_index is None)
                    or (
                        self.source_kind == "info_list_url"
                        and _valid_index(
                            self.info_list_index,
                            XHS_LIVEPHOTO_MAX_INFO_ITEMS,
                        )
                    )
                )
            )
        else:
            valid = (
                self.source_kind in _MOTION_SOURCE_KINDS
                and self.info_list_index is None
                and _valid_index(
                    self.codec_family_ordinal,
                    XHS_LIVEPHOTO_MAX_CODEC_FAMILIES,
                )
                and self.codec_family_class == _REVIEWED_MOTION_CODEC_CLASS
                and _valid_index(
                    self.stream_variant_index,
                    XHS_LIVEPHOTO_MAX_STREAM_VARIANTS,
                )
                and (
                    (self.source_kind == "master_url" and self.backup_index is None)
                    or (
                        self.source_kind == "backup_url"
                        and _valid_index(
                            self.backup_index,
                            XHS_LIVEPHOTO_MAX_CANDIDATES_PER_ITEM,
                        )
                    )
                )
            )
        if not valid:
            _reject("XHS_LIVEPHOTO_CANDIDATE_LOCATOR_INVALID", "locator")

    def to_structural_dict(self) -> dict[str, Any]:
        return {
            "source_item_ordinal": self.source_item_ordinal,
            "member_role": self.member_role,
            "source_kind": self.source_kind,
            "info_list_index": self.info_list_index,
            "codec_family_ordinal": self.codec_family_ordinal,
            "codec_family_class": self.codec_family_class,
            "stream_variant_index": self.stream_variant_index,
            "backup_index": self.backup_index,
        }


@dataclass(frozen=True)
class XhsLivePhotoItemCandidateMapV1:
    source_item_ordinal: int
    item_kind: str
    still_locator: XhsLivePhotoMemberLocatorV1
    motion_locator: XhsLivePhotoMemberLocatorV1 | None

    def __post_init__(self) -> None:
        valid = (
            _valid_index(
                self.source_item_ordinal,
                XHS_LIVEPHOTO_RELATIONSHIP_MAX_ITEMS,
            )
            and self.item_kind in {"regular_image", "live_photo"}
            and type(self.still_locator) is XhsLivePhotoMemberLocatorV1
            and self.still_locator.source_item_ordinal == self.source_item_ordinal
            and self.still_locator.member_role == "still"
        )
        if self.item_kind == "regular_image":
            valid = valid and self.motion_locator is None
        else:
            valid = (
                valid
                and type(self.motion_locator) is XhsLivePhotoMemberLocatorV1
                and self.motion_locator.source_item_ordinal == self.source_item_ordinal
                and self.motion_locator.member_role == "motion"
            )
        if not valid:
            _reject("XHS_LIVEPHOTO_CANDIDATE_ITEM_INVALID", "candidate_map")

    def to_structural_dict(self) -> dict[str, Any]:
        return {
            "source_item_ordinal": self.source_item_ordinal,
            "item_kind": self.item_kind,
            "still_locator": self.still_locator.to_structural_dict(),
            "motion_locator": (
                self.motion_locator.to_structural_dict()
                if self.motion_locator is not None
                else None
            ),
        }


@dataclass(frozen=True)
class XhsLivePhotoCandidateMapV1:
    schema_version: str
    relationship_schema_version: str
    items: tuple[XhsLivePhotoItemCandidateMapV1, ...]

    def __post_init__(self) -> None:
        if (
            self.schema_version != XHS_LIVEPHOTO_CANDIDATE_SCHEMA_VERSION
            or self.relationship_schema_version
            != XHS_LIVEPHOTO_RELATIONSHIP_SCHEMA_VERSION
            or type(self.items) is not tuple
            or not self.items
            or len(self.items) > XHS_LIVEPHOTO_RELATIONSHIP_MAX_ITEMS
            or any(
                type(item) is not XhsLivePhotoItemCandidateMapV1
                or item.source_item_ordinal != index
                for index, item in enumerate(self.items)
            )
        ):
            _reject("XHS_LIVEPHOTO_CANDIDATE_MAP_INVALID", "candidate_map")

    def to_structural_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "relationship_schema_version": self.relationship_schema_version,
            "items": [item.to_structural_dict() for item in self.items],
        }


@dataclass(frozen=True)
class XhsPrivateMediaCandidateV1:
    value: str = field(repr=False, compare=False, hash=False)
    locator: XhsLivePhotoMemberLocatorV1
    member_role: str

    def __post_init__(self) -> None:
        if (
            type(self.value) is not str
            or not self.value.strip()
            or type(self.locator) is not XhsLivePhotoMemberLocatorV1
            or self.member_role != self.locator.member_role
        ):
            _reject("XHS_LIVEPHOTO_CANDIDATE_MEMBER_MISSING", "locator")

    def to_structural_dict(self) -> dict[str, Any]:
        return {
            "locator": self.locator.to_structural_dict(),
            "member_role": self.member_role,
        }


@dataclass(frozen=True)
class XhsMotionFormatEvidenceV1:
    format_class: str = ""
    confidence: str = ""
    error_code: str = ""
    metadata_mismatch: bool = False

    @property
    def valid(self) -> bool:
        return self.format_class in {"mp4", "mov", "webm"}


def _candidate_value(value: Any, *, path: str) -> str:
    if value in (None, ""):
        return ""
    if type(value) is not str:
        _reject("XHS_LIVEPHOTO_CANDIDATE_ITEM_INVALID", path)
    return value.strip()


def _still_locator(
    item: Mapping[str, Any],
    *,
    source_item_ordinal: int,
) -> tuple[int, XhsLivePhotoMemberLocatorV1]:
    path = f"image_list[{source_item_ordinal}]"
    default_value = _candidate_value(
        item.get("urlDefault"),
        path=f"{path}.urlDefault",
    )
    count = int(bool(default_value))

    info_list = item.get("infoList", ())
    if info_list is None:
        info_list = ()
    if not isinstance(info_list, (list, tuple)) or len(info_list) > XHS_LIVEPHOTO_MAX_INFO_ITEMS:
        _reject("XHS_LIVEPHOTO_CANDIDATE_ITEM_INVALID", f"{path}.infoList")

    reviewed_fallbacks = []
    for index, info in enumerate(info_list):
        info_path = f"{path}.infoList[{index}]"
        if not isinstance(info, Mapping):
            _reject("XHS_LIVEPHOTO_CANDIDATE_ITEM_INVALID", info_path)
        value = _candidate_value(info.get("url"), path=f"{info_path}.url")
        count += int(bool(value))
        if value and info.get("imageScene") == _REVIEWED_STILL_SCENE:
            reviewed_fallbacks.append((index, value))

    if default_value:
        if any(value != default_value for _, value in reviewed_fallbacks):
            _reject(
                "XHS_LIVEPHOTO_CANDIDATE_STILL_POLICY_DRIFT",
                f"{path}.infoList",
            )
        locator = XhsLivePhotoMemberLocatorV1(
            source_item_ordinal=source_item_ordinal,
            member_role="still",
            source_kind="url_default",
        )
    elif len(reviewed_fallbacks) == 1:
        locator = XhsLivePhotoMemberLocatorV1(
            source_item_ordinal=source_item_ordinal,
            member_role="still",
            source_kind="info_list_url",
            info_list_index=reviewed_fallbacks[0][0],
        )
    elif reviewed_fallbacks:
        _reject(
            "XHS_LIVEPHOTO_CANDIDATE_STILL_POLICY_AMBIGUOUS",
            f"{path}.infoList",
        )
    else:
        _reject("XHS_LIVEPHOTO_CANDIDATE_MEMBER_MISSING", f"{path}.urlDefault")
    return count, locator


def _master_candidate_value(
    variant: Mapping[str, Any],
    *,
    path: str,
) -> tuple[bool, str]:
    if "masterUrl" not in variant:
        return False, ""
    value = variant["masterUrl"]
    if type(value) is not str or not value.strip():
        _reject("XHS_LIVEPHOTO_CANDIDATE_MASTER_FIELD_INVALID", path)
    return True, value.strip()


def _motion_locators(
    item: Mapping[str, Any],
    *,
    source_item_ordinal: int,
) -> tuple[int, tuple[XhsLivePhotoMemberLocatorV1, ...]]:
    path = f"image_list[{source_item_ordinal}]"
    stream = item.get("stream", {})
    if stream is None:
        stream = {}
    if not isinstance(stream, Mapping) or len(stream) > XHS_LIVEPHOTO_MAX_CODEC_FAMILIES:
        _reject("XHS_LIVEPHOTO_CANDIDATE_ITEM_INVALID", f"{path}.stream")

    count = 0
    viable_h264 = []
    unsupported_candidates = False
    variant_count = 0
    for codec_ordinal, (codec_key, variants) in enumerate(stream.items()):
        codec_path = f"{path}.stream[{codec_ordinal}]"
        if type(codec_key) is not str or not isinstance(variants, (list, tuple)):
            _reject("XHS_LIVEPHOTO_CANDIDATE_ITEM_INVALID", codec_path)
        variant_count += len(variants)
        if variant_count > XHS_LIVEPHOTO_MAX_STREAM_VARIANTS:
            _reject("XHS_LIVEPHOTO_CANDIDATE_ITEM_INVALID", f"{path}.stream")
        for variant_index, variant in enumerate(variants):
            variant_path = f"{codec_path}[{variant_index}]"
            if not isinstance(variant, Mapping):
                _reject("XHS_LIVEPHOTO_CANDIDATE_ITEM_INVALID", variant_path)
            master_present, master = _master_candidate_value(
                variant,
                path=f"{variant_path}.masterUrl",
            )
            backups = variant.get("backupUrls", ())
            if backups is None:
                backups = ()
            if (
                not isinstance(backups, (list, tuple))
                or len(backups) > XHS_LIVEPHOTO_MAX_CANDIDATES_PER_ITEM
            ):
                _reject(
                    "XHS_LIVEPHOTO_CANDIDATE_ITEM_INVALID",
                    f"{variant_path}.backupUrls",
                )
            backup_indexes = []
            for backup_index, backup in enumerate(backups):
                value = _candidate_value(
                    backup,
                    path=f"{variant_path}.backupUrls[{backup_index}]",
                )
                if value:
                    backup_indexes.append(backup_index)

            candidate_count = int(bool(master)) + len(backup_indexes)
            count += candidate_count
            if not candidate_count:
                continue
            if codec_key != _REVIEWED_MOTION_CODEC_KEY:
                unsupported_candidates = True
                continue
            viable_h264.append(
                (
                    codec_ordinal,
                    variant_index,
                    master_present,
                    tuple(backup_indexes),
                )
            )

    if count > XHS_LIVEPHOTO_MAX_CANDIDATES_PER_ITEM:
        _reject("XHS_LIVEPHOTO_CANDIDATE_ITEM_INVALID", f"{path}.stream")
    if unsupported_candidates:
        _reject("XHS_LIVEPHOTO_CANDIDATE_UNSUPPORTED_CODEC", f"{path}.stream")
    if not viable_h264:
        return count, ()
    if len(viable_h264) != 1:
        _reject(
            "XHS_LIVEPHOTO_CANDIDATE_MOTION_POLICY_AMBIGUOUS",
            f"{path}.stream",
        )

    codec_ordinal, variant_index, has_master, backup_indexes = viable_h264[0]
    locators = []
    if has_master:
        locators.append(
            XhsLivePhotoMemberLocatorV1(
                source_item_ordinal=source_item_ordinal,
                member_role="motion",
                source_kind="master_url",
                codec_family_ordinal=codec_ordinal,
                codec_family_class=_REVIEWED_MOTION_CODEC_CLASS,
                stream_variant_index=variant_index,
            )
        )
    locators.extend(
        XhsLivePhotoMemberLocatorV1(
            source_item_ordinal=source_item_ordinal,
            member_role="motion",
            source_kind="backup_url",
            codec_family_ordinal=codec_ordinal,
            codec_family_class=_REVIEWED_MOTION_CODEC_CLASS,
            stream_variant_index=variant_index,
            backup_index=backup_index,
        )
        for backup_index in backup_indexes
    )
    return count, tuple(locators)


def _motion_locator(
    item: Mapping[str, Any],
    *,
    source_item_ordinal: int,
) -> tuple[int, XhsLivePhotoMemberLocatorV1 | None]:
    count, locators = _motion_locators(
        item,
        source_item_ordinal=source_item_ordinal,
    )
    return count, locators[0] if locators else None


def list_xhs_livephoto_motion_candidate_locators(
    *,
    image_list: Any,
    source_item_ordinal: int,
) -> tuple[XhsLivePhotoMemberLocatorV1, ...]:
    """Return same-item candidates in reviewed master/backup preference order."""

    if not isinstance(image_list, (list, tuple)) or not _valid_index(
        source_item_ordinal,
        len(image_list),
    ):
        _reject("XHS_LIVEPHOTO_CANDIDATE_LOCATOR_INVALID", "locator")
    item = image_list[source_item_ordinal]
    if not isinstance(item, Mapping):
        _reject("XHS_LIVEPHOTO_CANDIDATE_ITEM_INVALID", "locator")
    _, locators = _motion_locators(
        item,
        source_item_ordinal=source_item_ordinal,
    )
    return locators


def locate_xhs_livephoto_candidates(
    *,
    relationship_map: XhsLivePhotoRelationshipMapV1,
    image_list: Any,
) -> XhsLivePhotoCandidateMapV1:
    """Select URL-free locators from the exact image list used by the mapper."""

    if type(relationship_map) is not XhsLivePhotoRelationshipMapV1:
        _reject(
            "XHS_LIVEPHOTO_CANDIDATE_RELATIONSHIP_INVALID",
            "relationship_map",
        )
    if relationship_map.note_kind != "image":
        _reject(
            "XHS_LIVEPHOTO_CANDIDATE_UNSUPPORTED_NOTE_KIND",
            "relationship_map",
        )
    if not isinstance(image_list, (list, tuple)):
        _reject("XHS_LIVEPHOTO_CANDIDATE_IMAGE_LIST_INVALID", "image_list")
    if len(image_list) != len(relationship_map.items):
        _reject("XHS_LIVEPHOTO_CANDIDATE_COUNT_MISMATCH", "image_list")

    mapped_items = []
    for index, (relationship_item, item) in enumerate(
        zip(relationship_map.items, image_list)
    ):
        path = f"image_list[{index}]"
        if relationship_item.source_item_ordinal != index or not isinstance(item, Mapping):
            _reject("XHS_LIVEPHOTO_CANDIDATE_ITEM_INVALID", path)

        marker = item.get("livePhoto", False)
        if type(marker) is not bool:
            _reject("XHS_LIVEPHOTO_CANDIDATE_ITEM_INVALID", f"{path}.livePhoto")
        item_kind = "live_photo" if marker else "regular_image"
        if item_kind != relationship_item.item_kind:
            _reject("XHS_LIVEPHOTO_CANDIDATE_ITEM_KIND_MISMATCH", path)

        still_count, still_locator = _still_locator(
            item,
            source_item_ordinal=index,
        )
        motion_count, motion_locator = _motion_locator(
            item,
            source_item_ordinal=index,
        )
        if (
            still_count != relationship_item.still_candidate_count
            or motion_count != relationship_item.motion_candidate_count
        ):
            _reject("XHS_LIVEPHOTO_CANDIDATE_COUNT_MISMATCH", path)
        if item_kind == "live_photo" and motion_locator is None:
            _reject("XHS_LIVEPHOTO_CANDIDATE_MEMBER_MISSING", f"{path}.stream")
        if item_kind == "regular_image" and motion_locator is not None:
            _reject("XHS_LIVEPHOTO_CANDIDATE_ITEM_KIND_MISMATCH", path)

        mapped_items.append(
            XhsLivePhotoItemCandidateMapV1(
                source_item_ordinal=index,
                item_kind=item_kind,
                still_locator=still_locator,
                motion_locator=motion_locator,
            )
        )

    return XhsLivePhotoCandidateMapV1(
        schema_version=XHS_LIVEPHOTO_CANDIDATE_SCHEMA_VERSION,
        relationship_schema_version=relationship_map.schema_version,
        items=tuple(mapped_items),
    )


def read_candidate_from_locator(
    *,
    image_list: Any,
    locator: XhsLivePhotoMemberLocatorV1,
) -> XhsPrivateMediaCandidateV1:
    """Revalidate and read within the same selected payload snapshot."""

    if type(locator) is not XhsLivePhotoMemberLocatorV1:
        _reject("XHS_LIVEPHOTO_CANDIDATE_LOCATOR_INVALID", "locator")
    if not isinstance(image_list, (list, tuple)) or not _valid_index(
        locator.source_item_ordinal,
        len(image_list),
    ):
        _reject("XHS_LIVEPHOTO_CANDIDATE_LOCATOR_INVALID", "locator")
    item = image_list[locator.source_item_ordinal]
    if not isinstance(item, Mapping):
        _reject("XHS_LIVEPHOTO_CANDIDATE_ITEM_INVALID", "locator")

    if locator.member_role == "still":
        _, current_locator = _still_locator(
            item,
            source_item_ordinal=locator.source_item_ordinal,
        )
    else:
        _, current_locators = _motion_locators(
            item,
            source_item_ordinal=locator.source_item_ordinal,
        )
        current_locator = locator if locator in current_locators else None
    if current_locator != locator:
        _reject("XHS_LIVEPHOTO_CANDIDATE_LOCATOR_INVALID", "locator")

    value: Any
    if locator.source_kind == "url_default":
        value = item.get("urlDefault")
    elif locator.source_kind == "info_list_url":
        info_list = item.get("infoList")
        if not isinstance(info_list, (list, tuple)) or not _valid_index(
            locator.info_list_index,
            len(info_list),
        ):
            _reject("XHS_LIVEPHOTO_CANDIDATE_LOCATOR_INVALID", "locator")
        info = info_list[locator.info_list_index]
        if not isinstance(info, Mapping) or info.get("imageScene") != _REVIEWED_STILL_SCENE:
            _reject("XHS_LIVEPHOTO_CANDIDATE_LOCATOR_INVALID", "locator")
        value = info.get("url")
    else:
        stream = item.get("stream")
        if not isinstance(stream, Mapping):
            _reject("XHS_LIVEPHOTO_CANDIDATE_LOCATOR_INVALID", "locator")
        codec_items = tuple(stream.items())
        if not _valid_index(locator.codec_family_ordinal, len(codec_items)):
            _reject("XHS_LIVEPHOTO_CANDIDATE_LOCATOR_INVALID", "locator")
        codec_key, variants = codec_items[locator.codec_family_ordinal]
        if (
            codec_key != _REVIEWED_MOTION_CODEC_KEY
            or locator.codec_family_class != _REVIEWED_MOTION_CODEC_CLASS
            or not isinstance(variants, (list, tuple))
            or not _valid_index(locator.stream_variant_index, len(variants))
        ):
            _reject("XHS_LIVEPHOTO_CANDIDATE_LOCATOR_INVALID", "locator")
        variant = variants[locator.stream_variant_index]
        if not isinstance(variant, Mapping):
            _reject("XHS_LIVEPHOTO_CANDIDATE_LOCATOR_INVALID", "locator")
        if locator.source_kind == "master_url":
            if "masterUrl" not in variant:
                _reject("XHS_LIVEPHOTO_CANDIDATE_LOCATOR_INVALID", "locator")
            value = variant["masterUrl"]
        else:
            backups = variant.get("backupUrls")
            if not isinstance(backups, (list, tuple)) or not _valid_index(
                locator.backup_index,
                len(backups),
            ):
                _reject("XHS_LIVEPHOTO_CANDIDATE_LOCATOR_INVALID", "locator")
            value = backups[locator.backup_index]

    value = _candidate_value(value, path="locator")
    if not value:
        _reject("XHS_LIVEPHOTO_CANDIDATE_MEMBER_MISSING", "locator")
    return XhsPrivateMediaCandidateV1(
        value=value,
        locator=locator,
        member_role=locator.member_role,
    )


def resolve_xhs_motion_format_for_research(
    prefix: bytes = b"",
    *,
    content_type: str = "",
) -> XhsMotionFormatEvidenceV1:
    """Classify bounded motion prefixes for evidence; this grants no authority."""

    prefix = bytes(prefix or b"")
    declared_type = normalize_image_content_type(content_type)
    declared_format = _MOTION_CONTENT_TYPES.get(declared_type, "")

    magic_format = ""
    if len(prefix) >= 12 and prefix[4:8] == b"ftyp":
        brand = prefix[8:12]
        if brand == b"qt  ":
            magic_format = "mov"
        elif brand in _MP4_BRANDS:
            magic_format = "mp4"
    elif prefix.startswith(b"\x1aE\xdf\xa3"):
        magic_format = "webm"

    if magic_format:
        return XhsMotionFormatEvidenceV1(
            format_class=magic_format,
            confidence="magic",
            metadata_mismatch=bool(
                declared_format and declared_format != magic_format
            ),
        )
    if prefix_looks_like_non_image(prefix):
        return XhsMotionFormatEvidenceV1(error_code="MOTION_CONTENT_INVALID")
    if declared_type.startswith("text/") or declared_type in {
        "application/json",
        "application/xml",
        "image/svg+xml",
    }:
        return XhsMotionFormatEvidenceV1(error_code="MOTION_CONTENT_INVALID")
    return XhsMotionFormatEvidenceV1(error_code="MOTION_FORMAT_UNDETERMINED")


def classify_xhs_livephoto_host_for_research(
    value: Any,
    *,
    member_role: str,
) -> str:
    """Return a reviewed HTTPS host family without granting runtime admission."""

    if type(value) is not str or member_role not in _MEMBER_ROLES:
        return ""
    try:
        parsed = urlparse(value.strip())
        hostname = (parsed.hostname or "").lower()
        port = parsed.port
    except ValueError:
        return ""
    if (
        parsed.scheme != "https"
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or not parsed.path.strip("/")
    ):
        return ""
    if member_role == "still":
        return "still_exact" if hostname == _STILL_HOST else ""
    if _MOTION_VERSIONED_HOST_RE.fullmatch(hostname):
        return "motion_versioned"
    if hostname == _MOTION_ZL_HOST:
        return "motion_zl"
    if _MOTION_BACKUP_HOST_RE.fullmatch(hostname):
        return "motion_backup"
    return ""


__all__ = (
    "XHS_LIVEPHOTO_CANDIDATE_SCHEMA_VERSION",
    "XHS_LIVEPHOTO_LOCATOR_SCOPE",
    "XhsLivePhotoCandidateError",
    "XhsLivePhotoCandidateMapV1",
    "XhsLivePhotoItemCandidateMapV1",
    "XhsLivePhotoMemberLocatorV1",
    "XhsMotionFormatEvidenceV1",
    "XhsPrivateMediaCandidateV1",
    "classify_xhs_livephoto_host_for_research",
    "locate_xhs_livephoto_candidates",
    "list_xhs_livephoto_motion_candidate_locators",
    "read_candidate_from_locator",
    "resolve_xhs_motion_format_for_research",
)
