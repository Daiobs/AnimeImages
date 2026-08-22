"""Dormant, URL-free XHS rich-media normalization."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ..assets import MediaAsset
from ..rich_media import (
    RICH_MEDIA_IMAGE_EXTENSIONS,
    RICH_MEDIA_SCHEMA_VERSION,
    RICH_MEDIA_VIDEO_EXTENSIONS,
    RichMediaAssetDescriptorV1,
    RichMediaContractV1,
    RichMediaGroupMemberV1,
    RichMediaGroupV1,
    normalize_media_extension,
)


XHS_RICH_MEDIA_MAX_ITEMS = 64

_NOTE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_DOMAIN_ID_RE = re.compile(
    r"^[a-z][a-z0-9_-]{0,31}(?::[a-z0-9][a-z0-9_-]{0,31}){1,5}$"
)
_PAYLOAD_FIELDS = {"requested_note_id", "payload_note_id", "items"}
_MEDIA_ITEM_FIELDS = {"kind", "asset_id", "extension"}
_LIVE_PHOTO_FIELDS = {
    "kind",
    "pair_id",
    "owner_note_id",
    "still",
    "motion",
}
_LIVE_PHOTO_MEMBER_FIELDS = {"asset_id", "extension"}


class XhsRichMediaNormalizationError(ValueError):
    """A fixed-code error that never includes rejected payload values."""

    def __init__(self, code: str, path: str):
        self.code = code
        self.path = path
        super().__init__(f"{code} path={path}")


def _reject(code: str, path: str) -> None:
    raise XhsRichMediaNormalizationError(code, path)


@dataclass(frozen=True)
class XhsRichMediaNormalization:
    assets: tuple[MediaAsset, ...]
    rich_media: RichMediaContractV1 | None


@dataclass(frozen=True)
class _AssetSpec:
    asset_id: str
    media_type: str
    extension: str
    relationship_role: str = ""


@dataclass(frozen=True)
class _LivePhotoSpec:
    pair_id: str
    still_asset_id: str
    motion_asset_id: str


def _require_exact_mapping(
    value: Any,
    *,
    fields: set[str],
    code: str,
    path: str,
) -> dict[str, Any]:
    if type(value) is not dict or set(value) != fields:
        _reject(code, path)
    return value


def _require_note_id(value: Any, *, path: str) -> str:
    if not isinstance(value, str) or not _NOTE_ID_RE.fullmatch(value):
        _reject("XHS_NOTE_ID_INVALID", path)
    return value


def _require_domain_id(value: Any, *, code: str, path: str) -> str:
    if not isinstance(value, str) or not _DOMAIN_ID_RE.fullmatch(value):
        _reject(code, path)
    return value


def _normalize_extension(value: Any, *, media_type: str, path: str) -> str:
    extension = normalize_media_extension(value)
    if extension == "heic":
        _reject("XHS_HEIC_CONTRACT_GAP", path)
    allowed = (
        RICH_MEDIA_IMAGE_EXTENSIONS
        if media_type == "image"
        else RICH_MEDIA_VIDEO_EXTENSIONS
    )
    if extension not in allowed:
        _reject("XHS_MEDIA_EXTENSION_UNSUPPORTED", path)
    return extension


def _parse_regular_item(item: dict[str, Any], *, path: str) -> _AssetSpec:
    item = _require_exact_mapping(
        item,
        fields=_MEDIA_ITEM_FIELDS,
        code="XHS_MEDIA_ITEM_FIELDS_INVALID",
        path=path,
    )
    media_type = item["kind"]
    if media_type not in {"image", "video"}:
        _reject("XHS_MEDIA_ITEM_KIND_UNSUPPORTED", f"{path}.kind")
    return _AssetSpec(
        asset_id=_require_domain_id(
            item["asset_id"],
            code="XHS_ASSET_ID_INVALID",
            path=f"{path}.asset_id",
        ),
        media_type=media_type,
        extension=_normalize_extension(
            item["extension"],
            media_type=media_type,
            path=f"{path}.extension",
        ),
    )


def _parse_live_photo_member(
    value: Any,
    *,
    media_type: str,
    role: str,
    missing_code: str,
    path: str,
) -> _AssetSpec:
    if value is None:
        _reject(missing_code, path)
    member = _require_exact_mapping(
        value,
        fields=_LIVE_PHOTO_MEMBER_FIELDS,
        code="XHS_LIVE_PHOTO_MEMBER_FIELDS_INVALID",
        path=path,
    )
    return _AssetSpec(
        asset_id=_require_domain_id(
            member["asset_id"],
            code="XHS_ASSET_ID_INVALID",
            path=f"{path}.asset_id",
        ),
        media_type=media_type,
        extension=_normalize_extension(
            member["extension"],
            media_type=media_type,
            path=f"{path}.extension",
        ),
        relationship_role=role,
    )


def _parse_live_photo_item(
    item: Any,
    *,
    payload_note_id: str,
    path: str,
) -> tuple[_AssetSpec, _AssetSpec, _LivePhotoSpec]:
    if type(item) is not dict:
        _reject("XHS_LIVE_PHOTO_FIELDS_INVALID", path)
    if "still" not in item or item.get("still") is None:
        _reject("XHS_LIVE_PHOTO_STILL_MISSING", f"{path}.still")
    if "motion" not in item or item.get("motion") is None:
        _reject("XHS_LIVE_PHOTO_MOTION_MISSING", f"{path}.motion")
    item = _require_exact_mapping(
        item,
        fields=_LIVE_PHOTO_FIELDS,
        code="XHS_LIVE_PHOTO_FIELDS_INVALID",
        path=path,
    )
    pair_id = _require_domain_id(
        item["pair_id"],
        code="XHS_LIVE_PHOTO_PAIR_ID_INVALID",
        path=f"{path}.pair_id",
    )
    owner_note_id = _require_note_id(
        item["owner_note_id"],
        path=f"{path}.owner_note_id",
    )
    if owner_note_id != payload_note_id:
        _reject("XHS_LIVE_PHOTO_OWNER_MISMATCH", f"{path}.owner_note_id")

    still = _parse_live_photo_member(
        item["still"],
        media_type="image",
        role="still",
        missing_code="XHS_LIVE_PHOTO_STILL_MISSING",
        path=f"{path}.still",
    )
    motion = _parse_live_photo_member(
        item["motion"],
        media_type="video",
        role="motion",
        missing_code="XHS_LIVE_PHOTO_MOTION_MISSING",
        path=f"{path}.motion",
    )
    if still.asset_id == motion.asset_id:
        _reject("XHS_LIVE_PHOTO_MEMBER_DUPLICATE", path)
    return (
        still,
        motion,
        _LivePhotoSpec(pair_id, still.asset_id, motion.asset_id),
    )


def _post_type_for_specs(
    specs: list[_AssetSpec],
    live_photos: list[_LivePhotoSpec],
) -> str:
    image_count = sum(spec.media_type == "image" for spec in specs)
    video_count = len(specs) - image_count
    grouped_ids = {
        asset_id
        for pair in live_photos
        for asset_id in (pair.still_asset_id, pair.motion_asset_id)
    }
    if live_photos and grouped_ids == {spec.asset_id for spec in specs}:
        return "live_photo"
    if image_count and video_count:
        return "multimedia"
    if image_count == 1:
        return "image"
    if image_count > 1:
        return "gallery"
    return "video"


def _role_for_spec(spec: _AssetSpec, *, post_type: str) -> str:
    if spec.relationship_role:
        return spec.relationship_role
    if post_type == "multimedia":
        return "inline"
    if post_type == "gallery":
        return "gallery"
    return "primary"


def normalize_xhs_rich_media_payload(
    payload: dict[str, Any] | None,
) -> XhsRichMediaNormalization:
    """Normalize explicit, sanitized XHS media evidence without network I/O."""

    if payload is None:
        return XhsRichMediaNormalization(assets=(), rich_media=None)
    payload = _require_exact_mapping(
        payload,
        fields=_PAYLOAD_FIELDS,
        code="XHS_RICH_MEDIA_PAYLOAD_FIELDS_INVALID",
        path="payload",
    )
    requested_note_id = _require_note_id(
        payload["requested_note_id"],
        path="payload.requested_note_id",
    )
    payload_note_id = _require_note_id(
        payload["payload_note_id"],
        path="payload.payload_note_id",
    )
    if requested_note_id != payload_note_id:
        _reject("XHS_SOURCE_IDENTITY_MISMATCH", "payload.payload_note_id")

    items = payload["items"]
    if (
        type(items) is not list
        or not items
        or len(items) > XHS_RICH_MEDIA_MAX_ITEMS
    ):
        _reject("XHS_MEDIA_ITEMS_INVALID", "payload.items")

    specs: list[_AssetSpec] = []
    live_photos: list[_LivePhotoSpec] = []
    asset_ids: set[str] = set()
    pair_ids: set[str] = set()
    for index, raw_item in enumerate(items):
        path = f"payload.items[{index}]"
        if type(raw_item) is not dict:
            _reject("XHS_MEDIA_ITEM_FIELDS_INVALID", path)
        if raw_item.get("kind") == "live_photo":
            still, motion, pair = _parse_live_photo_item(
                raw_item,
                payload_note_id=payload_note_id,
                path=path,
            )
            item_specs = (still, motion)
            if pair.pair_id in pair_ids:
                _reject("XHS_LIVE_PHOTO_PAIR_ID_DUPLICATE", f"{path}.pair_id")
            pair_ids.add(pair.pair_id)
            live_photos.append(pair)
        else:
            item_specs = (_parse_regular_item(raw_item, path=path),)
        for spec in item_specs:
            if spec.asset_id in asset_ids:
                _reject("XHS_ASSET_ID_DUPLICATE", f"{path}.asset_id")
            asset_ids.add(spec.asset_id)
            specs.append(spec)

    post_type = _post_type_for_specs(specs, live_photos)
    assets = tuple(
        MediaAsset(
            media_type=spec.media_type,
            url="",
            platform="xiaohongshu",
            ext=spec.extension,
            asset_id=spec.asset_id,
        )
        for spec in specs
    )
    descriptors = tuple(
        RichMediaAssetDescriptorV1(
            asset_id=spec.asset_id,
            media_type=spec.media_type,
            extension=spec.extension,
            role=_role_for_spec(spec, post_type=post_type),
        )
        for spec in specs
    )
    groups = tuple(
        RichMediaGroupV1(
            group_id=pair.pair_id,
            relationship_type="live_photo",
            members=(
                RichMediaGroupMemberV1(pair.still_asset_id, "still"),
                RichMediaGroupMemberV1(pair.motion_asset_id, "motion"),
            ),
        )
        for pair in live_photos
    )
    contract = RichMediaContractV1(
        schema_version=RICH_MEDIA_SCHEMA_VERSION,
        post_type=post_type,
        assets=descriptors,
        groups=groups,
        content_blocks=(),
    )
    return XhsRichMediaNormalization(assets=assets, rich_media=contract)


__all__ = [
    "XHS_RICH_MEDIA_MAX_ITEMS",
    "XhsRichMediaNormalization",
    "XhsRichMediaNormalizationError",
    "normalize_xhs_rich_media_payload",
]
