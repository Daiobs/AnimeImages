"""Dependency-free protocol helpers for isolated Provider Lab workers."""

from __future__ import annotations

from dataclasses import fields, is_dataclass
import ipaddress
import json
import math
import re
import sys
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit


SCHEMA_VERSION = "1.0"
MAX_INPUT_BYTES = 32 * 1024
MAX_SOURCE_LENGTH = 4096
MAX_ASSETS = 64
MAX_DEPTH = 6
MAX_KEYS = 128
MAX_LIST_ITEMS = 64
MAX_STRING = 512
MAX_RAW_BYTES = 256 * 1024
REQUEST_FIELDS = {
    "schema_version",
    "request_id",
    "source",
    "max_assets",
    "max_output_bytes",
}
SENSITIVE_KEY_RE = re.compile(
    r"(?:url|uri|href|src|query|cookie|authorization|token|secret|password|passwd|"
    r"header|proxy|path|filename|raw_data|body|payload_hash)",
    re.IGNORECASE,
)
URL_RE = re.compile(r"(?:https?|ftp|file|data|blob|javascript)://", re.IGNORECASE)
DOMAIN_LIKE_RE = re.compile(
    r"(?<![A-Za-z0-9_-])(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,63}"
    r"(?::\d{1,5})?(?:[/?:#][^\s]*)?",
    re.IGNORECASE,
)
ABSOLUTE_PATH_RE = re.compile(r"^(?:/|[A-Za-z]:[\\/]|~[/\\])")
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,80}$")


class WorkerError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def safe_text(value: Any, limit: int = MAX_STRING) -> str:
    return " ".join(str(value or "").strip().split())[:limit]


def safe_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def safe_float(value: Any) -> float:
    if isinstance(value, bool):
        return 0.0
    try:
        parsed = float(value or 0.0)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return max(0.0, parsed) if math.isfinite(parsed) else 0.0


def string_requires_redaction(value: Any) -> bool:
    candidate = str(value or "").strip()
    if not candidate:
        return False
    if (
        URL_RE.search(candidate)
        or candidate.startswith("//")
        or DOMAIN_LIKE_RE.search(candidate)
        or ABSOLUTE_PATH_RE.search(candidate)
    ):
        return True
    for token in re.split(r"[\s,;(){}]+", candidate):
        token = token.strip(".<>\"'")
        host = token.strip("[]")
        if token.startswith("[") and "]" in token:
            host = token[1 : token.index("]")]
        elif host.count(":") == 1 and "." in host:
            host = host.rsplit(":", 1)[0]
        try:
            ipaddress.ip_address(host)
            return True
        except ValueError:
            continue
    return False


def parse_request(
    *,
    optional_fields: set[str] | frozenset[str] = frozenset(),
    allow_empty_source: bool = False,
) -> dict[str, Any]:
    raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    if not raw or len(raw) > MAX_INPUT_BYTES:
        raise WorkerError("PROVIDER_INVALID_JSON")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise WorkerError("PROVIDER_INVALID_JSON") from error
    allowed_fields = REQUEST_FIELDS | set(optional_fields)
    if (
        not isinstance(payload, dict)
        or not REQUEST_FIELDS.issubset(payload)
        or not set(payload).issubset(allowed_fields)
    ):
        raise WorkerError("PROVIDER_INVALID_JSON")
    source = str(payload.get("source") or "")
    request_id = str(payload.get("request_id") or "")
    max_assets = payload.get("max_assets")
    max_output_bytes = payload.get("max_output_bytes")
    if (
        payload.get("schema_version") != SCHEMA_VERSION
        or not REQUEST_ID_RE.fullmatch(request_id)
        or (not source and not allow_empty_source)
        or source != source.strip()
        or len(source) > MAX_SOURCE_LENGTH
        or type(max_assets) is not int
        or not 1 <= max_assets <= MAX_ASSETS
        or type(max_output_bytes) is not int
        or not 1024 <= max_output_bytes <= 1024 * 1024
    ):
        raise WorkerError("PROVIDER_INVALID_JSON")
    parsed = {
        "request_id": request_id,
        "source": source,
        "max_assets": max_assets,
        "max_output_bytes": max_output_bytes,
    }
    parsed.update({key: payload[key] for key in optional_fields if key in payload})
    return parsed


def validated_url(value: Any) -> tuple[str, str]:
    candidate = str(value or "").strip()
    if not candidate or len(candidate) > 8192:
        raise WorkerError("PROVIDER_SCHEMA_INVALID")
    try:
        parsed = urlsplit(candidate)
        port = parsed.port
    except (TypeError, ValueError) as error:
        raise WorkerError("PROVIDER_SCHEMA_INVALID") from error
    hostname = (parsed.hostname or "").lower().rstrip(".")
    if (
        parsed.scheme not in {"http", "https"}
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 80, 443}
        or hostname == "localhost"
        or hostname.endswith(".localhost")
    ):
        raise WorkerError("PROVIDER_SCHEMA_INVALID")
    try:
        address = ipaddress.ip_address(hostname.strip("[]"))
    except ValueError:
        address = None
    if address and not address.is_global:
        raise WorkerError("PROVIDER_SCHEMA_INVALID")
    return candidate, hostname


def _object_fields(value: Any) -> Mapping[str, Any] | None:
    if is_dataclass(value) and not isinstance(value, type):
        return {item.name: getattr(value, item.name) for item in fields(value)}
    attributes = getattr(value, "__dict__", None)
    if isinstance(attributes, dict):
        return {key: item for key, item in attributes.items() if not str(key).startswith("_")}
    return None


def _sanitize(value: Any, *, depth: int, key_hint: str = "") -> Any:
    if depth > MAX_DEPTH:
        return {"type": type(value).__name__, "truncated": "max_depth"}
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else "non_finite_number"
    if isinstance(value, str):
        if (
            SENSITIVE_KEY_RE.search(key_hint)
            or string_requires_redaction(value)
        ):
            return "[redacted]"
        return safe_text(value)
    if isinstance(value, bytes):
        return {"type": "bytes", "length": len(value)}
    if isinstance(value, Mapping):
        output = {}
        items = list(value.items())
        for raw_key, item in items[:MAX_KEYS]:
            key = safe_text(raw_key, 120) or "field"
            output[key] = (
                "[redacted]"
                if SENSITIVE_KEY_RE.search(key)
                else _sanitize(item, depth=depth + 1, key_hint=key)
            )
        if len(items) > MAX_KEYS:
            output["_truncated_keys"] = len(items) - MAX_KEYS
        return output
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        items = list(value)
        inspected = items[:MAX_LIST_ITEMS]
        classes = []
        for item in inspected:
            class_name = type(item).__name__
            if class_name not in classes:
                classes.append(class_name)
        return {
            "type": "list",
            "count": len(items),
            "item_classes": classes[:16],
            "items": [_sanitize(item, depth=depth + 1, key_hint=key_hint) for item in inspected],
            "truncated": len(items) > MAX_LIST_ITEMS,
        }
    attributes = _object_fields(value)
    if attributes is not None:
        return {
            "result_class": type(value).__name__,
            "fields": _sanitize(attributes, depth=depth + 1),
        }
    return {"type": type(value).__name__}


def raw_shape(value: Any) -> dict[str, Any]:
    output = _sanitize(value, depth=0)
    if not isinstance(output, dict):
        output = {"type": type(value).__name__, "value": output}
    if len(json.dumps(output, ensure_ascii=True, separators=(",", ":")).encode()) > MAX_RAW_BYTES:
        return {
            "result_class": safe_text(output.get("result_class", type(value).__name__), 120),
            "truncated": "max_encoded_bytes",
            "limit_bytes": MAX_RAW_BYTES,
        }
    return output


def asset(
    *,
    asset_id: str,
    url: Any,
    media_type: str,
    role: str,
    declared_extension: Any = "",
    width: Any = 0,
    height: Any = 0,
    duration_seconds: Any = 0,
    size_bytes: Any = 0,
    quality_label: Any = "",
    codec: Any = "",
    requires_merge: bool = False,
    requires_cookie: bool = False,
    headers_present: bool = False,
    audio_asset_present: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    exact_url, hostname = validated_url(url)
    public = {
        "asset_id": safe_text(asset_id, 120),
        "media_type": media_type,
        "role": role,
        "resource_token": "",
        "host_class": hostname,
        "declared_extension": safe_text(declared_extension, 16).lower().lstrip("."),
        "width": safe_int(width),
        "height": safe_int(height),
        "duration_seconds": safe_float(duration_seconds),
        "size_bytes": safe_int(size_bytes),
        "quality_label": safe_text(quality_label, 120),
        "codec": safe_text(codec, 80),
        "requires_merge": bool(requires_merge),
        "requires_cookie": bool(requires_cookie),
        "headers_present": bool(headers_present),
        "audio_asset_present": bool(audio_asset_present),
    }
    private = {
        "asset_id": public["asset_id"],
        "url": exact_url,
        "media_type": media_type,
        "requires_cookie": bool(requires_cookie),
        "headers_present": bool(headers_present),
    }
    return public, private


def emit(result: dict[str, Any], resources: list[dict[str, Any]], *, max_output_bytes: int) -> None:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "result": result,
        "resources": resources,
    }
    encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    if len(encoded) > max_output_bytes:
        raise WorkerError("PROVIDER_OUTPUT_TOO_LARGE")
    sys.stdout.buffer.write(encoded + b"\n")


def emit_message(payload: dict[str, Any], *, max_output_bytes: int) -> None:
    message = {"schema_version": SCHEMA_VERSION, **payload}
    encoded = json.dumps(message, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    if len(encoded) > max_output_bytes:
        raise WorkerError("PROVIDER_OUTPUT_TOO_LARGE")
    sys.stdout.buffer.write(encoded + b"\n")


def emit_check(provider_id: str, provider_version: str) -> None:
    sys.stdout.write(
        json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "status": "ok",
                "provider_id": provider_id,
                "provider_version": provider_version,
            },
            ensure_ascii=True,
            separators=(",", ":"),
        )
        + "\n"
    )


def emit_error(code: str) -> None:
    sys.stdout.write(
        json.dumps(
            {"schema_version": SCHEMA_VERSION, "error": {"code": code}},
            ensure_ascii=True,
            separators=(",", ":"),
        )
        + "\n"
    )
