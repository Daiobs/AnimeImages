from collections import Counter, defaultdict

from django.utils import timezone

from ..models import PolicyControlRecord, PolicyControlState
from .download_routing import safe_download_size
from .production_intelligence import (
    format_admin_datetime,
    format_bytes,
    normalize_window_hours,
    percent_label,
    safe_ratio,
)


CONTROL_CONVERGENCE_VERSION = "control_convergence_v1"


def control_records_for_window(window_hours=24):
    window_hours = normalize_window_hours(window_hours)
    since = timezone.now() - timezone.timedelta(hours=window_hours)
    return PolicyControlRecord.objects.filter(created_at__gte=since)


def control_record_is_orphan(record):
    return not (
        str(record.state or "")
        and (str(record.policy_decision or "") or str(record.control_decision or ""))
    )


def find_orphan_control_decisions(*, window_hours=24):
    return [
        record
        for record in control_records_for_window(window_hours)
        if control_record_is_orphan(record)
    ]


def record_drift(record):
    policy_route = str(record.policy_decision or "")
    actual_route = str(record.route or "")
    if not policy_route or not actual_route:
        return 0.0
    return 0.0 if policy_route == actual_route else 1.0


def apply_control_feedback_to_records(*, window_hours=24):
    records = list(control_records_for_window(window_hours))
    if not records:
        return {"updated": 0, "failed_patterns": {}}

    state_counts = Counter(record.state for record in records)
    route_success = defaultdict(lambda: {"total": 0, "success": 0})
    provider_success = defaultdict(lambda: {"total": 0, "success": 0})
    failed_patterns = Counter()
    for record in records:
        is_terminal = record.state in {PolicyControlState.EXECUTED, PolicyControlState.FAILED}
        success = record.state == PolicyControlState.EXECUTED
        if is_terminal:
            if record.route:
                route_success[record.route]["total"] += 1
                route_success[record.route]["success"] += int(success)
            if record.provider:
                provider_success[record.provider]["total"] += 1
                provider_success[record.provider]["success"] += int(success)
        if record.failure_pattern:
            failed_patterns[record.failure_pattern] += 1

    updated = 0
    for record in records:
        route_row = route_success.get(record.route, {})
        provider_row = provider_success.get(record.provider, {})
        route_rate = safe_ratio(route_row.get("success", 0), route_row.get("total", 0))
        provider_rate = safe_ratio(provider_row.get("success", 0), provider_row.get("total", 0))
        consistency_score = 0.0 if control_record_is_orphan(record) else 1.0
        drift_score = record_drift(record)
        confidence_parts = [
            value for value in (
                float(record.control_confidence or 0),
                route_rate,
                provider_rate,
                1.0 - drift_score,
            )
            if value > 0
        ]
        control_confidence = round(sum(confidence_parts) / len(confidence_parts), 4) if confidence_parts else 0
        feedback_summary = {
            "version": CONTROL_CONVERGENCE_VERSION,
            "window_hours": normalize_window_hours(window_hours),
            "state_distribution": dict(state_counts),
            "route_success_rate": route_rate,
            "provider_success_rate": provider_rate,
            "drift_score": drift_score,
            "consistency_score": consistency_score,
        }
        record.feedback_summary = feedback_summary
        record.control_confidence = control_confidence
        record.drift_score = drift_score
        record.consistency_score = consistency_score
        record.save(update_fields=[
            "feedback_summary",
            "control_confidence",
            "drift_score",
            "consistency_score",
            "updated_at",
        ])
        updated += 1

    return {
        "updated": updated,
        "failed_patterns": dict(failed_patterns),
    }


def control_suggestion(level, title, body, reason):
    return {
        "level": level,
        "title": title,
        "body": body,
        "reason": reason,
        "automatic_execution": False,
    }


def build_control_convergence_snapshot(*, window_hours=24):
    window_hours = normalize_window_hours(window_hours)
    feedback_result = apply_control_feedback_to_records(window_hours=window_hours)
    records = list(control_records_for_window(window_hours))
    total = len(records)
    state_counts = Counter(record.state for record in records)
    executed = state_counts.get(PolicyControlState.EXECUTED, 0)
    failed = state_counts.get(PolicyControlState.FAILED, 0)
    approved = state_counts.get(PolicyControlState.APPROVED, 0)
    auto_applied = state_counts.get(PolicyControlState.AUTO_APPLIED, 0)
    rejected = state_counts.get(PolicyControlState.REJECTED, 0)
    pending = state_counts.get(PolicyControlState.PENDING, 0)
    execution_total = executed + failed
    execution_success_rate = safe_ratio(executed, execution_total)
    approval_rate = safe_ratio(approved + auto_applied + executed, total)
    rejection_rate = safe_ratio(rejected, total)
    pending_rate = safe_ratio(pending, total)
    orphan_records = [record for record in records if control_record_is_orphan(record)]
    consistency_score = round(1 - safe_ratio(len(orphan_records), total), 4) if total else 1.0
    drift_values = [record_drift(record) for record in records if record.policy_decision and record.route]
    drift_rate = round(sum(drift_values) / len(drift_values), 4) if drift_values else 0
    drift_score = round(1 - drift_rate, 4)
    failure_patterns = Counter(record.failure_pattern for record in records if record.failure_pattern)
    total_cost = sum(safe_download_size(record.cost) for record in records)
    stability_score = round(
        (execution_success_rate * 0.55)
        + (consistency_score * 0.25)
        + (drift_score * 0.2),
        4,
    ) if total else 1.0

    suggestions = []
    if failed and execution_success_rate < 0.8:
        suggestions.append(
            control_suggestion(
                "warning",
                "控制执行失败率偏高",
                "建议先审查失败模式和相关 Provider，不要自动扩大策略放量。",
                "execution-success-rate-low",
            )
        )
    if drift_rate >= 0.2:
        suggestions.append(
            control_suggestion(
                "warning",
                "Policy 决策与实际路由出现漂移",
                "建议检查 Policy Engine 权重和 Routing Engine 反馈，但不要自动修改生产配置。",
                "policy-route-drift",
            )
        )
    if pending_rate >= 0.5 and total >= 3:
        suggestions.append(
            control_suggestion(
                "info",
                "待审批控制记录积压",
                "建议管理员在 Policy Control 页面处理 pending 项，避免控制链路长期停滞。",
                "approval-backlog",
            )
        )
    if not suggestions:
        suggestions.append(
            control_suggestion(
                "neutral",
                "控制层暂无明显收敛风险",
                "继续观察执行反馈和状态分布；本页不会自动应用任何策略。",
                "control-stable",
            )
        )

    recent_records = [
        {
            "created_at": format_admin_datetime(record.created_at),
            "request_id": record.request_id,
            "policy_decision": record.policy_decision,
            "control_decision": record.control_decision,
            "state": record.state,
            "execution_result": record.execution_result,
            "provider": record.provider or "-",
            "route": record.route or "-",
            "cost": record.cost,
            "cost_label": format_bytes(record.cost),
            "confidence": record.control_confidence,
            "failure_pattern": record.failure_pattern or "-",
            "source": record.source or "-",
        }
        for record in records[:50]
    ]

    return {
        "version": CONTROL_CONVERGENCE_VERSION,
        "window_hours": window_hours,
        "generated_at": format_admin_datetime(timezone.now()),
        "total_records": total,
        "state_distribution": dict(state_counts),
        "execution_success_rate": execution_success_rate,
        "execution_success_rate_label": percent_label(execution_success_rate),
        "approval_rate": approval_rate,
        "approval_rate_label": percent_label(approval_rate),
        "rejection_rate": rejection_rate,
        "rejection_rate_label": percent_label(rejection_rate),
        "pending_rate": pending_rate,
        "pending_rate_label": percent_label(pending_rate),
        "failure_patterns": [
            {"pattern": pattern, "count": count}
            for pattern, count in failure_patterns.most_common(12)
        ],
        "control_drift_score": drift_score,
        "control_drift_score_label": percent_label(drift_score),
        "policy_consistency_score": consistency_score,
        "policy_consistency_score_label": percent_label(consistency_score),
        "control_stability_score": stability_score,
        "control_stability_score_label": percent_label(stability_score),
        "orphan_count": len(orphan_records),
        "total_cost": total_cost,
        "total_cost_label": format_bytes(total_cost),
        "feedback_result": feedback_result,
        "suggestions": suggestions,
        "recent_records": recent_records,
    }


def build_control_convergence_context(*, window_hours=24):
    snapshot = build_control_convergence_snapshot(window_hours=window_hours)
    return {
        "title": "Control Convergence",
        "snapshot": snapshot,
        **snapshot,
        "safety_note": "Control Convergence 只聚合状态、反馈和建议，不自动修改生产路由、Provider 或 Cost Guard。",
    }
