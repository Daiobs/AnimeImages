from collections import Counter, defaultdict

from django.utils import timezone

from ..models import (
    DownloadRequestLog,
    ObservabilityStatus,
    PolicyControlRecord,
    PolicyControlState,
)
from .control_convergence_engine import build_control_convergence_snapshot
from .download_routing import ROUTE_DIRECT, ROUTE_PROXY, ROUTE_ZIP, safe_download_size
from .feedback_loop import build_feedback
from .production_intelligence import (
    format_admin_datetime,
    normalize_window_hours,
    percent_label,
    safe_ratio,
)


STABILIZATION_ENGINE_VERSION = "stabilization_v6"
STABLE_ROUTES = (ROUTE_DIRECT, ROUTE_PROXY, ROUTE_ZIP)


def bounded(value):
    try:
        value = float(value or 0)
    except (TypeError, ValueError):
        value = 0.0
    return round(max(0.0, min(1.0, value)), 4)


def conflict_level_from_rate(rate):
    rate = bounded(rate)
    if rate >= 0.4:
        return "high"
    if rate >= 0.18:
        return "medium"
    return "low"


def route_or_default(route):
    route = str(route or "").strip()
    return route if route in STABLE_ROUTES else ROUTE_DIRECT


def route_distribution_fluctuation(routes):
    routes = [route for route in routes if route in STABLE_ROUTES]
    if not routes:
        return 0.0
    counts = Counter(routes)
    dominant = max(counts.values())
    return bounded(1 - dominant / len(routes))


def route_success_table(downloads):
    table = {
        route: {
            "route": route,
            "count": 0,
            "success": 0,
            "failed": 0,
            "fallback_count": 0,
        }
        for route in STABLE_ROUTES
    }
    for log in downloads:
        route = route_or_default(getattr(log, "route_type", ""))
        row = table[route]
        row["count"] += 1
        success = getattr(log, "status", "") == ObservabilityStatus.SUCCESS
        row["success"] += int(success)
        row["failed"] += int(not success)
        row["fallback_count"] += safe_download_size(getattr(log, "fallback_count", 0))
    for row in table.values():
        row["success_rate"] = safe_ratio(row["success"], row["count"])
        row["failure_rate"] = safe_ratio(row["failed"], row["count"])
        row["fallback_rate"] = safe_ratio(row["fallback_count"], row["count"])
        row["stable_score"] = bounded(
            (row["success_rate"] * 0.72)
            + ((1 - row["fallback_rate"]) * 0.18)
            + (safe_ratio(row["count"], len(downloads)) * 0.1)
        )
    return table


def policy_convergence_engine(downloads):
    downloads = list(downloads or ())
    policy_logs = [log for log in downloads if getattr(log, "final_policy_route", "")]
    policy_routes = [
        route_or_default(getattr(log, "final_policy_route", ""))
        for log in policy_logs
    ]
    actual_routes = [
        route_or_default(getattr(log, "route_type", ""))
        for log in downloads
        if getattr(log, "route_type", "")
    ]
    matches = sum(
        1
        for log in policy_logs
        if route_or_default(getattr(log, "final_policy_route", "")) == route_or_default(getattr(log, "route_type", ""))
    )
    drift_rate = bounded(1 - safe_ratio(matches, len(policy_logs))) if policy_logs else 0.0
    policy_fluctuation = route_distribution_fluctuation(policy_routes)
    actual_fluctuation = route_distribution_fluctuation(actual_routes)
    table = route_success_table(downloads)
    ranked = sorted(
        table.values(),
        key=lambda row: (-row["stable_score"], -row["count"], row["route"]),
    )
    stable_route = route_or_default(ranked[0]["route"] if ranked else ROUTE_DIRECT)
    convergence_score = bounded(
        (1 - drift_rate) * 0.45
        + (1 - policy_fluctuation) * 0.25
        + table[stable_route]["stable_score"] * 0.30
    )
    return {
        "stable_route": stable_route,
        "policy_drift_rate": drift_rate,
        "decision_fluctuation_rate": bounded(max(policy_fluctuation, actual_fluctuation)),
        "policy_fluctuation_rate": policy_fluctuation,
        "actual_route_fluctuation_rate": actual_fluctuation,
        "execution_consistency_score": table[stable_route]["success_rate"],
        "convergence_score": convergence_score,
        "route_scores": list(table.values()),
        "policy_sample_count": len(policy_logs),
        "download_sample_count": len(downloads),
    }


def control_simplifier(records):
    records = list(records or ())
    if not records:
        return {
            "control_conflict_rate": 0.0,
            "approval_redundancy_rate": 0.0,
            "simplification_score": 1.0,
            "duplicate_groups": [],
            "state_distribution": {},
        }
    state_counts = Counter(record.state for record in records)
    decision_groups = defaultdict(int)
    for record in records:
        key = (
            str(record.policy_decision or ""),
            str(record.control_decision or ""),
            str(record.provider or ""),
            str(record.route or ""),
        )
        decision_groups[key] += 1
    duplicate_total = sum(max(0, count - 1) for count in decision_groups.values())
    failed_or_rejected = (
        state_counts.get(PolicyControlState.FAILED, 0)
        + state_counts.get(PolicyControlState.REJECTED, 0)
    )
    pending = state_counts.get(PolicyControlState.PENDING, 0)
    control_conflict_rate = bounded((failed_or_rejected + pending * 0.5) / len(records))
    approval_redundancy_rate = bounded(duplicate_total / len(records))
    simplification_score = bounded(
        1 - (control_conflict_rate * 0.65 + approval_redundancy_rate * 0.35)
    )
    duplicate_groups = [
        {
            "policy_decision": key[0],
            "control_decision": key[1],
            "provider": key[2],
            "route": key[3],
            "count": count,
        }
        for key, count in sorted(decision_groups.items(), key=lambda item: (-item[1], item[0]))
        if count > 1
    ][:12]
    return {
        "control_conflict_rate": control_conflict_rate,
        "approval_redundancy_rate": approval_redundancy_rate,
        "simplification_score": simplification_score,
        "duplicate_groups": duplicate_groups,
        "state_distribution": dict(state_counts),
    }


def feedback_stabilizer(feedback):
    feedback = feedback or {}
    signals = feedback.get("signals") or {}
    failure_rate = bounded(signals.get("failure_rate"))
    failure_delta = float(signals.get("failure_rate_delta") or 0)
    fallback_rate = bounded(signals.get("fallback_rate"))
    server_ratio = float(signals.get("server_egress_ratio") or 0)
    provider_rows = signals.get("provider_signals") or ()
    provider_instability = 0.0
    if provider_rows:
        provider_instability = bounded(
            sum(float(row.get("failure_rate") or 0) for row in provider_rows)
            / max(1, len(provider_rows))
        )
    raw_volatility = bounded(
        abs(failure_delta) * 0.45
        + failure_rate * 0.25
        + fallback_rate * 0.2
        + min(server_ratio, 2.0) / 2.0 * 0.1
    )
    smoothed_delta = max(-0.15, min(0.15, failure_delta))
    smoothed_volatility = bounded(raw_volatility * 0.55)
    stabilizer_score = bounded(1 - smoothed_volatility)
    return {
        "feedback_volatility": raw_volatility,
        "smoothed_feedback_volatility": smoothed_volatility,
        "stabilizer_score": stabilizer_score,
        "failure_rate": failure_rate,
        "failure_rate_delta": failure_delta,
        "smoothed_failure_delta": round(smoothed_delta, 4),
        "provider_instability": provider_instability,
        "suppressed_extreme_change": abs(failure_delta) > abs(smoothed_delta),
    }


def stable_decision_reason(policy_result, control_result, feedback_result):
    reasons = ["stabilization-v6-read-only"]
    if policy_result["policy_drift_rate"] >= 0.18:
        reasons.append("policy-drift-detected")
    if policy_result["decision_fluctuation_rate"] >= 0.25:
        reasons.append("routing-fluctuation-detected")
    if control_result["control_conflict_rate"] >= 0.18:
        reasons.append("control-conflict-detected")
    if control_result["approval_redundancy_rate"] >= 0.15:
        reasons.append("duplicate-control-decisions-simplified")
    if feedback_result["feedback_volatility"] > feedback_result["smoothed_feedback_volatility"]:
        reasons.append("feedback-volatility-smoothed")
    if feedback_result["provider_instability"] >= 0.25:
        reasons.append("provider-instability-detected")
    if len(reasons) == 1:
        reasons.append("system-behavior-stable")
    return reasons


def stabilization_engine(*, downloads=None, control_records=None, feedback=None, window_hours=24):
    downloads = list(downloads or ())
    control_records = list(control_records or ())
    policy_result = policy_convergence_engine(downloads)
    control_result = control_simplifier(control_records)
    feedback_result = feedback_stabilizer(feedback or {})
    weighted_stability = bounded(
        policy_result["convergence_score"] * 0.42
        + control_result["simplification_score"] * 0.30
        + feedback_result["stabilizer_score"] * 0.28
    )
    conflict_rate = bounded(
        policy_result["policy_drift_rate"] * 0.35
        + policy_result["decision_fluctuation_rate"] * 0.2
        + control_result["control_conflict_rate"] * 0.25
        + feedback_result["feedback_volatility"] * 0.2
    )
    return {
        "version": STABILIZATION_ENGINE_VERSION,
        "window_hours": normalize_window_hours(window_hours),
        "stable_route": policy_result["stable_route"],
        "stability_score": weighted_stability,
        "stability_score_label": percent_label(weighted_stability),
        "conflict_level": conflict_level_from_rate(conflict_rate),
        "conflict_rate": conflict_rate,
        "conflict_rate_label": percent_label(conflict_rate),
        "reason": stable_decision_reason(policy_result, control_result, feedback_result),
        "metrics": {
            "stability_score": weighted_stability,
            "policy_drift_rate": policy_result["policy_drift_rate"],
            "decision_fluctuation_rate": policy_result["decision_fluctuation_rate"],
            "execution_consistency_score": policy_result["execution_consistency_score"],
            "control_conflict_rate": control_result["control_conflict_rate"],
            "feedback_volatility": feedback_result["feedback_volatility"],
            "provider_instability": feedback_result["provider_instability"],
        },
        "policy_convergence": policy_result,
        "control_simplifier": control_result,
        "feedback_stabilizer": feedback_result,
        "production_behavior_changed": False,
        "automatic_execution": False,
    }


def build_stabilization_snapshot(*, window_hours=24):
    window_hours = normalize_window_hours(window_hours)
    now = timezone.now()
    since = now - timezone.timedelta(hours=window_hours)
    downloads = list(DownloadRequestLog.objects.filter(created_at__gte=since))
    controls = list(PolicyControlRecord.objects.filter(created_at__gte=since))
    feedback = build_feedback(window_hours=window_hours)
    control_convergence = build_control_convergence_snapshot(window_hours=window_hours)
    result = stabilization_engine(
        downloads=downloads,
        control_records=controls,
        feedback=feedback,
        window_hours=window_hours,
    )
    result["generated_at"] = format_admin_datetime(now)
    result["window_started_at"] = format_admin_datetime(since)
    result["window_ended_at"] = format_admin_datetime(now)
    result["download_count"] = len(downloads)
    result["control_record_count"] = len(controls)
    result["feedback"] = feedback
    result["control_convergence"] = control_convergence
    result["trend"] = build_stability_trend(window_hours=window_hours)
    return result


def build_stability_trend(*, window_hours=24, buckets=6):
    window_hours = normalize_window_hours(window_hours)
    buckets = max(1, min(24, int(buckets or 6)))
    now = timezone.now()
    bucket_seconds = max(1, int(window_hours * 3600 / buckets))
    rows = []
    for index in range(buckets):
        start = now - timezone.timedelta(seconds=bucket_seconds * (buckets - index))
        end = start + timezone.timedelta(seconds=bucket_seconds)
        downloads = list(DownloadRequestLog.objects.filter(created_at__gte=start, created_at__lt=end))
        controls = list(PolicyControlRecord.objects.filter(created_at__gte=start, created_at__lt=end))
        result = stabilization_engine(
            downloads=downloads,
            control_records=controls,
            feedback={"signals": {}},
            window_hours=window_hours,
        )
        rows.append({
            "label": format_admin_datetime(start)[5:16],
            "start": format_admin_datetime(start),
            "end": format_admin_datetime(end),
            "stability_score": result["stability_score"],
            "stability_score_label": result["stability_score_label"],
            "conflict_level": result["conflict_level"],
            "stable_route": result["stable_route"],
            "download_count": len(downloads),
            "control_record_count": len(controls),
        })
    return rows


def build_stability_v6_context(*, window_hours=24):
    snapshot = build_stabilization_snapshot(window_hours=window_hours)
    return {
        "title": "Stability v6",
        "snapshot": snapshot,
        **snapshot,
        "safety_note": "Stability v6 只收敛并展示稳定决策，不自动修改 production policy、Routing、Provider 或 Cost Guard。",
    }
