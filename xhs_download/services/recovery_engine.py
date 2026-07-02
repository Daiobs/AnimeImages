import logging
from dataclasses import dataclass

import requests

from .common import XhsDownloadError, XhsProviderTimeoutError
from .download_routing import (
    ROUTE_BLOCKED,
    ROUTE_DIRECT,
    ROUTE_PROXY,
    ROUTE_ZIP,
    estimate_media_items_bytes,
    item_is_video,
    route_payload,
    single_item_can_attachment_download,
    single_video_can_server_proxy_download,
    single_video_can_browser_direct_attempt,
)
from .observability import sanitize_observability_text


logger = logging.getLogger(__name__)

FAILURE_PROVIDER_TIMEOUT = "provider_timeout"
FAILURE_PROVIDER_UNAVAILABLE = "provider_unavailable"
FAILURE_MERGE_FAILED = "merge_failed"
FAILURE_ZIP_FAILED = "zip_failed"
FAILURE_PROXY_FAILED = "proxy_failed"
FAILURE_DIRECT_FAILED = "direct_failed"
FAILURE_COST_BLOCKED = "cost_blocked"
FAILURE_ROUTING_FAILED = "routing_failed"
FAILURE_UNKNOWN = "unknown"

RECOVERY_ACTION_NONE = "none"
RECOVERY_ACTION_SAFE_ZIP_FALLBACK = "routing_safe_zip_fallback"
RECOVERY_ACTION_ZIP_TO_PROXY = "zip_to_proxy"
RECOVERY_ACTION_PROXY_TO_DIRECT = "proxy_to_direct"
RECOVERY_ACTION_BLOCKED = "blocked"


@dataclass(frozen=True)
class RecoveryDecision:
    action: str
    failure_type: str
    fallback_from_route: str = ""
    fallback_to_route: str = ""
    reason: str = ""
    route_info: dict | None = None

    @property
    def recovered(self):
        return self.action not in {"", RECOVERY_ACTION_NONE}

    @property
    def fallback_applied(self):
        return bool(
            self.fallback_from_route
            and self.fallback_to_route
            and self.fallback_from_route != self.fallback_to_route
        )

    def log_fields(self):
        return {
            "recovery_count": 1 if self.recovered else 0,
            "fallback_count": 1 if self.fallback_applied else 0,
            "recovery_action": self.action,
            "recovery_reason": self.reason,
            "fallback_from_route": self.fallback_from_route,
            "fallback_to_route": self.fallback_to_route,
            "failure_type": self.failure_type,
        }


def classify_failure(error=None, *, error_code="", route_type="", provider=""):
    code = str(error_code or "").upper()
    provider = str(provider or "")
    message = str(error or "")
    lowered = message.lower()

    if code in {
        "VISITOR_VIDEO_DOWNLOAD_DISABLED",
        "SINGLE_VIDEO_SIZE_LIMIT",
        "SINGLE_ARCHIVE_SIZE_LIMIT",
        "DAILY_BYTES_LIMIT",
        "GLOBAL_DAILY_BYTES_LIMIT",
        "DOWNLOAD_CONCURRENCY_LIMIT",
    }:
        return FAILURE_COST_BLOCKED

    if isinstance(error, XhsProviderTimeoutError) or "timeout" in lowered or "timed out" in lowered:
        return FAILURE_PROVIDER_TIMEOUT if provider else FAILURE_PROXY_FAILED

    if provider and (
        "provider" in lowered
        or "manifest" in lowered
        or "signed" in lowered
        or "403" in lowered
        or "404" in lowered
    ):
        return FAILURE_PROVIDER_UNAVAILABLE

    if "merge" in lowered or "ffmpeg" in lowered:
        return FAILURE_MERGE_FAILED

    if route_type == ROUTE_ZIP or "zip" in lowered or "没有资源保存成功" in message:
        return FAILURE_ZIP_FAILED

    if route_type == ROUTE_PROXY or isinstance(error, requests.RequestException):
        return FAILURE_PROXY_FAILED

    if route_type == ROUTE_DIRECT:
        return FAILURE_DIRECT_FAILED

    if isinstance(error, XhsDownloadError):
        return FAILURE_PROXY_FAILED

    return FAILURE_UNKNOWN


def recovery_log_fields(decision=None, **fields):
    if decision is not None:
        return decision.log_fields()
    return {
        "recovery_count": 1 if fields.get("recovery_action") else 0,
        "fallback_count": 1 if fields.get("fallback_from_route") and fields.get("fallback_to_route") else 0,
        "recovery_action": fields.get("recovery_action", ""),
        "recovery_reason": fields.get("recovery_reason", ""),
        "fallback_from_route": fields.get("fallback_from_route", ""),
        "fallback_to_route": fields.get("fallback_to_route", ""),
        "failure_type": fields.get("failure_type", ""),
    }


def safe_zip_route(request, media_items, *, reason, error=None):
    normalized_items = [item for item in media_items or () if isinstance(item, dict)]
    failure_type = FAILURE_ROUTING_FAILED
    route_info = route_payload(
        route_type=ROUTE_ZIP,
        route_reason=reason,
        download_strategy="server_zip_safe_fallback",
        media_items=normalized_items,
        estimated_bytes=estimate_media_items_bytes(normalized_items),
        warnings=[],
    )
    route_info.update(
        strategy_version="safe_wrapper",
        confidence=0,
        routing_score=0,
        recovery=RecoveryDecision(
            action=RECOVERY_ACTION_SAFE_ZIP_FALLBACK,
            failure_type=failure_type,
            fallback_from_route="routing",
            fallback_to_route=ROUTE_ZIP,
            reason=sanitize_observability_text(reason, limit=200),
        ).log_fields(),
    )
    logger.warning(
        "routing_safe_fallback",
        extra={
            "failure_type": failure_type,
            "fallback_from_route": "routing",
            "fallback_to_route": ROUTE_ZIP,
            "error_type": type(error).__name__ if error else "",
            "error": sanitize_observability_text(error),
        },
    )
    return route_info


def safe_routing_decision(request, media_items, route_callable, *, reason="路由决策异常，已降级为 ZIP 下载"):
    try:
        route_info = route_callable()
        if not isinstance(route_info, dict) or not route_info.get("route_type"):
            raise ValueError("routing returned invalid payload")
        return route_info
    except Exception as error:
        return safe_zip_route(request, media_items, reason=reason, error=error)


def zip_failure_recovery(media_items, route_info, error):
    normalized_items = [item for item in media_items or () if isinstance(item, dict)]
    reason = sanitize_observability_text(error, limit=200)
    provider = normalized_items[0].get("provider", "") if normalized_items else ""
    failure_type = classify_failure(error, route_type=ROUTE_ZIP, provider=provider)
    if len(normalized_items) == 1 and (
        single_item_can_attachment_download(normalized_items[0])
        or single_video_can_server_proxy_download(normalized_items[0])
    ):
        recovered_route = dict(route_info or {})
        recovered_route.update(
            route_type=ROUTE_PROXY,
            route_reason="ZIP 打包失败，已降级为单文件下载",
            download_strategy="server_single_attachment_recovery",
            server_egress_estimated_bytes=estimate_media_items_bytes(normalized_items),
            browser_egress_estimated_bytes=0,
            items=route_payload(
                route_type=ROUTE_PROXY,
                route_reason="ZIP 打包失败，已降级为单文件下载",
                download_strategy="server_single_attachment_recovery",
                media_items=normalized_items,
                estimated_bytes=estimate_media_items_bytes(normalized_items),
            )["items"],
        )
        return RecoveryDecision(
            action=RECOVERY_ACTION_ZIP_TO_PROXY,
            failure_type=failure_type,
            fallback_from_route=ROUTE_ZIP,
            fallback_to_route=ROUTE_PROXY,
            reason=reason,
            route_info=recovered_route,
        )
    return RecoveryDecision(
        action=RECOVERY_ACTION_BLOCKED,
        failure_type=failure_type,
        fallback_from_route=ROUTE_ZIP,
        fallback_to_route=ROUTE_BLOCKED,
        reason=reason,
    )


def proxy_failure_recovery(media_items, route_info, error):
    normalized_items = [item for item in media_items or () if isinstance(item, dict)]
    reason = sanitize_observability_text(error, limit=200)
    provider = normalized_items[0].get("provider", "") if normalized_items else ""
    failure_type = classify_failure(error, route_type=ROUTE_PROXY, provider=provider)
    if len(normalized_items) == 1 and single_video_can_browser_direct_attempt(normalized_items[0]):
        recovered_route = dict(route_info or {})
        recovered_route.update(
            route_type=ROUTE_DIRECT,
            route_reason="单文件代理失败，已降级为浏览器直链下载",
            download_strategy="browser_direct_recovery",
            server_egress_estimated_bytes=0,
            browser_egress_estimated_bytes=estimate_media_items_bytes(normalized_items),
            items=route_payload(
                route_type=ROUTE_DIRECT,
                route_reason="单文件代理失败，已降级为浏览器直链下载",
                download_strategy="browser_direct_recovery",
                media_items=normalized_items,
                estimated_bytes=estimate_media_items_bytes(normalized_items),
            )["items"],
        )
        return RecoveryDecision(
            action=RECOVERY_ACTION_PROXY_TO_DIRECT,
            failure_type=failure_type,
            fallback_from_route=ROUTE_PROXY,
            fallback_to_route=ROUTE_DIRECT,
            reason=reason,
            route_info=recovered_route,
        )
    return RecoveryDecision(
        action=RECOVERY_ACTION_BLOCKED,
        failure_type=failure_type,
        fallback_from_route=ROUTE_PROXY,
        fallback_to_route=ROUTE_BLOCKED,
        reason=reason,
    )


def provider_stability_status(score):
    if score >= 0.8:
        return "stable"
    if score >= 0.55:
        return "degraded"
    return "unstable"


def provider_stability_score(*, success_rate, timeout_rate, failure_rate, average_latency_ms):
    latency_penalty = min(max(float(average_latency_ms or 0) / 15000.0, 0), 1)
    score = (
        float(success_rate or 0) * 0.55
        + (1 - float(timeout_rate or 0)) * 0.2
        + (1 - float(failure_rate or 0)) * 0.15
        + (1 - latency_penalty) * 0.1
    )
    return round(min(max(score, 0), 1), 3)
