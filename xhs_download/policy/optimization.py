import hashlib
import json

from django.utils import timezone

from ..models import (
    PolicyOptimizationProposal,
    PolicyOptimizationStatus,
    PolicyRuntimeSetting,
)
from .evolution import (
    POLICY_EVOLUTION_VERSION,
    SAFETY_APPROVE,
    SAFETY_REJECT,
    SAFETY_REVIEW,
    build_policy_evolution_recommendation,
    max_abs_weight_delta,
)


POLICY_OPTIMIZATION_VERSION = "v5"


def stable_json(value):
    return json.dumps(value or {}, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def suggestion_signature(suggestion):
    payload = {
        "kind": (suggestion or {}).get("kind"),
        "policy_area": (suggestion or {}).get("policy_area"),
        "proposed_change": (suggestion or {}).get("proposed_change") or {},
        "source": (suggestion or {}).get("source"),
    }
    return hashlib.sha256(stable_json(payload).encode("utf-8")).hexdigest()


def drift_key_for_suggestion(suggestion):
    return f"{(suggestion or {}).get('policy_area') or 'policy'}:{(suggestion or {}).get('kind') or 'unknown'}"


def evaluate_policy_drift(suggestion, *, window_hours=168):
    drift_key = drift_key_for_suggestion(suggestion)
    since = timezone.now() - timezone.timedelta(hours=int(window_hours or 168))
    recent_auto_applied = PolicyOptimizationProposal.objects.filter(
        drift_key=drift_key,
        status=PolicyOptimizationStatus.AUTO_APPLIED,
        created_at__gte=since,
    ).count()
    proposed = (suggestion or {}).get("proposed_change") or {}
    max_delta = max_abs_weight_delta(proposed.get("delta") or {})
    drift_score = min(1.0, round(float(max_delta or 0) + recent_auto_applied * 0.15, 4))
    if recent_auto_applied >= 3 or drift_score >= 0.65:
        decision = SAFETY_REVIEW
        reason = "policy-drift-threshold-requires-human-review"
    else:
        decision = SAFETY_APPROVE
        reason = "policy-drift-within-bounds"
    return {
        "drift_key": drift_key,
        "window_hours": int(window_hours or 168),
        "recent_auto_applied": recent_auto_applied,
        "max_weight_delta": round(float(max_delta or 0), 4),
        "drift_score": drift_score,
        "decision": decision,
        "reason": reason,
    }


def proposal_status_for(suggestion, drift):
    safety = (suggestion or {}).get("safety") or {}
    safety_decision = safety.get("decision")
    if safety_decision == SAFETY_REJECT:
        return PolicyOptimizationStatus.REJECTED
    if safety_decision == SAFETY_REVIEW or (drift or {}).get("decision") == SAFETY_REVIEW:
        return PolicyOptimizationStatus.NEEDS_APPROVAL
    if safety_decision == SAFETY_APPROVE:
        return PolicyOptimizationStatus.AUTO_APPLIED
    return PolicyOptimizationStatus.QUEUED


def apply_policy_runtime_patch(proposal, *, allow_runtime_mutation=False, runtime_setting=None):
    if not allow_runtime_mutation:
        return {
            "behavior_changed": False,
            "runtime_mutated": False,
            "reason": "runtime-mutation-not-enabled",
        }
    if proposal.kind != "adaptive_policy_weights":
        return {
            "behavior_changed": False,
            "runtime_mutated": False,
            "reason": "unsupported-policy-optimization-kind",
        }

    runtime_setting = runtime_setting or PolicyRuntimeSetting.load()
    before = {
        "v4_auto_weights_enabled": runtime_setting.v4_auto_weights_enabled,
        "v4_adaptive_shadow_enabled": runtime_setting.v4_adaptive_shadow_enabled,
    }
    runtime_setting.v4_adaptive_shadow_enabled = True
    runtime_setting.v4_auto_weights_enabled = True
    runtime_setting.save(update_fields=[
        "v4_adaptive_shadow_enabled",
        "v4_auto_weights_enabled",
        "updated_at",
    ])
    after = {
        "v4_auto_weights_enabled": runtime_setting.v4_auto_weights_enabled,
        "v4_adaptive_shadow_enabled": runtime_setting.v4_adaptive_shadow_enabled,
    }
    return {
        "behavior_changed": before != after,
        "runtime_mutated": before != after,
        "reason": "enabled-v4-auto-weights-for-low-risk-adaptive-suggestion",
        "before": before,
        "after": after,
    }


def create_policy_optimization_proposal(
    suggestion,
    *,
    window_hours=24,
    auto_apply_low_risk=True,
    allow_runtime_mutation=False,
    actor_username="",
):
    safety = (suggestion or {}).get("safety") or {}
    drift = evaluate_policy_drift(suggestion)
    status = proposal_status_for(suggestion, drift)
    automatic_execution = False
    application_result = {
        "behavior_changed": False,
        "runtime_mutated": False,
        "reason": "not-applied",
    }

    if status == PolicyOptimizationStatus.AUTO_APPLIED and auto_apply_low_risk:
        automatic_execution = True
    elif status == PolicyOptimizationStatus.AUTO_APPLIED:
        status = PolicyOptimizationStatus.NEEDS_APPROVAL

    proposal = PolicyOptimizationProposal.objects.create(
        source_version=POLICY_OPTIMIZATION_VERSION,
        source_window_hours=int(window_hours or 24),
        status=status,
        kind=str((suggestion or {}).get("kind") or "unknown"),
        policy_area=str((suggestion or {}).get("policy_area") or ""),
        title=str((suggestion or {}).get("title") or "")[:160],
        body=str((suggestion or {}).get("body") or ""),
        risk=str(safety.get("risk") or ""),
        safety_decision=str(safety.get("decision") or ""),
        confidence=float((suggestion or {}).get("confidence") or 0),
        sample_count=int((suggestion or {}).get("sample_count") or 0),
        proposed_change=(suggestion or {}).get("proposed_change") or {},
        safety=safety,
        drift=drift,
        reason_chain=list((suggestion or {}).get("reason_chain") or ()),
        signature=suggestion_signature(suggestion),
        drift_key=drift.get("drift_key") or "",
        automatic_execution=automatic_execution,
        actor_username=actor_username or "",
    )

    if automatic_execution:
        application_result = apply_policy_runtime_patch(
            proposal,
            allow_runtime_mutation=allow_runtime_mutation,
        )
        proposal.application_result = application_result
        proposal.runtime_mutated = bool(application_result.get("runtime_mutated"))
        proposal.applied_at = timezone.now()
        proposal.save(update_fields=[
            "application_result",
            "runtime_mutated",
            "applied_at",
            "updated_at",
        ])

    return proposal


def build_policy_optimization_queue(
    *,
    window_hours=24,
    auto_apply_low_risk=True,
    allow_runtime_mutation=False,
    actor_username="",
):
    evolution = build_policy_evolution_recommendation(window_hours=window_hours)
    proposals = [
        create_policy_optimization_proposal(
            suggestion,
            window_hours=window_hours,
            auto_apply_low_risk=auto_apply_low_risk,
            allow_runtime_mutation=allow_runtime_mutation,
            actor_username=actor_username,
        )
        for suggestion in evolution.get("suggestions") or ()
    ]
    return {
        "version": POLICY_OPTIMIZATION_VERSION,
        "source_version": POLICY_EVOLUTION_VERSION,
        "window_hours": int(window_hours or 24),
        "proposal_count": len(proposals),
        "auto_applied_count": sum(
            proposal.status == PolicyOptimizationStatus.AUTO_APPLIED
            for proposal in proposals
        ),
        "needs_approval_count": sum(
            proposal.status == PolicyOptimizationStatus.NEEDS_APPROVAL
            for proposal in proposals
        ),
        "rejected_count": sum(
            proposal.status == PolicyOptimizationStatus.REJECTED
            for proposal in proposals
        ),
        "proposal_ids": [proposal.id for proposal in proposals],
        "production_behavior_changed": any(
            bool((proposal.application_result or {}).get("behavior_changed"))
            for proposal in proposals
        ),
    }
