from django.core.cache import cache
from django.utils import timezone

from ..models import DownloadRequestLog, ObservabilityStatus
from ..services.download_audit import operational_download_logs
from ..services.download_routing import ROUTE_DIRECT, ROUTE_PROXY, ROUTE_ZIP, safe_download_size
from .learning import build_policy_learning_snapshot


BASE_WEIGHTS = {
    "cost": 0.4,
    "provider": 0.4,
    "routing": 0.2,
}
ADAPTIVE_WEIGHT_CACHE_KEY = "policy:v4:adaptive_weights:{window_hours}"


def normalize_weights(weights):
    total = sum(max(0.0, float(value or 0)) for value in weights.values())
    if total <= 0:
        return dict(BASE_WEIGHTS)
    return {
        key: round(max(0.0, float(value or 0)) / total, 4)
        for key, value in weights.items()
    }


def safe_ratio(numerator, denominator):
    numerator = safe_download_size(numerator)
    denominator = safe_download_size(denominator)
    if denominator <= 0:
        return 0.0
    return round(numerator / denominator, 4)


def build_policy_feedback_summary(window_hours=24):
    window_hours = int(window_hours or 24)
    now = timezone.now()
    since = now - timezone.timedelta(hours=window_hours)
    logs = list(
        operational_download_logs(DownloadRequestLog.objects.filter(created_at__gte=since)).exclude(
            final_policy_route=""
        )
    )
    rows_by_recommended = {}
    rows_by_provider = {}
    matched = 0
    active = 0
    success = 0
    total_duration = 0
    total_server_bytes = 0
    for log in logs:
        recommended = str(log.final_policy_route or "unknown")
        actual = str(log.route_type or "unknown")
        provider = str(log.provider or "unknown")
        was_success = log.status == ObservabilityStatus.SUCCESS
        matched += int(recommended == actual)
        active += int(bool(log.policy_active))
        success += int(was_success)
        total_duration += safe_download_size(log.duration_ms)
        total_server_bytes += safe_download_size(log.actual_bytes)

        route_row = rows_by_recommended.setdefault(
            recommended,
            {
                "count": 0,
                "matched": 0,
                "success": 0,
                "duration_ms": 0,
                "actual_server_bytes": 0,
            },
        )
        route_row["count"] += 1
        route_row["matched"] += int(recommended == actual)
        route_row["success"] += int(was_success)
        route_row["duration_ms"] += safe_download_size(log.duration_ms)
        route_row["actual_server_bytes"] += safe_download_size(log.actual_bytes)

        provider_row = rows_by_provider.setdefault(
            provider,
            {"count": 0, "failed": 0},
        )
        provider_row["count"] += 1
        provider_row["failed"] += int(not was_success)

    total = len(logs)
    return {
        "window_hours": window_hours,
        "policy_feedback_count": total,
        "policy_active_count": active,
        "recommendation_match_rate": safe_ratio(matched, total),
        "success_rate": safe_ratio(success, total),
        "average_duration_ms": safe_ratio(total_duration, total),
        "average_server_bytes": safe_ratio(total_server_bytes, total),
        "by_recommended_route": rows_by_recommended,
        "by_provider": rows_by_provider,
    }


def route_success_rate(route_stats):
    return safe_ratio(route_stats.get("success"), route_stats.get("count"))


def provider_failure_rate(provider_stats):
    return safe_ratio(provider_stats.get("failed"), provider_stats.get("count"))


def build_adaptive_weight_recommendation(window_hours=24):
    snapshot = build_policy_learning_snapshot(window_hours=window_hours)
    route_distribution = snapshot["route_distribution"]
    provider_distribution = snapshot["provider_distribution"]
    feedback_summary = build_policy_feedback_summary(window_hours=window_hours)
    cost_delta = int(snapshot["cost_summary"].get("delta_bytes") or 0)
    sample_count = int(snapshot["sample_count"] or 0)
    weights = dict(BASE_WEIGHTS)
    reasons = ["policy-v4-shadow-adaptive-weights"]

    if cost_delta > 0:
        weights["cost"] += 0.1
        weights["routing"] -= 0.05
        reasons.append("server-cost-delta-positive")

    provider_count = sum(int(value or 0) for value in provider_distribution.values())
    if provider_count and len(provider_distribution) > 1:
        weights["provider"] += 0.05
        weights["routing"] -= 0.03
        reasons.append("multiple-providers-observed")

    provider_failures = [
        provider
        for provider, stats in feedback_summary["by_provider"].items()
        if int(stats.get("count") or 0) >= 3 and provider_failure_rate(stats) >= 0.25
    ]
    if provider_failures:
        weights["provider"] -= 0.08
        weights["routing"] += 0.04
        weights["cost"] += 0.04
        reasons.append("provider-failure-rate-high")

    direct_stats = feedback_summary["by_recommended_route"].get(ROUTE_DIRECT, {})
    if int(direct_stats.get("count") or 0) >= 3 and route_success_rate(direct_stats) >= 0.9:
        weights["cost"] += 0.06
        weights["routing"] -= 0.03
        reasons.append("direct-route-feedback-successful")

    zip_stats = feedback_summary["by_recommended_route"].get(ROUTE_ZIP, {})
    if int(zip_stats.get("count") or 0) >= 3:
        average_zip_bytes = safe_ratio(
            zip_stats.get("actual_server_bytes"),
            zip_stats.get("count"),
        )
        if average_zip_bytes >= 200 * 1024 * 1024:
            weights["cost"] += 0.08
            weights["routing"] -= 0.04
            reasons.append("zip-server-cost-high")

    proxy_stats = feedback_summary["by_recommended_route"].get(ROUTE_PROXY, {})
    if int(proxy_stats.get("count") or 0) >= 3 and route_success_rate(proxy_stats) < 0.75:
        weights["routing"] += 0.06
        weights["cost"] -= 0.03
        reasons.append("proxy-feedback-needs-routing-fallback")

    failed_routes = [
        route
        for route, stats in route_distribution.items()
        if int(stats.get("count") or 0)
        and int(stats.get("success") or 0) / int(stats.get("count") or 1) < 0.75
    ]
    if failed_routes:
        weights["routing"] += 0.08
        weights["cost"] -= 0.04
        reasons.append("route-failure-rate-needs-feedback")

    confidence = 0.0
    if sample_count:
        confidence = min(0.9, 0.25 + min(sample_count, 200) / 200 * 0.65)

    return {
        "window_hours": int(window_hours or 24),
        "weights": normalize_weights(weights),
        "confidence": round(confidence, 4),
        "reason_chain": reasons,
        "sample_count": sample_count,
        "shadow_recommendation": snapshot["shadow_recommendation"],
        "feedback_summary": feedback_summary,
    }


def get_adaptive_weight_recommendation(window_hours=24, ttl_seconds=60, force_refresh=False):
    window_hours = int(window_hours or 24)
    cache_key = ADAPTIVE_WEIGHT_CACHE_KEY.format(window_hours=window_hours)
    if not force_refresh:
        cached = cache.get(cache_key)
        if isinstance(cached, dict):
            return cached
    recommendation = build_adaptive_weight_recommendation(window_hours=window_hours)
    cache.set(cache_key, recommendation, max(1, int(ttl_seconds or 60)))
    return recommendation
