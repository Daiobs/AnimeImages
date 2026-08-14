import json
import logging
import re

from django.conf import settings


logger = logging.getLogger("xhs_download.browser_streaming_telemetry")

SCHEMA_VERSION = 1
DEFAULT_MAX_PAYLOAD_BYTES = 8192
DEFAULT_RATE_LIMIT_PER_MINUTE = 30
MAX_MEDIA_BYTES = 2 * 1024 * 1024 * 1024
MAX_DURATION_MS = 24 * 60 * 60 * 1000
DOWNLOAD_ID_PATTERN = re.compile(r"^[a-f0-9]{32}$")
PROVIDER_PATTERN = re.compile(r"^[a-z0-9_-]{1,64}$")
HOSTNAME_PATTERN = re.compile(r"^[a-z0-9.-]{1,255}$")
ALLOWED_MEDIA_HOST_SUFFIXES = ("365yg.com", "douyinvod.com")
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]"}

ALLOWED_FIELDS = frozenset(
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
REQUIRED_FIELDS = ALLOWED_FIELDS
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
ALLOWED_VISIBILITY_STATES = frozenset({"visible", "hidden", "prerender", "unknown"})
ALLOWED_WORKER_STATES = frozenset(
    {"activated", "activating", "installed", "installing", "redundant", "none", "unknown"}
)
ALLOWED_BROWSER_FAMILIES = frozenset({"chromium", "firefox", "safari", "unknown"})


class BrowserStreamingTelemetryValidationError(ValueError):
    pass


def _invalid():
    raise BrowserStreamingTelemetryValidationError("INVALID_TELEMETRY_PAYLOAD")


def _bounded_integer(value, maximum):
    if type(value) is not int or value < 0 or value > maximum:
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


def _media_host(value):
    if not isinstance(value, str):
        _invalid()
    hostname = value.strip().lower().rstrip(".")
    if hostname == "unknown":
        return hostname
    if not HOSTNAME_PATTERN.fullmatch(hostname):
        if getattr(settings, "DEBUG", False) and hostname in LOOPBACK_HOSTS:
            return hostname
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
    if any(_hostname_matches_suffix(hostname, suffix) for suffix in ALLOWED_MEDIA_HOST_SUFFIXES):
        return hostname
    if getattr(settings, "DEBUG", False) and hostname in LOOPBACK_HOSTS:
        return hostname
    _invalid()


def validate_browser_streaming_telemetry(payload):
    if not isinstance(payload, dict):
        _invalid()
    if set(payload) != REQUIRED_FIELDS:
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
        "protocol_version": _bounded_integer(payload["protocol_version"], 100),
        "browser_family": _fixed_string(payload["browser_family"], ALLOWED_BROWSER_FAMILIES),
    }
    if normalized["schema_version"] != SCHEMA_VERSION or normalized["protocol_version"] < 1:
        _invalid()
    if normalized["received_bytes"] > MAX_MEDIA_BYTES:
        _invalid()
    return normalized


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
