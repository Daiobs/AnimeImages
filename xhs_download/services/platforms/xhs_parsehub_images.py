"""Fail-closed ParseHub raw-image candidates for exact Native XHS notes."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from hashlib import sha256
import json
from pathlib import Path
import re
import secrets
import subprocess
import time
from typing import Any, Callable
from urllib.parse import urljoin, urlsplit

from django.core.cache import cache
import requests

from ..assets import MediaAsset, ParseResult
from ..common import HEADERS, clean_filename
from ..env_config import runtime_bool, runtime_float, runtime_int, runtime_str
from ..image_formats import resolve_image_format
from ..provider_manifest import build_provider_manifest_token
from .external_tools import (
    ExternalCommandOutputTooLarge,
    cache_external_failure,
    cache_external_payload,
    clear_cached_external_failure,
    get_cached_external_failure,
    get_cached_external_payload,
    run_external_command,
)
from .xhs_page_state import XhsPrivateSelectedNoteSnapshotV1, normalize_xhs_note_id


PARSEHUB_XHS_PROVIDER_ID = "parsehub_xhs_images"
PARSEHUB_PIN = "144260f4349c5bb57415f7555a572fcd48c3b8ba"
PARSEHUB_PROVIDER_VERSION = f"parsehub@{PARSEHUB_PIN}"
PARSEHUB_SCHEMA_VERSION = "1.0"
PARSEHUB_RAW_IMAGE_HOST = "sns-img-hw.xhscdn.com"
PARSEHUB_MAX_ITEMS = 64
PARSEHUB_MAX_URL_LENGTH = 4096
PARSEHUB_MAX_OUTPUT_BYTES = 512 * 1024
PARSEHUB_MAX_STDERR_BYTES = 128 * 1024
PARSEHUB_MAX_PROBE_BYTES = 4096
PARSEHUB_MAX_REDIRECTS = 2
PARSEHUB_MAX_PROBE_WORKERS = 6
PARSEHUB_DEFAULT_TIMEOUT_SECONDS = 6.0
PARSEHUB_MAX_TIMEOUT_SECONDS = 8.0
PARSEHUB_DEFAULT_PROBE_TIMEOUT_SECONDS = 3.0
PARSEHUB_SUCCESS_TTL_SECONDS = 120
PARSEHUB_FAILURE_TTL_SECONDS = 8
PARSEHUB_SINGLE_FLIGHT_TTL_SECONDS = 10
PARSEHUB_SINGLE_FLIGHT_POLL_SECONDS = 0.05

_RESOURCE_KEY_RE = re.compile(r"^[A-Za-z0-9._/-]{1,512}$")
_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")
_OPAQUE_ID_RE = re.compile(r"^[0-9a-f]{24}$")
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_GENERIC_BINARY_TYPES = frozenset(
    {
        "",
        "application/binary",
        "application/octet-stream",
        "binary/octet-stream",
    }
)
_WORKER_ERROR_CODES = frozenset(
    {
        "PARSEHUB_NOT_AVAILABLE",
        "PARSEHUB_TIMEOUT",
        "PARSEHUB_INVALID_JSON",
        "PARSEHUB_OUTPUT_TOO_LARGE",
        "PARSEHUB_IDENTITY_MISMATCH",
        "PARSEHUB_UNSUPPORTED_POST",
        "PARSEHUB_RESULT_INVALID",
    }
)
_ROOT = Path(__file__).resolve().parents[3]
_WORKER_PATH = _ROOT / "tools" / "parsehub_provider" / "parsehub_xhs_image_worker.py"


class XhsParseHubImageError(ValueError):
    def __init__(self, code: str):
        self.code = code if code in _WORKER_ERROR_CODES else "PARSEHUB_RESULT_INVALID"
        super().__init__(self.code)


@dataclass(frozen=True)
class XhsParseHubCandidateV1:
    source_ordinal: int
    source_kind: str
    source_fingerprint: str
    raw_https_url: str = field(repr=False, compare=False)

    def __post_init__(self):
        if (
            type(self.source_ordinal) is not int
            or not 0 <= self.source_ordinal < PARSEHUB_MAX_ITEMS
            or self.source_kind not in {"regular_image", "livephoto_still"}
            or not _FINGERPRINT_RE.fullmatch(self.source_fingerprint)
        ):
            raise XhsParseHubImageError("PARSEHUB_RESULT_INVALID")
        validate_xhs_parsehub_raw_url(self.raw_https_url)


@dataclass(frozen=True)
class XhsParseHubProbeV1:
    final_url: str = field(repr=False, compare=False)
    extension: str
    mime_type: str
    size_bytes: int
    width: int
    height: int
    bytes_read: int

    def __post_init__(self):
        validate_xhs_parsehub_raw_url(self.final_url)
        if (
            self.extension not in {"jpg", "png", "gif", "webp", "avif", "heic"}
            or not self.mime_type.startswith("image/")
            or type(self.size_bytes) is not int
            or self.size_bytes < 0
            or type(self.width) is not int
            or type(self.height) is not int
            or self.width <= 0
            or self.height <= 0
            or type(self.bytes_read) is not int
            or not 0 < self.bytes_read <= PARSEHUB_MAX_PROBE_BYTES
        ):
            raise XhsParseHubImageError("PARSEHUB_RESULT_INVALID")


@dataclass(frozen=True)
class XhsParseHubEnhancementOutcome:
    result: ParseResult
    worker_status: str
    selected_count: int
    fallback_count: int
    bounded_probe_bytes: int


def parsehub_provider_enabled() -> bool:
    return bool(
        runtime_bool("PARSEHUB_PROVIDER_ENABLED", False)
        and runtime_str("PARSEHUB_PROVIDER_PYTHON")
    )


def parsehub_provider_timeout_seconds() -> float:
    return min(
        PARSEHUB_MAX_TIMEOUT_SECONDS,
        max(
            0.1,
            runtime_float(
                "PARSEHUB_PROVIDER_TIMEOUT_SECONDS",
                PARSEHUB_DEFAULT_TIMEOUT_SECONDS,
            ),
        ),
    )


def _resource_key(value: Any) -> str:
    source = str(value or "").strip()
    if not source or len(source) > PARSEHUB_MAX_URL_LENGTH:
        raise XhsParseHubImageError("PARSEHUB_RESULT_INVALID")
    without_suffix = source.split("!", 1)[0].split("?", 1)[0]
    match = re.search(r"/[a-f0-9]{32}/(.*)/([^/!]+)$", without_suffix)
    if match:
        key = f"{match.group(1)}/{match.group(2)}"
    else:
        path = urlsplit(without_suffix).path
        key = path.rsplit("/", 1)[-1]
    key = str(key or "").strip().lstrip("/")
    if (
        not key
        or not _RESOURCE_KEY_RE.fullmatch(key)
        or ".." in key
        or "\\" in key
        or "://" in key
        or key.startswith(".")
    ):
        raise XhsParseHubImageError("PARSEHUB_RESULT_INVALID")
    return key


def xhs_image_source_fingerprint(item: Any) -> str:
    if not isinstance(item, dict) and not hasattr(item, "get"):
        raise XhsParseHubImageError("PARSEHUB_RESULT_INVALID")
    return sha256(_resource_key(item.get("urlDefault")).encode("utf-8")).hexdigest()


def xhs_image_source_kind(item: Any) -> str:
    return "livephoto_still" if item.get("livePhoto") is True else "regular_image"


def validate_xhs_parsehub_raw_url(value: Any) -> str:
    url = str(value or "")
    if (
        not url
        or url != url.strip()
        or len(url) > PARSEHUB_MAX_URL_LENGTH
        or any(ord(character) <= 32 or ord(character) == 127 for character in url)
    ):
        raise XhsParseHubImageError("PARSEHUB_RESULT_INVALID")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except (TypeError, ValueError) as error:
        raise XhsParseHubImageError("PARSEHUB_RESULT_INVALID") from error
    hostname = (parsed.hostname or "").lower()
    if (
        parsed.scheme != "https"
        or hostname != PARSEHUB_RAW_IMAGE_HOST
        or hostname.endswith(".")
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or not parsed.path.strip("/")
        or "\\" in parsed.path
        or ".." in parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise XhsParseHubImageError("PARSEHUB_RESULT_INVALID")
    _resource_key(url)
    return url


def _opaque_identity(note_id: str) -> str:
    return sha256(f"xiaohongshu:{note_id}".encode("utf-8")).hexdigest()[:24]


def _source_cache_key(note_id: str) -> str:
    return _opaque_identity(note_id)


def _worker_lock_key(source_key: str) -> str:
    return f"third-party:{PARSEHUB_XHS_PROVIDER_ID}:single-flight:{source_key}"


def _remaining(deadline_monotonic: float, default: float) -> float:
    if not deadline_monotonic:
        return default
    remaining = float(deadline_monotonic) - time.monotonic()
    if remaining <= 0:
        raise XhsParseHubImageError("PARSEHUB_TIMEOUT")
    return min(default, remaining)


def _worker_request(source_url: str, note_id: str) -> dict[str, Any]:
    source_url = str(source_url or "").strip()
    try:
        parsed = urlsplit(source_url)
        port = parsed.port
    except (TypeError, ValueError) as error:
        raise XhsParseHubImageError("PARSEHUB_RESULT_INVALID") from error
    hostname = (parsed.hostname or "").lower()
    if (
        parsed.scheme != "https"
        or len(source_url) > PARSEHUB_MAX_URL_LENGTH
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
        raise XhsParseHubImageError("PARSEHUB_RESULT_INVALID")
    return {
        "schema_version": PARSEHUB_SCHEMA_VERSION,
        "request_id": secrets.token_hex(12),
        "source_url": source_url,
        "expected_note_id": note_id,
        "capability": "xhs_images",
        "max_items": PARSEHUB_MAX_ITEMS,
    }


def _parse_worker_payload(value: str, *, note_id: str) -> dict[str, Any]:
    try:
        payload = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise XhsParseHubImageError("PARSEHUB_INVALID_JSON") from error
    if not isinstance(payload, dict):
        raise XhsParseHubImageError("PARSEHUB_INVALID_JSON")
    error_payload = payload.get("error")
    if isinstance(error_payload, dict):
        raise XhsParseHubImageError(str(error_payload.get("code") or ""))
    expected_fields = {
        "schema_version",
        "provider",
        "provider_version",
        "claimed_platform",
        "claimed_note_id",
        "post_type",
        "items",
    }
    if (
        set(payload) != expected_fields
        or payload.get("schema_version") != PARSEHUB_SCHEMA_VERSION
        or payload.get("provider") != PARSEHUB_XHS_PROVIDER_ID
        or payload.get("provider_version") != PARSEHUB_PROVIDER_VERSION
        or payload.get("claimed_platform") != "xiaohongshu"
        or normalize_xhs_note_id(payload.get("claimed_note_id")) != note_id
        or payload.get("post_type") != "image"
        or not isinstance(payload.get("items"), list)
        or not 1 <= len(payload["items"]) <= PARSEHUB_MAX_ITEMS
    ):
        raise XhsParseHubImageError("PARSEHUB_RESULT_INVALID")
    return payload


def _run_worker(source_url: str, note_id: str, *, deadline_monotonic: float) -> dict[str, Any]:
    python = runtime_str("PARSEHUB_PROVIDER_PYTHON")
    if not python or not _WORKER_PATH.is_file():
        raise XhsParseHubImageError("PARSEHUB_NOT_AVAILABLE")
    request_payload = _worker_request(source_url, note_id)
    timeout_seconds = _remaining(
        deadline_monotonic,
        parsehub_provider_timeout_seconds(),
    )
    try:
        completed = run_external_command(
            [python, str(_WORKER_PATH)],
            timeout_seconds=timeout_seconds,
            input_text=json.dumps(request_payload, ensure_ascii=True),
            max_stdout_bytes=min(
                PARSEHUB_MAX_OUTPUT_BYTES,
                max(
                    1024,
                    runtime_int(
                        "PARSEHUB_PROVIDER_MAX_OUTPUT_BYTES",
                        PARSEHUB_MAX_OUTPUT_BYTES,
                    ),
                ),
            ),
            max_stderr_bytes=PARSEHUB_MAX_STDERR_BYTES,
        )
    except subprocess.TimeoutExpired as error:
        raise XhsParseHubImageError("PARSEHUB_TIMEOUT") from error
    except ExternalCommandOutputTooLarge as error:
        raise XhsParseHubImageError("PARSEHUB_OUTPUT_TOO_LARGE") from error
    except OSError as error:
        raise XhsParseHubImageError("PARSEHUB_NOT_AVAILABLE") from error
    payload = _parse_worker_payload(completed.stdout, note_id=note_id)
    if completed.returncode != 0:
        raise XhsParseHubImageError("PARSEHUB_RESULT_INVALID")
    return payload


def load_parsehub_worker_payload(
    *,
    source_url: str,
    note_id: str,
    deadline_monotonic: float = 0,
) -> dict[str, Any]:
    source_key = _source_cache_key(note_id)
    cached = get_cached_external_payload(PARSEHUB_XHS_PROVIDER_ID, source_key)
    if isinstance(cached, dict):
        return cached
    if get_cached_external_failure(PARSEHUB_XHS_PROVIDER_ID, source_key):
        raise XhsParseHubImageError("PARSEHUB_NOT_AVAILABLE")

    lock_key = _worker_lock_key(source_key)
    owns_lock = cache.add(
        lock_key,
        "1",
        timeout=PARSEHUB_SINGLE_FLIGHT_TTL_SECONDS,
    )
    if not owns_lock:
        wait_deadline = time.monotonic() + _remaining(
            deadline_monotonic,
            parsehub_provider_timeout_seconds(),
        )
        while time.monotonic() < wait_deadline:
            cached = get_cached_external_payload(PARSEHUB_XHS_PROVIDER_ID, source_key)
            if isinstance(cached, dict):
                return cached
            if get_cached_external_failure(PARSEHUB_XHS_PROVIDER_ID, source_key):
                raise XhsParseHubImageError("PARSEHUB_NOT_AVAILABLE")
            time.sleep(PARSEHUB_SINGLE_FLIGHT_POLL_SECONDS)
        raise XhsParseHubImageError("PARSEHUB_TIMEOUT")

    try:
        payload = _run_worker(
            source_url,
            note_id,
            deadline_monotonic=deadline_monotonic,
        )
        cache_external_payload(
            PARSEHUB_XHS_PROVIDER_ID,
            source_key,
            payload,
            runtime_int(
                "PARSEHUB_PROVIDER_CACHE_TTL_SECONDS",
                PARSEHUB_SUCCESS_TTL_SECONDS,
            ),
        )
        clear_cached_external_failure(PARSEHUB_XHS_PROVIDER_ID, source_key)
        return payload
    except XhsParseHubImageError as error:
        cache_external_failure(
            PARSEHUB_XHS_PROVIDER_ID,
            source_key,
            error.code,
            runtime_int(
                "PARSEHUB_PROVIDER_FAILURE_COOLDOWN_SECONDS",
                PARSEHUB_FAILURE_TTL_SECONDS,
            ),
        )
        raise
    finally:
        cache.delete(lock_key)


def validate_parsehub_correspondence(
    payload: dict[str, Any],
    *,
    snapshot: XhsPrivateSelectedNoteSnapshotV1,
) -> tuple[XhsParseHubCandidateV1, ...]:
    items = payload.get("items")
    if not isinstance(items, list) or len(items) != len(snapshot.image_list):
        raise XhsParseHubImageError("PARSEHUB_RESULT_INVALID")
    candidates = []
    for ordinal, (raw_item, native_item) in enumerate(zip(items, snapshot.image_list)):
        if not isinstance(raw_item, dict) or set(raw_item) != {
            "source_ordinal",
            "source_kind",
            "source_fingerprint",
            "raw_https_url",
            "declared_extension",
        }:
            raise XhsParseHubImageError("PARSEHUB_RESULT_INVALID")
        if (
            raw_item.get("source_ordinal") != ordinal
            or raw_item.get("source_kind") != xhs_image_source_kind(native_item)
            or raw_item.get("source_fingerprint")
            != xhs_image_source_fingerprint(native_item)
        ):
            raise XhsParseHubImageError("PARSEHUB_RESULT_INVALID")
        candidates.append(
            XhsParseHubCandidateV1(
                source_ordinal=ordinal,
                source_kind=raw_item["source_kind"],
                source_fingerprint=raw_item["source_fingerprint"],
                raw_https_url=raw_item["raw_https_url"],
            )
        )
    return tuple(candidates)


def _size_from_headers(headers: Any, status_code: int) -> int:
    content_range = str(headers.get("Content-Range") or "")
    range_match = re.fullmatch(r"bytes\s+\d+-\d+/(\d+)", content_range, re.I)
    if range_match:
        return max(0, int(range_match.group(1)))
    if status_code == 200:
        try:
            return max(0, int(headers.get("Content-Length") or 0))
        except (TypeError, ValueError):
            return 0
    return 0


def _jpeg_dimensions(prefix: bytes) -> tuple[int, int]:
    index = 2
    while index + 9 <= len(prefix):
        if prefix[index] != 0xFF:
            index += 1
            continue
        marker = prefix[index + 1]
        index += 2
        if marker in {0xD8, 0xD9}:
            continue
        if index + 2 > len(prefix):
            break
        segment_length = int.from_bytes(prefix[index:index + 2], "big")
        if segment_length < 2 or index + segment_length > len(prefix):
            break
        if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}:
            height = int.from_bytes(prefix[index + 3:index + 5], "big")
            width = int.from_bytes(prefix[index + 5:index + 7], "big")
            return width, height
        index += segment_length
    return 0, 0


def image_dimensions_from_prefix(prefix: bytes, format_name: str) -> tuple[int, int]:
    prefix = bytes(prefix or b"")
    if format_name == "jpeg":
        return _jpeg_dimensions(prefix)
    if format_name == "png" and len(prefix) >= 24:
        return int.from_bytes(prefix[16:20], "big"), int.from_bytes(prefix[20:24], "big")
    if format_name == "gif" and len(prefix) >= 10:
        return int.from_bytes(prefix[6:8], "little"), int.from_bytes(prefix[8:10], "little")
    if format_name == "webp" and len(prefix) >= 30:
        chunk = prefix[12:16]
        if chunk == b"VP8X":
            return (
                1 + int.from_bytes(prefix[24:27], "little"),
                1 + int.from_bytes(prefix[27:30], "little"),
            )
        if chunk == b"VP8 " and prefix[23:26] == b"\x9d\x01\x2a":
            return (
                int.from_bytes(prefix[26:28], "little") & 0x3FFF,
                int.from_bytes(prefix[28:30], "little") & 0x3FFF,
            )
        if chunk == b"VP8L" and prefix[20] == 0x2F:
            bits = int.from_bytes(prefix[21:25], "little")
            return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    if format_name in {"avif", "heic"}:
        index = prefix.find(b"ispe")
        if index >= 4 and index + 16 <= len(prefix):
            box_size = int.from_bytes(prefix[index - 4:index], "big")
            if box_size < 20:
                return 0, 0
            return (
                int.from_bytes(prefix[index + 8:index + 12], "big"),
                int.from_bytes(prefix[index + 12:index + 16], "big"),
            )
    return 0, 0


def _read_prefix(response: Any) -> bytes:
    chunks = []
    total = 0
    for chunk in response.iter_content(chunk_size=1024):
        if not chunk:
            continue
        remaining = PARSEHUB_MAX_PROBE_BYTES - total
        if remaining <= 0:
            break
        bounded = bytes(chunk[:remaining])
        chunks.append(bounded)
        total += len(bounded)
        if total >= PARSEHUB_MAX_PROBE_BYTES:
            break
    return b"".join(chunks)


def probe_xhs_parsehub_raw_image(
    url: str,
    *,
    deadline_monotonic: float = 0,
    requester: Callable = requests.get,
) -> XhsParseHubProbeV1:
    current_url = validate_xhs_parsehub_raw_url(url)
    for redirect_index in range(PARSEHUB_MAX_REDIRECTS + 1):
        response = None
        try:
            response = requester(
                current_url,
                headers={
                    "User-Agent": HEADERS["User-Agent"],
                    "Accept": "image/avif,image/webp,image/png,image/*;q=0.9,*/*;q=0.1",
                    "Range": f"bytes=0-{PARSEHUB_MAX_PROBE_BYTES - 1}",
                },
                timeout=_remaining(
                    deadline_monotonic,
                    min(
                        PARSEHUB_DEFAULT_PROBE_TIMEOUT_SECONDS,
                        max(
                            0.1,
                            runtime_float(
                                "PARSEHUB_PROVIDER_PROBE_TIMEOUT_SECONDS",
                                PARSEHUB_DEFAULT_PROBE_TIMEOUT_SECONDS,
                            ),
                        ),
                    ),
                ),
                allow_redirects=False,
                stream=True,
            )
            status_code = int(getattr(response, "status_code", 0) or 0)
            if status_code in _REDIRECT_STATUSES:
                if redirect_index >= PARSEHUB_MAX_REDIRECTS:
                    raise XhsParseHubImageError("PARSEHUB_RESULT_INVALID")
                location = str(response.headers.get("Location") or "").strip()
                current_url = validate_xhs_parsehub_raw_url(urljoin(current_url, location))
                continue
            if status_code not in {200, 206}:
                raise XhsParseHubImageError("PARSEHUB_RESULT_INVALID")
            final_url = validate_xhs_parsehub_raw_url(
                str(getattr(response, "url", "") or current_url)
            )
            content_type = (
                str(response.headers.get("Content-Type") or "")
                .split(";", 1)[0]
                .strip()
                .lower()
            )
            if content_type not in _GENERIC_BINARY_TYPES and not content_type.startswith("image/"):
                raise XhsParseHubImageError("PARSEHUB_RESULT_INVALID")
            prefix = _read_prefix(response)
            resolution = resolve_image_format(prefix, content_type=content_type)
            if resolution.confidence != "magic" or not resolution.valid:
                raise XhsParseHubImageError("PARSEHUB_RESULT_INVALID")
            width, height = image_dimensions_from_prefix(prefix, resolution.format)
            if width <= 0 or height <= 0:
                raise XhsParseHubImageError("PARSEHUB_RESULT_INVALID")
            return XhsParseHubProbeV1(
                final_url=final_url,
                extension=resolution.extension,
                mime_type=resolution.mime_type,
                size_bytes=_size_from_headers(response.headers, status_code),
                width=width,
                height=height,
                bytes_read=len(prefix),
            )
        except requests.Timeout as error:
            raise XhsParseHubImageError("PARSEHUB_TIMEOUT") from error
        except requests.RequestException as error:
            raise XhsParseHubImageError("PARSEHUB_RESULT_INVALID") from error
        finally:
            if response is not None:
                response.close()
    raise XhsParseHubImageError("PARSEHUB_RESULT_INVALID")


def _native_fallback_asset(asset: MediaAsset) -> MediaAsset:
    from .xiaohongshu import normalize_xhs_preview_image_url

    preview_url = normalize_xhs_preview_image_url(asset.url, width=1280) or asset.preview_url or asset.url
    return MediaAsset(
        media_type="image",
        url=asset.url,
        platform=asset.platform,
        preview_url=preview_url,
        filename=asset.filename,
        ext=asset.ext or "jpg",
        quality_label=asset.quality_label,
        size_bytes=asset.size_bytes,
        size_is_estimate=asset.size_is_estimate,
        headers=asset.headers,
        media_host_suffixes=asset.media_host_suffixes,
        asset_id=asset.asset_id,
        provider=asset.provider,
        provider_resource_id=asset.provider_resource_id,
        provider_cache_key=asset.provider_cache_key,
        provider_manifest_token=asset.provider_manifest_token,
        source_platform=asset.source_platform,
    )


def _parsehub_asset(
    *,
    native_asset: MediaAsset,
    note_id: str,
    candidate: XhsParseHubCandidateV1,
    probe: XhsParseHubProbeV1,
) -> MediaAsset:
    from .xiaohongshu import normalize_xhs_preview_image_url

    identity = _opaque_identity(note_id)
    resource = candidate.source_fingerprint[:24]
    asset_id = f"xhs:{identity}:image:{candidate.source_ordinal}:parsehub"
    provider_resource_id = f"{identity}:{candidate.source_ordinal}:{resource}"
    provider_cache_key = f"xiaohongshu:parsehub:{identity}"
    token = build_provider_manifest_token(
        asset_id=asset_id,
        provider=PARSEHUB_XHS_PROVIDER_ID,
        provider_resource_id=provider_resource_id,
        provider_cache_key=provider_cache_key,
        url=probe.final_url,
        media_type="image",
        source_platform="xiaohongshu",
    )
    native_stem = Path(native_asset.filename or "image").stem or "image"
    return MediaAsset(
        media_type="image",
        url=probe.final_url,
        platform="xiaohongshu",
        preview_url=(
            normalize_xhs_preview_image_url(native_asset.url, width=1280)
            or native_asset.preview_url
            or native_asset.url
        ),
        filename=clean_filename(f"{native_stem}.{probe.extension}"),
        ext=probe.extension,
        quality_label="原始图片",
        size_bytes=probe.size_bytes,
        size_is_estimate=False,
        headers={},
        media_host_suffixes=(PARSEHUB_RAW_IMAGE_HOST,),
        asset_id=asset_id,
        provider=PARSEHUB_XHS_PROVIDER_ID,
        provider_resource_id=provider_resource_id,
        provider_cache_key=provider_cache_key,
        provider_manifest_token=token,
        source_platform="xiaohongshu",
    )


def _eligible_native_result(
    result: ParseResult,
    snapshot: XhsPrivateSelectedNoteSnapshotV1 | None,
) -> bool:
    metadata = result.metadata or {}
    note_id = normalize_xhs_note_id(metadata.get("note_id"))
    return bool(
        result.platform == "xiaohongshu"
        and result.post_type == "image"
        and result.rich_media is None
        and type(snapshot) is XhsPrivateSelectedNoteSnapshotV1
        and snapshot.note_id == note_id
        and metadata.get("source_identity_verified") is True
        and str(metadata.get("page_state") or "")
        not in {"deleted", "private", "unavailable", "login_required"}
        and len(result.image_assets) == len(snapshot.image_list)
        and 0 < len(result.image_assets) <= PARSEHUB_MAX_ITEMS
    )


def enhance_xhs_images_with_parsehub(
    *,
    native_result: ParseResult,
    snapshot: XhsPrivateSelectedNoteSnapshotV1 | None,
    source_url: str,
    deadline_monotonic: float = 0,
    payload_loader: Callable = load_parsehub_worker_payload,
    probe_candidate: Callable = probe_xhs_parsehub_raw_image,
) -> XhsParseHubEnhancementOutcome:
    fallback_assets = tuple(_native_fallback_asset(asset) for asset in native_result.image_assets)
    fallback_result = ParseResult(
        platform=native_result.platform,
        post_type=native_result.post_type,
        base_filename=native_result.base_filename,
        assets=fallback_assets,
        metadata=dict(native_result.metadata or {}),
    )
    if not _eligible_native_result(native_result, snapshot) or not parsehub_provider_enabled():
        return XhsParseHubEnhancementOutcome(
            result=fallback_result,
            worker_status="disabled_or_ineligible",
            selected_count=0,
            fallback_count=len(fallback_assets),
            bounded_probe_bytes=0,
        )

    note_id = snapshot.note_id
    started_at = time.monotonic()
    enhancement_deadline = started_at + PARSEHUB_MAX_TIMEOUT_SECONDS
    if deadline_monotonic:
        enhancement_deadline = min(
            enhancement_deadline,
            float(deadline_monotonic),
        )
    try:
        payload = payload_loader(
            source_url=source_url,
            note_id=note_id,
            deadline_monotonic=enhancement_deadline,
        )
        candidates = validate_parsehub_correspondence(payload, snapshot=snapshot)
    except XhsParseHubImageError as error:
        metadata = dict(fallback_result.metadata)
        metadata.update(
            {
                "xhs_parsehub_image_status": "fallback",
                "xhs_parsehub_image_error_code": error.code,
                "xhs_parsehub_image_duration_ms": round((time.monotonic() - started_at) * 1000),
            }
        )
        return XhsParseHubEnhancementOutcome(
            result=ParseResult(
                platform=fallback_result.platform,
                post_type=fallback_result.post_type,
                base_filename=fallback_result.base_filename,
                assets=fallback_result.assets,
                metadata=metadata,
            ),
            worker_status="fallback",
            selected_count=0,
            fallback_count=len(fallback_assets),
            bounded_probe_bytes=0,
        )

    probes: dict[int, XhsParseHubProbeV1] = {}
    max_workers = min(PARSEHUB_MAX_PROBE_WORKERS, len(candidates))
    executor = ThreadPoolExecutor(
        max_workers=max_workers,
        thread_name_prefix="xhs-parsehub-probe",
    )
    try:
        future_map = {
            executor.submit(
                probe_candidate,
                candidate.raw_https_url,
                deadline_monotonic=enhancement_deadline,
            ): candidate.source_ordinal
            for candidate in candidates
        }
        completed, unresolved = wait(
            tuple(future_map),
            timeout=_remaining(
                enhancement_deadline,
                PARSEHUB_DEFAULT_PROBE_TIMEOUT_SECONDS,
            ),
        )
        for future in unresolved:
            future.cancel()
        for future in completed:
            ordinal = future_map[future]
            try:
                probes[ordinal] = future.result()
            except Exception:
                continue
    finally:
        executor.shutdown(wait=False, cancel_futures=True)

    selected_assets = []
    selected_count = 0
    for ordinal, (native_asset, candidate) in enumerate(zip(fallback_assets, candidates)):
        probe = probes.get(ordinal)
        native_item = snapshot.image_list[ordinal]
        # Chromium cannot render HEIC as a LivePhoto still. Preserve original HEIC
        # for ordinary downloads, but use the compatible Native still in rich media.
        if probe is None or (
            xhs_image_source_kind(native_item) == "livephoto_still"
            and probe.extension == "heic"
        ):
            selected_assets.append(native_asset)
            continue
        selected_assets.append(
            _parsehub_asset(
                native_asset=native_asset,
                note_id=note_id,
                candidate=candidate,
                probe=probe,
            )
        )
        selected_count += 1

    metadata = dict(native_result.metadata or {})
    metadata.update(
        {
            "xhs_parsehub_image_status": "enhanced" if selected_count else "fallback",
            "xhs_parsehub_image_selected_count": selected_count,
            "xhs_parsehub_image_fallback_count": len(selected_assets) - selected_count,
            "xhs_parsehub_image_bounded_probe_bytes": sum(probe.bytes_read for probe in probes.values()),
            "xhs_parsehub_image_duration_ms": round((time.monotonic() - started_at) * 1000),
        }
    )
    result = ParseResult(
        platform=native_result.platform,
        post_type=native_result.post_type,
        base_filename=native_result.base_filename,
        assets=tuple(selected_assets),
        metadata=metadata,
    )
    return XhsParseHubEnhancementOutcome(
        result=result,
        worker_status="enhanced" if selected_count else "fallback",
        selected_count=selected_count,
        fallback_count=len(selected_assets) - selected_count,
        bounded_probe_bytes=sum(probe.bytes_read for probe in probes.values()),
    )


def xhs_parsehub_manifest_item_is_allowed(item: Any) -> bool:
    from ..provider_manifest import provider_manifest_is_valid

    if not isinstance(item, dict):
        return False
    if (
        str(item.get("provider") or "") != PARSEHUB_XHS_PROVIDER_ID
        or str(item.get("type") or "").lower() != "image"
        or str(item.get("source_platform") or "").lower() != "xiaohongshu"
    ):
        return False
    resource_parts = str(item.get("provider_resource_id") or "").split(":")
    if len(resource_parts) != 3:
        return False
    identity, ordinal, resource = resource_parts
    if (
        not _OPAQUE_ID_RE.fullmatch(identity)
        or not ordinal.isdigit()
        or not 0 <= int(ordinal) < PARSEHUB_MAX_ITEMS
        or not _OPAQUE_ID_RE.fullmatch(resource)
        or str(item.get("asset_id") or "")
        != f"xhs:{identity}:image:{int(ordinal)}:parsehub"
        or str(item.get("provider_cache_key") or "")
        != f"xiaohongshu:parsehub:{identity}"
    ):
        return False
    try:
        validate_xhs_parsehub_raw_url(item.get("url"))
    except XhsParseHubImageError:
        return False
    return provider_manifest_is_valid(item)


__all__ = (
    "PARSEHUB_PIN",
    "PARSEHUB_RAW_IMAGE_HOST",
    "PARSEHUB_XHS_PROVIDER_ID",
    "XhsParseHubCandidateV1",
    "XhsParseHubEnhancementOutcome",
    "XhsParseHubImageError",
    "XhsParseHubProbeV1",
    "enhance_xhs_images_with_parsehub",
    "image_dimensions_from_prefix",
    "load_parsehub_worker_payload",
    "parsehub_provider_enabled",
    "probe_xhs_parsehub_raw_image",
    "validate_parsehub_correspondence",
    "validate_xhs_parsehub_raw_url",
    "xhs_image_source_fingerprint",
    "xhs_parsehub_manifest_item_is_allowed",
)
