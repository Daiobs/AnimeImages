#!/usr/bin/env python3
"""Map an already-sanitized Parser Lab result into the v1 domain contract."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.parsehub_lab.compare_results import InputRejected, validate_record
from tools.rich_media_contract.validate_contract import (
    ContractRejected,
    normalize_document,
)


CONTENT_TYPE_MAP = {
    "video": "video",
    "image_gallery": "gallery",
    "live_photo": "live_photo",
    "multimedia": "multimedia",
    "rich_text": "rich_text",
}
ROLE_MAP = {
    "primary": "primary",
    "gallery": "gallery",
    "paired_image": "still",
    "paired_video": "motion",
    "inline": "inline",
}
TEXT_PRESENCE_MARKER = "Sanitized rich-text content is present."


class MappingRejected(ValueError):
    pass


def _asset_id(case_id: str, index: int) -> str:
    return f"asset:{case_id}:{index + 1}"


def _group_id(case_id: str, paired_group: str) -> str:
    safe_group = paired_group.replace("_", "-")
    return f"group:{case_id}:{safe_group}"


def map_record(value: Any) -> dict[str, Any]:
    try:
        record = validate_record(value)
    except (InputRejected, KeyError, TypeError, ValueError) as error:
        raise MappingRejected("MAPPING_INPUT_REJECTED") from error
    if record["outcome_class"] != "success" or record["content_type"] not in CONTENT_TYPE_MAP:
        raise MappingRejected("MAPPING_CONTENT_UNSUPPORTED")

    assets = []
    groups_by_source: dict[str, list[dict[str, str]]] = {}
    content_blocks = []
    for index, media in enumerate(record["media"]):
        media_type = media["type"]
        if media_type == "text":
            if record["content_type"] == "rich_text":
                content_blocks.append({"type": "text", "text": TEXT_PRESENCE_MARKER})
            continue
        if media["role"] == "cover":
            raise MappingRejected("MAPPING_COVER_IS_NOT_CANONICAL_ASSET")

        extension = media["extension"]
        if media_type == "animation":
            physical_type = "video" if extension == "mp4" else "image"
            role = "animation"
        elif media_type in {"image", "video"}:
            physical_type = media_type
            role = ROLE_MAP.get(media["role"])
        else:
            raise MappingRejected("MAPPING_MEDIA_TYPE_UNSUPPORTED")
        if not role:
            raise MappingRejected("MAPPING_MEDIA_ROLE_UNSUPPORTED")

        asset_id = _asset_id(record["case_id"], index)
        assets.append(
            {
                "asset_id": asset_id,
                "media_type": physical_type,
                "extension": extension,
                "role": role,
            }
        )
        if media["paired_group"]:
            groups_by_source.setdefault(media["paired_group"], []).append(
                {"asset_id": asset_id, "role": role}
            )
        if record["content_type"] == "rich_text":
            content_blocks.append({"type": "media", "asset_ids": [asset_id]})

    groups = [
        {
            "group_id": _group_id(record["case_id"], source_group),
            "relationship_type": "live_photo",
            "members": members,
        }
        for source_group, members in groups_by_source.items()
    ]
    if record["content_type"] == "rich_text":
        for group in groups:
            member_ids = [member["asset_id"] for member in group["members"]]
            positions = [
                index
                for index, block in enumerate(content_blocks)
                if block["type"] == "media"
                and set(block["asset_ids"]) <= set(member_ids)
            ]
            if positions:
                content_blocks[positions[0]] = {
                    "type": "media",
                    "asset_ids": member_ids,
                }
                for position in reversed(positions[1:]):
                    del content_blocks[position]
    if record["content_type"] == "rich_text" and not any(
        block["type"] == "text" for block in content_blocks
    ):
        if record["description_present"] or record["title_present"]:
            content_blocks.insert(0, {"type": "text", "text": TEXT_PRESENCE_MARKER})

    document = {
        "schema_version": "1.0",
        "case_id": record["case_id"],
        "post_type": CONTENT_TYPE_MAP[record["content_type"]],
        "assets": assets,
        "groups": groups,
        "content_blocks": content_blocks,
    }
    try:
        return normalize_document(document)
    except ContractRejected as error:
        raise MappingRejected(f"MAPPING_OUTPUT_{error.result.code}") from error


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise MappingRejected("MAPPING_JSON_INVALID") from error


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Map a sanitized Parser Lab result to Rich Media Contract v1."
    )
    parser.add_argument("input", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        result = map_record(load_json(args.input))
    except MappingRejected as error:
        print(f"INVALID_SCHEMA code={error} path=document")
        return 2
    rendered = json.dumps(result, indent=2, sort_keys=False) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    print("VALID code=OK path=document")
    return 0


if __name__ == "__main__":
    sys.exit(main())
