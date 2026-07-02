from ..models import (
    DownloadRequestLog,
    ObservabilityStatus,
    PolicyControlState,
    PolicyOptimizationProposal,
    PolicyOptimizationStatus,
)
from .control_state_machine import create_control_record, transition_control_record
from .download_routing import safe_download_size


CONTROL_EXECUTION_TRACKER_VERSION = "control_execution_tracker_v1"


def failure_pattern_for_execution(*, route="", provider="", error_type="", error_code="", failure_type=""):
    reason = str(failure_type or error_type or error_code or "execution_failed").strip() or "execution_failed"
    return ":".join((
        str(route or "unknown")[:32],
        str(provider or "unknown")[:80],
        reason[:48],
    ))[:160]


def execution_trace(record):
    return {
        "record_id": record.id,
        "request_id": record.request_id,
        "policy_decision": record.policy_decision,
        "control_decision": record.control_decision,
        "state": record.state,
        "execution_result": record.execution_result,
        "provider": record.provider,
        "route": record.route,
        "cost": record.cost,
        "transition_history": record.transition_history,
    }


def record_control_execution(
    *,
    proposal=None,
    request_id="",
    policy_decision="",
    control_decision="execution_observed",
    status=ObservabilityStatus.SUCCESS,
    provider="",
    route="",
    cost=0,
    policy_confidence=0,
    error_type="",
    error_code="",
    failure_type="",
    source="download_observability",
):
    success = status == ObservabilityStatus.SUCCESS or status is True
    target_state = PolicyControlState.EXECUTED if success else PolicyControlState.FAILED
    execution_result = "success" if success else "failed"
    failure_pattern = "" if success else failure_pattern_for_execution(
        route=route,
        provider=provider,
        error_type=error_type,
        error_code=error_code,
        failure_type=failure_type,
    )
    existing_proposal = proposal if isinstance(proposal, PolicyOptimizationProposal) else None
    record = None
    if existing_proposal is not None:
        record = existing_proposal.control_records.exclude(state__in=[
            PolicyControlState.REJECTED,
            PolicyControlState.FAILED,
            PolicyControlState.EXECUTED,
        ]).order_by("-created_at").first()
    if record is None:
        initial_state = PolicyControlState.AUTO_APPLIED
        if existing_proposal is not None:
            if existing_proposal.status in {
                PolicyOptimizationStatus.APPROVED,
                PolicyOptimizationStatus.APPLIED,
            }:
                initial_state = PolicyControlState.APPROVED
            elif existing_proposal.status == PolicyOptimizationStatus.AUTO_APPLIED:
                initial_state = PolicyControlState.AUTO_APPLIED
            else:
                initial_state = PolicyControlState.PENDING
        record = create_control_record(
            proposal=existing_proposal,
            request_id=request_id,
            policy_decision=policy_decision or route,
            policy_snapshot={
                "policy_confidence": float(policy_confidence or 0),
                "tracker": CONTROL_EXECUTION_TRACKER_VERSION,
            },
            control_decision=control_decision,
            provider=provider,
            route=route,
            cost=safe_download_size(cost),
            control_confidence=float(policy_confidence or 0),
            source=source,
            state=initial_state,
        )
    transition_control_record(
        record,
        target_state,
        reason="download-execution-observed",
        request_id=request_id,
        policy_decision=policy_decision or route,
        control_decision=control_decision,
        provider=provider,
        route=route,
        cost=safe_download_size(cost),
        control_confidence=float(policy_confidence or 0),
        source=source,
        execution_result=execution_result,
        execution_snapshot={
            "status": str(status or ""),
            "error_type": str(error_type or "")[:120],
            "error_code": str(error_code or "")[:80],
            "failure_type": str(failure_type or "")[:64],
        },
        failure_pattern=failure_pattern,
    )
    return record


def should_track_download_log(log):
    return bool(
        getattr(log, "final_policy_route", "")
        or getattr(log, "policy_active", False)
        or getattr(log, "policy_decision_source", "")
        or getattr(log, "route_type", "")
    )


def track_control_execution_from_download_log(log):
    if not isinstance(log, DownloadRequestLog) or not should_track_download_log(log):
        return None
    return record_control_execution(
        request_id=log.request_id,
        policy_decision=log.final_policy_route or log.route_type,
        control_decision=log.policy_decision_source or "download_policy_observed",
        status=log.status,
        provider=log.provider,
        route=log.route_type,
        cost=log.actual_bytes or log.estimated_bytes,
        policy_confidence=log.policy_confidence,
        error_type=log.error_type,
        error_code=log.error_code,
        failure_type=log.failure_type,
    )
