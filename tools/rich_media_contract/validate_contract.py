#!/usr/bin/env python3
"""Validate URL-free Rich Media Domain Contract v1 documents."""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "1.0"
VALID = "VALID"
INVALID_SCHEMA = "INVALID_SCHEMA"
INVALID_ASSET = "INVALID_ASSET"
INVALID_GROUP = "INVALID_GROUP"
INVALID_BLOCK = "INVALID_BLOCK"
SECURITY_REJECTED = "SECURITY_REJECTED"

TOP_LEVEL_KEYS = {
    "schema_version",
    "case_id",
    "post_type",
    "assets",
    "groups",
    "content_blocks",
}
ASSET_KEYS = {"asset_id", "media_type", "extension", "role"}
GROUP_KEYS = {"group_id", "relationship_type", "members"}
GROUP_MEMBER_KEYS = {"asset_id", "role"}
TEXT_BLOCK_KEYS = {"type", "text"}
MEDIA_BLOCK_KEYS = {"type", "asset_ids"}

POST_TYPES = {
    "image",
    "video",
    "gallery",
    "live_photo",
    "multimedia",
    "rich_text",
}
MEDIA_TYPES = {"image", "video"}
ASSET_ROLES = {"primary", "gallery", "inline", "still", "motion", "animation"}
IMAGE_ROLES = {"primary", "gallery", "inline", "still", "animation"}
VIDEO_ROLES = {"primary", "inline", "motion", "animation"}
IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "gif", "webp", "avif"}
VIDEO_EXTENSIONS = {"mp4", "mov", "m3u8"}
ANIMATION_IMAGE_EXTENSIONS = {"gif", "webp", "avif"}
ANIMATION_VIDEO_EXTENSIONS = {"mp4"}
RELATIONSHIP_TYPES = {"live_photo"}
LIVE_PHOTO_MEMBER_ROLES = {"still", "motion"}
BLOCK_TYPES = {"text", "media"}

MAX_ASSETS = 64
MAX_GROUPS = 32
MAX_CONTENT_BLOCKS = 128
MAX_MEDIA_REFS_PER_BLOCK = 64
MAX_TEXT_BLOCK_CHARS = 4096
MAX_TOTAL_TEXT_CHARS = 16384
MAX_DOCUMENT_NODES = 4096
MAX_DOCUMENT_DEPTH = 6

CASE_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
DOMAIN_ID_RE = re.compile(
    r"^[a-z][a-z0-9_-]{0,31}(?::[a-z0-9][a-z0-9_-]{0,31}){1,5}$"
)
FORBIDDEN_KEY_RE = re.compile(
    r"(?:url|uri|href|src|link|path|query|token|secret|cookie|headers?|"
    r"authorization|credential|password|manifest|signature|payload|html|"
    r"markdown|script|style|raw)",
    re.IGNORECASE,
)
URL_LIKE_RE = re.compile(
    r"(?:https?://|www\.|javascript:|data:|file:|ftp:)", re.IGNORECASE
)
HTML_TAG_RE = re.compile(r"<\s*/?\s*[a-z][^>]*>", re.IGNORECASE)
MARKDOWN_LINK_RE = re.compile(r"!?\[[^\]]*\]\s*\(")
SENSITIVE_TEXT_RE = re.compile(
    r"(?:bearer\s+|basic\s+|api[_-]?key\s*[:=]|token\s*[:=]|"
    r"cookie\s*[:=]|password\s*[:=]|secret\s*[:=])",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ValidationResult:
    status: str
    code: str
    path: str

    @property
    def is_valid(self) -> bool:
        return self.status == VALID

    def render(self) -> str:
        return f"{self.status} code={self.code} path={self.path}"


class ContractRejected(ValueError):
    def __init__(self, result: ValidationResult):
        super().__init__(result.code)
        self.result = result


def _reject(status: str, code: str, path: str) -> None:
    raise ContractRejected(ValidationResult(status=status, code=code, path=path))


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _scan_security(value: Any) -> None:
    state = {"nodes": 0}

    def visit(item: Any, *, depth: int, path: str) -> None:
        state["nodes"] += 1
        if state["nodes"] > MAX_DOCUMENT_NODES:
            _reject(SECURITY_REJECTED, "DOCUMENT_NODE_LIMIT", "document")
        if depth > MAX_DOCUMENT_DEPTH:
            _reject(SECURITY_REJECTED, "DOCUMENT_DEPTH_LIMIT", "document")

        if isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str):
                    _reject(SECURITY_REJECTED, "NON_TEXT_KEY", "document")
                if FORBIDDEN_KEY_RE.search(key):
                    _reject(SECURITY_REJECTED, "FORBIDDEN_FIELD", "document")
                child_path = f"{path}.{key}" if path else key
                visit(child, depth=depth + 1, path=child_path)
        elif isinstance(item, list):
            for index, child in enumerate(item):
                visit(child, depth=depth + 1, path=f"{path}[{index}]")
        elif isinstance(item, str):
            if URL_LIKE_RE.search(item):
                _reject(SECURITY_REJECTED, "URL_LIKE_TEXT", path or "document")
            if HTML_TAG_RE.search(item):
                _reject(SECURITY_REJECTED, "RAW_HTML", path or "document")
            if MARKDOWN_LINK_RE.search(item):
                _reject(SECURITY_REJECTED, "MARKDOWN_LINK", path or "document")
            if SENSITIVE_TEXT_RE.search(item):
                _reject(SECURITY_REJECTED, "SENSITIVE_TEXT", path or "document")

    visit(value, depth=0, path="")


def _require_exact_keys(
    value: Any,
    *,
    expected: set[str],
    status: str,
    code: str,
    path: str,
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        _reject(status, code, path)
    return value


def _require_domain_id(value: Any, *, status: str, code: str, path: str) -> str:
    if not isinstance(value, str) or len(value) > 128 or not DOMAIN_ID_RE.fullmatch(value):
        _reject(status, code, path)
    return value


def _validate_asset(item: Any, index: int) -> dict[str, str]:
    path = f"assets[{index}]"
    asset = _require_exact_keys(
        item,
        expected=ASSET_KEYS,
        status=INVALID_ASSET,
        code="ASSET_FIELDS_INVALID",
        path=path,
    )
    asset_id = _require_domain_id(
        asset["asset_id"],
        status=INVALID_ASSET,
        code="ASSET_ID_INVALID",
        path=f"{path}.asset_id",
    )
    media_type = asset["media_type"]
    if not isinstance(media_type, str) or media_type not in MEDIA_TYPES:
        _reject(INVALID_ASSET, "ASSET_MEDIA_TYPE_INVALID", f"{path}.media_type")
    extension = asset["extension"]
    allowed_extensions = IMAGE_EXTENSIONS if media_type == "image" else VIDEO_EXTENSIONS
    if not isinstance(extension, str) or extension not in allowed_extensions:
        _reject(INVALID_ASSET, "ASSET_EXTENSION_INVALID", f"{path}.extension")
    role = asset["role"]
    if not isinstance(role, str) or role not in ASSET_ROLES:
        _reject(INVALID_ASSET, "ASSET_ROLE_INVALID", f"{path}.role")
    allowed_roles = IMAGE_ROLES if media_type == "image" else VIDEO_ROLES
    if role not in allowed_roles:
        _reject(INVALID_ASSET, "ASSET_ROLE_TYPE_MISMATCH", f"{path}.role")
    if role == "animation":
        animation_extensions = (
            ANIMATION_IMAGE_EXTENSIONS
            if media_type == "image"
            else ANIMATION_VIDEO_EXTENSIONS
        )
        if extension not in animation_extensions:
            _reject(INVALID_ASSET, "ANIMATION_EXTENSION_INVALID", f"{path}.extension")
    return {
        "asset_id": asset_id,
        "media_type": media_type,
        "extension": extension,
        "role": role,
    }


def _validate_groups(
    value: Any,
    *,
    assets_by_id: dict[str, dict[str, str]],
) -> tuple[list[dict[str, Any]], set[str]]:
    if not isinstance(value, list) or len(value) > MAX_GROUPS:
        _reject(INVALID_GROUP, "GROUP_COLLECTION_INVALID", "groups")

    groups: list[dict[str, Any]] = []
    group_ids: set[str] = set()
    grouped_assets: set[str] = set()
    for index, item in enumerate(value):
        path = f"groups[{index}]"
        group = _require_exact_keys(
            item,
            expected=GROUP_KEYS,
            status=INVALID_GROUP,
            code="GROUP_FIELDS_INVALID",
            path=path,
        )
        group_id = _require_domain_id(
            group["group_id"],
            status=INVALID_GROUP,
            code="GROUP_ID_INVALID",
            path=f"{path}.group_id",
        )
        if group_id in group_ids:
            _reject(INVALID_GROUP, "DUPLICATE_GROUP_ID", f"{path}.group_id")
        group_ids.add(group_id)

        relationship_type = group["relationship_type"]
        if relationship_type not in RELATIONSHIP_TYPES:
            _reject(
                INVALID_GROUP,
                "RELATIONSHIP_TYPE_INVALID",
                f"{path}.relationship_type",
            )
        members = group["members"]
        if not isinstance(members, list) or len(members) != 2:
            _reject(INVALID_GROUP, "LIVE_PHOTO_MEMBER_COUNT", f"{path}.members")

        normalized_members = []
        member_ids: set[str] = set()
        member_roles: set[str] = set()
        for member_index, raw_member in enumerate(members):
            member_path = f"{path}.members[{member_index}]"
            member = _require_exact_keys(
                raw_member,
                expected=GROUP_MEMBER_KEYS,
                status=INVALID_GROUP,
                code="GROUP_MEMBER_FIELDS_INVALID",
                path=member_path,
            )
            asset_id = _require_domain_id(
                member["asset_id"],
                status=INVALID_GROUP,
                code="GROUP_ASSET_ID_INVALID",
                path=f"{member_path}.asset_id",
            )
            role = member["role"]
            if role not in LIVE_PHOTO_MEMBER_ROLES:
                _reject(INVALID_GROUP, "LIVE_PHOTO_ROLE_INVALID", f"{member_path}.role")
            if asset_id in member_ids:
                _reject(INVALID_GROUP, "DUPLICATE_GROUP_MEMBER", f"{member_path}.asset_id")
            if asset_id in grouped_assets:
                _reject(INVALID_GROUP, "ASSET_IN_MULTIPLE_LIVE_PHOTO_GROUPS", f"{member_path}.asset_id")
            if asset_id not in assets_by_id:
                _reject(INVALID_GROUP, "GROUP_ASSET_NOT_FOUND", f"{member_path}.asset_id")

            asset = assets_by_id[asset_id]
            expected_media_type = "image" if role == "still" else "video"
            if asset["media_type"] != expected_media_type or asset["role"] != role:
                _reject(INVALID_GROUP, "LIVE_PHOTO_MEMBER_MISMATCH", member_path)
            member_ids.add(asset_id)
            member_roles.add(role)
            grouped_assets.add(asset_id)
            normalized_members.append({"asset_id": asset_id, "role": role})

        if member_roles != LIVE_PHOTO_MEMBER_ROLES:
            _reject(INVALID_GROUP, "LIVE_PHOTO_PAIR_INCOMPLETE", f"{path}.members")
        groups.append(
            {
                "group_id": group_id,
                "relationship_type": relationship_type,
                "members": normalized_members,
            }
        )
    return groups, grouped_assets


def _validate_blocks(
    value: Any,
    *,
    assets_by_id: dict[str, dict[str, str]],
    groups: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], set[str]]:
    if not isinstance(value, list) or len(value) > MAX_CONTENT_BLOCKS:
        _reject(INVALID_BLOCK, "BLOCK_COLLECTION_INVALID", "content_blocks")

    blocks: list[dict[str, Any]] = []
    referenced_assets: set[str] = set()
    media_block_sets: list[set[str]] = []
    total_text_chars = 0
    for index, item in enumerate(value):
        path = f"content_blocks[{index}]"
        if not isinstance(item, dict):
            _reject(INVALID_BLOCK, "BLOCK_FIELDS_INVALID", path)
        block_type = item.get("type")
        if block_type not in BLOCK_TYPES:
            _reject(INVALID_BLOCK, "BLOCK_TYPE_INVALID", f"{path}.type")
        if block_type == "text":
            block = _require_exact_keys(
                item,
                expected=TEXT_BLOCK_KEYS,
                status=INVALID_BLOCK,
                code="TEXT_BLOCK_FIELDS_INVALID",
                path=path,
            )
            text = block["text"]
            if not isinstance(text, str) or not text or len(text) > MAX_TEXT_BLOCK_CHARS:
                _reject(INVALID_BLOCK, "TEXT_BLOCK_LENGTH_INVALID", f"{path}.text")
            total_text_chars += len(text)
            if total_text_chars > MAX_TOTAL_TEXT_CHARS:
                _reject(INVALID_BLOCK, "TOTAL_TEXT_LIMIT_EXCEEDED", "content_blocks")
            blocks.append({"type": "text", "text": text})
            continue

        block = _require_exact_keys(
            item,
            expected=MEDIA_BLOCK_KEYS,
            status=INVALID_BLOCK,
            code="MEDIA_BLOCK_FIELDS_INVALID",
            path=path,
        )
        asset_ids = block["asset_ids"]
        if (
            not isinstance(asset_ids, list)
            or not asset_ids
            or len(asset_ids) > MAX_MEDIA_REFS_PER_BLOCK
        ):
            _reject(INVALID_BLOCK, "MEDIA_REF_COLLECTION_INVALID", f"{path}.asset_ids")
        normalized_ids = []
        local_ids: set[str] = set()
        for ref_index, value in enumerate(asset_ids):
            ref_path = f"{path}.asset_ids[{ref_index}]"
            asset_id = _require_domain_id(
                value,
                status=INVALID_BLOCK,
                code="MEDIA_REF_ID_INVALID",
                path=ref_path,
            )
            if asset_id not in assets_by_id:
                _reject(INVALID_BLOCK, "MEDIA_REF_NOT_FOUND", ref_path)
            if asset_id in local_ids:
                _reject(INVALID_BLOCK, "DUPLICATE_MEDIA_REF", ref_path)
            local_ids.add(asset_id)
            referenced_assets.add(asset_id)
            normalized_ids.append(asset_id)
        media_block_sets.append(local_ids)
        blocks.append({"type": "media", "asset_ids": normalized_ids})

    for group in groups:
        member_ids = {member["asset_id"] for member in group["members"]}
        if media_block_sets and not any(member_ids <= block_ids for block_ids in media_block_sets):
            _reject(INVALID_BLOCK, "LIVE_PHOTO_BLOCK_PAIR_SPLIT", "content_blocks")
    return blocks, referenced_assets


def _validate_post_shape(
    post_type: str,
    assets: list[dict[str, str]],
    groups: list[dict[str, Any]],
    grouped_assets: set[str],
    blocks: list[dict[str, Any]],
    referenced_assets: set[str],
) -> None:
    image_count = sum(asset["media_type"] == "image" for asset in assets)
    video_count = sum(asset["media_type"] == "video" for asset in assets)
    asset_ids = {asset["asset_id"] for asset in assets}

    if post_type != "rich_text" and blocks:
        _reject(INVALID_SCHEMA, "BLOCKS_REQUIRE_RICH_TEXT", "content_blocks")
    if post_type not in {"live_photo", "multimedia", "rich_text"} and groups:
        _reject(INVALID_SCHEMA, "GROUPS_NOT_ALLOWED_FOR_POST_TYPE", "groups")

    if post_type == "image":
        if len(assets) != 1 or image_count != 1:
            _reject(INVALID_SCHEMA, "IMAGE_POST_SHAPE_INVALID", "assets")
    elif post_type == "video":
        if not assets or video_count != len(assets):
            _reject(INVALID_SCHEMA, "VIDEO_POST_SHAPE_INVALID", "assets")
    elif post_type == "gallery":
        if len(assets) < 2 or image_count != len(assets):
            _reject(INVALID_SCHEMA, "GALLERY_POST_SHAPE_INVALID", "assets")
    elif post_type == "live_photo":
        if not groups or grouped_assets != asset_ids:
            _reject(INVALID_SCHEMA, "LIVE_PHOTO_POST_SHAPE_INVALID", "groups")
    elif post_type == "multimedia":
        if not assets or not image_count or not video_count:
            _reject(INVALID_SCHEMA, "MULTIMEDIA_POST_SHAPE_INVALID", "assets")
    elif post_type == "rich_text":
        if not blocks:
            _reject(INVALID_SCHEMA, "RICH_TEXT_BLOCKS_REQUIRED", "content_blocks")
        if referenced_assets != asset_ids:
            _reject(INVALID_BLOCK, "RICH_TEXT_ASSET_REFERENCE_INCOMPLETE", "content_blocks")


def _validate(value: Any) -> dict[str, Any]:
    _scan_security(value)
    document = _require_exact_keys(
        value,
        expected=TOP_LEVEL_KEYS,
        status=INVALID_SCHEMA,
        code="TOP_LEVEL_FIELDS_INVALID",
        path="document",
    )
    if document["schema_version"] != SCHEMA_VERSION:
        _reject(INVALID_SCHEMA, "SCHEMA_VERSION_UNSUPPORTED", "schema_version")
    case_id = document["case_id"]
    if not isinstance(case_id, str) or not CASE_ID_RE.fullmatch(case_id):
        _reject(INVALID_SCHEMA, "CASE_ID_INVALID", "case_id")
    post_type = document["post_type"]
    if not isinstance(post_type, str) or post_type not in POST_TYPES:
        _reject(INVALID_SCHEMA, "POST_TYPE_INVALID", "post_type")

    raw_assets = document["assets"]
    if not isinstance(raw_assets, list) or len(raw_assets) > MAX_ASSETS:
        _reject(INVALID_ASSET, "ASSET_COLLECTION_INVALID", "assets")
    if post_type != "rich_text" and not raw_assets:
        _reject(INVALID_ASSET, "ASSET_COLLECTION_EMPTY", "assets")

    assets = []
    assets_by_id: dict[str, dict[str, str]] = {}
    for index, raw_asset in enumerate(raw_assets):
        asset = _validate_asset(raw_asset, index)
        if asset["asset_id"] in assets_by_id:
            _reject(INVALID_ASSET, "DUPLICATE_ASSET_ID", f"assets[{index}].asset_id")
        assets.append(asset)
        assets_by_id[asset["asset_id"]] = asset

    groups, grouped_assets = _validate_groups(
        document["groups"], assets_by_id=assets_by_id
    )
    blocks, referenced_assets = _validate_blocks(
        document["content_blocks"],
        assets_by_id=assets_by_id,
        groups=groups,
    )
    _validate_post_shape(
        post_type,
        assets,
        groups,
        grouped_assets,
        blocks,
        referenced_assets,
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "case_id": case_id,
        "post_type": post_type,
        "assets": assets,
        "groups": groups,
        "content_blocks": blocks,
    }


def validate_document(value: Any) -> ValidationResult:
    try:
        _validate(value)
    except ContractRejected as error:
        return error.result
    except (KeyError, TypeError, ValueError):
        return ValidationResult(INVALID_SCHEMA, "DOCUMENT_INVALID", "document")
    return ValidationResult(VALID, "OK", "document")


def normalize_document(value: Any) -> dict[str, Any]:
    """Return a normalized document or raise a safe ContractRejected error."""
    return _validate(value)


def load_document(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ContractRejected(
            ValidationResult(INVALID_SCHEMA, "JSON_INPUT_INVALID", "document")
        ) from error


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate URL-free AnimeImages Rich Media Contract v1 documents."
    )
    parser.add_argument("documents", nargs="+", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    failed = False
    for index, path in enumerate(args.documents):
        try:
            result = validate_document(load_document(path))
        except ContractRejected as error:
            result = error.result
        print(f"case={index} {result.render()}")
        failed = failed or not result.is_valid
    return 2 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
