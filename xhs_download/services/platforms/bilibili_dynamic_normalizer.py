"""Dormant, network-free Bilibili Dynamic/Opus research primitives."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any
from urllib.parse import urlsplit

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
from ..source_lifecycle import SourceInputKind, SourceLifecycleError, extract_source_url


BILIBILI_DYNAMIC_MAX_ITEMS = 64
BILIBILI_METADATA_MAX_CHARS = 512

_NUMERIC_CONTENT_ID_RE = re.compile(r"^[0-9]{18,19}$")
_DOMAIN_ID_RE = re.compile(
    r"^[a-z][a-z0-9_-]{0,31}(?::[a-z0-9][a-z0-9_-]{0,31}){1,5}$"
)
_BVID_RE = re.compile(r"^BV[0-9A-Za-z]{10}$", re.IGNORECASE)
_AVID_RE = re.compile(r"^av[0-9]+$", re.IGNORECASE)
_UNSAFE_METADATA_RE = re.compile(
    r"(?:https?://|www\.|javascript:|data:|file:|ftp:|"
    r"bearer\s+|api[_-]?key\s*[:=]|token\s*[:=]|cookie\s*[:=]|"
    r"password\s*[:=]|secret\s*[:=]|<\s*/?\s*[a-z]|"
    r"!?\[[^\]]*\]\s*\()",
    re.IGNORECASE,
)
_PAYLOAD_FIELDS = {
    "requested_identity",
    "payload_identity",
    "target_state",
    "title",
    "author",
    "items",
}
_IDENTITY_FIELDS = {"kind", "id"}
_MEDIA_ITEM_FIELDS = {"kind", "asset_id", "extension"}
_LIVE_PHOTO_FIELDS = {
    "kind",
    "pair_id",
    "owner_identity",
    "still",
    "motion",
}
_LIVE_PHOTO_MEMBER_FIELDS = {"asset_id", "extension"}
_TERMINAL_STATES = {"deleted", "private", "not_found"}


class BilibiliDynamicNormalizationError(ValueError):
    """A fixed-code error that never includes rejected input values."""

    def __init__(self, code: str, path: str):
        self.code = code
        self.path = path
        super().__init__(f"{code} path={path}")


def _reject(code: str, path: str) -> None:
    raise BilibiliDynamicNormalizationError(code, path)


class BilibiliSourceTargetKind(str, Enum):
    VIDEO = "video"
    DYNAMIC = "dynamic"
    OPUS = "opus"
    SHORT = "short"
    NUMERIC_UNVERIFIED = "numeric_unverified"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class BilibiliContentIdentity:
    kind: str
    value: str

    @property
    def canonical(self) -> str:
        return f"{self.kind}:{self.value}"


@dataclass(frozen=True)
class BilibiliSourceShape:
    input_kind: str
    target_kind: str
    identity: BilibiliContentIdentity | None
    requires_resolution: bool


@dataclass(frozen=True)
class BilibiliDynamicAssetSpec:
    """URL-free intermediate media evidence for a future Provider builder."""

    asset_id: str
    media_type: str
    extension: str
    role: str


@dataclass(frozen=True)
class BilibiliDynamicNormalization:
    identity: BilibiliContentIdentity
    title_candidate: str
    author_candidate: str
    asset_specs: tuple[BilibiliDynamicAssetSpec, ...]
    rich_media: RichMediaContractV1


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


def _is_host(hostname: str, suffix: str) -> bool:
    return hostname == suffix or hostname.endswith(f".{suffix}")


def _classified_input_kind(original_kind: str, direct_kind: SourceInputKind) -> str:
    if original_kind == SourceInputKind.SHARE_TEXT.value:
        return original_kind
    return direct_kind.value


def _identity(kind: str, value: str, *, path: str) -> BilibiliContentIdentity:
    if kind not in {"bilibili_dynamic", "bilibili_opus"}:
        _reject("BILIBILI_IDENTITY_KIND_INVALID", f"{path}.kind")
    if not isinstance(value, str) or not _NUMERIC_CONTENT_ID_RE.fullmatch(value):
        _reject("BILIBILI_IDENTITY_VALUE_INVALID", f"{path}.id")
    return BilibiliContentIdentity(kind=kind, value=value)


def classify_bilibili_source_shape(value: str) -> BilibiliSourceShape:
    """Classify direct Bilibili source shapes without resolving or routing them."""

    try:
        source_url = extract_source_url(value)
    except SourceLifecycleError as error:
        raise BilibiliDynamicNormalizationError(
            "BILIBILI_SOURCE_INVALID",
            "source",
        ) from error

    parsed = urlsplit(source_url)
    hostname = (parsed.hostname or "").rstrip(".").lower()
    original_input_kind = (
        SourceInputKind.SHARE_TEXT.value
        if str(value).strip() != source_url
        else SourceInputKind.URL.value
    )
    if _is_host(hostname, "b23.tv") or _is_host(hostname, "bili2233.cn"):
        return BilibiliSourceShape(
            input_kind=_classified_input_kind(
                original_input_kind,
                SourceInputKind.SHORT_URL,
            ),
            target_kind=BilibiliSourceTargetKind.SHORT.value,
            identity=None,
            requires_resolution=True,
        )
    if not _is_host(hostname, "bilibili.com"):
        return BilibiliSourceShape(
            input_kind=original_input_kind,
            target_kind=BilibiliSourceTargetKind.UNKNOWN.value,
            identity=None,
            requires_resolution=False,
        )

    segments = tuple(item for item in parsed.path.split("/") if item)
    if hostname == "t.bilibili.com" and len(segments) == 1:
        value = segments[0]
        if _NUMERIC_CONTENT_ID_RE.fullmatch(value):
            return BilibiliSourceShape(
                input_kind=_classified_input_kind(
                    original_input_kind,
                    SourceInputKind.DYNAMIC_PAGE,
                ),
                target_kind=BilibiliSourceTargetKind.DYNAMIC.value,
                identity=_identity("bilibili_dynamic", value, path="source.identity"),
                requires_resolution=False,
            )
    if len(segments) == 2 and segments[0].lower() == "opus":
        value = segments[1]
        if _NUMERIC_CONTENT_ID_RE.fullmatch(value):
            return BilibiliSourceShape(
                input_kind=_classified_input_kind(
                    original_input_kind,
                    SourceInputKind.OPUS_PAGE,
                ),
                target_kind=BilibiliSourceTargetKind.OPUS.value,
                identity=_identity("bilibili_opus", value, path="source.identity"),
                requires_resolution=False,
            )
    if len(segments) == 1 and _NUMERIC_CONTENT_ID_RE.fullmatch(segments[0]):
        return BilibiliSourceShape(
            input_kind=original_input_kind,
            target_kind=BilibiliSourceTargetKind.NUMERIC_UNVERIFIED.value,
            identity=None,
            requires_resolution=True,
        )
    if len(segments) >= 2 and segments[0].lower() == "video":
        video_id = segments[1]
        if _BVID_RE.fullmatch(video_id) or _AVID_RE.fullmatch(video_id):
            return BilibiliSourceShape(
                input_kind=_classified_input_kind(
                    original_input_kind,
                    SourceInputKind.VIDEO_PAGE,
                ),
                target_kind=BilibiliSourceTargetKind.VIDEO.value,
                identity=None,
                requires_resolution=False,
            )
    return BilibiliSourceShape(
        input_kind=original_input_kind,
        target_kind=BilibiliSourceTargetKind.UNKNOWN.value,
        identity=None,
        requires_resolution=False,
    )


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


def _require_identity(value: Any, *, path: str) -> BilibiliContentIdentity:
    identity = _require_exact_mapping(
        value,
        fields=_IDENTITY_FIELDS,
        code="BILIBILI_IDENTITY_FIELDS_INVALID",
        path=path,
    )
    return _identity(identity["kind"], identity["id"], path=path)


def _require_domain_id(value: Any, *, code: str, path: str) -> str:
    if not isinstance(value, str) or not _DOMAIN_ID_RE.fullmatch(value):
        _reject(code, path)
    return value


def _require_metadata_text(value: Any, *, path: str) -> str:
    if not isinstance(value, str) or len(value) > BILIBILI_METADATA_MAX_CHARS:
        _reject("BILIBILI_METADATA_INVALID", path)
    normalized = value.strip()
    if _UNSAFE_METADATA_RE.search(normalized):
        _reject("BILIBILI_METADATA_UNSAFE", path)
    return normalized


def _normalize_extension(value: Any, *, media_type: str, path: str) -> str:
    extension = normalize_media_extension(value)
    allowed = (
        RICH_MEDIA_IMAGE_EXTENSIONS
        if media_type == "image"
        else RICH_MEDIA_VIDEO_EXTENSIONS
    )
    if extension not in allowed:
        _reject("BILIBILI_MEDIA_EXTENSION_UNSUPPORTED", path)
    return extension


def _parse_regular_item(item: Any, *, path: str) -> _AssetSpec:
    item = _require_exact_mapping(
        item,
        fields=_MEDIA_ITEM_FIELDS,
        code="BILIBILI_MEDIA_ITEM_FIELDS_INVALID",
        path=path,
    )
    media_type = item["kind"]
    if media_type not in {"image", "video"}:
        _reject("BILIBILI_MEDIA_ITEM_KIND_UNSUPPORTED", f"{path}.kind")
    return _AssetSpec(
        asset_id=_require_domain_id(
            item["asset_id"],
            code="BILIBILI_ASSET_ID_INVALID",
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
        code="BILIBILI_LIVE_PHOTO_MEMBER_FIELDS_INVALID",
        path=path,
    )
    return _AssetSpec(
        asset_id=_require_domain_id(
            member["asset_id"],
            code="BILIBILI_ASSET_ID_INVALID",
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
    payload_identity: BilibiliContentIdentity,
    path: str,
) -> tuple[_AssetSpec, _AssetSpec, _LivePhotoSpec]:
    if type(item) is not dict:
        _reject("BILIBILI_LIVE_PHOTO_FIELDS_INVALID", path)
    if "still" not in item or item.get("still") is None:
        _reject("BILIBILI_LIVE_PHOTO_STILL_MISSING", f"{path}.still")
    if "motion" not in item or item.get("motion") is None:
        _reject("BILIBILI_LIVE_PHOTO_MOTION_MISSING", f"{path}.motion")
    item = _require_exact_mapping(
        item,
        fields=_LIVE_PHOTO_FIELDS,
        code="BILIBILI_LIVE_PHOTO_FIELDS_INVALID",
        path=path,
    )
    owner_identity = _require_identity(
        item["owner_identity"],
        path=f"{path}.owner_identity",
    )
    if owner_identity != payload_identity:
        _reject("BILIBILI_LIVE_PHOTO_OWNER_MISMATCH", f"{path}.owner_identity")
    pair_id = _require_domain_id(
        item["pair_id"],
        code="BILIBILI_LIVE_PHOTO_PAIR_ID_INVALID",
        path=f"{path}.pair_id",
    )
    still = _parse_live_photo_member(
        item["still"],
        media_type="image",
        role="still",
        missing_code="BILIBILI_LIVE_PHOTO_STILL_MISSING",
        path=f"{path}.still",
    )
    motion = _parse_live_photo_member(
        item["motion"],
        media_type="video",
        role="motion",
        missing_code="BILIBILI_LIVE_PHOTO_MOTION_MISSING",
        path=f"{path}.motion",
    )
    if still.asset_id == motion.asset_id:
        _reject("BILIBILI_LIVE_PHOTO_MEMBER_DUPLICATE", path)
    return still, motion, _LivePhotoSpec(pair_id, still.asset_id, motion.asset_id)


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


def normalize_bilibili_dynamic_payload(
    payload: dict[str, Any],
) -> BilibiliDynamicNormalization:
    """Normalize explicit sanitized evidence; never resolve or fetch a source."""

    payload = _require_exact_mapping(
        payload,
        fields=_PAYLOAD_FIELDS,
        code="BILIBILI_DYNAMIC_PAYLOAD_FIELDS_INVALID",
        path="payload",
    )
    requested_identity = _require_identity(
        payload["requested_identity"],
        path="payload.requested_identity",
    )
    payload_identity = _require_identity(
        payload["payload_identity"],
        path="payload.payload_identity",
    )
    if requested_identity != payload_identity:
        _reject("BILIBILI_SOURCE_IDENTITY_MISMATCH", "payload.payload_identity")

    target_state = payload["target_state"]
    if target_state in _TERMINAL_STATES:
        _reject("BILIBILI_TARGET_TERMINAL", "payload.target_state")
    if target_state != "available":
        _reject("BILIBILI_TARGET_STATE_INVALID", "payload.target_state")

    title_candidate = _require_metadata_text(payload["title"], path="payload.title")
    author_candidate = _require_metadata_text(payload["author"], path="payload.author")
    items = payload["items"]
    if (
        type(items) is not list
        or not items
        or len(items) > BILIBILI_DYNAMIC_MAX_ITEMS
    ):
        _reject("BILIBILI_MEDIA_ITEMS_INVALID", "payload.items")

    specs: list[_AssetSpec] = []
    live_photos: list[_LivePhotoSpec] = []
    asset_ids: set[str] = set()
    pair_ids: set[str] = set()
    for index, item in enumerate(items):
        path = f"payload.items[{index}]"
        if type(item) is not dict:
            _reject("BILIBILI_MEDIA_ITEM_FIELDS_INVALID", path)
        if item.get("kind") == "live_photo":
            still, motion, pair = _parse_live_photo_item(
                item,
                payload_identity=payload_identity,
                path=path,
            )
            item_specs = (still, motion)
            if pair.pair_id in pair_ids:
                _reject("BILIBILI_LIVE_PHOTO_PAIR_ID_DUPLICATE", f"{path}.pair_id")
            pair_ids.add(pair.pair_id)
            live_photos.append(pair)
        else:
            item_specs = (_parse_regular_item(item, path=path),)
        for spec in item_specs:
            if spec.asset_id in asset_ids:
                _reject("BILIBILI_ASSET_ID_DUPLICATE", f"{path}.asset_id")
            asset_ids.add(spec.asset_id)
            specs.append(spec)

    post_type = _post_type_for_specs(specs, live_photos)
    asset_specs = tuple(
        BilibiliDynamicAssetSpec(
            asset_id=spec.asset_id,
            media_type=spec.media_type,
            extension=spec.extension,
            role=_role_for_spec(spec, post_type=post_type),
        )
        for spec in specs
    )
    descriptors = tuple(
        RichMediaAssetDescriptorV1(
            asset_id=spec.asset_id,
            media_type=spec.media_type,
            extension=spec.extension,
            role=spec.role,
        )
        for spec in asset_specs
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
    rich_media = RichMediaContractV1(
        schema_version=RICH_MEDIA_SCHEMA_VERSION,
        post_type=post_type,
        assets=descriptors,
        groups=groups,
        content_blocks=(),
    )
    return BilibiliDynamicNormalization(
        identity=payload_identity,
        title_candidate=title_candidate,
        author_candidate=author_candidate,
        asset_specs=asset_specs,
        rich_media=rich_media,
    )


__all__ = [
    "BILIBILI_DYNAMIC_MAX_ITEMS",
    "BilibiliContentIdentity",
    "BilibiliDynamicAssetSpec",
    "BilibiliDynamicNormalization",
    "BilibiliDynamicNormalizationError",
    "BilibiliSourceShape",
    "BilibiliSourceTargetKind",
    "classify_bilibili_source_shape",
    "normalize_bilibili_dynamic_payload",
]
