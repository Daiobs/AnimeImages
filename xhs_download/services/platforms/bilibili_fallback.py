"""Request-authorized availability fallback for ordinary Bilibili videos."""

from dataclasses import replace
import logging
import math
import re
import time
from urllib.parse import parse_qs, urlsplit

import requests

from ..common import XhsDownloadError, XhsProviderTimeoutError, extract_url_from_text
from .tiantianjiexi_generic import TianTianJieXiGenericProvider, provider_configured

logger = logging.getLogger(__name__)
PROVIDER = "tiantianjiexi_generic"


def canonical_video_source(value):
    try:
        parsed = urlsplit(extract_url_from_text(value))
        port = parsed.port
    except ValueError:
        return ""
    if (parsed.scheme not in {"http", "https"}
            or parsed.hostname not in {"www.bilibili.com", "bilibili.com"}
            or parsed.username or parsed.password or port not in {None, 80, 443}):
        return ""
    match = re.fullmatch(r"/video/(BV[0-9A-Za-z]{10}|av[0-9]+)/?", parsed.path)
    # Service evidence currently covers a single first-part video, not collections.
    if not match or parse_qs(parsed.query).get("p", ["1"]) != ["1"]:
        return ""
    return f"https://www.bilibili.com/video/{match[1]}/"


def authorized_services(request, source_url, *, reserve_limits=True):
    """Do not grant a service access merely because Native accepts the source."""
    from django.conf import settings
    from ..env_config import runtime_default_int
    from ..provider_access import (
        PROVIDER_THIRD_PARTY, load_runtime_setting, provider_access_summary,
        request_user_is_staff, third_party_platform_is_blocked,
        upstream_match_is_blocked, upstream_match_is_public_allowed,
    )
    from ..rate_limits import consume_fixed_window, get_client_identity

    source = canonical_video_source(source_url)
    if not source or not provider_configured():
        return ()
    config = load_runtime_setting()
    if not config or not provider_access_summary(PROVIDER_THIRD_PARTY, request, config)["request_allowed"]:
        return ()
    match = {"provider_id": PROVIDER, "extractor_id": PROVIDER}
    host = urlsplit(source).hostname
    if (third_party_platform_is_blocked("bilibili", host, config)
            or upstream_match_is_blocked(match, config)
            or not (request_user_is_staff(request)
                    or upstream_match_is_public_allowed(match, host, config))):
        return ()
    if reserve_limits:
        try:
            for scope, identity, limit in (
                ("third-party-provider-minute-ip", get_client_identity(request),
                 runtime_default_int("THIRD_PARTY_RATE_LIMIT_PER_MINUTE", 10)),
                ("third-party-provider-minute-global", "all",
                 settings.THIRD_PARTY_GLOBAL_REQUEST_LIMIT_PER_MINUTE),
            ):
                if not consume_fixed_window(scope, identity, limit, 60).allowed:
                    return ()
        except Exception:
            return ()
    return (PROVIDER,)


def eligible_failure(error):
    if isinstance(error, requests.exceptions.SSLError):
        return False
    if isinstance(error, (XhsProviderTimeoutError, requests.Timeout, requests.ConnectionError)):
        return True
    status = getattr(error, "http_status", None)
    return isinstance(error, XhsDownloadError) and (
        status in {412, 429} or isinstance(status, int) and 500 <= status < 600
    )


def parse_fallback(source_url, *, native_parse, authorize,
                   timeout_seconds=None, deadline_monotonic=None):
    source = canonical_video_source(source_url)
    now = time.monotonic()
    budget = float(30 if timeout_seconds is None else timeout_seconds)
    deadline = min(float(deadline_monotonic or now + budget), now + budget)
    if not math.isfinite(deadline) or not math.isfinite(budget) or deadline <= now:
        raise XhsProviderTimeoutError("哔哩哔哩解析超过时间限制")
    if not source:
        return native_parse(deadline)
    # Native remains first. Reserve at most 15 seconds for one authorized service,
    # without extending the caller's deadline or starting a parallel request.
    native_deadline = deadline - min(15, (deadline - now) / 2)
    try:
        return native_parse(native_deadline)
    except Exception as native_error:
        if not eligible_failure(native_error):
            raise
        remaining = deadline - time.monotonic()
        if remaining < .1 or PROVIDER not in authorize():
            raise
        remaining = deadline - time.monotonic()
        if remaining < .1:
            raise
        try:
            from .bilibili_service_result import build_service_result
            payload = TianTianJieXiGenericProvider().parse(
                source, timeout_seconds=remaining, deadline_monotonic=deadline)
            if time.monotonic() >= deadline:
                raise XhsProviderTimeoutError("哔哩哔哩解析超过时间限制")
            result = build_service_result(payload, source)
        except Exception as error:
            logger.info("bilibili_fallback_unavailable provider=%s error_type=%s",
                        PROVIDER, type(error).__name__)
            raise native_error from None
        return replace(result, metadata={
            **result.metadata,
            "selected_provider": PROVIDER,
            "source_provider": PROVIDER,
            "bilibili_native_status": "unavailable",
            "bilibili_native_http_status": getattr(native_error, "http_status", None),
            "provider_fallback_count": 1,
        })
