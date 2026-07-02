from django.utils import timezone

from ..models import (
    DownloadRequestLog,
    PolicyControlRecord,
    PolicyControlState,
)
from .autonomous_evolution_engine import autonomous_evolution_engine
from .download_routing import safe_download_size
from .feedback_loop import build_feedback
from .production_intelligence import (
    format_admin_datetime,
    normalize_window_hours,
    percent_label,
    safe_ratio,
)
from .stabilization_engine import bounded, build_stabilization_snapshot
from .policy_constants import GOVERNANCE_VERSION


MAX_POLICY_CHANGE_RATIO = 0.10
DAILY_MUTATION_LIMIT = 5
ROLLBACK_FAILURE_RATE_THRESHOLD = 0.20
HIGH_RISK_THRESHOLD = 0.70
CRITICAL_RISK_THRESHOLD = 0.88


def risk_classification(risk_score):
    risk_score = bounded(risk_score)
    if risk_score >= CRITICAL_RISK_THRESHOLD:
        return "critical"
    if risk_score >= HIGH_RISK_THRESHOLD:
        return "high"
    if risk_score >= 0.40:
        return "medium"
    return "low"


def proposal_change_magnitude(proposal):
    expected_gain = (proposal or {}).get("expected_gain") or {}
    values = []
    for key in ("cost", "latency", "success_rate"):
        try:
            values.append(abs(float(expected_gain.get(key) or 0)))
        except (TypeError, ValueError):
            values.append(0.0)
    return bounded(max(values or [0.0]))


def provider_volatility_score(provider_metrics):
    rows = list(provider_metrics or ())
    if not rows:
        return 0.0
    weighted_failure = 0.0
    total = 0
    for row in rows:
        count = safe_download_size(row.get("count"))
        weighted_failure += float(row.get("failure_rate") or 0) * count
        total += count
    return bounded(weighted_failure / max(1, total))


def traffic_anomaly_score(evolution_result):
    feedback_risk = float((evolution_result or {}).get("feedback_risk_score") or 0)
    history = (evolution_result or {}).get("policy_execution_history") or {}
    failure_rate = float(history.get("failure_rate") or 0)
    gain = (evolution_result or {}).get("system_gain_metrics") or {}
    worse_shadow_rate = 1 - float(gain.get("better_rate") or 0)
    return bounded(feedback_risk * 0.45 + failure_rate * 0.35 + worse_shadow_rate * 0.20)


def routing_instability_score(evolution_result):
    stability = (evolution_result or {}).get("stability") or {}
    stability_score = float(stability.get("stability_score") or 0.5)
    control = (evolution_result or {}).get("policy_execution_history") or {}
    control_failure = float(control.get("failure_rate") or 0)
    return bounded((1 - stability_score) * 0.65 + control_failure * 0.35)


def risk_governor(proposal, shadow_result, mutation_decision, evolution_result=None):
    proposal = proposal or {}
    shadow_result = shadow_result or {}
    mutation_decision = mutation_decision or {}
    evolution_result = evolution_result or {}
    reasons = ["risk-governor"]
    change_magnitude = proposal_change_magnitude(proposal)
    score = 0.10

    if shadow_result.get("shadow_result") != "better":
        score += 0.35
        reasons.append("shadow-not-better")
    if not mutation_decision.get("allowed"):
        score += 0.25
        reasons.append("mutation-controller-not-allowed")
    if change_magnitude > MAX_POLICY_CHANGE_RATIO:
        score += min(0.25, (change_magnitude - MAX_POLICY_CHANGE_RATIO) * 0.8)
        reasons.append("change-magnitude-over-limit")

    provider_volatility = provider_volatility_score(evolution_result.get("provider_metrics"))
    if provider_volatility >= 0.25:
        score += 0.18
        reasons.append("provider-volatility")
    elif provider_volatility >= 0.10:
        score += 0.08
        reasons.append("provider-volatility-moderate")

    traffic_anomaly = traffic_anomaly_score(evolution_result)
    if traffic_anomaly >= 0.35:
        score += 0.18
        reasons.append("traffic-anomaly")
    elif traffic_anomaly >= 0.18:
        score += 0.08
        reasons.append("traffic-anomaly-moderate")

    routing_instability = routing_instability_score(evolution_result)
    if routing_instability >= 0.35:
        score += 0.18
        reasons.append("routing-instability")
    elif routing_instability >= 0.18:
        score += 0.08
        reasons.append("routing-instability-moderate")

    risk_score = bounded(score)
    return {
        "risk_score": risk_score,
        "classification": risk_classification(risk_score),
        "change_magnitude": change_magnitude,
        "provider_volatility": provider_volatility,
        "traffic_anomaly": traffic_anomaly,
        "routing_instability": routing_instability,
        "reason": reasons,
    }


def daily_mutation_count(control_records):
    today = timezone.localdate()
    count = 0
    for record in control_records or ():
        created = getattr(record, "created_at", None)
        if created and timezone.localtime(created).date() != today:
            continue
        if getattr(record, "source", "") in {GOVERNANCE_VERSION, "autonomous_evolution_v7"}:
            count += 1
    return count


def evolution_limiter(proposal, risk, control_records=None, policy_history=None):
    proposal = proposal or {}
    risk = risk or {}
    policy_history = policy_history or {}
    limits = []
    change_magnitude = risk.get("change_magnitude")
    if change_magnitude is None:
        change_magnitude = proposal_change_magnitude(proposal)
    change_magnitude = float(change_magnitude or 0)

    capped_change = min(change_magnitude, MAX_POLICY_CHANGE_RATIO)
    if change_magnitude > MAX_POLICY_CHANGE_RATIO:
        limits.append({
            "key": "max_policy_change",
            "limit": MAX_POLICY_CHANGE_RATIO,
            "requested": round(change_magnitude, 4),
            "applied": round(capped_change, 4),
        })

    daily_count = daily_mutation_count(control_records)
    if daily_count >= DAILY_MUTATION_LIMIT:
        limits.append({
            "key": "daily_mutation_limit",
            "limit": DAILY_MUTATION_LIMIT,
            "requested": daily_count + 1,
            "applied": daily_count,
            "blocking": True,
        })

    failure_rate = float(policy_history.get("failure_rate") or 0)
    if failure_rate >= ROLLBACK_FAILURE_RATE_THRESHOLD:
        limits.append({
            "key": "rollback_threshold",
            "limit": ROLLBACK_FAILURE_RATE_THRESHOLD,
            "requested": round(failure_rate, 4),
            "applied": "deny-until-stable",
            "blocking": True,
        })

    return {
        "change_magnitude": round(change_magnitude, 4),
        "allowed_change": round(capped_change, 4),
        "daily_mutation_count": daily_count,
        "limits_applied": limits,
        "blocked_by_limit": any(item.get("blocking") for item in limits),
    }


def policy_guard(proposal, shadow_result, mutation_decision, risk, limiter):
    reasons = ["policy-guard"]
    risk_score = float((risk or {}).get("risk_score") or 0)
    classification = (risk or {}).get("classification") or risk_classification(risk_score)
    limits = (limiter or {}).get("limits_applied") or []

    if (limiter or {}).get("blocked_by_limit"):
        reasons.append("evolution-limiter-blocked")
        return "deny", reasons
    if classification in {"high", "critical"}:
        reasons.append("risk-high-or-critical")
        return "deny", reasons
    if (shadow_result or {}).get("shadow_result") != "better":
        reasons.append("shadow-not-better")
        return "deny", reasons
    if not (mutation_decision or {}).get("allowed"):
        reasons.append("mutation-not-allowed")
        return "deny", reasons
    if any(item.get("key") == "max_policy_change" for item in limits):
        reasons.append("change-capped-by-limiter")
        return "partial_allow", reasons
    reasons.append("governance-allow")
    return "allow", reasons


def governance_decision_for_item(proposal, shadow_result, mutation_decision, evolution_result, control_records):
    risk = risk_governor(proposal, shadow_result, mutation_decision, evolution_result)
    limiter = evolution_limiter(
        proposal,
        risk,
        control_records=control_records,
        policy_history=(evolution_result or {}).get("policy_execution_history") or {},
    )
    decision, guard_reasons = policy_guard(
        proposal,
        shadow_result,
        mutation_decision,
        risk,
        limiter,
    )
    return {
        "version": GOVERNANCE_VERSION,
        "decision": decision,
        "risk_score": risk["risk_score"],
        "risk_classification": risk["classification"],
        "reason": [
            *risk["reason"],
            *guard_reasons,
        ],
        "limits_applied": limiter["limits_applied"],
        "proposal": proposal,
        "shadow_result": shadow_result,
        "mutation_decision": mutation_decision,
        "allowed_change": limiter["allowed_change"],
        "production_behavior_changed": False,
        "audit_required": True,
        "rollback_required": decision == "deny",
    }


def system_safety_status(governance_decisions, evolution_result):
    decisions = list(governance_decisions or ())
    max_risk = max((float(item.get("risk_score") or 0) for item in decisions), default=0)
    partial_count = sum(1 for item in decisions if item.get("decision") == "partial_allow")
    allow_count = sum(1 for item in decisions if item.get("decision") == "allow")
    deny_count = sum(1 for item in decisions if item.get("decision") == "deny")
    mutation_rate = safe_ratio(allow_count + partial_count, len(decisions))
    stability = (evolution_result or {}).get("stability") or {}
    stability_index = bounded(float(stability.get("stability_score") or 0.5))
    if max_risk >= HIGH_RISK_THRESHOLD or deny_count:
        autonomy_level = "guarded_manual"
    elif partial_count:
        autonomy_level = "partial_guarded"
    elif allow_count:
        autonomy_level = "controlled_autonomous"
    else:
        autonomy_level = "observation_only"
    return {
        "system_autonomy_level": autonomy_level,
        "risk_exposure": round(max_risk, 4),
        "risk_exposure_label": risk_classification(max_risk),
        "mutation_rate": mutation_rate,
        "mutation_rate_label": percent_label(mutation_rate),
        "stability_index": stability_index,
        "stability_index_label": percent_label(stability_index),
        "allow_count": allow_count,
        "partial_allow_count": partial_count,
        "deny_count": deny_count,
    }


def governance_engine(*, evolution_result=None, control_records=None):
    evolution_result = evolution_result or {}
    control_records = list(control_records or ())
    proposals = evolution_result.get("proposals") or []
    shadow_results = evolution_result.get("shadow_results") or []
    mutation_decisions = evolution_result.get("mutation_decisions") or []
    decisions = []
    for index, proposal in enumerate(proposals):
        shadow = (
            shadow_results[index]
            if index < len(shadow_results)
            else {"shadow_result": "worse", "reason": ["missing-shadow-result"]}
        )
        mutation = (
            mutation_decisions[index]
            if index < len(mutation_decisions)
            else {"decision": "reject", "allowed": False, "reason": "missing-mutation-decision"}
        )
        decisions.append(
            governance_decision_for_item(proposal, shadow, mutation, evolution_result, control_records)
        )
    safety = system_safety_status(decisions, evolution_result)
    return {
        "version": GOVERNANCE_VERSION,
        "governance_decisions": decisions,
        "blocked_optimizations": [item for item in decisions if item["decision"] == "deny"],
        "partial_applied_changes": [item for item in decisions if item["decision"] == "partial_allow"],
        "allowed_changes": [item for item in decisions if item["decision"] == "allow"],
        "system_safety_status": safety,
        "production_behavior_changed": False,
        "automatic_execution": False,
        "safety_note": "v8 governs v7 autonomous proposals with risk scoring, mutation limits, policy guard, and audit-ready decisions.",
    }


def record_governance_audit_decision(decision, *, actor_username=""):
    decision = decision or {}
    state = PolicyControlState.REJECTED
    if decision.get("decision") in {"allow", "partial_allow"}:
        state = PolicyControlState.PENDING
    proposal = decision.get("proposal") or {}
    return PolicyControlRecord.objects.create(
        request_id="",
        policy_decision=f"governance:{decision.get('decision', 'unknown')}",
        policy_snapshot={
            "version": GOVERNANCE_VERSION,
            "governance_decision": decision,
            "production_behavior_changed": False,
        },
        control_decision=decision.get("decision", ""),
        state=state,
        execution_result="not_executed",
        provider=str(proposal.get("provider") or ""),
        route=str(proposal.get("to") or ""),
        control_confidence=1 - float(decision.get("risk_score") or 0),
        feedback_summary={
            "risk_score": decision.get("risk_score"),
            "risk_classification": decision.get("risk_classification"),
            "limits_applied": decision.get("limits_applied") or [],
        },
        failure_pattern=";".join(decision.get("reason") or [])[:160],
        source=GOVERNANCE_VERSION,
        actor_username=actor_username,
    )


def build_governance_v8_context(*, window_hours=24):
    window_hours = normalize_window_hours(window_hours)
    now = timezone.now()
    since = now - timezone.timedelta(hours=window_hours)
    downloads = list(DownloadRequestLog.objects.filter(created_at__gte=since))
    control_records = list(PolicyControlRecord.objects.filter(created_at__gte=since))
    feedback = build_feedback(window_hours=window_hours)
    stability = build_stabilization_snapshot(window_hours=window_hours)
    evolution = autonomous_evolution_engine(
        downloads=downloads,
        feedback=feedback,
        stability_snapshot=stability,
        control_records=control_records,
        window_hours=window_hours,
    )
    governance = governance_engine(
        evolution_result={**evolution, "stability": stability},
        control_records=control_records,
    )
    audit_records = list(
        PolicyControlRecord.objects.filter(source=GOVERNANCE_VERSION).order_by("-created_at")[:20]
    )
    return {
        "title": "Autonomous Governance v8",
        "generated_at": format_admin_datetime(now),
        "window_started_at": format_admin_datetime(since),
        "window_ended_at": format_admin_datetime(now),
        "window_hours": window_hours,
        "download_count": len(downloads),
        "control_record_count": len(control_records),
        "evolution": evolution,
        "governance": governance,
        "audit_records": audit_records,
    }
