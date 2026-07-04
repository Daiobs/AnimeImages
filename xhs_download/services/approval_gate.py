from django.utils import timezone

from ..models import (
    PolicyControlState,
    PolicyOptimizationStatus,
    PolicyRuntimeSetting,
)
from ..policy.evolution import SAFETY_REJECT
from .control_state_machine import transition_control_record
from .policy_risk_engine import RISK_HIGH_MIN, RISK_LOW_MAX


CONTROL_AUTO_APPLY = "auto_apply"
CONTROL_REQUIRE_APPROVAL = "require_approval"
CONTROL_REJECT = "reject"


def approval_gate_decision(risk, suggestion=None):
    suggestion = suggestion or {}
    safety = suggestion.get("safety") or {}
    risk_score = float((risk or {}).get("risk_score") or 0)
    reasons = ["approval-gate"]
    if safety.get("decision") == SAFETY_REJECT or suggestion.get("automatic_execution"):
        return {
            "decision": CONTROL_REJECT,
            "reason": "unsafe-suggestion-rejected",
            "reason_chain": reasons + ["safety-reject-or-unapproved-automatic-execution"],
        }
    if risk_score >= 0.9:
        return {
            "decision": CONTROL_REJECT,
            "reason": "extreme-risk-rejected",
            "reason_chain": reasons + ["risk-score-above-reject-threshold"],
        }
    if risk_score >= RISK_LOW_MAX:
        return {
            "decision": CONTROL_REQUIRE_APPROVAL,
            "reason": "human-approval-required",
            "reason_chain": reasons + ["risk-score-requires-approval"],
        }
    return {
        "decision": CONTROL_AUTO_APPLY,
        "reason": "low-risk-auto-apply",
        "reason_chain": reasons + ["risk-score-low"],
    }


def status_for_control_decision(decision):
    if decision == CONTROL_AUTO_APPLY:
        return PolicyOptimizationStatus.AUTO_APPLIED
    if decision == CONTROL_REQUIRE_APPROVAL:
        return PolicyOptimizationStatus.NEEDS_APPROVAL
    if decision == CONTROL_REJECT:
        return PolicyOptimizationStatus.REJECTED
    return PolicyOptimizationStatus.QUEUED


def approve_policy_change(proposal, *, actor_username=""):
    if proposal.status == PolicyOptimizationStatus.REJECTED:
        return {
            "ok": False,
            "status": proposal.status,
            "reason": "rejected-proposal-cannot-be-approved",
        }
    proposal.status = PolicyOptimizationStatus.APPROVED
    proposal.approved_at = timezone.now()
    proposal.actor_username = actor_username or proposal.actor_username
    proposal.application_result = {
        **(proposal.application_result or {}),
        "approval": "approved",
        "behavior_changed": False,
        "runtime_mutated": False,
        "reason": "human-approved-without-runtime-mutation",
    }
    proposal.runtime_mutated = False
    proposal.save(update_fields=[
        "status",
        "approved_at",
        "actor_username",
        "application_result",
        "runtime_mutated",
        "updated_at",
    ])
    for record in proposal.control_records.all():
        if record.state == PolicyControlState.PENDING:
            transition_control_record(
                record,
                PolicyControlState.APPROVED,
                reason="human-approved",
                actor_username=actor_username,
            )
    return {"ok": True, "status": proposal.status, "reason": "approved"}


def reject_policy_change(proposal, *, actor_username="", reason="human-rejected"):
    proposal.status = PolicyOptimizationStatus.REJECTED
    proposal.rejected_at = timezone.now()
    proposal.actor_username = actor_username or proposal.actor_username
    proposal.application_result = {
        **(proposal.application_result or {}),
        "approval": "rejected",
        "behavior_changed": False,
        "runtime_mutated": False,
        "reason": reason,
    }
    proposal.runtime_mutated = False
    proposal.save(update_fields=[
        "status",
        "rejected_at",
        "actor_username",
        "application_result",
        "runtime_mutated",
        "updated_at",
    ])
    for record in proposal.control_records.all():
        if record.state == PolicyControlState.PENDING:
            transition_control_record(
                record,
                PolicyControlState.REJECTED,
                reason=reason,
                actor_username=actor_username,
            )
    return {"ok": True, "status": proposal.status, "reason": reason}


def rollback_policy_change(proposal, *, actor_username=""):
    result = {
        "ok": True,
        "behavior_changed": False,
        "runtime_mutated": False,
        "reason": "no-runtime-mutation-to-rollback",
    }
    application = proposal.application_result or {}
    before = application.get("before") or {}
    if proposal.runtime_mutated and before:
        runtime_setting = PolicyRuntimeSetting.load()
        allowed_fields = {
            "v4_auto_weights_enabled",
            "v4_adaptive_shadow_enabled",
        }
        changed_fields = []
        for field, value in before.items():
            if field not in allowed_fields:
                continue
            if hasattr(runtime_setting, field) and getattr(runtime_setting, field) != value:
                setattr(runtime_setting, field, value)
                changed_fields.append(field)
        if changed_fields:
            runtime_setting.save(update_fields=changed_fields + ["updated_at"])
            result = {
                "ok": True,
                "behavior_changed": True,
                "runtime_mutated": True,
                "reason": "runtime-setting-restored",
                "changed_fields": changed_fields,
            }

    proposal.status = PolicyOptimizationStatus.ROLLED_BACK
    proposal.rolled_back_at = timezone.now()
    proposal.actor_username = actor_username or proposal.actor_username
    proposal.rollback_result = result
    proposal.runtime_mutated = False
    proposal.save(update_fields=[
        "status",
        "rolled_back_at",
        "actor_username",
        "rollback_result",
        "runtime_mutated",
        "updated_at",
    ])
    for record in proposal.control_records.exclude(state__in=[
        PolicyControlState.REJECTED,
        PolicyControlState.FAILED,
    ]):
        transition_control_record(
            record,
            PolicyControlState.FAILED,
            reason="rollback-recorded",
            actor_username=actor_username,
            execution_result="rolled_back",
            execution_snapshot=result,
            failure_pattern="rollback:policy-control",
        )
    return result
