"""Independent, request-authorized XHS service candidates with Native fallback."""

from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import replace
import time

from ..common import XhsParseError
from ..quality_execution import execute_provider, service_configured

SERVICE_ORDER = ("kukutool_generic", "tiantianjiexi_generic")


def authorized_services(request, source_url, *, reserve_limits=True):
    """Resolve server-side access only; URL/form options cannot grant authority."""
    from urllib.parse import urlsplit
    from django.conf import settings
    from .xiaohongshu import XiaohongshuAdapter
    from ..common import extract_url_from_text
    from ..provider_access import (
        PROVIDER_THIRD_PARTY, load_runtime_setting, provider_access_summary,
        request_user_is_staff, third_party_platform_is_blocked,
        upstream_match_is_blocked, upstream_match_is_public_allowed,
    )
    from ..rate_limits import consume_fixed_window, get_client_identity
    from ..env_config import runtime_default_int

    source = extract_url_from_text(source_url)
    if not XiaohongshuAdapter().supports_url(source):
        return ()
    config = load_runtime_setting()
    if not config or not provider_access_summary(PROVIDER_THIRD_PARTY, request, config)["request_allowed"]:
        return ()
    host = urlsplit(source).hostname
    if third_party_platform_is_blocked("xiaohongshu", host, config):
        return ()
    providers = []
    for provider in SERVICE_ORDER:
        match = {"provider_id": provider, "extractor_id": provider}
        if (not upstream_match_is_blocked(match, config)
                and (request_user_is_staff(request)
                     or upstream_match_is_public_allowed(match, host, config))
                and service_configured(provider)):
            providers.append(provider)
    if not providers:
        return ()
    if not reserve_limits:
        return tuple(providers)
    # Reuse the ordinary service limits; throttling must not disable Native.
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
    return tuple(providers)


def parse_independent(url, *, native_parse, provider_ids, deadline_monotonic=None,
                      timeout_seconds=None):
    providers = tuple(pid for pid in SERVICE_ORDER if pid in provider_ids and service_configured(pid))
    started = time.monotonic()
    deadline = min(float(deadline_monotonic or started + 30),
                   started + max(0, float(30 if timeout_seconds is None else timeout_seconds)))
    if deadline <= time.monotonic():
        raise XhsParseError("小红书解析超时，请稍后再试。")
    if not providers:
        return native_parse(deadline)

    def service_parse():
        for provider in providers:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                result = execute_provider(provider, url, timeout_seconds=min(20, remaining),
                                          service_authorized=True)
                if result.assets or (result.metadata or {}).get("upstream_candidates"):
                    return result
            except Exception:
                # Never expose signed URLs or service exceptions to the public page.
                continue
        return None

    executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="xhs-independent")
    native = executor.submit(native_parse, deadline)
    service = executor.submit(service_parse)
    try:
        done, _ = wait((native, service), timeout=max(0, deadline - time.monotonic()))
        native_result, native_error = None, None
        if native in done:
            try:
                native_result = native.result()
            except Exception as error:
                native_error = error
        service_result = service.result() if service in done else None
        # Only Native's exact item snapshot can establish a LivePhoto pair.
        selected = (native_result if native_result is not None and (
                        native_result.rich_media is not None
                        or (native_result.assets and (service_result is None or not service_result.assets)))
                    else service_result or native_result)
        if selected is not None:
            metadata = dict(selected.metadata or {})
            if selected is native_result and service_result is not None:
                # Candidate-only service evidence must not remove usable Native
                # downloads or borrow their download authority/provider identity.
                metadata["upstream_candidates"] = (service_result.metadata or {}).get("upstream_candidates", [])
                for key in ("candidate_count", "candidate_returned_count", "candidate_retained_count",
                            "candidate_unexpanded_count", "returned_count", "retained_count",
                            "declared_count", "examined_count", "rejected_count", "unexpanded_count",
                            "truncated_count", "candidates_truncated"):
                    if key in service_result.metadata:
                        metadata[key] = service_result.metadata[key]
            if selected is service_result:
                # Image candidates alone are not evidence of an undeliverable video.
                metadata["limited_media_count"] = sum(
                    item.get("media_type") == "video"
                    for item in metadata.get("upstream_candidates", [])
                )
            return replace(selected, metadata={**metadata,
                "xhs_independent_source": "service" if selected is service_result else "native",
                "xhs_native_status": "failed" if native_error else "completed" if native in done else "deadline",
                "xhs_service_status": "candidates" if service_result else "unavailable" if service in done else "deadline"})
        if native_error is not None:
            raise native_error
        raise XhsParseError("小红书解析超时，请稍后再试。")
    finally:
        native.cancel()
        service.cancel()
        executor.shutdown(wait=False, cancel_futures=True)
