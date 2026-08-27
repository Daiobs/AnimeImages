"""Opaque, cache-backed control-plane handoff for one XHS LivePhoto pair."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from django.core import signing
from django.core.cache import cache
from django.middleware.csrf import get_token

from .assets import MediaAsset
from .common import clean_filename
from .provider_manifest import provider_manifest_is_valid
from .rate_limits import get_client_identity
from .platforms.xhs_livephoto_browser_admission import (
    XhsLivePhotoBrowserAdmissionError,
    validate_xhs_livephoto_browser_admitted_url,
)
from .platforms.xhs_parsehub_images import (
    PARSEHUB_XHS_PROVIDER_ID,
    PARSEHUB_XHS_IMAGE_EXTENSIONS,
    XhsParseHubImageError,
    validate_xhs_parsehub_raw_url,
)


XHS_LIVEPHOTO_PAIR_TOKEN_SALT = "xhs_download.xhs_livephoto_pair.v1"
XHS_LIVEPHOTO_PAIR_TTL_SECONDS = 120
XHS_LIVEPHOTO_PAIR_MAX_USES = 16
XHS_LIVEPHOTO_PAIR_MAX_CACHE_BYTES = 48 * 1024
XHS_LIVEPHOTO_PAIR_STILL_MAX_BYTES = 50 * 1024 * 1024
XHS_LIVEPHOTO_PAIR_MOTION_MAX_BYTES = 1024 * 1024 * 1024
XHS_LIVEPHOTO_PAIR_TOTAL_MAX_BYTES = 128 * 1024 * 1024

_TICKET_RE = re.compile(r"^[0-9a-f]{32}$")
_BINDING_RE = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_IDENTITY_RE = re.compile(r"^[0-9a-f]{64}$")
_GROUP_RE = re.compile(r"^xhs:([0-9a-f]{24}):i([0-9]{1,2}):live$")
_ASSET_RE = re.compile(r"^xhs:([0-9a-f]{24}):i([0-9]{1,2}):(still|motion)$")
_NATIVE_STILL_EXTENSIONS = frozenset({"jpg", "webp"})
_LIVEPHOTO_PROVIDER = "xhs_livephoto_preview_v1"


class XhsLivePhotoPairHandoffError(ValueError):
    def __init__(self, message: str, *, error_code: str, status_code: int):
        self.error_code = str(error_code or "XHS_LIVEPHOTO_PAIR_INVALID")[:64]
        self.status_code = int(status_code or 400)
        super().__init__(message)


@dataclass(frozen=True)
class XhsLivePhotoPairToken:
    token: str
    expires_in_seconds: int = XHS_LIVEPHOTO_PAIR_TTL_SECONDS


@dataclass(frozen=True)
class XhsLivePhotoPairDescriptor:
    still: dict
    motion: dict
    archive_filename: str
    expires_in_seconds: int

    def as_public_dict(self) -> dict:
        return {
            "ok": True,
            "still": dict(self.still),
            "motion": dict(self.motion),
            "archive_filename": self.archive_filename,
            "expires_in_seconds": self.expires_in_seconds,
        }


def _error(message="实况资源信息无效，请重新解析。", *, code="XHS_LIVEPHOTO_PAIR_INVALID", status=400):
    return XhsLivePhotoPairHandoffError(
        message,
        error_code=code,
        status_code=status,
    )


def _cache_key(ticket_id: str) -> str:
    return f"xhs-livephoto-pair:v1:{ticket_id}"


def _use_key(ticket_id: str) -> str:
    return f"xhs-livephoto-pair:v1:{ticket_id}:uses"


def _record_binding(record: dict) -> str:
    encoded = json.dumps(
        record,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _safe_base_filename(value: str) -> str:
    cleaned = clean_filename(str(value or "livephoto"))
    stem = Path(cleaned).stem.strip(". ")[:120]
    return stem or "livephoto"


def get_xhs_livephoto_pair_client_identity(request) -> str:
    """Bind one handoff to a browser CSRF secret plus the shared network identity."""

    get_token(request)
    csrf_secret = str(request.META.get("CSRF_COOKIE") or "")
    network_identity = get_client_identity(request)
    if not csrf_secret:
        raise _error(code="XHS_LIVEPHOTO_PAIR_CLIENT_MISMATCH", status=403)
    return hashlib.sha256(
        f"{network_identity}:{csrf_secret}".encode("utf-8")
    ).hexdigest()


def _asset_record(asset: MediaAsset, *, expected_type: str) -> dict:
    if type(asset) is not MediaAsset or asset.media_type != expected_type:
        raise _error()
    item = asset.to_download_item()
    item["filename"] = str(asset.filename or "")[:200]
    item["extension"] = str(asset.ext or "").lower().lstrip(".")[:12]
    item["expected_size_bytes"] = max(0, int(asset.size_bytes or 0))
    if (
        str(item.get("source_platform") or "") != "xiaohongshu"
        or not provider_manifest_is_valid(item)
    ):
        raise _error(code="XHS_LIVEPHOTO_PAIR_MANIFEST_REJECTED")
    return item


def _validate_relation(record: dict) -> None:
    try:
        ordinal = int(record.get("source_item_ordinal"))
    except (TypeError, ValueError) as error:
        raise _error() from error
    group_match = _GROUP_RE.fullmatch(str(record.get("group_id") or ""))
    still_match = _ASSET_RE.fullmatch(str((record.get("still") or {}).get("asset_id") or ""))
    motion_match = _ASSET_RE.fullmatch(str((record.get("motion") or {}).get("asset_id") or ""))
    if (
        not 0 <= ordinal < 64
        or group_match is None
        or still_match is None
        or motion_match is None
        or group_match.group(1) != still_match.group(1)
        or group_match.group(1) != motion_match.group(1)
        or int(group_match.group(2)) != ordinal
        or int(still_match.group(2)) != ordinal
        or int(motion_match.group(2)) != ordinal
        or still_match.group(3) != "still"
        or motion_match.group(3) != "motion"
    ):
        raise _error(code="XHS_LIVEPHOTO_PAIR_RELATION_REJECTED")


def _validate_still(item: dict) -> str:
    if (
        item.get("type") != "image"
        or item.get("provider") not in {_LIVEPHOTO_PROVIDER, PARSEHUB_XHS_PROVIDER_ID}
        or not provider_manifest_is_valid(item)
    ):
        raise _error(code="XHS_LIVEPHOTO_PAIR_STILL_REJECTED")
    url = str(item.get("url") or "")
    if item.get("provider") == PARSEHUB_XHS_PROVIDER_ID:
        try:
            validate_xhs_parsehub_raw_url(url)
        except XhsParseHubImageError as error:
            raise _error(code="XHS_LIVEPHOTO_PAIR_STILL_REJECTED") from error
        allowed_extensions = PARSEHUB_XHS_IMAGE_EXTENSIONS
    else:
        try:
            host_class = validate_xhs_livephoto_browser_admitted_url(
                url,
                member_role="still",
            )
        except XhsLivePhotoBrowserAdmissionError as error:
            raise _error(code="XHS_LIVEPHOTO_PAIR_STILL_REJECTED") from error
        if host_class != "still_exact":
            raise _error(code="XHS_LIVEPHOTO_PAIR_STILL_REJECTED")
        allowed_extensions = _NATIVE_STILL_EXTENSIONS
    extension = str(item.get("extension") or "").lower()
    if extension == "jpeg":
        extension = "jpg"
    if extension not in allowed_extensions:
        raise _error(code="XHS_LIVEPHOTO_PAIR_STILL_REJECTED")
    return extension


def _validate_motion(item: dict) -> str:
    if (
        item.get("type") != "video"
        or item.get("provider") != _LIVEPHOTO_PROVIDER
        or item.get("extension") != "mp4"
        or not provider_manifest_is_valid(item)
    ):
        raise _error(code="XHS_LIVEPHOTO_PAIR_MOTION_REJECTED")
    try:
        host_class = validate_xhs_livephoto_browser_admitted_url(
            str(item.get("url") or ""),
            member_role="motion",
        )
    except XhsLivePhotoBrowserAdmissionError as error:
        raise _error(code="XHS_LIVEPHOTO_PAIR_MOTION_REJECTED") from error
    if host_class not in {"motion_versioned", "motion_backup"}:
        raise _error(code="XHS_LIVEPHOTO_PAIR_MOTION_REJECTED")
    return "mp4"


def _validated_record(record: dict) -> tuple[str, str]:
    if not isinstance(record, dict):
        raise _error()
    if not _SOURCE_IDENTITY_RE.fullmatch(str(record.get("source_identity") or "")):
        raise _error(code="XHS_LIVEPHOTO_PAIR_SOURCE_REJECTED")
    _validate_relation(record)
    still = record.get("still")
    motion = record.get("motion")
    if not isinstance(still, dict) or not isinstance(motion, dict):
        raise _error()
    still_extension = _validate_still(still)
    motion_extension = _validate_motion(motion)
    for item, maximum in (
        (still, XHS_LIVEPHOTO_PAIR_STILL_MAX_BYTES),
        (motion, XHS_LIVEPHOTO_PAIR_MOTION_MAX_BYTES),
    ):
        value = item.get("expected_size_bytes", 0)
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
            raise _error(code="XHS_LIVEPHOTO_PAIR_SIZE_REJECTED")
    known_total = int(still.get("expected_size_bytes") or 0) + int(
        motion.get("expected_size_bytes") or 0
    )
    if known_total and known_total > XHS_LIVEPHOTO_PAIR_TOTAL_MAX_BYTES:
        raise _error(
            "该实况素材过大，暂无法在浏览器中打包。",
            code="XHS_LIVEPHOTO_PAIR_TOO_LARGE",
            status=413,
        )
    return still_extension, motion_extension


def issue_xhs_livephoto_pair_token(
    *,
    source_identity: str,
    group_id: str,
    source_item_ordinal: int,
    still_asset: MediaAsset,
    motion_asset: MediaAsset,
    base_filename: str,
    client_identity: str,
) -> XhsLivePhotoPairToken:
    source_identity = str(source_identity or "")
    client_identity = str(client_identity or "")
    if not _SOURCE_IDENTITY_RE.fullmatch(source_identity) or not client_identity:
        raise _error(code="XHS_LIVEPHOTO_PAIR_SOURCE_REJECTED")
    issued_at = time.time()
    record = {
        "source_identity": source_identity,
        "group_id": str(group_id or ""),
        "source_item_ordinal": source_item_ordinal,
        "still": _asset_record(still_asset, expected_type="image"),
        "motion": _asset_record(motion_asset, expected_type="video"),
        "base_filename": _safe_base_filename(base_filename),
        "client_identity": client_identity[:256],
        "expires_at": issued_at + XHS_LIVEPHOTO_PAIR_TTL_SECONDS,
    }
    _validated_record(record)
    encoded = json.dumps(record, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("utf-8")
    if len(encoded) > XHS_LIVEPHOTO_PAIR_MAX_CACHE_BYTES:
        raise _error(code="XHS_LIVEPHOTO_PAIR_RECORD_TOO_LARGE")
    ticket_id = uuid4().hex
    binding = _record_binding(record)
    cache.set(_cache_key(ticket_id), record, timeout=XHS_LIVEPHOTO_PAIR_TTL_SECONDS)
    token = signing.dumps(
        {"ticket_id": ticket_id, "binding": binding},
        salt=XHS_LIVEPHOTO_PAIR_TOKEN_SALT,
        compress=False,
    )
    return XhsLivePhotoPairToken(token=token)


def load_xhs_livephoto_pair_descriptor(
    token: str,
    *,
    client_identity: str,
) -> XhsLivePhotoPairDescriptor:
    try:
        payload = signing.loads(
            str(token or ""),
            salt=XHS_LIVEPHOTO_PAIR_TOKEN_SALT,
            max_age=XHS_LIVEPHOTO_PAIR_TTL_SECONDS,
        )
    except signing.SignatureExpired as error:
        raise _error(
            "资源已失效，请重新解析。",
            code="XHS_LIVEPHOTO_PAIR_EXPIRED",
            status=410,
        ) from error
    except signing.BadSignature as error:
        raise _error() from error
    if not isinstance(payload, dict):
        raise _error()
    ticket_id = str(payload.get("ticket_id") or "")
    binding = str(payload.get("binding") or "")
    if not _TICKET_RE.fullmatch(ticket_id) or not _BINDING_RE.fullmatch(binding):
        raise _error()
    record = cache.get(_cache_key(ticket_id))
    if not isinstance(record, dict):
        raise _error(
            "资源已失效，请重新解析。",
            code="XHS_LIVEPHOTO_PAIR_EXPIRED",
            status=410,
        )
    if not hmac.compare_digest(binding, _record_binding(record)):
        raise _error(code="XHS_LIVEPHOTO_PAIR_BINDING_REJECTED")
    current_identity = str(client_identity or "")
    if not current_identity or not hmac.compare_digest(
        str(record.get("client_identity") or ""),
        current_identity,
    ):
        raise _error(
            "实况资源与当前客户端不匹配，请重新解析。",
            code="XHS_LIVEPHOTO_PAIR_CLIENT_MISMATCH",
            status=403,
        )
    remaining = max(0, int(float(record.get("expires_at") or 0) - time.time()))
    if not remaining:
        raise _error(
            "资源已失效，请重新解析。",
            code="XHS_LIVEPHOTO_PAIR_EXPIRED",
            status=410,
        )
    cache.add(_use_key(ticket_id), 0, timeout=remaining)
    try:
        uses = cache.incr(_use_key(ticket_id))
    except ValueError:
        cache.set(_use_key(ticket_id), 1, timeout=remaining)
        uses = 1
    if uses > XHS_LIVEPHOTO_PAIR_MAX_USES:
        raise _error(
            "实况资源请求次数过多，请重新解析。",
            code="XHS_LIVEPHOTO_PAIR_USE_LIMIT",
            status=429,
        )
    still_extension, motion_extension = _validated_record(record)
    ordinal = int(record["source_item_ordinal"]) + 1
    base = _safe_base_filename(record.get("base_filename"))
    prefix = f"{base}_{ordinal:02d}"
    still = record["still"]
    motion = record["motion"]
    return XhsLivePhotoPairDescriptor(
        still={
            "url": still["url"],
            "filename": f"{prefix}_still.{still_extension}",
            "media_type": "image",
            "format_class": still_extension,
            "expected_size_bytes": int(still.get("expected_size_bytes") or 0),
        },
        motion={
            "url": motion["url"],
            "filename": f"{prefix}_motion.{motion_extension}",
            "media_type": "video",
            "format_class": motion_extension,
            "expected_size_bytes": int(motion.get("expected_size_bytes") or 0),
        },
        archive_filename=f"{prefix}_livephoto.zip",
        expires_in_seconds=remaining,
    )


__all__ = (
    "XHS_LIVEPHOTO_PAIR_MAX_USES",
    "XHS_LIVEPHOTO_PAIR_MOTION_MAX_BYTES",
    "XHS_LIVEPHOTO_PAIR_STILL_MAX_BYTES",
    "XHS_LIVEPHOTO_PAIR_TOTAL_MAX_BYTES",
    "XHS_LIVEPHOTO_PAIR_TTL_SECONDS",
    "XhsLivePhotoPairDescriptor",
    "XhsLivePhotoPairHandoffError",
    "XhsLivePhotoPairToken",
    "get_xhs_livephoto_pair_client_identity",
    "issue_xhs_livephoto_pair_token",
    "load_xhs_livephoto_pair_descriptor",
)
