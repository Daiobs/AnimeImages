"""Dormant, URL-free XHS LivePhoto relationship mapping."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


XHS_LIVEPHOTO_RELATIONSHIP_SCHEMA_VERSION = "1.0"
XHS_LIVEPHOTO_RELATIONSHIP_MAX_ITEMS = 64
XHS_LIVEPHOTO_MAX_INFO_ITEMS = 32
XHS_LIVEPHOTO_MAX_CODEC_FAMILIES = 16
XHS_LIVEPHOTO_MAX_STREAM_VARIANTS = 32
XHS_LIVEPHOTO_MAX_CANDIDATES_PER_ITEM = 64

_NOTE_ID_RE = re.compile(r"^[0-9a-f]{24}$")
_ITEM_KINDS = frozenset({"regular_image", "live_photo"})
_RELATIONSHIP_BASES = frozenset({"none", "same_item_explicit_fields"})
_FORMAT_STATUS = "unverified"
_GENERIC_ERROR_CODE = "XHS_LIVEPHOTO_MAPPING_ERROR"
_ERROR_CODES = frozenset(
    {
        _GENERIC_ERROR_CODE,
        "XHS_LIVEPHOTO_IMAGE_ITEM_INVALID",
        "XHS_LIVEPHOTO_IMAGE_LIST_INVALID",
        "XHS_LIVEPHOTO_INFO_LIST_INVALID",
        "XHS_LIVEPHOTO_MARKER_INVALID",
        "XHS_LIVEPHOTO_MOTION_EVIDENCE_MISSING",
        "XHS_LIVEPHOTO_MOTION_WITHOUT_MARKER",
        "XHS_LIVEPHOTO_RELATIONSHIP_ITEM_INVALID",
        "XHS_LIVEPHOTO_RELATIONSHIP_MAP_INVALID",
        "XHS_LIVEPHOTO_SOURCE_IDENTITY_INVALID",
        "XHS_LIVEPHOTO_SOURCE_IDENTITY_MISMATCH",
        "XHS_LIVEPHOTO_STILL_EVIDENCE_MISSING",
        "XHS_LIVEPHOTO_STILL_FIELDS_INVALID",
        "XHS_LIVEPHOTO_STREAM_INVALID",
        "XHS_LIVEPHOTO_VIDEO_FLAG_INVALID",
        "XHS_VIDEO_NOTE_LIVEPHOTO_SHAPE_UNREVIEWED",
    }
)
_SAFE_ERROR_PATH_RE = re.compile(
    r"^(?:"
    r"requested_note_id|payload_note_id|note_level_video_present|"
    r"relationship_item|relationship_map|unknown|"
    r"image_list(?:\[\d+\](?:"
    r"\.livePhoto|\.urlDefault|"
    r"\.infoList(?:\[\d+\](?:\.url)?)?|"
    r"\.stream(?:\[\*\](?:\[\d+\](?:\."
    r"(?:masterUrl|backupUrls(?:\[\d+\])?))?)?)?"
    r")?)?"
    r")$"
)


class XhsLivePhotoRelationshipMappingError(ValueError):
    """Fixed-code mapping error that never includes rejected payload values."""

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
    raise XhsLivePhotoRelationshipMappingError(code, path)


@dataclass(frozen=True)
class XhsLivePhotoRelationshipItemV1:
    source_item_ordinal: int
    item_kind: str
    relationship_basis: str
    still_candidate_count: int
    motion_candidate_count: int
    member_order: tuple[str, ...]
    format_status: str = _FORMAT_STATUS

    def __post_init__(self) -> None:
        valid_count = lambda value: (
            type(value) is int
            and 0 <= value <= XHS_LIVEPHOTO_MAX_CANDIDATES_PER_ITEM
        )
        if (
            type(self.source_item_ordinal) is not int
            or not 0 <= self.source_item_ordinal < XHS_LIVEPHOTO_RELATIONSHIP_MAX_ITEMS
            or self.item_kind not in _ITEM_KINDS
            or self.relationship_basis not in _RELATIONSHIP_BASES
            or not valid_count(self.still_candidate_count)
            or not valid_count(self.motion_candidate_count)
            or type(self.member_order) is not tuple
            or self.format_status != _FORMAT_STATUS
        ):
            _reject("XHS_LIVEPHOTO_RELATIONSHIP_ITEM_INVALID", "relationship_item")
        if self.item_kind == "regular_image" and (
            self.relationship_basis != "none"
            or self.still_candidate_count == 0
            or self.motion_candidate_count != 0
            or self.member_order != ("still",)
        ):
            _reject("XHS_LIVEPHOTO_RELATIONSHIP_ITEM_INVALID", "relationship_item")
        if self.item_kind == "live_photo" and (
            self.relationship_basis != "same_item_explicit_fields"
            or self.still_candidate_count == 0
            or self.motion_candidate_count == 0
            or self.member_order != ("still", "motion")
        ):
            _reject("XHS_LIVEPHOTO_RELATIONSHIP_ITEM_INVALID", "relationship_item")

    def to_structural_dict(self) -> dict[str, Any]:
        return {
            "source_item_ordinal": self.source_item_ordinal,
            "item_kind": self.item_kind,
            "relationship_basis": self.relationship_basis,
            "still_candidate_count": self.still_candidate_count,
            "motion_candidate_count": self.motion_candidate_count,
            "member_order": list(self.member_order),
            "format_status": self.format_status,
        }


@dataclass(frozen=True)
class XhsLivePhotoRelationshipMapV1:
    schema_version: str
    identity_confirmed: bool
    note_kind: str
    items: tuple[XhsLivePhotoRelationshipItemV1, ...]

    def __post_init__(self) -> None:
        if (
            self.schema_version != XHS_LIVEPHOTO_RELATIONSHIP_SCHEMA_VERSION
            or self.identity_confirmed is not True
            or self.note_kind not in {"image", "video"}
            or type(self.items) is not tuple
            or len(self.items) > XHS_LIVEPHOTO_RELATIONSHIP_MAX_ITEMS
            or any(
                type(item) is not XhsLivePhotoRelationshipItemV1
                or item.source_item_ordinal != index
                for index, item in enumerate(self.items)
            )
            or (self.note_kind == "image" and not self.items)
            or (self.note_kind == "video" and self.items)
        ):
            _reject("XHS_LIVEPHOTO_RELATIONSHIP_MAP_INVALID", "relationship_map")

    @property
    def livephoto_count(self) -> int:
        return sum(item.item_kind == "live_photo" for item in self.items)

    @property
    def regular_image_count(self) -> int:
        return sum(item.item_kind == "regular_image" for item in self.items)

    def to_structural_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "identity_confirmed": self.identity_confirmed,
            "note_kind": self.note_kind,
            "livephoto_count": self.livephoto_count,
            "regular_image_count": self.regular_image_count,
            "items": [item.to_structural_dict() for item in self.items],
        }


def _require_note_id(value: Any, *, path: str) -> str:
    if type(value) is not str or not _NOTE_ID_RE.fullmatch(value):
        _reject("XHS_LIVEPHOTO_SOURCE_IDENTITY_INVALID", path)
    return value


def _candidate_present(value: Any, *, code: str, path: str) -> bool:
    if value is None or value == "":
        return False
    if type(value) is not str:
        _reject(code, path)
    return bool(value.strip())


def _still_candidate_count(item: Mapping[str, Any], *, path: str) -> int:
    count = 0
    if "urlDefault" in item and _candidate_present(
        item["urlDefault"],
        code="XHS_LIVEPHOTO_STILL_FIELDS_INVALID",
        path=f"{path}.urlDefault",
    ):
        count += 1

    if "infoList" in item:
        info_list = item["infoList"]
        if not isinstance(info_list, (list, tuple)):
            _reject("XHS_LIVEPHOTO_INFO_LIST_INVALID", f"{path}.infoList")
        if len(info_list) > XHS_LIVEPHOTO_MAX_INFO_ITEMS:
            _reject("XHS_LIVEPHOTO_INFO_LIST_INVALID", f"{path}.infoList")
        for index, info in enumerate(info_list):
            info_path = f"{path}.infoList[{index}]"
            if not isinstance(info, Mapping):
                _reject("XHS_LIVEPHOTO_INFO_LIST_INVALID", info_path)
            if "url" in info and _candidate_present(
                info["url"],
                code="XHS_LIVEPHOTO_INFO_LIST_INVALID",
                path=f"{info_path}.url",
            ):
                count += 1

    if count > XHS_LIVEPHOTO_MAX_CANDIDATES_PER_ITEM:
        _reject("XHS_LIVEPHOTO_STILL_FIELDS_INVALID", path)
    return count


def _motion_candidate_count(item: Mapping[str, Any], *, path: str) -> int:
    if "stream" not in item or item["stream"] is None:
        return 0
    stream = item["stream"]
    if not isinstance(stream, Mapping):
        _reject("XHS_LIVEPHOTO_STREAM_INVALID", f"{path}.stream")
    if len(stream) > XHS_LIVEPHOTO_MAX_CODEC_FAMILIES:
        _reject("XHS_LIVEPHOTO_STREAM_INVALID", f"{path}.stream")

    candidate_count = 0
    variant_count = 0
    for variants in stream.values():
        if not isinstance(variants, (list, tuple)):
            _reject("XHS_LIVEPHOTO_STREAM_INVALID", f"{path}.stream[*]")
        variant_count += len(variants)
        if variant_count > XHS_LIVEPHOTO_MAX_STREAM_VARIANTS:
            _reject("XHS_LIVEPHOTO_STREAM_INVALID", f"{path}.stream")
        for index, variant in enumerate(variants):
            variant_path = f"{path}.stream[*][{index}]"
            if not isinstance(variant, Mapping):
                _reject("XHS_LIVEPHOTO_STREAM_INVALID", variant_path)
            if "masterUrl" in variant and _candidate_present(
                variant["masterUrl"],
                code="XHS_LIVEPHOTO_STREAM_INVALID",
                path=f"{variant_path}.masterUrl",
            ):
                candidate_count += 1

            if "backupUrls" not in variant:
                continue
            backup_urls = variant["backupUrls"]
            if not isinstance(backup_urls, (list, tuple)):
                _reject("XHS_LIVEPHOTO_STREAM_INVALID", f"{variant_path}.backupUrls")
            if len(backup_urls) > XHS_LIVEPHOTO_MAX_CANDIDATES_PER_ITEM:
                _reject("XHS_LIVEPHOTO_STREAM_INVALID", f"{variant_path}.backupUrls")
            for backup_index, backup in enumerate(backup_urls):
                if _candidate_present(
                    backup,
                    code="XHS_LIVEPHOTO_STREAM_INVALID",
                    path=f"{variant_path}.backupUrls[{backup_index}]",
                ):
                    candidate_count += 1
            if candidate_count > XHS_LIVEPHOTO_MAX_CANDIDATES_PER_ITEM:
                _reject("XHS_LIVEPHOTO_STREAM_INVALID", f"{path}.stream")
    return candidate_count


def _livephoto_marker(item: Mapping[str, Any], *, path: str) -> bool:
    if "livePhoto" not in item:
        return False
    marker = item["livePhoto"]
    if type(marker) is not bool:
        _reject("XHS_LIVEPHOTO_MARKER_INVALID", f"{path}.livePhoto")
    return marker


def _map_image_item(
    item: Any,
    *,
    source_item_ordinal: int,
) -> XhsLivePhotoRelationshipItemV1:
    path = f"image_list[{source_item_ordinal}]"
    if not isinstance(item, Mapping):
        _reject("XHS_LIVEPHOTO_IMAGE_ITEM_INVALID", path)

    marker = _livephoto_marker(item, path=path)
    still_count = _still_candidate_count(item, path=path)
    motion_count = _motion_candidate_count(item, path=path)

    if still_count == 0:
        _reject("XHS_LIVEPHOTO_STILL_EVIDENCE_MISSING", path)
    if marker and motion_count == 0:
        _reject("XHS_LIVEPHOTO_MOTION_EVIDENCE_MISSING", f"{path}.stream")
    if not marker and motion_count > 0:
        _reject("XHS_LIVEPHOTO_MOTION_WITHOUT_MARKER", f"{path}.stream")

    if marker:
        return XhsLivePhotoRelationshipItemV1(
            source_item_ordinal=source_item_ordinal,
            item_kind="live_photo",
            relationship_basis="same_item_explicit_fields",
            still_candidate_count=still_count,
            motion_candidate_count=motion_count,
            member_order=("still", "motion"),
        )
    return XhsLivePhotoRelationshipItemV1(
        source_item_ordinal=source_item_ordinal,
        item_kind="regular_image",
        relationship_basis="none",
        still_candidate_count=still_count,
        motion_candidate_count=0,
        member_order=("still",),
    )


def map_xhs_livephoto_relationships(
    *,
    requested_note_id: str,
    payload_note_id: str,
    image_list: Any,
    note_level_video_present: bool = False,
) -> XhsLivePhotoRelationshipMapV1:
    """Map an already-selected exact note into URL-free relationship evidence."""

    requested_note_id = _require_note_id(
        requested_note_id,
        path="requested_note_id",
    )
    payload_note_id = _require_note_id(
        payload_note_id,
        path="payload_note_id",
    )
    if requested_note_id != payload_note_id:
        _reject("XHS_LIVEPHOTO_SOURCE_IDENTITY_MISMATCH", "payload_note_id")
    if type(note_level_video_present) is not bool:
        _reject("XHS_LIVEPHOTO_VIDEO_FLAG_INVALID", "note_level_video_present")
    if not isinstance(image_list, (list, tuple)):
        _reject("XHS_LIVEPHOTO_IMAGE_LIST_INVALID", "image_list")
    if len(image_list) > XHS_LIVEPHOTO_RELATIONSHIP_MAX_ITEMS:
        _reject("XHS_LIVEPHOTO_IMAGE_LIST_INVALID", "image_list")
    if not image_list and not note_level_video_present:
        _reject("XHS_LIVEPHOTO_IMAGE_LIST_INVALID", "image_list")

    mapped_items = tuple(
        _map_image_item(item, source_item_ordinal=index)
        for index, item in enumerate(image_list)
    )
    if note_level_video_present:
        if any(item.item_kind == "live_photo" for item in mapped_items):
            _reject("XHS_VIDEO_NOTE_LIVEPHOTO_SHAPE_UNREVIEWED", "image_list")
        mapped_items = ()

    return XhsLivePhotoRelationshipMapV1(
        schema_version=XHS_LIVEPHOTO_RELATIONSHIP_SCHEMA_VERSION,
        identity_confirmed=True,
        note_kind="video" if note_level_video_present else "image",
        items=mapped_items,
    )


__all__ = (
    "XHS_LIVEPHOTO_RELATIONSHIP_MAX_ITEMS",
    "XHS_LIVEPHOTO_RELATIONSHIP_SCHEMA_VERSION",
    "XhsLivePhotoRelationshipItemV1",
    "XhsLivePhotoRelationshipMapV1",
    "XhsLivePhotoRelationshipMappingError",
    "map_xhs_livephoto_relationships",
)
