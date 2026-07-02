from collections import defaultdict

from django.utils import timezone

from ..models import (
    DownloadRequestLog,
    ObservabilityStatus,
    ParseRequestLog,
    ProviderHealthTestRun,
    ProviderHealthTestStatus,
    RoutingDecisionLog,
)
from .download_routing import (
    ROUTE_BLOCKED,
    ROUTE_DIRECT,
    ROUTE_PROXY,
    ROUTE_ZIP,
    safe_download_size,
)
from .production_intelligence import (
    format_admin_datetime,
    format_bytes,
    normalize_window_hours,
    percent_label,
    route_label,
    safe_ratio,
)
from .recovery_engine import provider_stability_score, provider_stability_status


FEEDBACK_LOOP_VERSION = "mvp"
DEFAULT_FEEDBACK_WINDOW_HOURS = 24
ROUTE_ORDER = (ROUTE_DIRECT, ROUTE_PROXY, ROUTE_ZIP, ROUTE_BLOCKED)


def queryset_between(model, start, end):
    return model.objects.filter(created_at__gte=start, created_at__lt=end)


def trend_delta(current, previous):
    current = float(current or 0)
    previous = float(previous or 0)
    if previous <= 0:
        return 1.0 if current > 0 else 0.0
    return round(current - previous, 4)


def trend_ratio(current, previous):
    current = float(current or 0)
    previous = float(previous or 0)
    if previous <= 0:
        return 0.0 if current <= 0 else 999.0
    return round(current / previous, 4)


def status_success(status):
    return status == ObservabilityStatus.SUCCESS


def health_success(status):
    return status == ProviderHealthTestStatus.SUCCESS


def build_route_signals(downloads, routing_logs):
    rows = {
        route: {
            "route_type": route,
            "label": route_label(route),
            "download_count": 0,
            "download_success": 0,
            "download_failed": 0,
            "fallback_count": 0,
            "routing_count": 0,
            "routing_success": 0,
            "duration_ms": 0,
            "server_egress_bytes": 0,
            "browser_egress_bytes": 0,
        }
        for route in ROUTE_ORDER
    }
    for log in downloads:
        route = str(log.route_type or "unknown")
        rows.setdefault(
            route,
            {
                "route_type": route,
                "label": route_label(route),
                "download_count": 0,
                "download_success": 0,
                "download_failed": 0,
                "fallback_count": 0,
                "routing_count": 0,
                "routing_success": 0,
                "duration_ms": 0,
                "server_egress_bytes": 0,
                "browser_egress_bytes": 0,
            },
        )
        row = rows[route]
        row["download_count"] += 1
        row["download_success"] += int(status_success(log.status))
        row["download_failed"] += int(not status_success(log.status))
        row["fallback_count"] += safe_download_size(log.fallback_count)
        row["duration_ms"] += safe_download_size(log.duration_ms)
        row["server_egress_bytes"] += safe_download_size(log.server_egress_estimated_bytes)
        row["browser_egress_bytes"] += safe_download_size(log.browser_egress_estimated_bytes)

    for log in routing_logs:
        route = str(log.route_type or "unknown")
        rows.setdefault(
            route,
            {
                "route_type": route,
                "label": route_label(route),
                "download_count": 0,
                "download_success": 0,
                "download_failed": 0,
                "fallback_count": 0,
                "routing_count": 0,
                "routing_success": 0,
                "duration_ms": 0,
                "server_egress_bytes": 0,
                "browser_egress_bytes": 0,
            },
        )
        row = rows[route]
        row["routing_count"] += 1
        row["routing_success"] += int(bool(log.success))
        row["duration_ms"] += safe_download_size(log.duration_ms)

    normalized = {}
    for route, row in rows.items():
        count = row["download_count"]
        routing_count = row["routing_count"]
        avg_duration = round(row["duration_ms"] / max(1, count + routing_count), 1)
        success_rate = safe_ratio(row["download_success"], count)
        routing_success_rate = safe_ratio(row["routing_success"], routing_count)
        fallback_rate = safe_ratio(row["fallback_count"], count)
        normalized[route] = {
            **row,
            "success_rate": success_rate,
            "success_rate_label": percent_label(success_rate),
            "routing_success_rate": routing_success_rate,
            "routing_success_rate_label": percent_label(routing_success_rate),
            "fallback_rate": fallback_rate,
            "fallback_rate_label": percent_label(fallback_rate),
            "average_duration_ms": avg_duration,
            "server_egress_label": format_bytes(row["server_egress_bytes"]),
            "browser_egress_label": format_bytes(row["browser_egress_bytes"]),
        }
    return normalized


def provider_failure_key(log):
    return str(log.failure_type or log.error_type or log.error_code or "unknown")


def build_provider_signals(downloads, parses, health_runs):
    rows = defaultdict(
        lambda: {
            "provider": "",
            "download_count": 0,
            "download_success": 0,
            "download_failed": 0,
            "parse_count": 0,
            "parse_success": 0,
            "parse_failed": 0,
            "health_count": 0,
            "health_success": 0,
            "duration_ms": 0,
            "failure_types": defaultdict(int),
        }
    )
    for log in downloads:
        provider = str(log.provider or "unknown")
        row = rows[provider]
        row["provider"] = provider
        row["download_count"] += 1
        row["download_success"] += int(status_success(log.status))
        row["download_failed"] += int(not status_success(log.status))
        row["duration_ms"] += safe_download_size(log.duration_ms)
        if not status_success(log.status):
            row["failure_types"][provider_failure_key(log)] += 1

    for log in parses:
        provider = str(log.selected_provider or "unknown")
        row = rows[provider]
        row["provider"] = provider
        row["parse_count"] += 1
        row["parse_success"] += int(status_success(log.status))
        row["parse_failed"] += int(not status_success(log.status))
        row["duration_ms"] += safe_download_size(log.duration_ms)
        if not status_success(log.status):
            failure = str(log.error_type or log.error_code or "parse_failed")
            row["failure_types"][failure] += 1

    for run in health_runs:
        provider = str(run.provider or run.test_key or "unknown")
        row = rows[provider]
        row["provider"] = provider
        row["health_count"] += 1
        row["health_success"] += int(health_success(run.status))
        row["duration_ms"] += safe_download_size(run.duration_ms)
        if not health_success(run.status):
            failure = str(run.error_type or run.status or "health_failed")
            row["failure_types"][failure] += 1

    normalized = []
    for provider, row in rows.items():
        total_events = row["download_count"] + row["parse_count"] + row["health_count"]
        total_success = row["download_success"] + row["parse_success"] + row["health_success"]
        total_failed = (
            row["download_failed"]
            + row["parse_failed"]
            + (row["health_count"] - row["health_success"])
        )
        average_latency = round(row["duration_ms"] / max(1, total_events), 1)
        success_rate = safe_ratio(total_success, total_events)
        failure_rate = safe_ratio(total_failed, total_events)
        download_success_rate = safe_ratio(row["download_success"], row["download_count"])
        parse_success_rate = safe_ratio(row["parse_success"], row["parse_count"])
        health_success_rate = safe_ratio(row["health_success"], row["health_count"])
        score = provider_stability_score(
            success_rate=success_rate,
            timeout_rate=safe_ratio(row["failure_types"].get("provider_timeout"), total_events),
            failure_rate=failure_rate,
            average_latency_ms=average_latency,
        )
        normalized.append(
            {
                "provider": provider,
                "event_count": total_events,
                "success_rate": success_rate,
                "success_rate_label": percent_label(success_rate),
                "failure_rate": failure_rate,
                "failure_rate_label": percent_label(failure_rate),
                "download_success_rate": download_success_rate,
                "download_success_rate_label": percent_label(download_success_rate),
                "parse_success_rate": parse_success_rate,
                "parse_success_rate_label": percent_label(parse_success_rate),
                "health_success_rate": health_success_rate,
                "health_success_rate_label": percent_label(health_success_rate),
                "average_latency_ms": average_latency,
                "stability_score": score,
                "stability_status": provider_stability_status(score),
                "failure_types": dict(row["failure_types"]),
            }
        )
    return sorted(normalized, key=lambda item: (-item["event_count"], item["provider"]))


def build_failure_signals(downloads, parses, health_runs):
    failures = defaultdict(int)
    for log in downloads:
        if not status_success(log.status):
            failures[provider_failure_key(log)] += 1
    for log in parses:
        if not status_success(log.status):
            failures[str(log.error_type or log.error_code or "parse_failed")] += 1
    for run in health_runs:
        if not health_success(run.status):
            failures[str(run.error_type or run.status or "health_failed")] += 1
    return [
        {"failure_type": key, "count": count}
        for key, count in sorted(failures.items(), key=lambda item: (-item[1], item[0]))[:20]
    ]


def build_cost_signals(downloads):
    estimated = sum(safe_download_size(log.estimated_bytes) for log in downloads)
    actual = sum(safe_download_size(log.actual_bytes) for log in downloads)
    server = sum(safe_download_size(log.server_egress_estimated_bytes) for log in downloads)
    browser = sum(safe_download_size(log.browser_egress_estimated_bytes) for log in downloads)
    zip_count = sum(1 for log in downloads if log.route_type == ROUTE_ZIP)
    accuracy_scores = []
    for log in downloads:
        item_estimated = safe_download_size(log.estimated_bytes)
        item_actual = safe_download_size(log.actual_bytes)
        if item_estimated <= 0 or item_actual <= 0:
            continue
        accuracy_scores.append(1 - abs(item_actual - item_estimated) / max(item_actual, item_estimated))
    cost_accuracy = round(sum(accuracy_scores) / len(accuracy_scores), 4) if accuracy_scores else 0
    return {
        "estimated_bytes": estimated,
        "estimated_bytes_label": format_bytes(estimated),
        "actual_bytes": actual,
        "actual_bytes_label": format_bytes(actual),
        "server_egress_bytes": server,
        "server_egress_label": format_bytes(server),
        "browser_egress_bytes": browser,
        "browser_egress_label": format_bytes(browser),
        "zip_ratio": safe_ratio(zip_count, len(downloads)),
        "zip_ratio_label": percent_label(safe_ratio(zip_count, len(downloads))),
        "cost_accuracy": cost_accuracy,
        "cost_accuracy_label": percent_label(cost_accuracy),
    }


def summarize_window(start, end):
    downloads = list(queryset_between(DownloadRequestLog, start, end))
    parses = list(queryset_between(ParseRequestLog, start, end))
    health_runs = list(queryset_between(ProviderHealthTestRun, start, end))
    routing_logs = list(queryset_between(RoutingDecisionLog, start, end))
    download_total = len(downloads)
    parse_total = len(parses)
    health_total = len(health_runs)
    download_failures = sum(1 for log in downloads if not status_success(log.status))
    parse_failures = sum(1 for log in parses if not status_success(log.status))
    health_failures = sum(1 for run in health_runs if not health_success(run.status))
    total_events = download_total + parse_total + health_total
    total_failures = download_failures + parse_failures + health_failures
    return {
        "downloads": downloads,
        "parses": parses,
        "health_runs": health_runs,
        "routing_logs": routing_logs,
        "download_count": download_total,
        "parse_count": parse_total,
        "health_count": health_total,
        "routing_count": len(routing_logs),
        "total_events": total_events,
        "failure_count": total_failures,
        "failure_rate": safe_ratio(total_failures, total_events),
        "server_egress_bytes": sum(safe_download_size(log.server_egress_estimated_bytes) for log in downloads),
        "actual_bytes": sum(safe_download_size(log.actual_bytes) for log in downloads),
        "fallback_count": sum(safe_download_size(log.fallback_count) for log in downloads),
    }


def build_feedback(*, window_hours=DEFAULT_FEEDBACK_WINDOW_HOURS):
    window_hours = normalize_window_hours(window_hours)
    now = timezone.now()
    current_start = now - timezone.timedelta(hours=window_hours)
    previous_start = current_start - timezone.timedelta(hours=window_hours)
    current = summarize_window(current_start, now)
    previous = summarize_window(previous_start, current_start)
    route_signals = build_route_signals(current["downloads"], current["routing_logs"])
    provider_signals = build_provider_signals(
        current["downloads"],
        current["parses"],
        current["health_runs"],
    )
    cost_signals = build_cost_signals(current["downloads"])
    failure_signals = build_failure_signals(
        current["downloads"],
        current["parses"],
        current["health_runs"],
    )
    provider_trend = []
    for row in provider_signals:
        provider_trend.append(
            {
                "provider": row["provider"],
                "event_count": row["event_count"],
                "failure_rate": row["failure_rate"],
                "failure_rate_label": row["failure_rate_label"],
                "stability_score": row["stability_score"],
                "stability_status": row["stability_status"],
            }
        )
    routing_trend = [
        {
            "route_type": route,
            "label": row["label"],
            "download_count": row["download_count"],
            "success_rate": row["success_rate"],
            "success_rate_label": row["success_rate_label"],
            "fallback_rate": row["fallback_rate"],
            "fallback_rate_label": row["fallback_rate_label"],
        }
        for route, row in route_signals.items()
    ]
    return {
        "provider": "all",
        "signals": {
            "window_hours": window_hours,
            "window_started_at": format_admin_datetime(current_start),
            "window_ended_at": format_admin_datetime(now),
            "download_count": current["download_count"],
            "parse_count": current["parse_count"],
            "health_count": current["health_count"],
            "routing_count": current["routing_count"],
            "failure_rate": current["failure_rate"],
            "failure_rate_label": percent_label(current["failure_rate"]),
            "failure_rate_previous": previous["failure_rate"],
            "failure_rate_delta": trend_delta(current["failure_rate"], previous["failure_rate"]),
            "server_egress_bytes": current["server_egress_bytes"],
            "server_egress_label": format_bytes(current["server_egress_bytes"]),
            "server_egress_previous": previous["server_egress_bytes"],
            "server_egress_ratio": trend_ratio(current["server_egress_bytes"], previous["server_egress_bytes"]),
            "fallback_rate": safe_ratio(current["fallback_count"], current["download_count"]),
            "fallback_rate_label": percent_label(safe_ratio(current["fallback_count"], current["download_count"])),
            "provider_signals": provider_signals,
            "cost": cost_signals,
            "failures": failure_signals,
        },
        "routes": route_signals,
        "provider_trend": provider_trend,
        "routing_trend": routing_trend,
    }


def insight(level, kind, title, body, signals=None):
    return {
        "level": level,
        "kind": kind,
        "title": title,
        "body": body,
        "signals": signals or {},
    }


def build_insights(feedback):
    signals = feedback.get("signals") or {}
    insights = []
    failure_delta = float(signals.get("failure_rate_delta") or 0)
    failure_rate = float(signals.get("failure_rate") or 0)
    if failure_rate >= 0.2 and failure_delta >= 0.1:
        insights.append(
            insight(
                "warning",
                "failure_increase",
                "失败率上升",
                "当前窗口失败率明显高于上一窗口，需要优先查看失败类型和 Provider 趋势。",
                {
                    "failure_rate": failure_rate,
                    "previous": signals.get("failure_rate_previous"),
                    "delta": failure_delta,
                },
            )
        )

    cost = signals.get("cost") or {}
    server_ratio = float(signals.get("server_egress_ratio") or 0)
    if safe_download_size(cost.get("server_egress_bytes")) >= 200 * 1024 * 1024 and server_ratio >= 1.5:
        insights.append(
            insight(
                "warning",
                "cost_spike",
                "服务器出网成本突增",
                "当前窗口 server egress 明显高于上一窗口，建议检查 ZIP 比例和大文件下载。",
                {
                    "server_egress_bytes": cost.get("server_egress_bytes"),
                    "server_egress_ratio": server_ratio,
                },
            )
        )

    for row in signals.get("provider_signals") or ():
        if int(row.get("event_count") or 0) >= 2 and float(row.get("failure_rate") or 0) >= 0.3:
            insights.append(
                insight(
                    "warning",
                    "provider_degraded",
                    f"{row['provider']} Provider 可靠性下降",
                    "该 Provider 失败率偏高，建议先保持 staff-only 或运行健康测试。",
                    {
                        "provider": row["provider"],
                        "failure_rate": row["failure_rate"],
                        "stability_score": row["stability_score"],
                    },
                )
            )

    for route, row in (feedback.get("routes") or {}).items():
        if int(row.get("download_count") or 0) <= 0:
            continue
        if float(row.get("success_rate") or 0) < 0.75 or float(row.get("fallback_rate") or 0) >= 0.2:
            insights.append(
                insight(
                    "info",
                    "route_inefficiency",
                    f"{route_label(route)} 路由效率偏低",
                    "该路由成功率偏低或 fallback 偏高，建议结合 routing feedback 检查是否需要调整策略。",
                    {
                        "route_type": route,
                        "success_rate": row.get("success_rate"),
                        "fallback_rate": row.get("fallback_rate"),
                    },
                )
            )

    if not insights:
        insights.append(
            insight(
                "neutral",
                "no_major_trend",
                "暂无明显趋势风险",
                "当前窗口未发现失败上升、成本突增或路由效率异常。继续积累样本。",
            )
        )
    return {"insights": insights}


def suggestion(level, policy_area, title, body, source_insight):
    return {
        "level": level,
        "policy_area": policy_area,
        "title": title,
        "body": body,
        "source_insight": source_insight,
        "automatic_execution": False,
    }


def build_suggestions(insights):
    suggestions = []
    for item in insights.get("insights") or ():
        kind = item.get("kind")
        if kind == "failure_increase":
            suggestions.append(
                suggestion(
                    "warning",
                    "provider_runtime",
                    "先运行 Provider 健康测试再扩大灰度",
                    "失败率上升时不要直接 public；建议先在后台运行 Provider 测试并查看失败热力图。",
                    kind,
                )
            )
        elif kind == "provider_degraded":
            provider = (item.get("signals") or {}).get("provider") or "Provider"
            suggestions.append(
                suggestion(
                    "warning",
                    "provider_runtime",
                    f"保持 {provider} staff-only 或降低公开比例",
                    "该建议只作为人工决策参考，不会自动修改 Provider runtime gate。",
                    kind,
                )
            )
        elif kind == "cost_spike":
            suggestions.append(
                suggestion(
                    "warning",
                    "download_cost",
                    "检查 ZIP 比例和大文件下载限制",
                    "若成本突增来自 ZIP 或大文件，应人工检查 Download Runtime Setting，而不是自动调整 Cost Guard。",
                    kind,
                )
            )
        elif kind == "route_inefficiency":
            route = (item.get("signals") or {}).get("route_type") or "route"
            suggestions.append(
                suggestion(
                    "info",
                    "routing_policy",
                    f"审查 {route} 路由反馈",
                    "建议先观察 RoutingDecisionLog 和 DownloadRequestLog，不直接修改 Routing Engine。",
                    kind,
                )
            )
    if not suggestions:
        suggestions.append(
            suggestion(
                "neutral",
                "observation",
                "继续观察",
                "当前样本没有触发策略建议。保持现有生产策略。",
                "no_major_trend",
            )
        )
    return {"suggestions": suggestions}


def build_feedback_loop_context(*, window_hours=DEFAULT_FEEDBACK_WINDOW_HOURS):
    window_hours = normalize_window_hours(window_hours)
    feedback = build_feedback(window_hours=window_hours)
    insights = build_insights(feedback)
    suggestions = build_suggestions(insights)
    return {
        "title": "Feedback Loop",
        "version": FEEDBACK_LOOP_VERSION,
        "window_hours": window_hours,
        "generated_at": format_admin_datetime(timezone.now()),
        "feedback": feedback,
        "insight": insights,
        "suggestion": suggestions,
        "signals": feedback["signals"],
        "routes": feedback["routes"],
        "provider_trend": feedback["provider_trend"],
        "routing_trend": feedback["routing_trend"],
        "insights": insights["insights"],
        "suggestions": suggestions["suggestions"],
        "flow_steps": (
            "Observation",
            "Feedback",
            "Insight",
            "Suggestion",
        ),
        "safety_note": "Feedback Loop MVP 只读日志并输出建议，不会自动修改 production。",
    }
