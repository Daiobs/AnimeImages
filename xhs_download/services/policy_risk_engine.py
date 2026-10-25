from django.utils import timezone

from ..models import DownloadRequestLog, ObservabilityStatus, PolicyOptimizationProposal
from .download_audit import operational_download_logs
from ..policy.evolution import SAFETY_REJECT
from ..services.download_routing import safe_download_size


RISK_LOW_MAX = 0.35
RISK_HIGH_MIN = 0.65


def clamp_risk(value):
    try:
        value = float(value or 0)
    except (TypeError, ValueError):
        value = 0.0
    return round(max(0.0, min(1.0, value)), 4)


def infer_impact_area(suggestion):
    suggestion = suggestion or {}
    policy_area = str(suggestion.get("policy_area") or "").lower()
    kind = str(suggestion.get("kind") or "").lower()
    proposed = suggestion.get("proposed_change") or {}
    text = " ".join((
        policy_area,
        kind,
        " ".join(str(key).lower() for key in proposed.keys()),
    ))
    if policy_area == "policy_weights" or "adaptive_policy_weights" in text:
        return "policy_weights"
    if "provider" in text:
        return "provider"
    if "cost" in text or "limit" in text or "bytes" in text:
        return "cost"
    if "route" in text or "routing" in text:
        return "routing"
    if "weight" in text or "policy" in text:
        return "routing"
    return "routing"


def policy_area_historical_instability(*, impact_area, window_hours=24):
    since = timezone.now() - timezone.timedelta(hours=int(window_hours or 24))
    downloads = operational_download_logs(DownloadRequestLog.objects.filter(created_at__gte=since))
    if impact_area == "provider":
        total = downloads.exclude(provider="").count()
        failed = downloads.exclude(provider="").exclude(status=ObservabilityStatus.SUCCESS).count()
    elif impact_area == "cost":
        total = downloads.filter(actual_bytes__gt=0).count()
        failed = downloads.filter(actual_bytes__gt=512 * 1024 * 1024).count()
    else:
        total = downloads.exclude(route_type="").count()
        failed = downloads.exclude(route_type="").filter(fallback_count__gt=0).count()
    if total <= 0:
        return 0.0
    return clamp_risk(failed / total)


def repeated_change_pressure(suggestion, *, window_hours=168):
    since = timezone.now() - timezone.timedelta(hours=int(window_hours or 168))
    policy_area = str((suggestion or {}).get("policy_area") or "")
    kind = str((suggestion or {}).get("kind") or "")
    recent = PolicyOptimizationProposal.objects.filter(
        policy_area=policy_area,
        kind=kind,
        created_at__gte=since,
    ).count()
    return clamp_risk(recent * 0.08)


def score_policy_risk(suggestion, *, window_hours=24):
    suggestion = suggestion or {}
    proposed = suggestion.get("proposed_change") or {}
    safety = suggestion.get("safety") or {}
    impact_area = infer_impact_area(suggestion)
    reasons = ["policy-risk-engine"]
    score = 0.12

    if impact_area == "provider":
        score += 0.55
        reasons.append("provider-change-risk")
    elif impact_area == "cost":
        score += 0.35
        reasons.append("cost-impact-risk")
    elif impact_area == "routing":
        score += 0.28
        reasons.append("routing-change-risk")
    elif impact_area == "policy_weights":
        score += 0.12
        reasons.append("policy-weight-tuning-risk")

    delta = proposed.get("delta") or {}
    max_weight_delta = 0.0
    if isinstance(delta, dict):
        for value in delta.values():
            try:
                max_weight_delta = max(max_weight_delta, abs(float(value or 0)))
            except (TypeError, ValueError):
                pass
    if max_weight_delta >= 0.2:
        score += 0.22
        reasons.append("large-policy-weight-delta")
    elif max_weight_delta >= 0.15:
        score += 0.12
        reasons.append("medium-policy-weight-delta")

    estimated_cost = safe_download_size(proposed.get("estimated_cost_bytes"))
    if estimated_cost >= 1024 * 1024 * 1024:
        score += 0.28
        reasons.append("large-cost-impact")
    elif estimated_cost >= 200 * 1024 * 1024:
        score += 0.16
        reasons.append("medium-cost-impact")

    if safety.get("decision") == SAFETY_REJECT or safety.get("risk") == "high":
        score += 0.45
        reasons.append("safety-guard-reject-or-high-risk")
    elif safety.get("risk") == "medium":
        score += 0.18
        reasons.append("safety-guard-medium-risk")

    instability = policy_area_historical_instability(
        impact_area=impact_area,
        window_hours=window_hours,
    )
    if instability >= 0.3:
        score += 0.22
        reasons.append("historical-instability-high")
    elif instability >= 0.15:
        score += 0.12
        reasons.append("historical-instability-medium")

    repeated_pressure = repeated_change_pressure(suggestion)
    if repeated_pressure:
        score += repeated_pressure
        reasons.append("recent-similar-policy-changes")

    sample_count = safe_download_size(suggestion.get("sample_count"))
    if sample_count and sample_count < 5:
        score += 0.08
        reasons.append("low-sample-count")

    risk_score = clamp_risk(score)
    if risk_score >= RISK_HIGH_MIN:
        risk_level = "high"
    elif risk_score >= RISK_LOW_MAX:
        risk_level = "medium"
    else:
        risk_level = "low"
    return {
        "risk_score": risk_score,
        "risk_level": risk_level,
        "impact_area": impact_area,
        "reason_chain": reasons,
    }
