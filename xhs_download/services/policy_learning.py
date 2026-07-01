from dataclasses import dataclass

from django.conf import settings
from django.db.models import Sum
from django.utils import timezone

from download_console.models import DownloadRuntimeSetting

from ..models import DownloadRequestLog, ObservabilityStatus, PolicyLearningRun, PolicyLearningStatus
from ..policy.policy_scoring import (
    POLICY_COST,
    POLICY_PROVIDER,
    POLICY_ROUTING,
    resolve_policy_conflicts,
)
from ..policy.policy_types import PolicyScoreResult
from .download_routing import (
    ROUTE_BLOCKED,
    ROUTE_DIRECT,
    ROUTE_PROXY,
    ROUTE_ZIP,
    SERVER_SIDE_PROVIDERS,
)


MIN_SAMPLE_COUNT = 20
WEIGHT_MIN = 0.1
WEIGHT_MAX = 0.7
SERVER_EGRESS_PRESSURE_BYTES = 5 * 1024 * 1024 * 1024
ROUTE_ORDER = (ROUTE_DIRECT, ROUTE_PROXY, ROUTE_ZIP, ROUTE_BLOCKED)


@dataclass(frozen=True)
class PolicyLearningDataset:
    window_hours: int
    since: object
    logs: tuple

    @property
    def sample_count(self):
        return len(self.logs)


def safe_int(value, default=0):
    try:
        if value in (None, ""):
            return default
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def safe_float(value, default=0.0):
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def format_bytes(size_bytes):
    size_bytes = safe_int(size_bytes)
    if size_bytes <= 0:
        return "0 B"
    units = ("B", "KB", "MB", "GB", "TB")
    value = float(size_bytes)
    unit_index = 0
    while value >= 1024 and unit_index < len(units) - 1:
        value /= 1024
        unit_index += 1
    if unit_index == 0:
        return f"{int(value)} {units[unit_index]}"
    return f"{value:.1f} {units[unit_index]}"


def success_rate(success_count, total_count):
    total_count = safe_int(total_count)
    if total_count <= 0:
        return 0.0
    return round(safe_int(success_count) / total_count, 4)


def current_policy_weights():
    return {
        POLICY_COST: safe_float(getattr(settings, "POLICY_ENGINE_COST_WEIGHT", 0.4), 0.4),
        POLICY_PROVIDER: safe_float(getattr(settings, "POLICY_ENGINE_PROVIDER_WEIGHT", 0.4), 0.4),
        POLICY_ROUTING: safe_float(getattr(settings, "POLICY_ENGINE_ROUTING_WEIGHT", 0.2), 0.2),
    }


def normalize_bounded_weights(weights):
    raw = {
        POLICY_COST: safe_float(weights.get(POLICY_COST), 0.4),
        POLICY_PROVIDER: safe_float(weights.get(POLICY_PROVIDER), 0.4),
        POLICY_ROUTING: safe_float(weights.get(POLICY_ROUTING), 0.2),
    }
    bounded = {
        key: min(WEIGHT_MAX, max(WEIGHT_MIN, value))
        for key, value in raw.items()
    }
    total = sum(bounded.values())
    if total <= 0:
        bounded = {POLICY_COST: 0.4, POLICY_PROVIDER: 0.4, POLICY_ROUTING: 0.2}
        total = 1.0

    normalized = {key: value / total for key, value in bounded.items()}
    for _index in range(8):
        changed = False
        low_keys = [key for key, value in normalized.items() if value < WEIGHT_MIN]
        high_keys = [key for key, value in normalized.items() if value > WEIGHT_MAX]
        if not low_keys and not high_keys:
            break
        for key in low_keys:
            normalized[key] = WEIGHT_MIN
            changed = True
        for key in high_keys:
            normalized[key] = WEIGHT_MAX
            changed = True
        remaining = 1.0 - sum(
            value
            for key, value in normalized.items()
            if key in set(low_keys + high_keys)
        )
        flexible = [
            key for key in normalized
            if key not in set(low_keys + high_keys)
        ]
        flexible_total = sum(normalized[key] for key in flexible)
        if flexible and flexible_total > 0:
            for key in flexible:
                normalized[key] = normalized[key] / flexible_total * remaining
        elif changed:
            break

    rounded = {key: round(value, 4) for key, value in normalized.items()}
    delta = round(1.0 - sum(rounded.values()), 4)
    if delta:
        target_key = max(rounded, key=rounded.get)
        rounded[target_key] = round(rounded[target_key] + delta, 4)
    return rounded


def build_policy_learning_dataset(window_hours=24):
    window_hours = max(1, safe_int(window_hours, 24))
    since = timezone.now() - timezone.timedelta(hours=window_hours)
    required_fields = (
        "policy_cost_score",
        "policy_provider_score",
        "policy_routing_score",
        "final_policy_route",
        "policy_confidence",
    )
    missing_fields = [
        field for field in required_fields
        if not hasattr(DownloadRequestLog, field)
    ]
    if missing_fields:
        raise RuntimeError("Policy Engine v2 fields missing: " + ", ".join(missing_fields))
    logs = tuple(
        DownloadRequestLog.objects.filter(created_at__gte=since)
        .only(
            "route_type",
            "final_policy_route",
            "status",
            "provider",
            "source_platform",
            "download_type",
            "item_count",
            "image_count",
            "video_count",
            "server_egress_estimated_bytes",
            "browser_egress_estimated_bytes",
            "actual_bytes",
            "duration_ms",
            "policy_cost_score",
            "policy_provider_score",
            "policy_routing_score",
            "policy_confidence",
        )
        .order_by("-created_at")[:5000]
    )
    return PolicyLearningDataset(window_hours=window_hours, since=since, logs=logs)


def compute_route_metrics(dataset):
    metrics = {
        route: {
            "route_type": route,
            "sample_count": 0,
            "success_count": 0,
            "failure_count": 0,
            "success_rate": 0.0,
            "failure_rate": 0.0,
            "avg_duration_ms": 0,
            "server_egress_estimated_bytes": 0,
            "actual_server_egress_bytes": 0,
        }
        for route in ROUTE_ORDER
    }
    durations = {route: [] for route in ROUTE_ORDER}
    for log in dataset.logs:
        route = log.route_type or ROUTE_ZIP
        if route not in metrics:
            metrics[route] = {
                "route_type": route,
                "sample_count": 0,
                "success_count": 0,
                "failure_count": 0,
                "success_rate": 0.0,
                "failure_rate": 0.0,
                "avg_duration_ms": 0,
                "server_egress_estimated_bytes": 0,
                "actual_server_egress_bytes": 0,
            }
            durations[route] = []
        row = metrics[route]
        row["sample_count"] += 1
        if log.status == ObservabilityStatus.SUCCESS:
            row["success_count"] += 1
        else:
            row["failure_count"] += 1
        row["server_egress_estimated_bytes"] += safe_int(log.server_egress_estimated_bytes)
        row["actual_server_egress_bytes"] += safe_int(log.actual_bytes)
        durations[route].append(safe_int(log.duration_ms))

    for route, row in metrics.items():
        row["success_rate"] = success_rate(row["success_count"], row["sample_count"])
        row["failure_rate"] = success_rate(row["failure_count"], row["sample_count"])
        route_durations = durations.get(route) or []
        row["avg_duration_ms"] = int(sum(route_durations) / len(route_durations)) if route_durations else 0
    return metrics


def compute_provider_metrics(dataset):
    metrics = {}
    durations = {}
    for log in dataset.logs:
        provider = log.provider or "native"
        row = metrics.setdefault(
            provider,
            {
                "provider": provider,
                "sample_count": 0,
                "success_count": 0,
                "failure_count": 0,
                "success_rate": 0.0,
                "failure_rate": 0.0,
                "avg_duration_ms": 0,
                "server_egress_estimated_bytes": 0,
                "actual_server_egress_bytes": 0,
            },
        )
        durations.setdefault(provider, [])
        row["sample_count"] += 1
        if log.status == ObservabilityStatus.SUCCESS:
            row["success_count"] += 1
        else:
            row["failure_count"] += 1
        row["server_egress_estimated_bytes"] += safe_int(log.server_egress_estimated_bytes)
        row["actual_server_egress_bytes"] += safe_int(log.actual_bytes)
        durations[provider].append(safe_int(log.duration_ms))

    for provider, row in metrics.items():
        row["success_rate"] = success_rate(row["success_count"], row["sample_count"])
        row["failure_rate"] = success_rate(row["failure_count"], row["sample_count"])
        provider_durations = durations.get(provider) or []
        row["avg_duration_ms"] = int(sum(provider_durations) / len(provider_durations)) if provider_durations else 0
    return metrics


def today_global_traffic_usage_ratio():
    setting = DownloadRuntimeSetting.load()
    limit = safe_int(getattr(setting, "global_daily_bytes_limit", 0))
    if limit <= 0:
        return 0.0
    today_start = timezone.localtime(timezone.now()).replace(hour=0, minute=0, second=0, microsecond=0)
    actual = safe_int(
        DownloadRequestLog.objects.filter(created_at__gte=today_start)
        .aggregate(total=Sum("actual_bytes"))["total"]
    )
    return min(1.0, actual / limit)


def recommend_policy_weights(route_metrics, provider_metrics, current_weights, sample_count=0):
    current = normalize_bounded_weights(current_weights)
    reasons = []
    if sample_count < MIN_SAMPLE_COUNT:
        return {
            "weights": current,
            "confidence": 0.25,
            "status": PolicyLearningStatus.INSUFFICIENT_DATA,
            "reasons": ["样本不足，暂不建议调整权重"],
        }

    recommended = dict(current)
    zip_count = safe_int(route_metrics.get(ROUTE_ZIP, {}).get("sample_count"))
    total_count = sum(safe_int(row.get("sample_count")) for row in route_metrics.values())
    zip_ratio = zip_count / total_count if total_count else 0
    server_egress = sum(
        safe_int(row.get("server_egress_estimated_bytes"))
        for row in route_metrics.values()
    )
    traffic_usage = today_global_traffic_usage_ratio()
    if (
        zip_ratio > 0.6
        or server_egress >= SERVER_EGRESS_PRESSURE_BYTES
        or traffic_usage > 0.7
    ):
        boost = 0.2 if traffic_usage > 0.85 or server_egress >= SERVER_EGRESS_PRESSURE_BYTES * 2 else 0.1
        recommended[POLICY_COST] += boost
        recommended[POLICY_ROUTING] -= boost / 2
        recommended[POLICY_PROVIDER] -= boost / 2
        reasons.append("cost pressure: ZIP 占比、服务器出网或全站流量使用率较高")

    risky_providers = [
        row for row in provider_metrics.values()
        if row["sample_count"] >= 5 and row["failure_rate"] > 0.3
    ]
    if risky_providers:
        recommended[POLICY_PROVIDER] += 0.1
        recommended[POLICY_ROUTING] -= 0.05
        recommended[POLICY_COST] -= 0.05
        provider_names = ", ".join(row["provider"] for row in risky_providers[:5])
        reasons.append(f"provider failure: {provider_names} 失败率超过 30%")

    direct_metrics = route_metrics.get(ROUTE_DIRECT, {})
    if (
        safe_int(direct_metrics.get("sample_count")) >= MIN_SAMPLE_COUNT
        and safe_float(direct_metrics.get("success_rate")) >= 0.9
    ):
        recommended[POLICY_ROUTING] += 0.05
        recommended[POLICY_PROVIDER] -= 0.05
        reasons.append("direct route stable: direct 成功率稳定且样本充足")

    if not reasons:
        reasons.append("近期策略表现稳定，建议保持当前权重")

    normalized = normalize_bounded_weights(recommended)
    confidence = recommendation_confidence(sample_count, reasons, route_metrics, provider_metrics)
    return {
        "weights": normalized,
        "confidence": confidence,
        "status": PolicyLearningStatus.SUCCESS,
        "reasons": reasons,
    }


def recommendation_confidence(sample_count, reasons, route_metrics, provider_metrics):
    confidence = 0.35 + min(0.35, safe_int(sample_count) / 300)
    if any("cost pressure" in reason for reason in reasons):
        confidence += 0.08
    if any("provider failure" in reason for reason in reasons):
        confidence += 0.08
    if any("direct route stable" in reason for reason in reasons):
        confidence += 0.05
    unstable_routes = [
        row for row in route_metrics.values()
        if row["sample_count"] >= 10 and row["failure_rate"] > 0.35
    ]
    if unstable_routes:
        confidence -= 0.08
    if not provider_metrics:
        confidence -= 0.05
    return round(max(0.0, min(0.95, confidence)), 3)


def route_server_egress(route, log):
    if route in {ROUTE_DIRECT, ROUTE_BLOCKED}:
        return 0
    return safe_int(log.server_egress_estimated_bytes)


def route_actual_server_egress(route, log):
    if route in {ROUTE_DIRECT, ROUTE_BLOCKED}:
        return 0
    return safe_int(log.actual_bytes)


def log_policy_result(log, policy, route, score, confidence, reason):
    return PolicyScoreResult(
        policy=policy,
        route=route,
        score=max(0.0, min(1.0, safe_float(score, 0.75))),
        confidence=max(0.0, min(1.0, safe_float(confidence, 0.75))),
        reason=reason,
    )


def shadow_policy_results_for_log(log):
    current_route = log.route_type or ROUTE_ZIP
    server_bytes = safe_int(log.server_egress_estimated_bytes)
    if current_route == ROUTE_BLOCKED or log.status == ObservabilityStatus.BLOCKED:
        cost_route = ROUTE_BLOCKED
        cost_reason = "historical_blocked"
        cost_score = 1.0
    elif server_bytes == 0 and current_route == ROUTE_DIRECT:
        cost_route = ROUTE_DIRECT
        cost_reason = "historical_zero_server_egress"
        cost_score = safe_float(log.policy_cost_score, 0.95)
    elif server_bytes >= SERVER_EGRESS_PRESSURE_BYTES / 10 and safe_int(log.item_count) <= 1:
        cost_route = ROUTE_PROXY
        cost_reason = "historical_high_server_egress_single_item"
        cost_score = safe_float(log.policy_cost_score, 0.86)
    else:
        cost_route = current_route
        cost_reason = "historical_cost_keep_route"
        cost_score = safe_float(log.policy_cost_score, 0.75)

    provider = log.provider or "native"
    if current_route == ROUTE_BLOCKED:
        provider_route = ROUTE_BLOCKED
        provider_reason = "historical_provider_blocked"
    elif provider in SERVER_SIDE_PROVIDERS:
        provider_route = ROUTE_ZIP
        provider_reason = "historical_provider_server_side"
    elif safe_int(log.image_count) == safe_int(log.item_count) and safe_int(log.item_count) <= 12:
        provider_route = ROUTE_DIRECT
        provider_reason = "historical_provider_public_images"
    elif safe_int(log.video_count) == 1 and safe_int(log.item_count) == 1:
        provider_route = ROUTE_PROXY
        provider_reason = "historical_provider_single_video"
    else:
        provider_route = ROUTE_ZIP
        provider_reason = "historical_provider_archive"

    routing_route = log.final_policy_route or current_route or ROUTE_ZIP
    return {
        POLICY_COST: log_policy_result(
            log,
            POLICY_COST,
            cost_route,
            cost_score,
            safe_float(log.policy_confidence, 0.75),
            cost_reason,
        ),
        POLICY_PROVIDER: log_policy_result(
            log,
            POLICY_PROVIDER,
            provider_route,
            safe_float(log.policy_provider_score, 0.75),
            safe_float(log.policy_confidence, 0.75),
            provider_reason,
        ),
        POLICY_ROUTING: log_policy_result(
            log,
            POLICY_ROUTING,
            routing_route,
            safe_float(log.policy_routing_score, 0.75),
            safe_float(log.policy_confidence, 0.75),
            "historical_routing_or_policy_route",
        ),
    }


def estimate_shadow_outcome(dataset, recommended_weights, route_metrics=None):
    actual_distribution = {route: 0 for route in ROUTE_ORDER}
    shadow_distribution = {route: 0 for route in ROUTE_ORDER}
    actual_server_egress = 0
    shadow_server_egress = 0
    mismatch_count = 0
    shadow_success_total = 0.0
    route_metrics = route_metrics or compute_route_metrics(dataset)

    for log in dataset.logs:
        actual_route = log.route_type or ROUTE_ZIP
        if actual_route not in actual_distribution:
            actual_distribution[actual_route] = 0
        actual_distribution[actual_route] += 1
        actual_server_egress += route_actual_server_egress(actual_route, log)

        resolution = resolve_policy_conflicts(
            shadow_policy_results_for_log(log),
            weights=recommended_weights,
        )
        shadow_route = resolution.final_route
        if shadow_route not in shadow_distribution:
            shadow_distribution[shadow_route] = 0
        shadow_distribution[shadow_route] += 1
        shadow_server_egress += route_server_egress(shadow_route, log)
        if shadow_route != actual_route:
            mismatch_count += 1
        shadow_success_total += safe_float(
            route_metrics.get(shadow_route, {}).get("success_rate"),
            success_rate(
                1 if log.status == ObservabilityStatus.SUCCESS else 0,
                1,
            ),
        )

    sample_count = dataset.sample_count
    return {
        "actual_route_distribution": actual_distribution,
        "shadow_route_distribution": shadow_distribution,
        "actual_server_egress_bytes": actual_server_egress,
        "estimated_shadow_server_egress_bytes": shadow_server_egress,
        "estimated_savings_bytes": max(0, actual_server_egress - shadow_server_egress),
        "mismatch_count": mismatch_count,
        "recommended_shadow_success_rate": round(shadow_success_total / sample_count, 4) if sample_count else 0.0,
    }


def build_policy_learning_result(window_hours=24):
    dataset = build_policy_learning_dataset(window_hours)
    route_metrics = compute_route_metrics(dataset)
    provider_metrics = compute_provider_metrics(dataset)
    current_weights = current_policy_weights()
    recommendation = recommend_policy_weights(
        route_metrics,
        provider_metrics,
        current_weights,
        sample_count=dataset.sample_count,
    )
    shadow = estimate_shadow_outcome(
        dataset,
        recommendation["weights"],
        route_metrics=route_metrics,
    )
    success_count = sum(
        1 for log in dataset.logs
        if log.status == ObservabilityStatus.SUCCESS
    )
    current_success = success_rate(success_count, dataset.sample_count)
    reason_summary = "；".join(recommendation["reasons"])
    details = {
        "route_metrics": route_metrics,
        "provider_metrics": provider_metrics,
        "actual_route_distribution": shadow["actual_route_distribution"],
        "shadow_route_distribution": shadow["shadow_route_distribution"],
        "mismatch_count": shadow["mismatch_count"],
        "current_weights": current_weights,
        "recommended_weights": recommendation["weights"],
        "reasons": recommendation["reasons"],
        "notes": [
            "shadow-only",
            "no production settings changed",
        ],
    }
    return {
        "window_hours": dataset.window_hours,
        "sample_count": dataset.sample_count,
        "current_weights": current_weights,
        "recommended_weights": recommendation["weights"],
        "current_success_rate": current_success,
        "recommended_shadow_success_rate": shadow["recommended_shadow_success_rate"],
        "actual_server_egress_bytes": shadow["actual_server_egress_bytes"],
        "estimated_shadow_server_egress_bytes": shadow["estimated_shadow_server_egress_bytes"],
        "estimated_savings_bytes": shadow["estimated_savings_bytes"],
        "confidence": recommendation["confidence"],
        "status": recommendation["status"],
        "reason_summary": reason_summary,
        "details_json": details,
    }


def create_policy_learning_run(window_hours=24):
    result = build_policy_learning_result(window_hours)
    current = result["current_weights"]
    recommended = result["recommended_weights"]
    return PolicyLearningRun.objects.create(
        window_hours=result["window_hours"],
        sample_count=result["sample_count"],
        current_cost_weight=current[POLICY_COST],
        current_provider_weight=current[POLICY_PROVIDER],
        current_routing_weight=current[POLICY_ROUTING],
        recommended_cost_weight=recommended[POLICY_COST],
        recommended_provider_weight=recommended[POLICY_PROVIDER],
        recommended_routing_weight=recommended[POLICY_ROUTING],
        current_success_rate=result["current_success_rate"],
        recommended_shadow_success_rate=result["recommended_shadow_success_rate"],
        actual_server_egress_bytes=result["actual_server_egress_bytes"],
        estimated_shadow_server_egress_bytes=result["estimated_shadow_server_egress_bytes"],
        estimated_savings_bytes=result["estimated_savings_bytes"],
        confidence=result["confidence"],
        status=result["status"],
        reason_summary=result["reason_summary"][:500],
        details_json=result["details_json"],
    )


def serialize_policy_learning_result(result):
    current = result["current_weights"]
    recommended = result["recommended_weights"]
    return {
        "window_hours": result["window_hours"],
        "sample_count": result["sample_count"],
        "status": result["status"],
        "confidence": result["confidence"],
        "current_weights": current,
        "recommended_weights": recommended,
        "current_success_rate": result["current_success_rate"],
        "recommended_shadow_success_rate": result["recommended_shadow_success_rate"],
        "actual_server_egress": format_bytes(result["actual_server_egress_bytes"]),
        "estimated_shadow_server_egress": format_bytes(result["estimated_shadow_server_egress_bytes"]),
        "estimated_savings": format_bytes(result["estimated_savings_bytes"]),
        "reason_summary": result["reason_summary"],
        "details": result["details_json"],
    }
