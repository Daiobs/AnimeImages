import ipaddress
import json
import logging
import re
from datetime import datetime, time, timedelta, timezone as datetime_timezone
from math import ceil

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from .platforms.douyin_media_policy import DOUYIN_REVIEWED_EXACT_MEDIA_HOSTS
from .platforms.xhs_tiantian_media_policy import XHS_TIANTIAN_VIDEO_HOSTS
from .platforms.xiaohongshu import (
    XHS_NATIVE_BROWSER_VIDEO_HOSTS,
    XHS_NATIVE_BROWSER_VIDEO_HOST_PATTERN,
)


logger = logging.getLogger("xhs_download.browser_streaming_telemetry")

SCHEMA_VERSION = 1
MAX_PROTOCOL_VERSION = 100
PROTOCOL_DISTRIBUTION_DAYS = 14
DEFAULT_MAX_PAYLOAD_BYTES = 8192
DEFAULT_RATE_LIMIT_PER_MINUTE = 30
MAX_MEDIA_BYTES = 2 * 1024 * 1024 * 1024
MAX_DURATION_MS = 24 * 60 * 60 * 1000
DOWNLOAD_ID_PATTERN = re.compile(r"^[a-f0-9]{32}$")
PROVIDER_PATTERN = re.compile(r"^[a-z0-9_-]{1,64}$")
HOSTNAME_PATTERN = re.compile(r"^[a-z0-9.-]{1,255}$")
ALLOWED_MEDIA_HOST_SUFFIXES = ("365yg.com", "douyinvod.com")
ALLOWED_EXACT_MEDIA_HOSTS = DOUYIN_REVIEWED_EXACT_MEDIA_HOSTS
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]"}

REQUIRED_FIELDS = frozenset(
    {
        "schema_version",
        "telemetry_id",
        "download_id",
        "provider",
        "media_host",
        "transport",
        "state",
        "error_code",
        "failure_class",
        "failure_stage",
        "streaming_started",
        "expected_bytes",
        "received_bytes",
        "total_bytes",
        "registration_to_fetch_ms",
        "fetch_reject_ms",
        "time_to_first_chunk_ms",
        "stream_duration_ms",
        "navigator_online",
        "page_visibility",
        "worker_controller_state",
        "worker_active_state",
        "worker_waiting",
        "worker_installing",
        "protocol_version",
        "browser_family",
    }
)
OPTIONAL_FIELDS = frozenset(
    {
        "response_reject_reason",
        "http_status",
        "content_type_class",
        "final_media_host",
    }
)
FINAL_REDIRECT_FIELDS = frozenset(
    {
        "initial_media_host",
        "initial_media_family",
        "observed_final_host",
        "observed_final_host_class",
        "redirected",
    }
)
ALLOWED_FIELDS = REQUIRED_FIELDS | OPTIONAL_FIELDS | FINAL_REDIRECT_FIELDS
ALLOWED_STATES = frozenset({"failed", "cancelled"})
ALLOWED_ERROR_CODES = frozenset(
    {
        "UPSTREAM_FETCH_FAILED",
        "UPSTREAM_MEDIA_INVALID",
        "UPSTREAM_SIZE_LIMIT_EXCEEDED",
        "UPSTREAM_SIZE_MISMATCH",
        "UPSTREAM_STREAM_FAILED",
        "DOWNLOAD_MANAGER_CANCELLED",
        "USER_CANCELLED",
        "DOWNLOAD_CANCELLED",
        "DESCRIPTOR_EXPIRED",
        "DESCRIPTOR_DELETED",
    }
)
ALLOWED_FAILURE_CLASSES = frozenset(
    {
        "NOT_APPLICABLE",
        "FETCH_REJECTED_TYPE_ERROR",
        "FETCH_ABORTED",
        "FETCH_NETWORK_ERROR",
        "FETCH_SECURITY_ERROR",
        "FETCH_TIMEOUT_ERROR",
        "FETCH_REJECTED_OTHER",
    }
)
ALLOWED_FAILURE_STAGES = frozenset(
    {
        "PRE_START_FAILURE",
        "FETCH_REJECTED_BEFORE_RESPONSE",
        "HTTP_RESPONSE_REJECTED",
        "STREAM_BODY_FAILURE",
        "SIZE_VALIDATION_FAILURE",
        "USER_CANCELLED",
        "DOWNLOAD_MANAGER_CANCELLED",
        "DESCRIPTOR_FAILURE",
    }
)
ALLOWED_RESPONSE_REJECT_REASONS = frozenset(
    {
        "HTTP_STATUS_REJECTED",
        "BODY_UNAVAILABLE",
        "CONTENT_TYPE_REJECTED",
        "FINAL_HOST_REJECTED",
        "UNKNOWN_RESPONSE_REJECTED",
    }
)
ALLOWED_CONTENT_TYPE_CLASSES = frozenset(
    {"VIDEO", "OCTET_STREAM", "MISSING", "OTHER"}
)
ALLOWED_VISIBILITY_STATES = frozenset({"visible", "hidden", "prerender", "unknown"})
ALLOWED_WORKER_STATES = frozenset(
    {"activated", "activating", "installed", "installing", "redundant", "none", "unknown"}
)
ALLOWED_BROWSER_FAMILIES = frozenset({"chromium", "firefox", "safari", "unknown"})
ALLOWED_INITIAL_MEDIA_FAMILIES = frozenset(
    {
        "DOUYIN_365YG",
        "DOUYIN_DOUYINVOD",
        "DOUYIN_IXIGUA_EXACT",
        "XHS_TIANTIAN_HW_EXACT",
        "XHS_NATIVE_REVIEWED",
        "LOOPBACK_TEST",
        "UNKNOWN",
    }
)
ALLOWED_FINAL_MEDIA_HOST_CLASSES = frozenset(
    {
        "INITIAL_SOURCE",
        "REVIEWED_FINAL_ONLY",
        "UNREVIEWED_PUBLIC",
        "LOOPBACK_TEST",
        "INVALID",
    }
)
REVIEWED_FINAL_ONLY_HOST_SUFFIX = "bdcgslb.com"


class BrowserStreamingTelemetryValidationError(ValueError):
    pass


def _invalid():
    raise BrowserStreamingTelemetryValidationError("INVALID_TELEMETRY_PAYLOAD")


def _bounded_integer(value, maximum):
    if type(value) is not int or value < 0 or value > maximum:
        _invalid()
    return value


def _http_status(value):
    if type(value) is not int or (value != 0 and not 100 <= value <= 599):
        _invalid()
    return value


def _fixed_string(value, allowed):
    if not isinstance(value, str) or value not in allowed:
        _invalid()
    return value


def _boolean(value):
    if type(value) is not bool:
        _invalid()
    return value


def _hostname_matches_suffix(hostname, suffix):
    return hostname == suffix or hostname.endswith(f".{suffix}")


def _hostname_is_strict_subdomain(hostname, suffix):
    return hostname != suffix and hostname.endswith(f".{suffix}")


def _validated_public_hostname(value, *, allow_unknown=True):
    if not isinstance(value, str):
        _invalid()
    hostname = value.strip().lower()
    if allow_unknown and hostname == "unknown":
        return hostname
    if not hostname or hostname.endswith(".") or not HOSTNAME_PATTERN.fullmatch(hostname):
        _invalid()
    labels = hostname.split(".")
    if any(
        not label
        or len(label) > 63
        or label.startswith("-")
        or label.endswith("-")
        for label in labels
    ):
        _invalid()
    if hostname in LOOPBACK_HOSTS:
        _invalid()
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        _invalid()
    return hostname


def _media_host(value):
    if not isinstance(value, str):
        _invalid()
    hostname = value.strip().lower()
    if hostname == "unknown":
        return hostname
    if getattr(settings, "DEBUG", False) and hostname in LOOPBACK_HOSTS:
        return hostname
    hostname = _validated_public_hostname(hostname, allow_unknown=False)
    if (
        hostname in ALLOWED_EXACT_MEDIA_HOSTS
        or hostname in XHS_TIANTIAN_VIDEO_HOSTS
        or hostname in XHS_NATIVE_BROWSER_VIDEO_HOSTS
        or XHS_NATIVE_BROWSER_VIDEO_HOST_PATTERN.fullmatch(hostname)
        or any(
            _hostname_matches_suffix(hostname, suffix)
            for suffix in ALLOWED_MEDIA_HOST_SUFFIXES
        )
    ):
        return hostname
    _invalid()


def _initial_media_family_for_host(hostname):
    if hostname == "unknown":
        return "UNKNOWN"
    if hostname in LOOPBACK_HOSTS:
        return "LOOPBACK_TEST"
    if _hostname_matches_suffix(hostname, "365yg.com"):
        return "DOUYIN_365YG"
    if _hostname_matches_suffix(hostname, "douyinvod.com"):
        return "DOUYIN_DOUYINVOD"
    if hostname in ALLOWED_EXACT_MEDIA_HOSTS:
        return "DOUYIN_IXIGUA_EXACT"
    if hostname in XHS_TIANTIAN_VIDEO_HOSTS:
        return "XHS_TIANTIAN_HW_EXACT"
    if (hostname in XHS_NATIVE_BROWSER_VIDEO_HOSTS
            or XHS_NATIVE_BROWSER_VIDEO_HOST_PATTERN.fullmatch(hostname)):
        return "XHS_NATIVE_REVIEWED"
    return "UNKNOWN"


def _observed_final_host(value):
    return _validated_public_hostname(value)


def _observed_final_host_class(hostname):
    if hostname == "unknown":
        return "INVALID"
    if (
        hostname in ALLOWED_EXACT_MEDIA_HOSTS
        or hostname in XHS_TIANTIAN_VIDEO_HOSTS
        or hostname in XHS_NATIVE_BROWSER_VIDEO_HOSTS
        or XHS_NATIVE_BROWSER_VIDEO_HOST_PATTERN.fullmatch(hostname)
        or any(
            _hostname_matches_suffix(hostname, suffix)
            for suffix in ALLOWED_MEDIA_HOST_SUFFIXES
        )
    ):
        return "INITIAL_SOURCE"
    if _hostname_is_strict_subdomain(hostname, REVIEWED_FINAL_ONLY_HOST_SUFFIX):
        return "REVIEWED_FINAL_ONLY"
    return "UNREVIEWED_PUBLIC"


def _admitted_final_media_host(value, *, initial_media_family):
    if value == "unknown":
        return value
    try:
        return _media_host(value)
    except BrowserStreamingTelemetryValidationError:
        hostname = _validated_public_hostname(value, allow_unknown=False)
        if (
            initial_media_family == "DOUYIN_365YG"
            and _hostname_is_strict_subdomain(
                hostname,
                REVIEWED_FINAL_ONLY_HOST_SUFFIX,
            )
        ):
            return hostname
        _invalid()


def validate_browser_streaming_telemetry(payload):
    if not isinstance(payload, dict):
        _invalid()
    payload_fields = set(payload)
    if not REQUIRED_FIELDS.issubset(payload_fields) or not payload_fields.issubset(ALLOWED_FIELDS):
        _invalid()
    optional_fields = OPTIONAL_FIELDS.intersection(payload_fields)
    if optional_fields and optional_fields != OPTIONAL_FIELDS:
        _invalid()
    redirect_fields = FINAL_REDIRECT_FIELDS.intersection(payload_fields)
    if redirect_fields and redirect_fields != FINAL_REDIRECT_FIELDS:
        _invalid()
    if redirect_fields and optional_fields != OPTIONAL_FIELDS:
        _invalid()

    telemetry_id = payload["telemetry_id"]
    download_id = payload["download_id"]
    if not isinstance(telemetry_id, str) or not DOWNLOAD_ID_PATTERN.fullmatch(telemetry_id):
        _invalid()
    if not isinstance(download_id, str) or not DOWNLOAD_ID_PATTERN.fullmatch(download_id):
        _invalid()

    provider = payload["provider"]
    if not isinstance(provider, str) or not PROVIDER_PATTERN.fullmatch(provider):
        _invalid()

    error_code = payload["error_code"]
    if not isinstance(error_code, str) or len(error_code) > 80:
        _invalid()

    normalized = {
        "schema_version": _bounded_integer(payload["schema_version"], SCHEMA_VERSION),
        "telemetry_id": telemetry_id,
        "download_id": download_id,
        "provider": provider,
        "media_host": _media_host(payload["media_host"]),
        "transport": _fixed_string(payload["transport"], {"browser_streaming"}),
        "state": _fixed_string(payload["state"], ALLOWED_STATES),
        "error_code": _fixed_string(error_code, ALLOWED_ERROR_CODES),
        "failure_class": _fixed_string(payload["failure_class"], ALLOWED_FAILURE_CLASSES),
        "failure_stage": _fixed_string(payload["failure_stage"], ALLOWED_FAILURE_STAGES),
        "streaming_started": _boolean(payload["streaming_started"]),
        "expected_bytes": _bounded_integer(payload["expected_bytes"], MAX_MEDIA_BYTES),
        "received_bytes": _bounded_integer(payload["received_bytes"], MAX_MEDIA_BYTES),
        "total_bytes": _bounded_integer(payload["total_bytes"], MAX_MEDIA_BYTES),
        "registration_to_fetch_ms": _bounded_integer(
            payload["registration_to_fetch_ms"], MAX_DURATION_MS
        ),
        "fetch_reject_ms": _bounded_integer(payload["fetch_reject_ms"], MAX_DURATION_MS),
        "time_to_first_chunk_ms": _bounded_integer(
            payload["time_to_first_chunk_ms"], MAX_DURATION_MS
        ),
        "stream_duration_ms": _bounded_integer(payload["stream_duration_ms"], MAX_DURATION_MS),
        "navigator_online": _boolean(payload["navigator_online"]),
        "page_visibility": _fixed_string(
            payload["page_visibility"], ALLOWED_VISIBILITY_STATES
        ),
        "worker_controller_state": _fixed_string(
            payload["worker_controller_state"], ALLOWED_WORKER_STATES
        ),
        "worker_active_state": _fixed_string(
            payload["worker_active_state"], ALLOWED_WORKER_STATES
        ),
        "worker_waiting": _boolean(payload["worker_waiting"]),
        "worker_installing": _boolean(payload["worker_installing"]),
        "protocol_version": _bounded_integer(payload["protocol_version"], MAX_PROTOCOL_VERSION),
        "browser_family": _fixed_string(payload["browser_family"], ALLOWED_BROWSER_FAMILIES),
    }
    if optional_fields:
        initial_media_family = _fixed_string(
            payload.get("initial_media_family", "UNKNOWN"),
            ALLOWED_INITIAL_MEDIA_FAMILIES,
        )
        normalized.update(
            response_reject_reason=_fixed_string(
                payload["response_reject_reason"],
                ALLOWED_RESPONSE_REJECT_REASONS,
            ),
            http_status=_http_status(payload["http_status"]),
            content_type_class=_fixed_string(
                payload["content_type_class"],
                ALLOWED_CONTENT_TYPE_CLASSES,
            ),
            final_media_host=_admitted_final_media_host(
                payload["final_media_host"],
                initial_media_family=initial_media_family,
            ),
        )
        if (
            normalized["state"] != "failed"
            or normalized["error_code"] != "UPSTREAM_MEDIA_INVALID"
            or normalized["failure_stage"] != "HTTP_RESPONSE_REJECTED"
        ):
            _invalid()
    if redirect_fields:
        initial_media_host = _media_host(payload["initial_media_host"])
        initial_media_family = _fixed_string(
            payload["initial_media_family"],
            ALLOWED_INITIAL_MEDIA_FAMILIES,
        )
        observed_final_host = _observed_final_host(payload["observed_final_host"])
        observed_final_host_class = _fixed_string(
            payload["observed_final_host_class"],
            ALLOWED_FINAL_MEDIA_HOST_CLASSES,
        )
        redirected = _boolean(payload["redirected"])
        if initial_media_host != normalized["media_host"]:
            _invalid()
        if _initial_media_family_for_host(initial_media_host) != initial_media_family:
            _invalid()
        if _observed_final_host_class(observed_final_host) != observed_final_host_class:
            _invalid()
        final_media_host = normalized["final_media_host"]
        if final_media_host != "unknown" and final_media_host != observed_final_host:
            _invalid()
        if observed_final_host_class == "REVIEWED_FINAL_ONLY" and not redirected:
            _invalid()
        normalized.update(
            initial_media_host=initial_media_host,
            initial_media_family=initial_media_family,
            observed_final_host=observed_final_host,
            observed_final_host_class=observed_final_host_class,
            redirected=redirected,
        )
    if normalized["schema_version"] != SCHEMA_VERSION or normalized["protocol_version"] < 1:
        _invalid()
    if normalized["received_bytes"] > MAX_MEDIA_BYTES:
        _invalid()
    return normalized


def _protocol_version_key(day, version):
    return f"browser-streaming:protocol-version:{day.isoformat()}:{version}"


def record_protocol_version(protocol_version):
    if type(protocol_version) is not int or not 1 <= protocol_version <= MAX_PROTOCOL_VERSION:
        return
    now = timezone.now().astimezone(datetime_timezone.utc)
    day = now.date()
    expires_at = datetime.combine(
        day + timedelta(days=PROTOCOL_DISTRIBUTION_DAYS),
        time.min,
        tzinfo=datetime_timezone.utc,
    )
    timeout = max(1, ceil((expires_at - now).total_seconds()))
    key = _protocol_version_key(day, protocol_version)
    try:
        for attempt in range(2):
            if cache.add(key, 1, timeout=timeout):
                return
            try:
                cache.incr(key)
                return
            except ValueError:
                # Retry once if eviction occurred between add and incr.
                if attempt:
                    raise
    except Exception:
        logger.warning(
            "browser_streaming_protocol_count_unavailable",
            extra={"event": "browser_streaming_protocol_count_unavailable"},
        )


def get_protocol_version_distribution(*, now=None):
    today = (now or timezone.now()).astimezone(datetime_timezone.utc).date()
    days = [today - timedelta(days=offset) for offset in range(PROTOCOL_DISTRIBUTION_DAYS)]
    keys = [
        _protocol_version_key(day, version)
        for day in days
        for version in range(1, MAX_PROTOCOL_VERSION + 1)
    ]
    try:
        counts = cache.get_many(keys)
    except Exception:
        logger.warning(
            "browser_streaming_protocol_distribution_unavailable",
            extra={"event": "browser_streaming_protocol_distribution_unavailable"},
        )
        return {"available": False, "days": [], "sample_days": 0, "total": 0}

    rows = []
    for day in days:
        versions = []
        for version in range(1, MAX_PROTOCOL_VERSION + 1):
            count = counts.get(_protocol_version_key(day, version), 0)
            if type(count) is int and count > 0:
                versions.append({"version": version, "count": count})
        rows.append({
            "date": day.isoformat(),
            "versions": versions,
            "total": sum(item["count"] for item in versions),
        })
    return {
        "available": True,
        "days": rows,
        "sample_days": sum(bool(row["total"]) for row in rows),
        "total": sum(row["total"] for row in rows),
    }


def log_browser_streaming_telemetry(payload):
    event = {"client_observed": True, **payload}
    serialized = json.dumps(event, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    logger.info(
        "browser_streaming_terminal client_observed=%s",
        serialized,
        extra={
            "event": "browser_streaming_terminal",
            "provider": payload["provider"],
            "status": payload["state"],
            "duration_ms": payload["stream_duration_ms"],
            "error_type": payload["failure_class"],
        },
    )
    record_protocol_version(payload["protocol_version"])
