from django.utils import timezone

from ..models import (
    PolicyControlRecord,
    PolicyControlState,
    PolicyOptimizationProposal,
)
from .download_routing import safe_download_size


CONTROL_STATE_VERSION = "control_state_machine_v1"


VALID_CONTROL_TRANSITIONS = {
    PolicyControlState.PENDING: {
        PolicyControlState.APPROVED,
        PolicyControlState.REJECTED,
        PolicyControlState.AUTO_APPLIED,
        PolicyControlState.FAILED,
    },
    PolicyControlState.APPROVED: {
        PolicyControlState.EXECUTED,
        PolicyControlState.FAILED,
    },
    PolicyControlState.AUTO_APPLIED: {
        PolicyControlState.EXECUTED,
        PolicyControlState.FAILED,
    },
    PolicyControlState.REJECTED: set(),
    PolicyControlState.EXECUTED: {
        PolicyControlState.FAILED,
    },
    PolicyControlState.FAILED: set(),
}


def normalize_control_state(value, default=PolicyControlState.PENDING):
    value = str(value or "").strip()
    valid = {choice[0] for choice in PolicyControlState.choices}
    return value if value in valid else default


def control_transition_allowed(from_state, to_state):
    from_state = normalize_control_state(from_state)
    to_state = normalize_control_state(to_state)
    return from_state == to_state or to_state in VALID_CONTROL_TRANSITIONS.get(from_state, set())


def transition_event(from_state, to_state, *, reason="", actor_username=""):
    return {
        "at": timezone.now().isoformat(),
        "from": str(from_state or ""),
        "to": str(to_state or ""),
        "reason": str(reason or ""),
        "actor_username": str(actor_username or ""),
        "state_machine": CONTROL_STATE_VERSION,
    }


def append_transition_history(record, to_state, *, reason="", actor_username=""):
    history = list(record.transition_history or [])
    history.append(
        transition_event(
            record.state,
            to_state,
            reason=reason,
            actor_username=actor_username,
        )
    )
    return history[-50:]


def create_control_record(
    *,
    proposal=None,
    request_id="",
    policy_decision="",
    policy_snapshot=None,
    control_decision="",
    state=PolicyControlState.PENDING,
    execution_result="",
    execution_snapshot=None,
    provider="",
    route="",
    cost=0,
    control_confidence=0,
    source="",
    actor_username="",
):
    state = normalize_control_state(state)
    record = PolicyControlRecord.objects.create(
        proposal=proposal if isinstance(proposal, PolicyOptimizationProposal) else None,
        request_id=str(request_id or "")[:128],
        policy_decision=str(policy_decision or "")[:80],
        policy_snapshot=policy_snapshot or {},
        control_decision=str(control_decision or "")[:32],
        state=state,
        execution_result=str(execution_result or "")[:32],
        execution_snapshot=execution_snapshot or {},
        provider=str(provider or "")[:80],
        route=str(route or "")[:32],
        cost=safe_download_size(cost),
        control_confidence=float(control_confidence or 0),
        source=str(source or "")[:64],
        actor_username=str(actor_username or "")[:150],
        transition_history=[
            transition_event("", state, reason="create-control-record", actor_username=actor_username)
        ],
    )
    return record


def transition_control_record(record, to_state, *, reason="", actor_username="", **updates):
    to_state = normalize_control_state(to_state)
    if not control_transition_allowed(record.state, to_state):
        raise ValueError(f"Invalid control transition: {record.state} -> {to_state}")

    update_fields = ["state", "transition_history", "updated_at"]
    record.transition_history = append_transition_history(
        record,
        to_state,
        reason=reason,
        actor_username=actor_username,
    )
    record.state = to_state

    scalar_fields = {
        "request_id",
        "policy_decision",
        "control_decision",
        "execution_result",
        "provider",
        "route",
        "failure_pattern",
        "source",
        "actor_username",
    }
    json_fields = {
        "policy_snapshot",
        "execution_snapshot",
        "feedback_summary",
    }
    numeric_fields = {
        "cost",
        "control_confidence",
        "drift_score",
        "consistency_score",
    }
    for field, value in updates.items():
        if field in scalar_fields:
            max_len_by_field = {
                "actor_username": 150,
                "failure_pattern": 160,
                "request_id": 128,
                "policy_decision": 80,
                "control_decision": 32,
                "execution_result": 32,
                "provider": 80,
                "route": 32,
                "source": 64,
            }
            max_len = max_len_by_field.get(field, 80)
            setattr(record, field, str(value or "")[:max_len])
            update_fields.append(field)
        elif field in json_fields:
            setattr(record, field, value or {})
            update_fields.append(field)
        elif field == "cost":
            record.cost = safe_download_size(value)
            update_fields.append(field)
        elif field in numeric_fields:
            try:
                setattr(record, field, float(value or 0))
            except (TypeError, ValueError):
                setattr(record, field, 0)
            update_fields.append(field)

    record.save(update_fields=sorted(set(update_fields)))
    return record


def state_for_control_decision(control_decision):
    control_decision = str(control_decision or "")
    if control_decision == "auto_apply":
        return PolicyControlState.AUTO_APPLIED
    if control_decision == "require_approval":
        return PolicyControlState.PENDING
    if control_decision == "reject":
        return PolicyControlState.REJECTED
    return PolicyControlState.PENDING


def record_control_decision_for_proposal(proposal, *, risk=None, gate=None, actor_username=""):
    risk = risk or {}
    gate = gate or {}
    target_state = state_for_control_decision(proposal.control_decision)
    record = proposal.control_records.order_by("-created_at").first()
    if record is None:
        record = create_control_record(
            proposal=proposal,
            policy_decision=proposal.kind,
            policy_snapshot={
                "policy_area": proposal.policy_area,
                "risk": proposal.risk,
                "risk_score": proposal.risk_score,
                "safety_decision": proposal.safety_decision,
                "proposed_change": proposal.proposed_change,
            },
            control_decision=proposal.control_decision,
            control_confidence=proposal.confidence,
            source="policy_control_layer",
            actor_username=actor_username or proposal.actor_username,
        )
    else:
        record.control_decision = proposal.control_decision
        record.policy_snapshot = {
            **(record.policy_snapshot or {}),
            "policy_area": proposal.policy_area,
            "risk": proposal.risk,
            "risk_score": proposal.risk_score,
            "safety_decision": proposal.safety_decision,
            "proposed_change": proposal.proposed_change,
        }
        record.save(update_fields=["control_decision", "policy_snapshot", "updated_at"])

    if record.state != target_state:
        transition_control_record(
            record,
            target_state,
            reason=(gate or {}).get("reason") or "control-decision-recorded",
            actor_username=actor_username or proposal.actor_username,
            feedback_summary={
                "risk": risk,
                "gate": gate,
            },
        )
    return record
