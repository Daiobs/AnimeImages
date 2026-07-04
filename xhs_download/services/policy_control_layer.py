from ..models import PolicyOptimizationProposal
from ..policy.evolution import build_policy_evolution_recommendation
from ..policy.optimization import create_policy_optimization_proposal
from .approval_gate import (
    CONTROL_AUTO_APPLY,
    CONTROL_REJECT,
    CONTROL_REQUIRE_APPROVAL,
    approval_gate_decision,
    status_for_control_decision,
)
from .control_state_machine import record_control_decision_for_proposal
from .policy_risk_engine import score_policy_risk


POLICY_CONTROL_VERSION = "production_control_v1"


def control_item_for_suggestion(suggestion, *, window_hours=24):
    risk = score_policy_risk(suggestion, window_hours=window_hours)
    gate = approval_gate_decision(risk, suggestion)
    return {
        "suggestion": suggestion,
        "risk": risk,
        "gate": gate,
        "decision": gate["decision"],
        "impact_area": risk["impact_area"],
        "risk_score": risk["risk_score"],
    }


def summarize_control_decision(items):
    if any(item["decision"] == CONTROL_REJECT for item in items):
        return CONTROL_REJECT
    if any(item["decision"] == CONTROL_REQUIRE_APPROVAL for item in items):
        return CONTROL_REQUIRE_APPROVAL
    if items:
        return CONTROL_AUTO_APPLY
    return CONTROL_REQUIRE_APPROVAL


def evaluate_policy_control_layer(policy_suggestions, *, risk_score=None, impact_area="", window_hours=24):
    items = [control_item_for_suggestion(item, window_hours=window_hours) for item in policy_suggestions or ()]
    if risk_score is not None:
        for item in items:
            item["risk"]["risk_score"] = float(risk_score or 0)
            item["risk_score"] = item["risk"]["risk_score"]
            if impact_area:
                item["risk"]["impact_area"] = impact_area
                item["impact_area"] = impact_area
            item["gate"] = approval_gate_decision(item["risk"], item["suggestion"])
            item["decision"] = item["gate"]["decision"]
    decision = summarize_control_decision(items)
    approved = [
        item for item in items
        if item["decision"] == CONTROL_AUTO_APPLY
    ]
    blocked = [
        item for item in items
        if item["decision"] in {CONTROL_REQUIRE_APPROVAL, CONTROL_REJECT}
    ]
    reason = "policy-control-layer"
    if decision == CONTROL_AUTO_APPLY:
        reason = "all-suggestions-low-risk"
    elif decision == CONTROL_REQUIRE_APPROVAL:
        reason = "one-or-more-suggestions-require-human-approval"
    elif decision == CONTROL_REJECT:
        reason = "one-or-more-suggestions-rejected-by-safety-gate"
    return {
        "version": POLICY_CONTROL_VERSION,
        "decision": decision,
        "reason": reason,
        "risk_score": max((item["risk_score"] for item in items), default=0),
        "impact_area": impact_area or (items[0]["impact_area"] if items else ""),
        "approved_changes": approved,
        "blocked_changes": blocked,
        "items": items,
    }


def build_policy_control_preview(*, window_hours=24):
    evolution = build_policy_evolution_recommendation(window_hours=window_hours)
    control = evaluate_policy_control_layer(
        evolution.get("suggestions") or (),
        window_hours=window_hours,
    )
    return {
        "version": POLICY_CONTROL_VERSION,
        "window_hours": int(window_hours or 24),
        "evolution": evolution,
        "control": control,
        "policy_suggestions": evolution.get("suggestions") or [],
        "risk_score": control["risk_score"],
        "impact_area": control["impact_area"],
    }


def create_controlled_policy_proposals(*, window_hours=24, actor_username=""):
    preview = build_policy_control_preview(window_hours=window_hours)
    proposals = []
    for item in preview["control"]["items"]:
        proposal = create_policy_optimization_proposal(
            item["suggestion"],
            window_hours=window_hours,
            auto_apply_low_risk=item["decision"] == CONTROL_AUTO_APPLY,
            allow_runtime_mutation=False,
            actor_username=actor_username,
        )
        proposal.status = status_for_control_decision(item["decision"])
        proposal.risk_score = item["risk_score"]
        proposal.impact_area = item["impact_area"]
        proposal.control_decision = item["decision"]
        proposal.risk = item["risk"]["risk_level"]
        proposal.application_result = {
            **(proposal.application_result or {}),
            "behavior_changed": False,
            "runtime_mutated": False,
            "reason": "production-control-layer-does-not-mutate-runtime",
            "control_reason": item["gate"]["reason"],
        }
        proposal.runtime_mutated = False
        proposal.save(update_fields=[
            "status",
            "risk_score",
            "impact_area",
            "control_decision",
            "risk",
            "application_result",
            "runtime_mutated",
            "updated_at",
        ])
        record_control_decision_for_proposal(
            proposal,
            risk=item["risk"],
            gate=item["gate"],
            actor_username=actor_username,
        )
        proposals.append(proposal)

    return {
        **preview,
        "proposal_count": len(proposals),
        "proposal_ids": [proposal.id for proposal in proposals],
        "pending_approvals": PolicyOptimizationProposal.objects.filter(
            status="needs_approval",
        ).count(),
    }
