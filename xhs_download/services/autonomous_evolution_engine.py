from collections import defaultdict

from django.utils import timezone
from .download_audit import operational_download_logs

from ..models import (
    DownloadRequestLog,
    ObservabilityStatus,
    PolicyControlRecord,
    PolicyControlState,
)
from .download_routing import ROUTE_DIRECT, ROUTE_PROXY, ROUTE_ZIP, safe_download_size
from .feedback_loop import build_feedback
from .production_intelligence import (
    format_admin_datetime,
    format_bytes,
    normalize_window_hours,
    percent_label,
    safe_ratio,
)
from .stabilization_engine import bounded, build_stabilization_snapshot, route_or_default


AUTONOMOUS_EVOLUTION_VERSION = "autonomous_evolution_v7"
MUTATION_CONFIDENCE_THRESHOLD = 0.8
MIN_SHADOW_SAMPLE_SIZE = 2
OPTIMIZABLE_ROUTES = (ROUTE_DIRECT, ROUTE_PROXY, ROUTE_ZIP)


def pct_delta(candidate, baseline):
    candidate = float(candidate or 0)
    baseline = float(baseline or 0)
    if baseline <= 0:
        return 0.0
    return round((candidate - baseline) / baseline, 4)


def route_metric_rows(downloads):
    rows = defaultdict(
        lambda: {
            "route": "",
            "count": 0,
            "success": 0,
            "failed": 0,
            "actual_bytes": 0,
            "duration_ms": 0,
            "fallback_count": 0,
        }
    )
    for log in downloads or ():
        route = route_or_default(getattr(log, "route_type", ""))
        row = rows[route]
        row["route"] = route
        row["count"] += 1
        success = getattr(log, "status", "") == ObservabilityStatus.SUCCESS
        row["success"] += int(success)
        row["failed"] += int(not success)
        row["actual_bytes"] += safe_download_size(getattr(log, "actual_bytes", 0))
        row["duration_ms"] += safe_download_size(getattr(log, "duration_ms", 0))
        row["fallback_count"] += safe_download_size(getattr(log, "fallback_count", 0))

    normalized = {}
    for route in OPTIMIZABLE_ROUTES:
        row = rows[route]
        count = row["count"]
        success_rate = safe_ratio(row["success"], count)
        failure_rate = safe_ratio(row["failed"], count)
        average_cost = round(row["actual_bytes"] / max(1, count), 2)
        average_latency = round(row["duration_ms"] / max(1, count), 2)
        fallback_rate = safe_ratio(row["fallback_count"], count)
        normalized[route] = {
            **row,
            "success_rate": success_rate,
            "failure_rate": failure_rate,
            "average_cost": average_cost,
            "average_latency_ms": average_latency,
            "fallback_rate": fallback_rate,
        }
    return normalized


def expected_gain_between(source, target):
    source_cost = float(source.get("average_cost") or 0)
    target_cost = float(target.get("average_cost") or 0)
    source_latency = float(source.get("average_latency_ms") or 0)
    target_latency = float(target.get("average_latency_ms") or 0)
    source_success = float(source.get("success_rate") or 0)
    target_success = float(target.get("success_rate") or 0)
    return {
        "cost": pct_delta(target_cost, source_cost),
        "latency": pct_delta(target_latency, source_latency),
        "success_rate": round(target_success - source_success, 4),
    }


def gain_score(expected_gain):
    cost_gain = max(0.0, -float(expected_gain.get("cost") or 0))
    latency_gain = max(0.0, -float(expected_gain.get("latency") or 0))
    success_gain = max(0.0, float(expected_gain.get("success_rate") or 0))
    return bounded(cost_gain * 0.35 + latency_gain * 0.25 + success_gain * 1.6)


def feedback_risk_score(feedback):
    signals = (feedback or {}).get("signals") or {}
    provider_rows = signals.get("provider_signals") or ()
    provider_failure = 0.0
    if provider_rows:
        provider_failure = sum(float(row.get("failure_rate") or 0) for row in provider_rows) / max(
            1,
            len(provider_rows),
        )
    failure_rate = float(signals.get("failure_rate") or 0)
    fallback_rate = float(signals.get("fallback_rate") or 0)
    server_egress_ratio = min(float(signals.get("server_egress_ratio") or 0), 2.0) / 2.0
    return bounded(
        failure_rate * 0.35
        + fallback_rate * 0.25
        + provider_failure * 0.25
        + server_egress_ratio * 0.15
    )


def proposal_confidence(source, target, stability_snapshot=None, feedback=None):
    stability_snapshot = stability_snapshot or {}
    sample_strength = bounded(min(source.get("count", 0), target.get("count", 0)) / 10)
    target_success = float(target.get("success_rate") or 0)
    stability = float(stability_snapshot.get("stability_score") or 0.5)
    feedback_safety = 1 - feedback_risk_score(feedback)
    return bounded(
        sample_strength * 0.30
        + target_success * 0.30
        + stability * 0.25
        + feedback_safety * 0.15
    )


def provider_metric_rows(downloads):
    rows = defaultdict(
        lambda: {
            "provider": "",
            "count": 0,
            "success": 0,
            "failed": 0,
            "actual_bytes": 0,
            "duration_ms": 0,
        }
    )
    for log in downloads or ():
        provider = str(getattr(log, "provider", "") or "unknown")
        row = rows[provider]
        row["provider"] = provider
        row["count"] += 1
        success = getattr(log, "status", "") == ObservabilityStatus.SUCCESS
        row["success"] += int(success)
        row["failed"] += int(not success)
        row["actual_bytes"] += safe_download_size(getattr(log, "actual_bytes", 0))
        row["duration_ms"] += safe_download_size(getattr(log, "duration_ms", 0))
    normalized = []
    for row in rows.values():
        count = row["count"]
        success_rate = safe_ratio(row["success"], count)
        failure_rate = safe_ratio(row["failed"], count)
        average_latency = round(row["duration_ms"] / max(1, count), 2)
        normalized.append({
            **row,
            "success_rate": success_rate,
            "failure_rate": failure_rate,
            "average_latency_ms": average_latency,
            "average_cost": round(row["actual_bytes"] / max(1, count), 2),
        })
    return sorted(normalized, key=lambda item: (-item["count"], item["provider"]))[:12]


def policy_execution_history(control_records):
    rows = {
        "total": 0,
        "pending": 0,
        "approved": 0,
        "rejected": 0,
        "auto_applied": 0,
        "executed": 0,
        "failed": 0,
        "failure_rate": 0,
        "execution_success_rate": 0,
        "recent_failures": [],
    }
    for record in control_records or ():
        rows["total"] += 1
        state = str(getattr(record, "state", "") or "")
        if state in rows:
            rows[state] += 1
        if state == PolicyControlState.FAILED:
            rows["recent_failures"].append({
                "policy_decision": getattr(record, "policy_decision", ""),
                "provider": getattr(record, "provider", ""),
                "route": getattr(record, "route", ""),
                "failure_pattern": getattr(record, "failure_pattern", ""),
            })
    terminal = rows["executed"] + rows["failed"]
    rows["failure_rate"] = safe_ratio(rows["failed"], rows["total"])
    rows["execution_success_rate"] = safe_ratio(rows["executed"], terminal)
    rows["recent_failures"] = rows["recent_failures"][:8]
    return rows


def detect_optimization_opportunities(downloads, feedback=None, stability_snapshot=None):
    downloads = list(downloads or ())
    feedback = feedback or {}
    stability_snapshot = stability_snapshot or {}
    rows = route_metric_rows(downloads)
    proposals = []
    for from_route, source in rows.items():
        if source["count"] <= 0:
            continue
        for to_route, target in rows.items():
            if from_route == to_route or target["count"] <= 0:
                continue
            expected_gain = expected_gain_between(source, target)
            score = gain_score(expected_gain)
            if score <= 0:
                continue
            confidence = proposal_confidence(source, target, stability_snapshot, feedback)
            if score < 0.03 and confidence < 0.8:
                continue
            proposals.append({
                "type": "routing_adjustment",
                "from": from_route,
                "to": to_route,
                "expected_gain": expected_gain,
                "confidence": confidence,
                "gain_score": score,
                "sample": {
                    "from_count": source["count"],
                    "to_count": target["count"],
                    "from_success_rate": source["success_rate"],
                    "to_success_rate": target["success_rate"],
                },
                "reason": [
                    "historical-route-comparison",
                    "stability-v6-input",
                    "safe-autonomous-proposal",
                ],
            })
    proposals = sorted(
        proposals,
        key=lambda item: (-item["confidence"], -item["gain_score"], item["from"], item["to"]),
    )[:8]
    return {"proposals": proposals, "route_metrics": rows}


def shadow_experiment(proposal, downloads):
    downloads = list(downloads or ())
    from_route = route_or_default((proposal or {}).get("from"))
    to_route = route_or_default((proposal or {}).get("to"))
    rows = route_metric_rows(downloads)
    source = rows[from_route]
    target = rows[to_route]
    sample_count = min(source.get("count", 0), target.get("count", 0))
    expected_gain = expected_gain_between(source, target)
    delta_cost = expected_gain["cost"]
    delta_success = expected_gain["success_rate"]
    delta_latency = expected_gain["latency"]
    better = (
        sample_count >= MIN_SHADOW_SAMPLE_SIZE
        and delta_success >= -0.01
        and (delta_cost <= -0.03 or delta_latency <= -0.03 or delta_success >= 0.03)
    )
    worse = (
        sample_count < MIN_SHADOW_SAMPLE_SIZE
        or delta_success < -0.03
        or delta_cost > 0.08
        or delta_latency > 0.08
    )
    if better:
        result = "better"
    elif worse:
        result = "worse"
    else:
        result = "neutral"
    return {
        "shadow_result": result,
        "delta_cost": delta_cost,
        "delta_success_rate": delta_success,
        "delta_latency": delta_latency,
        "sample_count": sample_count,
        "from": from_route,
        "to": to_route,
        "production_traffic_changed": False,
        "reason": [
            "shadow-only-cloned-sample",
            "no-production-mutation",
        ],
    }


def policy_mutation_controller(proposal, shadow_result):
    proposal = proposal or {}
    shadow_result = shadow_result or {}
    confidence = float(proposal.get("confidence") or 0)
    if shadow_result.get("shadow_result") != "better":
        return {
            "decision": "reject",
            "allowed": False,
            "reason": "shadow-result-not-better",
            "production_behavior_changed": False,
        }
    if confidence < MUTATION_CONFIDENCE_THRESHOLD:
        return {
            "decision": "delay",
            "allowed": False,
            "reason": "confidence-below-threshold",
            "production_behavior_changed": False,
        }
    return {
        "decision": "allow_apply",
        "allowed": True,
        "reason": "shadow-validated-and-confidence-high",
        "production_behavior_changed": False,
        "requires_approval_gate": True,
    }


def rollback_engine(applied_policy, current_metrics, previous_policy=None):
    applied_policy = applied_policy or {}
    current_metrics = current_metrics or {}
    previous_policy = previous_policy or {}
    success_delta = float(current_metrics.get("success_rate_delta") or 0)
    cost_delta = float(current_metrics.get("cost_delta") or 0)
    latency_delta = float(current_metrics.get("latency_delta") or 0)
    error_spike = float(current_metrics.get("error_spike") or 0)
    triggers = []
    if success_delta <= -0.03:
        triggers.append("success-rate-drop")
    if cost_delta >= 0.08:
        triggers.append("cost-increase")
    if latency_delta >= 0.08:
        triggers.append("latency-increase")
    if error_spike >= 0.05:
        triggers.append("error-spike")
    rollback_needed = bool(triggers)
    return {
        "rollback_needed": rollback_needed,
        "action": "restore_previous_stable_policy" if rollback_needed else "keep_policy",
        "restore_policy": previous_policy if rollback_needed else applied_policy,
        "triggers": triggers,
        "production_behavior_changed": False,
        "reason": "rollback-guard-evaluated",
    }


def system_gain_metrics(shadow_results):
    results = list(shadow_results or ())
    better = [item for item in results if item.get("shadow_result") == "better"]
    return {
        "shadow_count": len(results),
        "better_count": len(better),
        "average_delta_cost": round(
            sum(float(item.get("delta_cost") or 0) for item in results) / max(1, len(results)),
            4,
        ),
        "average_delta_latency": round(
            sum(float(item.get("delta_latency") or 0) for item in results) / max(1, len(results)),
            4,
        ),
        "average_delta_success_rate": round(
            sum(float(item.get("delta_success_rate") or 0) for item in results) / max(1, len(results)),
            4,
        ),
        "better_rate": safe_ratio(len(better), len(results)),
        "better_rate_label": percent_label(safe_ratio(len(better), len(results))),
    }


def autonomous_evolution_engine(
    *,
    downloads=None,
    feedback=None,
    stability_snapshot=None,
    control_records=None,
    window_hours=24,
):
    downloads = list(downloads or ())
    control_records = list(control_records or ())
    feedback = feedback or {}
    stability_snapshot = stability_snapshot or {}
    opportunity = detect_optimization_opportunities(
        downloads,
        feedback=feedback,
        stability_snapshot=stability_snapshot,
    )
    shadow_results = [
        shadow_experiment(proposal, downloads)
        for proposal in opportunity["proposals"]
    ]
    mutation_decisions = [
        policy_mutation_controller(proposal, shadow)
        for proposal, shadow in zip(opportunity["proposals"], shadow_results)
    ]
    rollback_history = [
        rollback_engine(
            {"route": decision.get("decision"), "proposal": proposal},
            {
                "success_rate_delta": shadow.get("delta_success_rate", 0),
                "cost_delta": shadow.get("delta_cost", 0),
                "latency_delta": shadow.get("delta_latency", 0),
                "error_spike": 0,
            },
            previous_policy={"route": proposal.get("from")},
        )
        for proposal, shadow, decision in zip(opportunity["proposals"], shadow_results, mutation_decisions)
        if decision.get("decision") == "allow_apply"
    ]
    return {
        "version": AUTONOMOUS_EVOLUTION_VERSION,
        "window_hours": normalize_window_hours(window_hours),
        "proposals": opportunity["proposals"],
        "shadow_results": shadow_results,
        "mutation_decisions": mutation_decisions,
        "applied_policies": [
            {"proposal": proposal, "decision": decision, "shadow": shadow}
            for proposal, shadow, decision in zip(opportunity["proposals"], shadow_results, mutation_decisions)
            if decision.get("decision") == "allow_apply"
        ],
        "rejected_policies": [
            {"proposal": proposal, "decision": decision, "shadow": shadow}
            for proposal, shadow, decision in zip(opportunity["proposals"], shadow_results, mutation_decisions)
            if decision.get("decision") in {"reject", "delay"}
        ],
        "rollback_history": rollback_history,
        "system_gain_metrics": system_gain_metrics(shadow_results),
        "route_metrics": opportunity["route_metrics"],
        "provider_metrics": provider_metric_rows(downloads),
        "policy_execution_history": policy_execution_history(control_records),
        "feedback_risk_score": feedback_risk_score(feedback),
        "production_behavior_changed": False,
        "automatic_execution": False,
        "safety_note": "v7 evaluates autonomous evolution with shadow experiments and safety gates, but does not directly mutate production.",
    }


def build_autonomous_evolution_context(*, window_hours=24):
    window_hours = normalize_window_hours(window_hours)
    now = timezone.now()
    since = now - timezone.timedelta(hours=window_hours)
    downloads = list(operational_download_logs(DownloadRequestLog.objects.filter(created_at__gte=since)))
    _control_records = list(PolicyControlRecord.objects.filter(created_at__gte=since))
    feedback = build_feedback(window_hours=window_hours)
    stability = build_stabilization_snapshot(window_hours=window_hours)
    result = autonomous_evolution_engine(
        downloads=downloads,
        feedback=feedback,
        stability_snapshot=stability,
        control_records=_control_records,
        window_hours=window_hours,
    )
    return {
        "title": "Autonomous Evolution v7",
        "generated_at": format_admin_datetime(now),
        "window_started_at": format_admin_datetime(since),
        "window_ended_at": format_admin_datetime(now),
        "download_count": len(downloads),
        "control_record_count": len(_control_records),
        "stability": stability,
        **result,
        "gain_cost_label": format_bytes(abs(int(result["system_gain_metrics"]["average_delta_cost"] * 1024 * 1024))),
    }
