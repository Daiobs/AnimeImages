"""Typed, URL-free Rich Media Domain Contract v1 runtime model."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, TypeAlias


RICH_MEDIA_SCHEMA_VERSION = "1.0"

RICH_MEDIA_POST_TYPES = {
    "image",
    "video",
    "gallery",
    "live_photo",
    "multimedia",
    "rich_text",
}
RICH_MEDIA_MEDIA_TYPES = {"image", "video"}
RICH_MEDIA_ASSET_ROLES = {
    "primary",
    "gallery",
    "inline",
    "still",
    "motion",
    "animation",
}
RICH_MEDIA_IMAGE_ROLES = {"primary", "gallery", "inline", "still", "animation"}
RICH_MEDIA_VIDEO_ROLES = {"primary", "inline", "motion", "animation"}
RICH_MEDIA_IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "gif", "webp", "avif"}
RICH_MEDIA_VIDEO_EXTENSIONS = {"mp4", "mov", "m3u8"}
RICH_MEDIA_ANIMATION_IMAGE_EXTENSIONS = {"gif", "webp", "avif"}
RICH_MEDIA_ANIMATION_VIDEO_EXTENSIONS = {"mp4"}
RICH_MEDIA_RELATIONSHIP_TYPES = {"live_photo"}
RICH_MEDIA_LIVE_PHOTO_ROLES = {"still", "motion"}

RICH_MEDIA_MAX_ASSETS = 64
RICH_MEDIA_MAX_GROUPS = 32
RICH_MEDIA_MAX_CONTENT_BLOCKS = 128
RICH_MEDIA_MAX_MEDIA_REFS_PER_BLOCK = 64
RICH_MEDIA_MAX_TEXT_BLOCK_CHARS = 4096
RICH_MEDIA_MAX_TOTAL_TEXT_CHARS = 16384

_DOMAIN_ID_RE = re.compile(
    r"^[a-z][a-z0-9_-]{0,31}(?::[a-z0-9][a-z0-9_-]{0,31}){1,5}$"
)
_URL_LIKE_RE = re.compile(
    r"(?:https?://|www\.|javascript:|data:|file:|ftp:)", re.IGNORECASE
)
_HTML_TAG_RE = re.compile(r"<\s*/?\s*[a-z][^>]*>", re.IGNORECASE)
_MARKDOWN_LINK_RE = re.compile(r"!?\[[^\]]*\]\s*\(")
_SENSITIVE_TEXT_RE = re.compile(
    r"(?:bearer\s+|basic\s+|api[_-]?key\s*[:=]|token\s*[:=]|"
    r"cookie\s*[:=]|password\s*[:=]|secret\s*[:=])",
    re.IGNORECASE,
)


class RichMediaContractError(ValueError):
    """A fixed-code error that never includes rejected runtime values."""

    def __init__(self, code: str, path: str):
        self.code = code
        self.path = path
        super().__init__(f"{code} path={path}")


def _reject(code: str, path: str) -> None:
    raise RichMediaContractError(code, path)


def _require_tuple(value: Any, *, code: str, path: str) -> tuple:
    if type(value) is not tuple:
        _reject(code, path)
    return value


def _require_domain_id(value: Any, *, code: str, path: str) -> str:
    if not isinstance(value, str) or len(value) > 128 or not _DOMAIN_ID_RE.fullmatch(value):
        _reject(code, path)
    return value


def normalize_media_extension(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip().lower().lstrip(".")


@dataclass(frozen=True)
class RichMediaAssetDescriptorV1:
    asset_id: str
    media_type: str
    extension: str
    role: str


@dataclass(frozen=True)
class RichMediaGroupMemberV1:
    asset_id: str
    role: str


@dataclass(frozen=True)
class RichMediaGroupV1:
    group_id: str
    relationship_type: str
    members: tuple[RichMediaGroupMemberV1, ...]


@dataclass(frozen=True)
class RichMediaTextBlockV1:
    text: str


@dataclass(frozen=True)
class RichMediaMediaBlockV1:
    asset_ids: tuple[str, ...]


RichMediaContentBlockV1: TypeAlias = RichMediaTextBlockV1 | RichMediaMediaBlockV1


@dataclass(frozen=True)
class RichMediaContractV1:
    schema_version: str
    post_type: str
    assets: tuple[RichMediaAssetDescriptorV1, ...]
    groups: tuple[RichMediaGroupV1, ...]
    content_blocks: tuple[RichMediaContentBlockV1, ...]

    def __post_init__(self) -> None:
        _validate_contract(self)

    def to_structural_dict(self) -> dict[str, Any]:
        blocks = []
        for block in self.content_blocks:
            if type(block) is RichMediaTextBlockV1:
                blocks.append({"type": "text", "text": block.text})
            else:
                blocks.append({"type": "media", "asset_ids": list(block.asset_ids)})
        return {
            "schema_version": self.schema_version,
            "post_type": self.post_type,
            "assets": [
                {
                    "asset_id": asset.asset_id,
                    "media_type": asset.media_type,
                    "extension": asset.extension,
                    "role": asset.role,
                }
                for asset in self.assets
            ],
            "groups": [
                {
                    "group_id": group.group_id,
                    "relationship_type": group.relationship_type,
                    "members": [
                        {"asset_id": member.asset_id, "role": member.role}
                        for member in group.members
                    ],
                }
                for group in self.groups
            ],
            "content_blocks": blocks,
        }


def _validate_descriptor(
    descriptor: RichMediaAssetDescriptorV1,
    *,
    index: int,
) -> None:
    path = f"rich_media.assets[{index}]"
    if type(descriptor) is not RichMediaAssetDescriptorV1:
        _reject("RICH_MEDIA_ASSET_TYPE_INVALID", path)
    _require_domain_id(
        descriptor.asset_id,
        code="RICH_MEDIA_ASSET_ID_INVALID",
        path=f"{path}.asset_id",
    )
    if (
        not isinstance(descriptor.media_type, str)
        or descriptor.media_type not in RICH_MEDIA_MEDIA_TYPES
    ):
        _reject("RICH_MEDIA_MEDIA_TYPE_INVALID", f"{path}.media_type")
    extension = descriptor.extension
    allowed_extensions = (
        RICH_MEDIA_IMAGE_EXTENSIONS
        if descriptor.media_type == "image"
        else RICH_MEDIA_VIDEO_EXTENSIONS
    )
    if not isinstance(extension, str) or extension not in allowed_extensions:
        _reject("RICH_MEDIA_EXTENSION_INVALID", f"{path}.extension")
    if (
        not isinstance(descriptor.role, str)
        or descriptor.role not in RICH_MEDIA_ASSET_ROLES
    ):
        _reject("RICH_MEDIA_ASSET_ROLE_INVALID", f"{path}.role")
    allowed_roles = (
        RICH_MEDIA_IMAGE_ROLES
        if descriptor.media_type == "image"
        else RICH_MEDIA_VIDEO_ROLES
    )
    if descriptor.role not in allowed_roles:
        _reject("RICH_MEDIA_ASSET_ROLE_TYPE_MISMATCH", f"{path}.role")
    if descriptor.role == "animation":
        animation_extensions = (
            RICH_MEDIA_ANIMATION_IMAGE_EXTENSIONS
            if descriptor.media_type == "image"
            else RICH_MEDIA_ANIMATION_VIDEO_EXTENSIONS
        )
        if extension not in animation_extensions:
            _reject("RICH_MEDIA_ANIMATION_EXTENSION_INVALID", f"{path}.extension")


def _validate_groups(
    groups: Any,
    *,
    assets_by_id: dict[str, RichMediaAssetDescriptorV1],
) -> tuple[set[str], list[set[str]]]:
    groups = _require_tuple(
        groups,
        code="RICH_MEDIA_GROUP_COLLECTION_INVALID",
        path="rich_media.groups",
    )
    if len(groups) > RICH_MEDIA_MAX_GROUPS:
        _reject("RICH_MEDIA_GROUP_COLLECTION_INVALID", "rich_media.groups")

    group_ids: set[str] = set()
    grouped_assets: set[str] = set()
    group_member_sets: list[set[str]] = []
    for index, group in enumerate(groups):
        path = f"rich_media.groups[{index}]"
        if type(group) is not RichMediaGroupV1:
            _reject("RICH_MEDIA_GROUP_TYPE_INVALID", path)
        _require_domain_id(
            group.group_id,
            code="RICH_MEDIA_GROUP_ID_INVALID",
            path=f"{path}.group_id",
        )
        if group.group_id in group_ids:
            _reject("RICH_MEDIA_GROUP_ID_DUPLICATE", f"{path}.group_id")
        group_ids.add(group.group_id)
        if (
            not isinstance(group.relationship_type, str)
            or group.relationship_type not in RICH_MEDIA_RELATIONSHIP_TYPES
        ):
            _reject("RICH_MEDIA_RELATIONSHIP_TYPE_INVALID", f"{path}.relationship_type")

        members = _require_tuple(
            group.members,
            code="RICH_MEDIA_GROUP_MEMBERS_INVALID",
            path=f"{path}.members",
        )
        if len(members) != 2:
            _reject("RICH_MEDIA_LIVE_PHOTO_MEMBER_COUNT", f"{path}.members")

        member_ids: set[str] = set()
        member_roles: set[str] = set()
        for member_index, member in enumerate(members):
            member_path = f"{path}.members[{member_index}]"
            if type(member) is not RichMediaGroupMemberV1:
                _reject("RICH_MEDIA_GROUP_MEMBER_TYPE_INVALID", member_path)
            _require_domain_id(
                member.asset_id,
                code="RICH_MEDIA_GROUP_ASSET_ID_INVALID",
                path=f"{member_path}.asset_id",
            )
            if (
                not isinstance(member.role, str)
                or member.role not in RICH_MEDIA_LIVE_PHOTO_ROLES
            ):
                _reject("RICH_MEDIA_LIVE_PHOTO_ROLE_INVALID", f"{member_path}.role")
            if member.asset_id in member_ids:
                _reject("RICH_MEDIA_GROUP_MEMBER_DUPLICATE", f"{member_path}.asset_id")
            if member.asset_id in grouped_assets:
                _reject("RICH_MEDIA_ASSET_IN_MULTIPLE_GROUPS", f"{member_path}.asset_id")
            descriptor = assets_by_id.get(member.asset_id)
            if descriptor is None:
                _reject("RICH_MEDIA_GROUP_ASSET_NOT_FOUND", f"{member_path}.asset_id")
            expected_type = "image" if member.role == "still" else "video"
            if descriptor.media_type != expected_type or descriptor.role != member.role:
                _reject("RICH_MEDIA_LIVE_PHOTO_MEMBER_MISMATCH", member_path)
            member_ids.add(member.asset_id)
            member_roles.add(member.role)
            grouped_assets.add(member.asset_id)
        if member_roles != RICH_MEDIA_LIVE_PHOTO_ROLES:
            _reject("RICH_MEDIA_LIVE_PHOTO_PAIR_INCOMPLETE", f"{path}.members")
        group_member_sets.append(member_ids)
    return grouped_assets, group_member_sets


def _validate_safe_text(text: Any, *, path: str) -> int:
    if not isinstance(text, str) or not text or len(text) > RICH_MEDIA_MAX_TEXT_BLOCK_CHARS:
        _reject("RICH_MEDIA_TEXT_LENGTH_INVALID", path)
    if _URL_LIKE_RE.search(text):
        _reject("RICH_MEDIA_TEXT_URL_REJECTED", path)
    if _HTML_TAG_RE.search(text):
        _reject("RICH_MEDIA_TEXT_HTML_REJECTED", path)
    if _MARKDOWN_LINK_RE.search(text):
        _reject("RICH_MEDIA_TEXT_MARKDOWN_REJECTED", path)
    if _SENSITIVE_TEXT_RE.search(text):
        _reject("RICH_MEDIA_TEXT_CREDENTIAL_REJECTED", path)
    return len(text)


def _validate_blocks(
    blocks: Any,
    *,
    assets_by_id: dict[str, RichMediaAssetDescriptorV1],
    group_member_sets: list[set[str]],
) -> set[str]:
    blocks = _require_tuple(
        blocks,
        code="RICH_MEDIA_BLOCK_COLLECTION_INVALID",
        path="rich_media.content_blocks",
    )
    if len(blocks) > RICH_MEDIA_MAX_CONTENT_BLOCKS:
        _reject("RICH_MEDIA_BLOCK_COLLECTION_INVALID", "rich_media.content_blocks")

    referenced_assets: set[str] = set()
    group_by_asset = {
        asset_id: group_index
        for group_index, member_ids in enumerate(group_member_sets)
        for asset_id in member_ids
    }
    paired_group_indexes: set[int] = set()
    media_block_count = 0
    total_text_chars = 0
    for index, block in enumerate(blocks):
        path = f"rich_media.content_blocks[{index}]"
        if type(block) is RichMediaTextBlockV1:
            total_text_chars += _validate_safe_text(block.text, path=f"{path}.text")
            if total_text_chars > RICH_MEDIA_MAX_TOTAL_TEXT_CHARS:
                _reject("RICH_MEDIA_TOTAL_TEXT_LIMIT", "rich_media.content_blocks")
            continue
        if type(block) is not RichMediaMediaBlockV1:
            _reject("RICH_MEDIA_BLOCK_TYPE_INVALID", path)

        asset_ids = _require_tuple(
            block.asset_ids,
            code="RICH_MEDIA_MEDIA_REFS_INVALID",
            path=f"{path}.asset_ids",
        )
        if not asset_ids or len(asset_ids) > RICH_MEDIA_MAX_MEDIA_REFS_PER_BLOCK:
            _reject("RICH_MEDIA_MEDIA_REFS_INVALID", f"{path}.asset_ids")
        local_ids: set[str] = set()
        touched_group_indexes: set[int] = set()
        for ref_index, asset_id in enumerate(asset_ids):
            ref_path = f"{path}.asset_ids[{ref_index}]"
            _require_domain_id(
                asset_id,
                code="RICH_MEDIA_MEDIA_REF_ID_INVALID",
                path=ref_path,
            )
            if asset_id not in assets_by_id:
                _reject("RICH_MEDIA_MEDIA_REF_NOT_FOUND", ref_path)
            if asset_id in local_ids:
                _reject("RICH_MEDIA_MEDIA_REF_DUPLICATE", ref_path)
            local_ids.add(asset_id)
            referenced_assets.add(asset_id)
            group_index = group_by_asset.get(asset_id)
            if group_index is not None:
                touched_group_indexes.add(group_index)
        media_block_count += 1
        for group_index in touched_group_indexes:
            if group_member_sets[group_index] <= local_ids:
                paired_group_indexes.add(group_index)

    if media_block_count and len(paired_group_indexes) != len(group_member_sets):
        _reject("RICH_MEDIA_LIVE_PHOTO_BLOCK_PAIR_SPLIT", "rich_media.content_blocks")
    return referenced_assets


def _validate_post_shape(
    contract: RichMediaContractV1,
    *,
    grouped_assets: set[str],
    referenced_assets: set[str],
) -> None:
    assets = contract.assets
    asset_ids = {asset.asset_id for asset in assets}
    image_count = sum(asset.media_type == "image" for asset in assets)
    video_count = sum(asset.media_type == "video" for asset in assets)

    if contract.post_type != "rich_text" and contract.content_blocks:
        _reject("RICH_MEDIA_BLOCKS_REQUIRE_RICH_TEXT", "rich_media.content_blocks")
    if (
        contract.post_type not in {"live_photo", "multimedia", "rich_text"}
        and contract.groups
    ):
        _reject("RICH_MEDIA_GROUPS_NOT_ALLOWED", "rich_media.groups")

    if contract.post_type == "image":
        if len(assets) != 1 or image_count != 1:
            _reject("RICH_MEDIA_IMAGE_SHAPE_INVALID", "rich_media.assets")
    elif contract.post_type == "video":
        if not assets or video_count != len(assets):
            _reject("RICH_MEDIA_VIDEO_SHAPE_INVALID", "rich_media.assets")
    elif contract.post_type == "gallery":
        if len(assets) < 2 or image_count != len(assets):
            _reject("RICH_MEDIA_GALLERY_SHAPE_INVALID", "rich_media.assets")
    elif contract.post_type == "live_photo":
        if not contract.groups or grouped_assets != asset_ids:
            _reject("RICH_MEDIA_LIVE_PHOTO_SHAPE_INVALID", "rich_media.groups")
    elif contract.post_type == "multimedia":
        if not assets or not image_count or not video_count:
            _reject("RICH_MEDIA_MULTIMEDIA_SHAPE_INVALID", "rich_media.assets")
    elif contract.post_type == "rich_text":
        if not contract.content_blocks:
            _reject("RICH_MEDIA_RICH_TEXT_BLOCKS_REQUIRED", "rich_media.content_blocks")
        if referenced_assets != asset_ids:
            _reject("RICH_MEDIA_RICH_TEXT_ASSET_ORPHAN", "rich_media.content_blocks")


def _validate_contract(contract: RichMediaContractV1) -> None:
    if contract.schema_version != RICH_MEDIA_SCHEMA_VERSION:
        _reject("RICH_MEDIA_SCHEMA_VERSION_INVALID", "rich_media.schema_version")
    if (
        not isinstance(contract.post_type, str)
        or contract.post_type not in RICH_MEDIA_POST_TYPES
    ):
        _reject("RICH_MEDIA_POST_TYPE_INVALID", "rich_media.post_type")
    assets = _require_tuple(
        contract.assets,
        code="RICH_MEDIA_ASSET_COLLECTION_INVALID",
        path="rich_media.assets",
    )
    if len(assets) > RICH_MEDIA_MAX_ASSETS:
        _reject("RICH_MEDIA_ASSET_COLLECTION_INVALID", "rich_media.assets")
    if contract.post_type != "rich_text" and not assets:
        _reject("RICH_MEDIA_ASSET_COLLECTION_EMPTY", "rich_media.assets")

    assets_by_id: dict[str, RichMediaAssetDescriptorV1] = {}
    for index, descriptor in enumerate(assets):
        _validate_descriptor(descriptor, index=index)
        if descriptor.asset_id in assets_by_id:
            _reject("RICH_MEDIA_ASSET_ID_DUPLICATE", f"rich_media.assets[{index}].asset_id")
        assets_by_id[descriptor.asset_id] = descriptor

    grouped_assets, group_member_sets = _validate_groups(
        contract.groups,
        assets_by_id=assets_by_id,
    )
    referenced_assets = _validate_blocks(
        contract.content_blocks,
        assets_by_id=assets_by_id,
        group_member_sets=group_member_sets,
    )
    _validate_post_shape(
        contract,
        grouped_assets=grouped_assets,
        referenced_assets=referenced_assets,
    )


def validate_parse_result_rich_media(
    *,
    post_type: Any,
    assets: Any,
    rich_media: Any,
) -> None:
    """Cross-check a validated contract against its ordered physical assets."""

    if rich_media is None:
        return
    if type(rich_media) is not RichMediaContractV1:
        _reject("RICH_MEDIA_CONTRACT_TYPE_INVALID", "rich_media")
    _validate_contract(rich_media)
    if post_type != rich_media.post_type:
        _reject("RICH_MEDIA_POST_TYPE_MISMATCH", "rich_media.post_type")
    assets = _require_tuple(
        assets,
        code="RICH_MEDIA_RESULT_ASSETS_INVALID",
        path="assets",
    )
    if len(assets) != len(rich_media.assets):
        _reject("RICH_MEDIA_ASSET_COUNT_MISMATCH", "rich_media.assets")

    cover_locations = {
        value
        for asset in assets
        for value in (
            getattr(asset, "cover_url", ""),
            getattr(asset, "cover_preview_url", ""),
        )
        if value
    }
    actual_ids: set[str] = set()
    for index, (asset, descriptor) in enumerate(zip(assets, rich_media.assets)):
        path = f"rich_media.assets[{index}]"
        actual_id = _require_domain_id(
            getattr(asset, "asset_id", ""),
            code="RICH_MEDIA_RESULT_ASSET_ID_INVALID",
            path=f"{path}.asset_id",
        )
        if actual_id in actual_ids:
            _reject("RICH_MEDIA_RESULT_ASSET_ID_DUPLICATE", f"{path}.asset_id")
        actual_ids.add(actual_id)
        if descriptor.asset_id != actual_id:
            _reject("RICH_MEDIA_ASSET_ID_MISMATCH", f"{path}.asset_id")
        if descriptor.media_type != getattr(asset, "media_type", None):
            _reject("RICH_MEDIA_MEDIA_TYPE_MISMATCH", f"{path}.media_type")
        if descriptor.extension != normalize_media_extension(getattr(asset, "ext", "")):
            _reject("RICH_MEDIA_EXTENSION_MISMATCH", f"{path}.extension")
        if actual_id in cover_locations:
            _reject("RICH_MEDIA_COVER_REFERENCE_REJECTED", f"{path}.asset_id")
