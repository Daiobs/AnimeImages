from django.utils import timezone

from ..models import RoutingDecisionLog
from .download_routing import (
    ROUTE_BLOCKED,
    ROUTE_DIRECT,
    ROUTE_PROXY,
    ROUTE_ZIP,
    decide_download_route,
    item_provider,
    item_type,
    provider_item_requires_server,
    safe_download_size,
)


STRATEGY_VERSION = "v3"
DEFAULT_ROUTE_SCORE = 0.75
HIGH_SERVER_BYTES_THRESHOLD = 300 * 1024 * 1024
PROVIDER_SCORES = {
    "native": 0.9,
    "xiaohongshu": 0.9,
    "douyin": 0.9,
    "bilibili": 0.85,
    "yt_dlp_generic": 0.72,
    "gallery_dl_generic": 0.78,
    "kukutool": 0.7,
    "kukutool_generic": 0.68,
    "youtube_merge": 0.6,
    "yt_dlp_merge": 0.62,
}


def clamp_score(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        value = DEFAULT_ROUTE_SCORE
    return max(0.0, min(1.0, value))


def infer_provider(media_items, context=None):
    context = context or {}
    if context.get("provider"):
        return str(context["provider"])
    for item in media_items or ():
        if not isinstance(item, dict):
            continue
        provider = item_provider(item)
        if provider:
            return provider
        platform = str(item.get("source_platform") or item.get("platform") or "")
        if platform:
            return platform
    return "native"


def provider_score(provider):
    return PROVIDER_SCORES.get(str(provider or "native"), 0.75)


def route_logs_for(route_type, provider="", limit=100):
    queryset = RoutingDecisionLog.objects.filter(route_type=route_type)
    if provider:
        queryset = queryset.filter(provider=provider)
    return list(queryset.order_by("-created_at")[:limit])


def compute_routing_score(route_type, logs):
    logs = list(logs or ())
    if not logs:
        return DEFAULT_ROUTE_SCORE

    total = len(logs)
    success_count = sum(1 for log in logs if bool(getattr(log, "success", False)))
    success_rate = success_count / total if total else 0
    cost_accuracy_scores = []
    duration_scores = []

    for log in logs:
        estimated = safe_download_size(getattr(log, "estimated_server_bytes", 0))
        actual = safe_download_size(getattr(log, "actual_server_bytes", 0))
        if estimated <= 0 and actual <= 0:
            cost_accuracy_scores.append(1.0)
        elif estimated <= 0 or actual <= 0:
            cost_accuracy_scores.append(0.6)
        else:
            delta_ratio = abs(actual - estimated) / max(estimated, actual)
            cost_accuracy_scores.append(max(0.0, 1.0 - delta_ratio))

        duration_ms = safe_download_size(getattr(log, "duration_ms", 0))
        if duration_ms <= 0:
            duration_scores.append(0.85)
        elif duration_ms <= 3000:
            duration_scores.append(1.0)
        elif duration_ms <= 10000:
            duration_scores.append(0.75)
        else:
            duration_scores.append(0.45)

    cost_accuracy = sum(cost_accuracy_scores) / total
    duration_score = sum(duration_scores) / total
    route_bias = {
        ROUTE_DIRECT: 0.04,
        ROUTE_PROXY: 0.0,
        ROUTE_ZIP: -0.02,
        ROUTE_BLOCKED: -0.15,
    }.get(route_type, 0)
    return clamp_score(
        success_rate * 0.55
        + cost_accuracy * 0.30
        + duration_score * 0.15
        + route_bias
    )


def cost_aware_route_override(v2_route, media_items):
    if v2_route.get("route_type") != ROUTE_ZIP:
        return v2_route
    if len(media_items or ()) != 1:
        return v2_route
    item = media_items[0]
    if not isinstance(item, dict) or provider_item_requires_server(item):
        return v2_route
    estimated_server = safe_download_size(v2_route.get("server_egress_estimated_bytes"))
    if estimated_server < HIGH_SERVER_BYTES_THRESHOLD:
        return v2_route

    adjusted = dict(v2_route)
    adjusted["route_type"] = ROUTE_PROXY
    adjusted["route_reason"] = "预计服务器 ZIP 成本较高，单资源优先代理下载以降低打包开销"
    adjusted["download_strategy"] = "adaptive_proxy_high_cost"
    return adjusted


def confidence_for(route_type, route_score, provider_quality, estimated_server_bytes):
    confidence = route_score * 0.55 + provider_quality * 0.35
    if route_type == ROUTE_DIRECT:
        confidence += 0.08
    if route_type == ROUTE_ZIP and estimated_server_bytes > HIGH_SERVER_BYTES_THRESHOLD:
        confidence -= 0.12
    if route_type == ROUTE_BLOCKED:
        confidence = 1.0
    return clamp_score(confidence)


def decide_route_v3(request, media_items, context=None):
    context = context or {}
    media_items = [item for item in media_items or () if isinstance(item, dict)]
    requested_mode = context.get("requested_mode", "")
    platform_key = context.get("platform_key", "")
    v2_route = decide_download_route(
        request,
        media_items,
        requested_mode=requested_mode,
        platform_key=platform_key,
    )
    adjusted_route = cost_aware_route_override(v2_route, media_items)
    provider = infer_provider(media_items, context)
    recent_logs = route_logs_for(adjusted_route["route_type"], provider=provider)
    route_score = compute_routing_score(adjusted_route["route_type"], recent_logs)
    provider_quality = provider_score(provider)
    estimated_server = safe_download_size(
        adjusted_route.get("server_egress_estimated_bytes")
    )
    estimated_browser = safe_download_size(
        adjusted_route.get("browser_egress_estimated_bytes")
    )

    return {
        "route_type": adjusted_route["route_type"],
        "reason": adjusted_route.get("route_reason", ""),
        "confidence": confidence_for(
            adjusted_route["route_type"],
            route_score,
            provider_quality,
            estimated_server,
        ),
        "estimated_cost": {
            "server_bytes": estimated_server,
            "browser_bytes": estimated_browser,
        },
        "routing_score": route_score,
        "strategy_version": STRATEGY_VERSION,
        "provider": provider,
        "provider_score": provider_quality,
        "v2_route_type": v2_route.get("route_type", ""),
        "download_strategy": adjusted_route.get("download_strategy", ""),
        "error_code": adjusted_route.get("error_code", ""),
        "warnings": adjusted_route.get("warnings", []),
    }


def build_routing_intelligence_snapshot(hours=24):
    since = timezone.now() - timezone.timedelta(hours=hours)
    queryset = RoutingDecisionLog.objects.filter(created_at__gte=since)
    route_types = (ROUTE_DIRECT, ROUTE_PROXY, ROUTE_ZIP, ROUTE_BLOCKED)
    rows = []
    for route_type in route_types:
        logs = list(queryset.filter(route_type=route_type))
        total = len(logs)
        success_count = sum(1 for log in logs if log.success)
        estimated = sum(safe_download_size(log.estimated_server_bytes) for log in logs)
        actual = sum(safe_download_size(log.actual_server_bytes) for log in logs)
        cost_delta = actual - estimated
        rows.append(
            {
                "route_type": route_type,
                "count": total,
                "success_rate": round((success_count / total) * 100, 1) if total else 0,
                "estimated_server_bytes": estimated,
                "actual_server_bytes": actual,
                "cost_delta_bytes": cost_delta,
                "routing_score": round(compute_routing_score(route_type, logs), 3),
            }
        )
    return {
        "has_data": queryset.exists(),
        "rows": rows,
        "routing_efficiency_score": round(
            sum(row["routing_score"] for row in rows) / len(rows),
            3,
        ),
        "cost_accuracy_score": round(
            compute_routing_score(
                ROUTE_ZIP,
                queryset.exclude(estimated_server_bytes=0).order_by("-created_at")[:100],
            ),
            3,
        ),
    }
