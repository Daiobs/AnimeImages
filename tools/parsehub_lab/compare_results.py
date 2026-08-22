#!/usr/bin/env python3
"""Compare two sanitized parser-result records without executing parsers."""

from __future__ import annotations

import argparse
import ipaddress
import json
import re
import sys
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "1.0"
COMPARISON_CLASSES = {
    "MATCH",
    "ANIMEIMAGES_RICHER",
    "SUPPLIER_RICHER",
    "STRUCTURAL_MISMATCH",
    "SECURITY_REVIEW_REQUIRED",
    "INVALID_INPUT",
}
TOP_LEVEL_KEYS = {
    "schema_version",
    "case_id",
    "source_project",
    "source_commit",
    "platform",
    "input_kind",
    "content_type",
    "title_present",
    "description_present",
    "author_present",
    "media_count",
    "media",
    "elapsed_ms",
    "outcome_class",
    "error_class",
    "credential_required",
    "network_executed",
    "notes",
}
MEDIA_KEYS = {
    "index",
    "type",
    "role",
    "paired_group",
    "extension",
    "width",
    "height",
    "duration_ms",
    "size_bytes",
    "size_is_estimate",
    "quality_label",
    "hostname",
    "has_thumbnail",
    "has_video_pair",
}
CONTENT_TYPES = {"video", "image_gallery", "live_photo", "multimedia", "rich_text", "error"}
INPUT_KINDS = {"sanitized_fixture", "offline_export"}
OUTCOME_CLASSES = {"success", "failed", "timeout", "skipped"}
ERROR_CLASSES = {"none", "parse_error", "auth_required", "unsupported", "timeout", "network", "invalid_result", "unknown"}
MEDIA_TYPES = {"video", "image", "animation", "text"}
MEDIA_ROLES = {"primary", "gallery", "cover", "paired_image", "paired_video", "inline"}
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
PROJECT_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{7,64}$")
EXTENSION_RE = re.compile(r"^(?:|[a-z0-9]{1,10})$")
HOSTNAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?$")
FULL_URL_RE = re.compile(r"https?://", re.IGNORECASE)
SENSITIVE_ASSIGNMENT_RE = re.compile(
    r"(?:api[_-]?key|access[_-]?key|client[_-]?secret|credential|session(?:[_-]?id)?|"
    r"xsec[_-]?token|token|cookie|authorization|auth|password|passwd|phone|"
    r"signature|signed[_-]?url|secret)\s*[:=]",
    re.IGNORECASE,
)
BEARER_RE = re.compile(r"\bbearer\s+[a-z0-9._~+/-]+", re.IGNORECASE)
BASIC_AUTH_RE = re.compile(r"\bbasic\s+[a-z0-9+/=]{8,}", re.IGNORECASE)
JWT_RE = re.compile(r"\beyJ[a-z0-9_-]{8,}\.[a-z0-9_-]{8,}\.[a-z0-9_-]{8,}\b", re.IGNORECASE)
OPAQUE_TOKEN_RE = re.compile(r"^[a-z0-9._~+/=-]{24,}$", re.IGNORECASE)
PHONE_RE = re.compile(r"(?<![A-Za-z0-9])(?:\+?\d[\d .()-]{7,}\d)(?![A-Za-z0-9])")
QUERY_RE = re.compile(r"\?")
FORBIDDEN_KEY_RE = re.compile(
    r"(?:url|uri|cookie|authorization|token|password|phone|secret|signature|payload|raw_error|headers?)",
    re.IGNORECASE,
)


class InputRejected(ValueError):
    """Raised when schema or redaction validation fails."""


def _is_bool(value: Any) -> bool:
    return isinstance(value, bool)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _require_int(value: Any, *, minimum: int, maximum: int) -> None:
    if not _is_int(value) or not minimum <= value <= maximum:
        raise InputRejected("integer field is outside the accepted range")


def _require_safe_text(
    value: Any,
    *,
    maximum: int,
    allow_empty: bool = True,
    check_phone: bool = True,
    check_opaque_token: bool = True,
) -> str:
    if not isinstance(value, str) or len(value) > maximum or (not allow_empty and not value):
        raise InputRejected("text field is invalid")
    if (
        FULL_URL_RE.search(value)
        or QUERY_RE.search(value)
        or SENSITIVE_ASSIGNMENT_RE.search(value)
        or BEARER_RE.search(value)
        or BASIC_AUTH_RE.search(value)
        or JWT_RE.search(value)
        or (check_opaque_token and OPAQUE_TOKEN_RE.fullmatch(value))
        or (check_phone and PHONE_RE.search(value))
    ):
        raise InputRejected("sensitive or URL-like text is not allowed")
    return value


def _validate_hostname(value: Any) -> str:
    hostname = _require_safe_text(value, maximum=253, check_opaque_token=False).lower().rstrip(".")
    if not hostname:
        return ""
    if not HOSTNAME_RE.fullmatch(hostname) or ".." in hostname:
        raise InputRejected("hostname is invalid")
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        raise InputRejected("IP addresses are not accepted as hostnames")
    if any(not label or len(label) > 63 or label.startswith("-") or label.endswith("-") for label in hostname.split(".")):
        raise InputRejected("hostname labels are invalid")
    return hostname


def _validate_keys(value: Any, *, allowed: set[str]) -> None:
    if not isinstance(value, dict) or set(value) != allowed:
        extras = set(value) - allowed if isinstance(value, dict) else set()
        if any(FORBIDDEN_KEY_RE.search(str(key)) for key in extras):
            raise InputRejected("forbidden key detected")
        raise InputRejected("record keys do not match schema")


def _require_enum(value: Any, *, allowed: set[str], message: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise InputRejected(message)
    return value


def _validate_media(item: Any, position: int) -> dict[str, Any]:
    _validate_keys(item, allowed=MEDIA_KEYS)
    _require_int(item["index"], minimum=0, maximum=999)
    if item["index"] != position:
        raise InputRejected("media indexes must be contiguous and ordered")
    _require_enum(item["type"], allowed=MEDIA_TYPES, message="media type or role is invalid")
    _require_enum(item["role"], allowed=MEDIA_ROLES, message="media type or role is invalid")
    paired_group = _require_safe_text(item["paired_group"], maximum=32, check_opaque_token=False)
    if paired_group and not SLUG_RE.fullmatch(paired_group):
        raise InputRejected("paired group is invalid")
    extension = _require_safe_text(item["extension"], maximum=10, check_opaque_token=False)
    if not EXTENSION_RE.fullmatch(extension):
        raise InputRejected("extension is invalid")
    for field, maximum in (
        ("width", 100000),
        ("height", 100000),
        ("duration_ms", 86400000),
        ("size_bytes", 1099511627776),
    ):
        _require_int(item[field], minimum=0, maximum=maximum)
    for field in ("size_is_estimate", "has_thumbnail", "has_video_pair"):
        if not _is_bool(item[field]):
            raise InputRejected("media boolean field is invalid")
    quality_label = _require_safe_text(item["quality_label"], maximum=80)
    hostname = _validate_hostname(item["hostname"])
    normalized = dict(item)
    normalized["paired_group"] = paired_group
    normalized["extension"] = extension
    normalized["quality_label"] = quality_label
    normalized["hostname"] = hostname
    return normalized


def validate_record(value: Any) -> dict[str, Any]:
    _validate_keys(value, allowed=TOP_LEVEL_KEYS)
    if value["schema_version"] != SCHEMA_VERSION:
        raise InputRejected("unsupported schema version")
    case_id = _require_safe_text(
        value["case_id"], maximum=64, allow_empty=False, check_phone=False, check_opaque_token=False
    )
    project = _require_safe_text(
        value["source_project"], maximum=64, allow_empty=False, check_phone=False, check_opaque_token=False
    )
    commit = _require_safe_text(
        value["source_commit"], maximum=64, allow_empty=False, check_phone=False, check_opaque_token=False
    )
    platform = _require_safe_text(
        value["platform"], maximum=32, allow_empty=False, check_phone=False, check_opaque_token=False
    )
    if not SLUG_RE.fullmatch(case_id) or not PROJECT_RE.fullmatch(project):
        raise InputRejected("record identifier is invalid")
    if not COMMIT_RE.fullmatch(commit) or not SLUG_RE.fullmatch(platform):
        raise InputRejected("commit or platform identifier is invalid")
    _require_enum(value["input_kind"], allowed=INPUT_KINDS, message="input kind or content type is invalid")
    _require_enum(value["content_type"], allowed=CONTENT_TYPES, message="input kind or content type is invalid")
    for field in ("title_present", "description_present", "author_present", "credential_required", "network_executed"):
        if not _is_bool(value[field]):
            raise InputRejected("record boolean field is invalid")
    _require_int(value["media_count"], minimum=0, maximum=1000)
    if not isinstance(value["media"], list) or len(value["media"]) != value["media_count"]:
        raise InputRejected("media count does not match media array")
    media = [_validate_media(item, index) for index, item in enumerate(value["media"])]
    _require_int(value["elapsed_ms"], minimum=0, maximum=86400000)
    _require_enum(value["outcome_class"], allowed=OUTCOME_CLASSES, message="outcome or error class is invalid")
    _require_enum(value["error_class"], allowed=ERROR_CLASSES, message="outcome or error class is invalid")
    notes = _require_safe_text(value["notes"], maximum=240)
    normalized = dict(value)
    normalized.update({"case_id": case_id, "source_project": project, "source_commit": commit, "platform": platform, "media": media, "notes": notes})
    return normalized


def _difference(field: str, left: Any, right: Any) -> dict[str, Any]:
    return {"field": field, "animeimages": left, "supplier": right}


def _presence_score(record: dict[str, Any]) -> int:
    score = sum(int(record[field]) for field in ("title_present", "description_present", "author_present"))
    for media in record["media"]:
        score += sum(
            int(bool(media[field]))
            for field in ("extension", "width", "height", "duration_ms", "size_bytes", "quality_label", "hostname", "has_thumbnail", "has_video_pair")
        )
    return score


def compare_records(animeimages_value: Any, supplier_value: Any) -> dict[str, Any]:
    try:
        left = validate_record(animeimages_value)
        right = validate_record(supplier_value)
    except (InputRejected, KeyError, TypeError, ValueError):
        return {
            "schema_version": SCHEMA_VERSION,
            "classification": "INVALID_INPUT",
            "case_id": "rejected",
            "summary": "One or both inputs failed the schema or redaction gate.",
            "differences": [],
        }

    differences: list[dict[str, Any]] = []
    classification = "MATCH"
    case_id = left["case_id"] if left["case_id"] == right["case_id"] else "mismatched_cases"

    if left["credential_required"] or right["credential_required"]:
        classification = "SECURITY_REVIEW_REQUIRED"
        differences.append(_difference("credential_required", left["credential_required"], right["credential_required"]))

    structural_fields = ("case_id", "platform", "content_type", "outcome_class", "error_class")
    structural = False
    for field in structural_fields:
        if left[field] != right[field]:
            structural = True
            differences.append(_difference(field, left[field], right[field]))

    max_media = max(left["media_count"], right["media_count"])
    for index in range(max_media):
        if index >= left["media_count"]:
            differences.append(_difference(f"media[{index}]", "missing", "present"))
            continue
        if index >= right["media_count"]:
            differences.append(_difference(f"media[{index}]", "present", "missing"))
            continue
        left_media = left["media"][index]
        right_media = right["media"][index]
        for field in MEDIA_KEYS - {"index"}:
            if left_media[field] != right_media[field]:
                differences.append(_difference(f"media[{index}].{field}", left_media[field], right_media[field]))
                if field in {"type", "role", "paired_group", "has_video_pair"}:
                    structural = True

    for field in ("title_present", "description_present", "author_present", "media_count"):
        if left[field] != right[field]:
            differences.append(_difference(field, left[field], right[field]))

    host_conflict = any(
        item["field"].endswith(".hostname")
        and item["animeimages"]
        and item["supplier"]
        for item in differences
    )
    conflicting_values = any(
        item["field"].endswith((".extension", ".width", ".height", ".duration_ms", ".size_bytes"))
        and item["animeimages"] not in (0, "")
        and item["supplier"] not in (0, "")
        for item in differences
    )

    if classification != "SECURITY_REVIEW_REQUIRED":
        if host_conflict:
            classification = "SECURITY_REVIEW_REQUIRED"
        elif structural or conflicting_values:
            classification = "STRUCTURAL_MISMATCH"
        elif differences:
            left_score = _presence_score(left) + left["media_count"] * 10
            right_score = _presence_score(right) + right["media_count"] * 10
            if left_score > right_score:
                classification = "ANIMEIMAGES_RICHER"
            elif right_score > left_score:
                classification = "SUPPLIER_RICHER"
            else:
                classification = "STRUCTURAL_MISMATCH"

    assert classification in COMPARISON_CLASSES
    summary_map = {
        "MATCH": "The sanitized result structures match.",
        "ANIMEIMAGES_RICHER": "AnimeImages exposes more normalized result information.",
        "SUPPLIER_RICHER": "The supplier exposes more normalized result information.",
        "STRUCTURAL_MISMATCH": "The sanitized result structures conflict.",
        "SECURITY_REVIEW_REQUIRED": "Credential or media-host differences require security review.",
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "classification": classification,
        "case_id": case_id,
        "summary": summary_map[classification],
        "differences": sorted(differences, key=lambda item: item["field"]),
    }


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise InputRejected("input file is missing or invalid JSON") from error


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Compare two sanitized offline parser results.")
    parser.add_argument("animeimages_result", type=Path)
    parser.add_argument("supplier_result", type=Path)
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        left = load_json(args.animeimages_result)
        right = load_json(args.supplier_result)
        result = compare_records(left, right)
    except InputRejected:
        result = {
            "schema_version": SCHEMA_VERSION,
            "classification": "INVALID_INPUT",
            "case_id": "rejected",
            "summary": "One or both inputs failed the schema or redaction gate.",
            "differences": [],
        }
    if args.output:
        args.output.write_text(json.dumps(result, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    print(f"classification: {result['classification']}")
    print(f"case_id: {result['case_id']}")
    print(f"summary: {result['summary']}")
    print(f"difference_count: {len(result['differences'])}")
    return 2 if result["classification"] == "INVALID_INPUT" else 0


if __name__ == "__main__":
    raise SystemExit(main())
