from collections import Counter, defaultdict

from django.utils import timezone

from ..models import DownloadRequestLog, ObservabilityStatus, ParseRequestLog, PolicyLearningRun
from ..services.download_audit import operational_download_logs
from ..services.download_routing import (
    ROUTE_DIRECT,
    ROUTE_PROXY,
    ROUTE_ZIP,
    safe_download_size,
)


STANDARD_WINDOWS = (24, 24 * 7)


def route_success_score(total, success):
    if total <= 0:
        return 0.0
    return success / total


def route_cost_score(actual_server_bytes, count):
    if count <= 0:
        return 0.0
    average = actual_server_bytes / count
    if average <= 10 * 1024 * 1024:
        return 1.0
    if average <= 100 * 1024 * 1024:
        return 0.78
    if average <= 500 * 1024 * 1024:
        return 0.55
    return 0.32


def build_route_distribution(downloads):
    rows = defaultdict(lambda: {
        "count": 0,
        "success": 0,
        "actual_server_bytes": 0,
        "estimated_server_bytes": 0,
        "duration_ms": 0,
    })
    for log in downloads:
        route = str(log.route_type or "unknown")
        rows[route]["count"] += 1
        rows[route]["success"] += int(log.status == ObservabilityStatus.SUCCESS)
        rows[route]["actual_server_bytes"] += safe_download_size(log.actual_bytes)
        rows[route]["estimated_server_bytes"] += safe_download_size(
            log.server_egress_estimated_bytes
        )
        rows[route]["duration_ms"] += safe_download_size(log.duration_ms)
    return dict(rows)


def build_provider_distribution(downloads, parses):
    counter = Counter()
    for log in downloads:
        provider = str(log.provider or "unknown")
        counter[provider] += 1
    for log in parses:
        provider = str(log.selected_provider or "unknown")
        counter[provider] += 1
    return dict(counter)


def recommend_shadow_route(route_distribution):
    if not route_distribution:
        return {
            "route": "",
            "reason": "insufficient_data",
            "score": 0,
        }

    best_route = ""
    best_score = -1
    for route, stats in route_distribution.items():
        count = safe_download_size(stats.get("count"))
        success = safe_download_size(stats.get("success"))
        actual = safe_download_size(stats.get("actual_server_bytes"))
        success_score = route_success_score(count, success)
        cost_score = route_cost_score(actual, count)
        route_bias = {
            ROUTE_DIRECT: 0.08,
            ROUTE_PROXY: 0.04,
            ROUTE_ZIP: -0.03,
        }.get(route, 0)
        score = max(0.0, min(1.0, success_score * 0.65 + cost_score * 0.35 + route_bias))
        if score > best_score:
            best_route = route
            best_score = score

    return {
        "route": best_route,
        "reason": "best-observed-success-cost-score",
        "score": round(best_score, 4),
    }


def estimate_savings_bytes(route_distribution, recommendation):
    recommended_route = str((recommendation or {}).get("route") or "")
    zip_stats = route_distribution.get(ROUTE_ZIP, {})
    if recommended_route in {ROUTE_DIRECT, ROUTE_PROXY}:
        return safe_download_size(zip_stats.get("actual_server_bytes"))
    return 0


def build_policy_learning_snapshot(window_hours=24, now=None):
    window_hours = int(window_hours or 24)
    now = now or timezone.now()
    since = now - timezone.timedelta(hours=window_hours)
    downloads = list(operational_download_logs(DownloadRequestLog.objects.filter(created_at__gte=since)))
    parses = list(ParseRequestLog.objects.filter(created_at__gte=since))
    route_distribution = build_route_distribution(downloads)
    provider_distribution = build_provider_distribution(downloads, parses)
    recommendation = recommend_shadow_route(route_distribution)
    sample_count = len(downloads) + len(parses)
    confidence = 0.0
    if sample_count:
        confidence = min(0.95, 0.35 + min(sample_count, 100) / 100 * 0.6)
        confidence *= float(recommendation.get("score") or 0)
    actual_server_bytes = sum(safe_download_size(log.actual_bytes) for log in downloads)
    estimated_server_bytes = sum(
        safe_download_size(log.server_egress_estimated_bytes) for log in downloads
    )

    return {
        "window_hours": window_hours,
        "window_started_at": since,
        "window_ended_at": now,
        "download_count": len(downloads),
        "parse_count": len(parses),
        "sample_count": sample_count,
        "shadow_recommendation": recommendation,
        "confidence": round(confidence, 4),
        "estimated_savings_bytes": estimate_savings_bytes(route_distribution, recommendation),
        "route_distribution": route_distribution,
        "provider_distribution": provider_distribution,
        "cost_summary": {
            "actual_server_bytes": actual_server_bytes,
            "estimated_server_bytes": estimated_server_bytes,
            "delta_bytes": actual_server_bytes - estimated_server_bytes,
        },
        "reason_chain": [
            "policy-v3-shadow-learning",
            f"window-{window_hours}h",
            recommendation.get("reason", "insufficient_data"),
        ],
    }


def record_policy_learning_run(window_hours=24):
    snapshot = build_policy_learning_snapshot(window_hours=window_hours)
    return PolicyLearningRun.objects.create(**snapshot)


def build_standard_policy_learning_snapshots():
    return [
        build_policy_learning_snapshot(window_hours=window_hours)
        for window_hours in STANDARD_WINDOWS
    ]
