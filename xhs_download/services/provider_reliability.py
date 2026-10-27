import logging
import re
import time
from enum import Enum
from urllib.parse import urlsplit

import requests

from .common import XhsProviderTimeoutError
from .douyin_ultra_attempts import (
    DouyinUltraAttemptAccountingError,
    DouyinUltraAttemptRateLimitedError,
    DouyinUltraAttemptSupersededError,
)


logger = logging.getLogger("xhs_download.provider_reliability")

HOSTNAME_PATTERN = re.compile(r"^[a-z0-9.-]{1,255}$")
MAX_MEDIA_BYTES = 2 * 1024 * 1024 * 1024
MAX_DURATION_MS = 24 * 60 * 60 * 1000


class ProviderOutcome(str, Enum):
    SKIPPED_ADMISSION = "SKIPPED_ADMISSION"
    SUCCESS_FRESH = "SUCCESS_FRESH"
    SUCCESS_EXISTING_RESULT = "SUCCESS_EXISTING_RESULT"
    SUCCESS_PAYLOAD_CACHE = "SUCCESS_PAYLOAD_CACHE"
    SUCCESS_SINGLE_FLIGHT = "SUCCESS_SINGLE_FLIGHT"
    SUCCESS_CONTINUATION = "SUCCESS_CONTINUATION"
    FAILURE_COOLDOWN = "FAILURE_COOLDOWN"
    FAILURE_TIMEOUT = "FAILURE_TIMEOUT"
    FAILURE_HTTP = "FAILURE_HTTP"
    FAILURE_NETWORK = "FAILURE_NETWORK"
    FAILURE_SCHEMA = "FAILURE_SCHEMA"
    FAILURE_NO_MEDIA = "FAILURE_NO_MEDIA"
    FAILURE_RESULT_INVALID = "FAILURE_RESULT_INVALID"
    FAILURE_RATE_LIMIT = "FAILURE_RATE_LIMIT"
    FAILURE_CONCURRENCY = "FAILURE_CONCURRENCY"
    FAILURE_CIRCUIT_OPEN = "FAILURE_CIRCUIT_OPEN"
    FAILURE_UNKNOWN = "FAILURE_UNKNOWN"


class ProviderFailureClass(str, Enum):
    NOT_APPLICABLE = "NOT_APPLICABLE"
    TIMEOUT = "TIMEOUT"
    HTTP = "HTTP"
    NETWORK = "NETWORK"
    SCHEMA = "SCHEMA"
    NO_MEDIA = "NO_MEDIA"
    RESULT_INVALID = "RESULT_INVALID"
    RATE_LIMIT = "RATE_LIMIT"
    CONCURRENCY = "CONCURRENCY"
    CIRCUIT_OPEN = "CIRCUIT_OPEN"
    UNKNOWN = "UNKNOWN"


class ProviderResultInvalidError(ValueError):
    pass


def _bounded_integer(value, maximum):
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return min(max(0, parsed), maximum)


def _enum_value(value, enum_type, default):
    if isinstance(value, enum_type):
        return value.value
    try:
        return enum_type(str(value or "")).value
    except ValueError:
        return default.value


def safe_provider_name(value):
    normalized = re.sub(r"[^a-z0-9_-]+", "_", str(value or "unknown").lower())
    return normalized.strip("_")[:64] or "unknown"


def safe_media_host(value):
    text = str(value or "").strip()
    try:
        hostname = (
            urlsplit(text).hostname
            if "://" in text
            else text
        )
    except ValueError:
        return "unknown"
    hostname = str(hostname or "").lower().rstrip(".")
    if not HOSTNAME_PATTERN.fullmatch(hostname):
        return "unknown"
    labels = hostname.split(".")
    if any(
        not label
        or len(label) > 63
        or label.startswith("-")
        or label.endswith("-")
        for label in labels
    ):
        return "unknown"
    return hostname


def normalize_provider_failure_class(value):
    try:
        return ProviderFailureClass(str(getattr(value, "value", value) or ""))
    except ValueError:
        return ProviderFailureClass.UNKNOWN


def provider_continuation_active(context, *, now_monotonic=None):
    if not context or not bool(getattr(context, "allow_continuation", False)):
        return False
    foreground_deadline = float(
        getattr(context, "deadline_monotonic", 0) or 0
    )
    if foreground_deadline <= 0:
        return False
    current = time.monotonic() if now_monotonic is None else float(now_monotonic)
    return current >= foreground_deadline


def _classify_provider_failure(error, seen):
    error_id = id(error)
    if error_id in seen:
        return ProviderOutcome.FAILURE_UNKNOWN, ProviderFailureClass.UNKNOWN
    seen.add(error_id)

    if isinstance(error, ProviderResultInvalidError):
        return ProviderOutcome.FAILURE_RESULT_INVALID, ProviderFailureClass.RESULT_INVALID
    if isinstance(error, DouyinUltraAttemptRateLimitedError):
        return ProviderOutcome.FAILURE_RATE_LIMIT, ProviderFailureClass.RATE_LIMIT
    if isinstance(error, DouyinUltraAttemptSupersededError):
        return ProviderOutcome.SUCCESS_EXISTING_RESULT, ProviderFailureClass.NOT_APPLICABLE
    if isinstance(error, DouyinUltraAttemptAccountingError):
        return ProviderOutcome.FAILURE_UNKNOWN, ProviderFailureClass.UNKNOWN
    declared_outcome = getattr(error, "provider_outcome", None)
    declared_failure_class = getattr(error, "provider_failure_class", None)
    if declared_outcome is not None or declared_failure_class is not None:
        try:
            outcome = ProviderOutcome(
                str(getattr(declared_outcome, "value", declared_outcome) or "")
            )
        except ValueError:
            outcome = ProviderOutcome.FAILURE_UNKNOWN
        failure_class = normalize_provider_failure_class(declared_failure_class)
        return outcome, failure_class
    if isinstance(error, (XhsProviderTimeoutError, requests.Timeout, TimeoutError)):
        return ProviderOutcome.FAILURE_TIMEOUT, ProviderFailureClass.TIMEOUT
    if isinstance(error, requests.HTTPError):
        return ProviderOutcome.FAILURE_HTTP, ProviderFailureClass.HTTP
    if isinstance(error, requests.RequestException):
        return ProviderOutcome.FAILURE_NETWORK, ProviderFailureClass.NETWORK
    cause = getattr(error, "__cause__", None)
    if isinstance(cause, BaseException):
        cause_outcome, cause_failure_class = _classify_provider_failure(
            cause,
            seen,
        )
        if cause_failure_class != ProviderFailureClass.UNKNOWN:
            return cause_outcome, cause_failure_class
    if isinstance(error, (TypeError, ValueError)):
        return ProviderOutcome.FAILURE_SCHEMA, ProviderFailureClass.SCHEMA
    return ProviderOutcome.FAILURE_UNKNOWN, ProviderFailureClass.UNKNOWN


def classify_provider_failure(error):
    return _classify_provider_failure(error, set())


def record_provider_attempt(
    *,
    provider,
    outcome,
    external_call=False,
    cache_hit=False,
    cache_bypassed=False,
    single_flight_wait=False,
    continuation=False,
    attempt_consumed=False,
    elapsed_ms=0,
    payload_age_ms=0,
    retry_after_seconds=0,
    candidate_count=0,
    media_host="",
    expected_bytes=0,
    failure_class=ProviderFailureClass.NOT_APPLICABLE,
    diagnostic_detail="",
):
    from .provider_lab.contracts import safe_error_detail

    fields = {
        "event": "provider_attempt_completed",
        "provider": safe_provider_name(provider),
        "outcome": _enum_value(
            outcome,
            ProviderOutcome,
            ProviderOutcome.FAILURE_UNKNOWN,
        ),
        "external_call": bool(external_call),
        "cache_hit": bool(cache_hit),
        "cache_bypassed": bool(cache_bypassed),
        "single_flight_wait": bool(single_flight_wait),
        "continuation": bool(continuation),
        "attempt_consumed": bool(attempt_consumed),
        "elapsed_ms": _bounded_integer(elapsed_ms, MAX_DURATION_MS),
        "payload_age_ms": _bounded_integer(payload_age_ms, MAX_DURATION_MS),
        "retry_after_seconds": _bounded_integer(retry_after_seconds, 24 * 60 * 60),
        "candidate_count": _bounded_integer(candidate_count, 10000),
        "media_host": safe_media_host(media_host),
        "expected_bytes": _bounded_integer(expected_bytes, MAX_MEDIA_BYTES),
        "failure_class": _enum_value(
            failure_class,
            ProviderFailureClass,
            ProviderFailureClass.UNKNOWN,
        ),
    }
    detail = safe_error_detail(diagnostic_detail)
    message = "provider_attempt_completed"
    if detail:
        message += f" diagnostic_detail={detail}"
    logger.info(message, extra=fields)
    return fields
