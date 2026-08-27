#!/usr/bin/env python3
"""One-request ParseHub XHS image worker.

The worker intentionally exposes only exact-note image resource fingerprints and
raw HTTPS candidates. AnimeImages remains authoritative for identity, ordering,
format admission, and download authorization.
"""

from __future__ import annotations

import argparse
import asyncio
from hashlib import sha256
from importlib import metadata
import json
import re
import sys
from typing import Any
from urllib.parse import urlsplit


SCHEMA_VERSION = "1.0"
PARSEHUB_PIN = "144260f4349c5bb57415f7555a572fcd48c3b8ba"
PROVIDER_ID = "parsehub_xhs_images"
MAX_INPUT_BYTES = 16 * 1024
MAX_ITEMS = 64
MAX_URL_LENGTH = 4096
NOTE_ID_RE = re.compile(r"^[0-9a-f]{24}$")
RESOURCE_KEY_RE = re.compile(r"^[A-Za-z0-9._/-]{1,512}$")
REQUEST_FIELDS = {
    "schema_version",
    "request_id",
    "source_url",
    "expected_note_id",
    "capability",
    "max_items",
}


class WorkerError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _direct_url_commit() -> str:
    try:
        direct_url = metadata.distribution("parsehub").read_text("direct_url.json")
        payload = json.loads(direct_url or "{}")
    except (metadata.PackageNotFoundError, TypeError, ValueError, json.JSONDecodeError):
        return ""
    return str((payload.get("vcs_info") or {}).get("commit_id") or "").lower()


def _verify_parsehub_install() -> None:
    try:
        installed_version = metadata.version("parsehub")
        from parsehub.provider_api.xhs import XHSAPI  # noqa: F401
    except Exception as error:
        raise WorkerError("PARSEHUB_NOT_AVAILABLE") from error
    if installed_version != "2.2.0" or _direct_url_commit() != PARSEHUB_PIN:
        raise WorkerError("PARSEHUB_NOT_AVAILABLE")


def _normalize_note_id(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if NOTE_ID_RE.fullmatch(normalized) else ""


def _validated_source_url(value: Any) -> str:
    source_url = str(value or "")
    if (
        not source_url
        or source_url != source_url.strip()
        or len(source_url) > MAX_URL_LENGTH
        or any(ord(character) <= 32 or ord(character) == 127 for character in source_url)
    ):
        raise WorkerError("PARSEHUB_INVALID_JSON")
    try:
        parsed = urlsplit(source_url)
        port = parsed.port
    except (TypeError, ValueError) as error:
        raise WorkerError("PARSEHUB_INVALID_JSON") from error
    hostname = (parsed.hostname or "").lower()
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or not (
            hostname == "xhslink.cn"
            or hostname == "xhslink.com"
            or hostname == "xiaohongshu.com"
            or hostname.endswith(".xiaohongshu.com")
        )
    ):
        raise WorkerError("PARSEHUB_INVALID_JSON")
    return source_url


def _resource_key(api, value: Any) -> str:
    source = str(value or "").strip()
    if not source or len(source) > MAX_URL_LENGTH:
        raise WorkerError("PARSEHUB_RESULT_INVALID")
    key = str(api.get_trace_id(source) or "").strip().lstrip("/")
    if (
        not key
        or not RESOURCE_KEY_RE.fullmatch(key)
        or ".." in key
        or "\\" in key
        or "://" in key
        or key.startswith(".")
    ):
        raise WorkerError("PARSEHUB_RESULT_INVALID")
    return key


def _exact_note(data: Any, expected_note_id: str) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise WorkerError("PARSEHUB_RESULT_INVALID")
    note_root = data.get("note")
    detail_map = note_root.get("noteDetailMap") if isinstance(note_root, dict) else None
    if not isinstance(detail_map, dict):
        raise WorkerError("PARSEHUB_UNSUPPORTED_POST")
    for raw_identity, value in detail_map.items():
        if _normalize_note_id(raw_identity) != expected_note_id:
            continue
        note = value.get("note") if isinstance(value, dict) else None
        if not isinstance(note, dict):
            break
        claimed_note_id = _normalize_note_id(note.get("noteId") or raw_identity)
        if claimed_note_id != expected_note_id:
            raise WorkerError("PARSEHUB_IDENTITY_MISMATCH")
        return note
    raise WorkerError("PARSEHUB_IDENTITY_MISMATCH")


def _parse_request(raw: bytes) -> dict[str, Any]:
    if not raw or len(raw) > MAX_INPUT_BYTES:
        raise WorkerError("PARSEHUB_INVALID_JSON")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise WorkerError("PARSEHUB_INVALID_JSON") from error
    if not isinstance(payload, dict) or set(payload) != REQUEST_FIELDS:
        raise WorkerError("PARSEHUB_INVALID_JSON")
    if payload.get("schema_version") != SCHEMA_VERSION or payload.get("capability") != "xhs_images":
        raise WorkerError("PARSEHUB_INVALID_JSON")
    request_id = str(payload.get("request_id") or "")
    source_url = _validated_source_url(payload.get("source_url"))
    note_id = _normalize_note_id(payload.get("expected_note_id"))
    max_items = payload.get("max_items")
    if (
        not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", request_id)
        or not note_id
        or type(max_items) is not int
        or not 1 <= max_items <= MAX_ITEMS
    ):
        raise WorkerError("PARSEHUB_INVALID_JSON")
    return {
        "request_id": request_id,
        "source_url": source_url,
        "expected_note_id": note_id,
        "max_items": max_items,
    }


async def _extract(payload: dict[str, Any]) -> dict[str, Any]:
    from parsehub.provider_api.xhs import XHSAPI

    api = XHSAPI(proxy=None, cookie=None)
    html = await api._XHSAPI__fetch_html(payload["source_url"])
    data = await api._XHSAPI__extract_data(html)
    note = _exact_note(data, payload["expected_note_id"])
    if str(note.get("type") or "").strip().lower() not in {"normal", "image"}:
        raise WorkerError("PARSEHUB_UNSUPPORTED_POST")
    image_list = note.get("imageList")
    if (
        not isinstance(image_list, list)
        or not image_list
        or len(image_list) > payload["max_items"]
    ):
        raise WorkerError("PARSEHUB_RESULT_INVALID")

    items = []
    for ordinal, item in enumerate(image_list):
        if not isinstance(item, dict):
            raise WorkerError("PARSEHUB_RESULT_INVALID")
        key = _resource_key(api, item.get("urlDefault"))
        is_livephoto = item.get("livePhoto") is True
        items.append(
            {
                "source_ordinal": ordinal,
                "source_kind": "livephoto_still" if is_livephoto else "regular_image",
                "source_fingerprint": sha256(key.encode("utf-8")).hexdigest(),
                "raw_https_url": f"https://sns-img-hw.xhscdn.com/{key}",
                "declared_extension": "",
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "provider": PROVIDER_ID,
        "provider_version": f"parsehub@{PARSEHUB_PIN}",
        "claimed_platform": "xiaohongshu",
        "claimed_note_id": payload["expected_note_id"],
        "post_type": "image",
        "items": items,
    }


def _emit(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=True, separators=(",", ":")))
    sys.stdout.write("\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    try:
        _verify_parsehub_install()
        if args.check_only:
            _emit(
                {
                    "schema_version": SCHEMA_VERSION,
                    "provider": PROVIDER_ID,
                    "provider_version": f"parsehub@{PARSEHUB_PIN}",
                    "status": "ok",
                }
            )
            return 0
        request = _parse_request(sys.stdin.buffer.read(MAX_INPUT_BYTES + 1))
        _emit(asyncio.run(_extract(request)))
        return 0
    except WorkerError as error:
        _emit({"schema_version": SCHEMA_VERSION, "error": {"code": error.code}})
        return 2
    except Exception:
        _emit(
            {
                "schema_version": SCHEMA_VERSION,
                "error": {"code": "PARSEHUB_RESULT_INVALID"},
            }
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
