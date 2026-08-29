from __future__ import annotations

from dataclasses import dataclass, field, fields, is_dataclass
import ipaddress
import json
import math
import re
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit


SCHEMA_VERSION = "1.0"
MAX_SOURCE_LENGTH = 4096
MAX_ASSETS = 64
MAX_RELATIONSHIPS = 64
MAX_RAW_DEPTH = 6
MAX_RAW_MAPPING_KEYS = 128
MAX_RAW_LIST_ITEMS = 64
MAX_SAFE_STRING_LENGTH = 512
MAX_RAW_SHAPE_BYTES = 256 * 1024

RESULT_STATUSES = frozenset(
    {"success", "unsupported", "not_configured", "timeout", "failed"}
)
RESULT_KINDS = frozenset(
    {"video", "image", "gallery", "livephoto", "multimedia", "article", "unknown"}
)
MEDIA_TYPES = frozenset({"image", "video", "audio", "unknown"})
ASSET_ROLES = frozenset(
    {"primary", "cover", "gallery", "still", "motion", "audio", "unknown"}
)
ERROR_CODES = frozenset(
    {
        "PROVIDER_LAB_DISABLED",
        "PROVIDER_NOT_SELECTED",
        "PROVIDER_NOT_CONFIGURED",
        "PROVIDER_UNSUPPORTED",
        "PROVIDER_TIMEOUT",
        "PROVIDER_OUTPUT_TOO_LARGE",
        "PROVIDER_INVALID_JSON",
        "PROVIDER_SCHEMA_INVALID",
        "PROVIDER_PROCESS_FAILED",
        "PROVIDER_RESULT_EMPTY",
        "PROVIDER_LICENSE_RESTRICTED",
        "PROVIDER_RATE_LIMITED",
    }
)

SENSITIVE_KEY_RE = re.compile(
    r"(?:url|uri|href|src|query|cookie|authorization|token|secret|password|passwd|"
    r"header|proxy|path|filename|file_path|raw_data|body|payload_hash)",
    re.IGNORECASE,
)
URL_RE = re.compile(r"(?:https?|ftp|file|data|blob|javascript)://", re.IGNORECASE)
DOMAIN_LIKE_RE = re.compile(
    r"(?<![A-Za-z0-9_-])(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,63}"
    r"(?::\d{1,5})?(?:[/?:#][^\s]*)?",
    re.IGNORECASE,
)
ABSOLUTE_PATH_RE = re.compile(r"^(?:/|[A-Za-z]:[\\/]|~[/\\])")
SAFE_FIELD_RE = re.compile(r"[^A-Za-z0-9_.:-]+")


class ProviderLabError(RuntimeError):
    def __init__(self, code: str, *, http_status: int = 400):
        self.code = code if code in ERROR_CODES else "PROVIDER_PROCESS_FAILED"
        self.http_status = int(http_status)
        super().__init__(self.code)


def safe_text(value: Any, limit: int = MAX_SAFE_STRING_LENGTH) -> str:
    text = str(value or "").strip()
    text = " ".join(text.split())
    return text[: max(0, int(limit))]


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


def safe_field_name(value: Any) -> str:
    return SAFE_FIELD_RE.sub("_", safe_text(value, 120))[:120] or "field"


def string_requires_redaction(value: str) -> bool:
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


def safe_public_text(value: Any, limit: int = MAX_SAFE_STRING_LENGTH) -> str:
    text = safe_text(value, limit)
    return "[redacted]" if string_requires_redaction(text) else text


def _object_fields(value: Any) -> Mapping[str, Any] | None:
    if is_dataclass(value) and not isinstance(value, type):
        return {item.name: getattr(value, item.name) for item in fields(value)}
    attributes = getattr(value, "__dict__", None)
    if isinstance(attributes, dict):
        return {key: item for key, item in attributes.items() if not str(key).startswith("_")}
    return None


def _sanitize_raw(value: Any, *, depth: int, key_hint: str = "") -> Any:
    if depth > MAX_RAW_DEPTH:
        return {"type": type(value).__name__, "truncated": "max_depth"}
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else "non_finite_number"
    if isinstance(value, str):
        if SENSITIVE_KEY_RE.search(key_hint) or string_requires_redaction(value):
            return "[redacted]"
        return safe_text(value)
    if isinstance(value, bytes):
        return {"type": "bytes", "length": len(value)}
    if isinstance(value, Mapping):
        output: dict[str, Any] = {}
        items = list(value.items())
        for raw_key, item in items[:MAX_RAW_MAPPING_KEYS]:
            key = safe_field_name(raw_key)
            if SENSITIVE_KEY_RE.search(key):
                output[key] = "[redacted]"
            else:
                output[key] = _sanitize_raw(item, depth=depth + 1, key_hint=key)
        if len(items) > MAX_RAW_MAPPING_KEYS:
            output["_truncated_keys"] = len(items) - MAX_RAW_MAPPING_KEYS
        return output
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        items = list(value)
        inspected = items[:MAX_RAW_LIST_ITEMS]
        classes = []
        for item in inspected:
            class_name = type(item).__name__
            if class_name not in classes:
                classes.append(class_name)
        return {
            "type": "list",
            "count": len(items),
            "item_classes": classes[:16],
            "items": [
                _sanitize_raw(item, depth=depth + 1, key_hint=key_hint)
                for item in inspected
            ],
            "truncated": len(items) > MAX_RAW_LIST_ITEMS,
        }
    attributes = _object_fields(value)
    if attributes is not None:
        return {
            "result_class": type(value).__name__,
            "fields": _sanitize_raw(attributes, depth=depth + 1),
        }
    return {"type": type(value).__name__}


def sanitize_raw_shape(value: Any) -> dict[str, Any]:
    sanitized = _sanitize_raw(value, depth=0)
    if not isinstance(sanitized, dict):
        sanitized = {"type": type(value).__name__, "value": sanitized}
    encoded = json.dumps(sanitized, ensure_ascii=True, separators=(",", ":")).encode()
    if len(encoded) <= MAX_RAW_SHAPE_BYTES:
        return sanitized
    return {
        "result_class": safe_text(sanitized.get("result_class", type(value).__name__), 120),
        "truncated": "max_encoded_bytes",
        "limit_bytes": MAX_RAW_SHAPE_BYTES,
    }


@dataclass(frozen=True)
class LabAssetV1:
    asset_id: str
    media_type: str = "unknown"
    role: str = "unknown"
    resource_token: str = ""
    host_class: str = ""
    declared_extension: str = ""
    width: int = 0
    height: int = 0
    duration_seconds: float = 0.0
    size_bytes: int = 0
    quality_label: str = ""
    codec: str = ""
    requires_merge: bool = False
    requires_cookie: bool = False
    headers_present: bool = False
    audio_asset_present: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "asset_id": safe_public_text(self.asset_id, 120),
            "media_type": self.media_type if self.media_type in MEDIA_TYPES else "unknown",
            "role": self.role if self.role in ASSET_ROLES else "unknown",
            "resource_token": safe_text(self.resource_token, 160),
            "host_class": safe_text(self.host_class, 253),
            "declared_extension": safe_public_text(self.declared_extension, 16).lower().lstrip("."),
            "width": safe_int(self.width),
            "height": safe_int(self.height),
            "duration_seconds": safe_float(self.duration_seconds),
            "size_bytes": safe_int(self.size_bytes),
            "quality_label": safe_public_text(self.quality_label, 120),
            "codec": safe_public_text(self.codec, 80),
            "requires_merge": bool(self.requires_merge),
            "requires_cookie": bool(self.requires_cookie),
            "headers_present": bool(self.headers_present),
            "audio_asset_present": bool(self.audio_asset_present),
        }


@dataclass(frozen=True)
class LabRelationshipV1:
    relationship_type: str
    source_asset_id: str
    target_asset_id: str

    def as_dict(self) -> dict[str, str]:
        return {
            "relationship_type": safe_public_text(self.relationship_type, 80),
            "source_asset_id": safe_public_text(self.source_asset_id, 120),
            "target_asset_id": safe_public_text(self.target_asset_id, 120),
        }


@dataclass(frozen=True)
class LabProviderResultV1:
    request_id: str
    provider_id: str
    provider_version: str
    provider_scope: str
    status: str
    elapsed_ms: int = 0
    claimed_platform: str = ""
    selected_backend: str = ""
    result_kind: str = "unknown"
    metadata: Mapping[str, Any] = field(default_factory=dict)
    assets: Sequence[LabAssetV1] = field(default_factory=tuple)
    relationships: Sequence[LabRelationshipV1] = field(default_factory=tuple)
    warnings: Sequence[str] = field(default_factory=tuple)
    error_code: str = ""
    raw_shape: Mapping[str, Any] = field(default_factory=dict)
    license_name: str = ""
    license_scope: str = ""

    def as_dict(self) -> dict[str, Any]:
        status = self.status if self.status in RESULT_STATUSES else "failed"
        error_code = self.error_code if self.error_code in ERROR_CODES else ""
        metadata = {
            "title": safe_public_text(self.metadata.get("title"), 300),
            "author": safe_public_text(self.metadata.get("author"), 200),
            "content_summary": safe_public_text(self.metadata.get("content_summary"), 512),
        }
        return {
            "schema_version": SCHEMA_VERSION,
            "request_id": safe_text(self.request_id, 80),
            "provider_id": safe_text(self.provider_id, 80),
            "provider_version": safe_text(self.provider_version, 160),
            "provider_scope": safe_text(self.provider_scope, 80),
            "status": status,
            "elapsed_ms": safe_int(self.elapsed_ms),
            "claimed_platform": safe_public_text(self.claimed_platform, 80),
            "selected_backend": safe_public_text(self.selected_backend, 160),
            "result_kind": self.result_kind if self.result_kind in RESULT_KINDS else "unknown",
            "metadata": metadata,
            "assets": [asset.as_dict() for asset in tuple(self.assets)[:MAX_ASSETS]],
            "relationships": [
                relationship.as_dict()
                for relationship in tuple(self.relationships)[:MAX_RELATIONSHIPS]
            ],
            "warnings": [safe_public_text(item, 240) for item in tuple(self.warnings)[:32]],
            "error_code": error_code,
            "raw_shape": sanitize_raw_shape(dict(self.raw_shape or {})),
            "license_name": safe_text(self.license_name, 120),
            "license_scope": safe_text(self.license_scope, 120),
        }


def validated_resource_url(value: Any) -> tuple[str, str]:
    candidate = str(value or "").strip()
    if not candidate or len(candidate) > 8192:
        raise ProviderLabError("PROVIDER_SCHEMA_INVALID")
    try:
        parsed = urlsplit(candidate)
        port = parsed.port
    except (TypeError, ValueError) as error:
        raise ProviderLabError("PROVIDER_SCHEMA_INVALID") from error
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
        raise ProviderLabError("PROVIDER_SCHEMA_INVALID")
    try:
        address = ipaddress.ip_address(hostname.strip("[]"))
    except ValueError:
        address = None
    if address and not address.is_global:
        raise ProviderLabError("PROVIDER_SCHEMA_INVALID")
    return candidate, hostname
