from collections import defaultdict
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.db.models import Count, Sum
from django.db.models.functions import TruncHour
from django.utils import timezone

from ..models import DownloadRequestLog, ObservabilityStatus, PolicyRuntimeSetting
from ..policy.adaptive import build_adaptive_weight_recommendation
from ..policy.engine import policy_weight_diagnostics
from .download_routing import (
    ROUTE_BLOCKED,
    ROUTE_DIRECT,
    ROUTE_PROXY,
    ROUTE_ZIP,
    safe_download_size,
)
from .recovery_engine import provider_stability_score, provider_stability_status


PRODUCTION_INTELLIGENCE_VERSION = "v1"
OBSERVABILITY_V2_WINDOW_HOURS = 24
ADMIN_DISPLAY_TIME_ZONE = "Asia/Shanghai"
ROUTE_ORDER = (ROUTE_DIRECT, ROUTE_PROXY, ROUTE_ZIP, ROUTE_BLOCKED)
CONFIDENCE_BUCKETS = (
    ("low", 0.0, 0.4, "0.0 - 0.4"),
    ("medium", 0.4, 0.7, "0.4 - 0.7"),
    ("high", 0.7, 1.01, "0.7 - 1.0"),
)


def admin_display_timezone():
    timezone_name = str(
        getattr(settings, "ADMIN_DISPLAY_TIME_ZONE", ADMIN_DISPLAY_TIME_ZONE)
        or ADMIN_DISPLAY_TIME_ZONE
    )
    try:
        return ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        return ZoneInfo(ADMIN_DISPLAY_TIME_ZONE)


def format_admin_datetime(value):
    if not value:
        return ""
    return timezone.localtime(value, admin_display_timezone()).strftime("%Y-%m-%d %H:%M:%S")


def format_bytes(size_bytes):
    try:
        size_bytes = int(size_bytes or 0)
    except (TypeError, ValueError):
        size_bytes = 0
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


def safe_ratio(numerator, denominator):
    denominator = safe_download_size(denominator)
    if denominator <= 0:
        return 0.0
    return round(safe_download_size(numerator) / denominator, 4)


def percent_label(value):
    try:
        value = float(value or 0)
    except (TypeError, ValueError):
        value = 0.0
    return f"{round(value * 100, 1)}%"


def bounded_score(value):
    try:
        value = float(value or 0)
    except (TypeError, ValueError):
        value = 0.0
    return round(max(0.0, min(1.0, value)), 4)


def route_label(route):
    return {
        ROUTE_DIRECT: "浏览器直连",
        ROUTE_PROXY: "服务端单文件",
        ROUTE_ZIP: "ZIP 打包",
        ROUTE_BLOCKED: "拦截",
    }.get(route, route or "unknown")


def provider_name(value):
    return str(value or "unknown")


def build_route_distribution(downloads):
    total = len(downloads)
    rows_by_route = {
        route: {
            "route_type": route,
            "label": route_label(route),
            "count": 0,
            "success": 0,
            "failed": 0,
            "fallback_count": 0,
            "server_egress": 0,
            "browser_egress": 0,
            "actual_bytes": 0,
        }
        for route in ROUTE_ORDER
    }
    for log in downloads:
        route = str(log.route_type or "unknown")
        if route not in rows_by_route:
            rows_by_route[route] = {
                "route_type": route,
                "label": route_label(route),
                "count": 0,
                "success": 0,
                "failed": 0,
                "fallback_count": 0,
                "server_egress": 0,
                "browser_egress": 0,
                "actual_bytes": 0,
            }
        row = rows_by_route[route]
        row["count"] += 1
        row["success"] += int(log.status == ObservabilityStatus.SUCCESS)
        row["failed"] += int(log.status != ObservabilityStatus.SUCCESS)
        row["fallback_count"] += safe_download_size(log.fallback_count)
        row["server_egress"] += safe_download_size(log.server_egress_estimated_bytes)
        row["browser_egress"] += safe_download_size(log.browser_egress_estimated_bytes)
        row["actual_bytes"] += safe_download_size(log.actual_bytes)

    rows = []
    for route in list(ROUTE_ORDER) + sorted(set(rows_by_route) - set(ROUTE_ORDER)):
        row = rows_by_route[route]
        count = row["count"]
        success_rate = safe_ratio(row["success"], count)
        failure_rate = safe_ratio(row["failed"], count)
        fallback_rate = safe_ratio(row["fallback_count"], count)
        rows.append(
            {
                **row,
                "distribution_rate": safe_ratio(count, total),
                "distribution_label": percent_label(safe_ratio(count, total)),
                "success_rate": success_rate,
                "success_rate_label": percent_label(success_rate),
                "failure_rate": failure_rate,
                "failure_rate_label": percent_label(failure_rate),
                "fallback_rate": fallback_rate,
                "fallback_rate_label": percent_label(fallback_rate),
                "server_egress_label": format_bytes(row["server_egress"]),
                "browser_egress_label": format_bytes(row["browser_egress"]),
                "actual_bytes_label": format_bytes(row["actual_bytes"]),
            }
        )

    return {
        "total": total,
        "rows": rows,
        "success_rate": safe_ratio(
            sum(row["success"] for row in rows),
            total,
        ),
        "failure_rate": safe_ratio(
            sum(row["failed"] for row in rows),
            total,
        ),
        "fallback_rate": safe_ratio(
            sum(row["fallback_count"] for row in rows),
            total,
        ),
    }


def build_provider_view(downloads):
    rows_by_provider = defaultdict(
        lambda: {
            "provider": "",
            "count": 0,
            "success": 0,
            "failed": 0,
            "timeouts": 0,
            "duration_ms": 0,
            "actual_bytes": 0,
            "failure_types": defaultdict(int),
        }
    )
    for log in downloads:
        provider = provider_name(log.provider)
        row = rows_by_provider[provider]
        row["provider"] = provider
        row["count"] += 1
        success = log.status == ObservabilityStatus.SUCCESS
        row["success"] += int(success)
        row["failed"] += int(not success)
        row["timeouts"] += int(
            log.failure_type == "provider_timeout"
            or "timeout" in str(log.error_type or "").lower()
            or "TIMEOUT" in str(log.error_code or "").upper()
        )
        row["duration_ms"] += safe_download_size(log.duration_ms)
        row["actual_bytes"] += safe_download_size(log.actual_bytes)
        if not success:
            failure = str(log.failure_type or log.error_type or log.error_code or "unknown")
            row["failure_types"][failure] += 1

    rows = []
    heatmap = []
    for provider, row in rows_by_provider.items():
        count = row["count"]
        average_latency = round(row["duration_ms"] / count, 1) if count else 0
        success_rate = safe_ratio(row["success"], count)
        failure_rate = safe_ratio(row["failed"], count)
        timeout_rate = safe_ratio(row["timeouts"], count)
        stability_score = provider_stability_score(
            success_rate=success_rate,
            timeout_rate=timeout_rate,
            failure_rate=failure_rate,
            average_latency_ms=average_latency,
        )
        rows.append(
            {
                "provider": provider,
                "count": count,
                "success_rate": success_rate,
                "success_rate_label": percent_label(success_rate),
                "failure_rate": failure_rate,
                "failure_rate_label": percent_label(failure_rate),
                "timeout_rate": timeout_rate,
                "timeout_rate_label": percent_label(timeout_rate),
                "average_latency_ms": average_latency,
                "stability_score": stability_score,
                "stability_status": provider_stability_status(stability_score),
                "actual_bytes_label": format_bytes(row["actual_bytes"]),
            }
        )
        for failure, failure_count in row["failure_types"].items():
            heatmap.append(
                {
                    "provider": provider,
                    "failure_type": failure,
                    "count": failure_count,
                }
            )

    rows = sorted(rows, key=lambda item: (-item["count"], item["provider"]))
    heatmap = sorted(heatmap, key=lambda item: (-item["count"], item["provider"]))[:20]
    total_count = sum(row["count"] for row in rows)
    total_success = sum(row["success"] for row in rows_by_provider.values())
    reliability = safe_ratio(total_success, total_count)
    return {
        "rows": rows,
        "failure_heatmap": heatmap,
        "provider_reliability": reliability,
        "provider_reliability_label": percent_label(reliability),
    }


def build_cost_view(download_queryset, downloads):
    display_tz = admin_display_timezone()
    hourly = list(
        download_queryset.annotate(hour=TruncHour("created_at", tzinfo=display_tz))
        .values("hour")
        .annotate(
            count=Count("id"),
            server_egress=Sum("server_egress_estimated_bytes"),
            browser_egress=Sum("browser_egress_estimated_bytes"),
            actual_bytes=Sum("actual_bytes"),
        )
        .order_by("hour")
    )
    zip_counts = {
        row["hour"]: row["count"]
        for row in download_queryset.filter(route_type=ROUTE_ZIP)
        .annotate(hour=TruncHour("created_at", tzinfo=display_tz))
        .values("hour")
        .annotate(count=Count("id"))
    }
    trend_rows = []
    for row in hourly:
        count = safe_download_size(row.get("count"))
        zip_count = safe_download_size(zip_counts.get(row["hour"]))
        zip_ratio = safe_ratio(zip_count, count)
        trend_rows.append(
            {
                "hour": row["hour"],
                "hour_display": format_admin_datetime(row["hour"])[:13] + ":00",
                "count": count,
                "server_egress": safe_download_size(row.get("server_egress")),
                "browser_egress": safe_download_size(row.get("browser_egress")),
                "actual_bytes": safe_download_size(row.get("actual_bytes")),
                "server_egress_label": format_bytes(row.get("server_egress")),
                "browser_egress_label": format_bytes(row.get("browser_egress")),
                "actual_bytes_label": format_bytes(row.get("actual_bytes")),
                "zip_ratio": zip_ratio,
                "zip_ratio_label": percent_label(zip_ratio),
            }
        )

    cost_accuracy_scores = []
    for log in downloads:
        estimated = safe_download_size(log.estimated_bytes)
        actual = safe_download_size(log.actual_bytes)
        if estimated <= 0 or actual <= 0:
            continue
        denominator = max(estimated, actual)
        cost_accuracy_scores.append(max(0.0, 1.0 - abs(actual - estimated) / denominator))
    cost_accuracy = (
        round(sum(cost_accuracy_scores) / len(cost_accuracy_scores), 4)
        if cost_accuracy_scores
        else 0.0
    )
    estimated_savings = sum(int(log.policy_estimated_savings_bytes or 0) for log in downloads)
    server_total = sum(safe_download_size(log.server_egress_estimated_bytes) for log in downloads)
    browser_total = sum(safe_download_size(log.browser_egress_estimated_bytes) for log in downloads)
    zip_count = sum(1 for log in downloads if log.route_type == ROUTE_ZIP)
    return {
        "trend_rows": trend_rows,
        "server_egress_total": server_total,
        "server_egress_total_label": format_bytes(server_total),
        "browser_egress_total": browser_total,
        "browser_egress_total_label": format_bytes(browser_total),
        "estimated_savings_bytes": estimated_savings,
        "estimated_savings_label": format_bytes(abs(estimated_savings)),
        "estimated_savings_direction": "节省" if estimated_savings >= 0 else "增加",
        "zip_ratio": safe_ratio(zip_count, len(downloads)),
        "zip_ratio_label": percent_label(safe_ratio(zip_count, len(downloads))),
        "cost_accuracy": cost_accuracy,
        "cost_accuracy_label": percent_label(cost_accuracy),
    }


def build_policy_view(downloads):
    policy_logs = [log for log in downloads if str(log.final_policy_route or "")]
    total = len(policy_logs)
    matched = sum(1 for log in policy_logs if log.final_policy_route == log.route_type)
    active_count = sum(1 for log in policy_logs if log.policy_active)
    confidence_rows = []
    for key, start, end, label in CONFIDENCE_BUCKETS:
        count = sum(
            1
            for log in policy_logs
            if float(log.policy_confidence or 0) >= start
            and float(log.policy_confidence or 0) < end
        )
        confidence_rows.append(
            {
                "key": key,
                "label": label,
                "count": count,
                "rate": safe_ratio(count, total),
                "rate_label": percent_label(safe_ratio(count, total)),
            }
        )

    diff_rows_by_key = defaultdict(
        lambda: {"actual_route": "", "recommended_route": "", "count": 0}
    )
    for log in policy_logs:
        if log.final_policy_route == log.route_type:
            continue
        key = (str(log.route_type or "unknown"), str(log.final_policy_route or "unknown"))
        row = diff_rows_by_key[key]
        row["actual_route"] = key[0]
        row["recommended_route"] = key[1]
        row["count"] += 1

    policy_score_rows = (
        {
            "policy": "cost",
            "score": round(
                sum(float(log.policy_cost_score or 0) for log in policy_logs) / total,
                4,
            )
            if total
            else 0,
        },
        {
            "policy": "provider",
            "score": round(
                sum(float(log.policy_provider_score or 0) for log in policy_logs) / total,
                4,
            )
            if total
            else 0,
        },
        {
            "policy": "routing",
            "score": round(
                sum(float(log.policy_routing_score or 0) for log in policy_logs) / total,
                4,
            )
            if total
            else 0,
        },
    )
    consistency = safe_ratio(matched, total)
    mismatch = total - matched
    return {
        "policy_count": total,
        "policy_active_count": active_count,
        "decision_consistency": consistency,
        "decision_consistency_label": percent_label(consistency),
        "route_recommendation_accuracy": consistency,
        "route_recommendation_accuracy_label": percent_label(consistency),
        "policy_conflict_rate": safe_ratio(mismatch, total),
        "policy_conflict_rate_label": percent_label(safe_ratio(mismatch, total)),
        "confidence_distribution": confidence_rows,
        "shadow_actual_diff": sorted(
            diff_rows_by_key.values(),
            key=lambda row: (-row["count"], row["actual_route"], row["recommended_route"]),
        )[:20],
        "policy_score_rows": policy_score_rows,
    }


def build_policy_insights(metrics, provider_view, policy_view, cost_view, routing_view):
    insights = []
    if metrics["policy_conflict_rate"] >= 0.25:
        insights.append(
            {
                "level": "warning",
                "title": "Policy 建议与实际路由差异偏高",
                "body": "保持 shadow/staff 模式，先检查 shadow vs actual diff 中的主要冲突。",
            }
        )
    if metrics["fallback_frequency"] >= 0.15:
        insights.append(
            {
                "level": "warning",
                "title": "Fallback 频率偏高",
                "body": "优先查看失败热力图和 Self-Healing 事件，确认是否是单个 Provider 或 CDN 问题。",
            }
        )
    if metrics["provider_reliability"] and metrics["provider_reliability"] < 0.8:
        insights.append(
            {
                "level": "warning",
                "title": "Provider 可靠性不足",
                "body": "建议继续 staff-only 或小比例灰度，暂不扩大第三方 Provider 公网开放。",
            }
        )
    if cost_view["cost_accuracy"] and cost_view["cost_accuracy"] < 0.7:
        insights.append(
            {
                "level": "info",
                "title": "成本估算需要校准",
                "body": "estimated_bytes 与 actual_bytes 偏差较大，后续应增强 Content-Length 和候选大小探测。",
            }
        )
    if routing_view["success_rate"] >= 0.9 and metrics["fallback_frequency"] <= 0.05:
        insights.append(
            {
                "level": "success",
                "title": "路由执行稳定",
                "body": "当前窗口内成功率较高且 fallback 较低，可以继续观察 Policy Engine 推荐命中率。",
            }
        )
    if not insights:
        insights.append(
            {
                "level": "neutral",
                "title": "样本不足或暂无明显风险",
                "body": "继续收集下载日志、Provider 测试和 Policy shadow 数据。",
            }
        )
    return insights


def build_recommendation_mode(runtime_setting, metrics):
    try:
        adaptive = build_adaptive_weight_recommendation(
            window_hours=runtime_setting.v4_adaptive_window_hours,
        )
    except Exception:
        adaptive = {
            "weights": {},
            "confidence": 0,
            "reason_chain": ["adaptive-recommendation-unavailable"],
            "sample_count": 0,
        }

    return {
        "mode": "recommendation_only",
        "automatic_execution": False,
        "recommended_weights": adaptive.get("weights") or {},
        "confidence": adaptive.get("confidence") or 0,
        "sample_count": adaptive.get("sample_count") or 0,
        "reason_chain": adaptive.get("reason_chain") or [],
        "safety_note": "Observability v2 只生成建议，不写入运行时配置，不自动修改生产策略。",
        "suggested_actions": [
            "保持 Policy Engine v3.5 shadow/staff 观察，直到推荐命中率稳定。",
            "当 provider_reliability 低于 80% 时，不扩大 Provider 公网灰度。",
            "当 policy_conflict_rate 高于 25% 时，先审查冲突样本而不是提高 rollout。",
        ],
        "weight_diagnostics": policy_weight_diagnostics(),
        "metrics_used": {
            "policy_conflict_rate": metrics["policy_conflict_rate"],
            "routing_efficiency": metrics["routing_efficiency"],
            "cost_accuracy": metrics["cost_accuracy"],
            "provider_reliability": metrics["provider_reliability"],
            "fallback_frequency": metrics["fallback_frequency"],
        },
    }


def normalize_window_hours(value):
    try:
        value = int(value or OBSERVABILITY_V2_WINDOW_HOURS)
    except (TypeError, ValueError):
        value = OBSERVABILITY_V2_WINDOW_HOURS
    return min(168, max(1, value))


def build_observability_v2_context(*, window_hours=OBSERVABILITY_V2_WINDOW_HOURS):
    window_hours = normalize_window_hours(window_hours)
    now = timezone.now()
    since = now - timezone.timedelta(hours=window_hours)
    download_queryset = DownloadRequestLog.objects.filter(created_at__gte=since)
    downloads = list(download_queryset)
    runtime_setting = PolicyRuntimeSetting.load()

    routing_view = build_route_distribution(downloads)
    provider_view = build_provider_view(downloads)
    cost_view = build_cost_view(download_queryset, downloads)
    policy_view = build_policy_view(downloads)

    fallback_frequency = routing_view["fallback_rate"]
    routing_efficiency = bounded_score(
        routing_view["success_rate"] * (1 - fallback_frequency)
    )
    metrics = {
        "policy_conflict_rate": policy_view["policy_conflict_rate"],
        "routing_efficiency": routing_efficiency,
        "cost_accuracy": cost_view["cost_accuracy"],
        "provider_reliability": provider_view["provider_reliability"],
        "fallback_frequency": fallback_frequency,
    }
    metric_cards = (
        {
            "key": "policy_conflict_rate",
            "label": "policy_conflict_rate",
            "value": percent_label(metrics["policy_conflict_rate"]),
            "hint": "Policy 推荐路由与实际执行路由不一致的比例。",
        },
        {
            "key": "routing_efficiency",
            "label": "routing_efficiency",
            "value": percent_label(metrics["routing_efficiency"]),
            "hint": "下载成功率扣除 fallback 频率后的路由效率。",
        },
        {
            "key": "cost_accuracy",
            "label": "cost_accuracy",
            "value": percent_label(metrics["cost_accuracy"]),
            "hint": "estimated_bytes 与 actual_bytes 的平均接近程度。",
        },
        {
            "key": "provider_reliability",
            "label": "provider_reliability",
            "value": percent_label(metrics["provider_reliability"]),
            "hint": "当前窗口内 Provider 下载成功率。",
        },
        {
            "key": "fallback_frequency",
            "label": "fallback_frequency",
            "value": percent_label(metrics["fallback_frequency"]),
            "hint": "下载过程中出现 fallback 的频率。",
        },
    )
    insights = build_policy_insights(
        metrics,
        provider_view,
        policy_view,
        cost_view,
        routing_view,
    )
    recommendation = build_recommendation_mode(runtime_setting, metrics)
    return {
        "title": "Observability v2",
        "version": PRODUCTION_INTELLIGENCE_VERSION,
        "window_hours": window_hours,
        "window_started_at": format_admin_datetime(since),
        "window_ended_at": format_admin_datetime(now),
        "generated_at": format_admin_datetime(now),
        "download_count": len(downloads),
        "runtime_setting": runtime_setting,
        "metric_cards": metric_cards,
        "metrics": metrics,
        "routing": routing_view,
        "provider": provider_view,
        "cost": cost_view,
        "policy": policy_view,
        "insights": insights,
        "recommendation": recommendation,
        "flow_steps": (
            "logs",
            "metrics",
            "policy insights",
            "recommendations",
        ),
    }
