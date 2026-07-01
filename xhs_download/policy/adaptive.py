from .learning import build_policy_learning_snapshot


BASE_WEIGHTS = {
    "cost": 0.4,
    "provider": 0.4,
    "routing": 0.2,
}


def normalize_weights(weights):
    total = sum(max(0.0, float(value or 0)) for value in weights.values())
    if total <= 0:
        return dict(BASE_WEIGHTS)
    return {
        key: round(max(0.0, float(value or 0)) / total, 4)
        for key, value in weights.items()
    }


def build_adaptive_weight_recommendation(window_hours=24):
    snapshot = build_policy_learning_snapshot(window_hours=window_hours)
    route_distribution = snapshot["route_distribution"]
    provider_distribution = snapshot["provider_distribution"]
    cost_delta = int(snapshot["cost_summary"].get("delta_bytes") or 0)
    sample_count = int(snapshot["sample_count"] or 0)
    weights = dict(BASE_WEIGHTS)
    reasons = ["policy-v4-shadow-adaptive-weights"]

    if cost_delta > 0:
        weights["cost"] += 0.1
        weights["routing"] -= 0.05
        reasons.append("server-cost-delta-positive")

    provider_count = sum(int(value or 0) for value in provider_distribution.values())
    if provider_count and len(provider_distribution) > 1:
        weights["provider"] += 0.05
        weights["routing"] -= 0.03
        reasons.append("multiple-providers-observed")

    failed_routes = [
        route
        for route, stats in route_distribution.items()
        if int(stats.get("count") or 0)
        and int(stats.get("success") or 0) / int(stats.get("count") or 1) < 0.75
    ]
    if failed_routes:
        weights["routing"] += 0.08
        weights["cost"] -= 0.04
        reasons.append("route-failure-rate-needs-feedback")

    confidence = 0.0
    if sample_count:
        confidence = min(0.9, 0.25 + min(sample_count, 200) / 200 * 0.65)

    return {
        "window_hours": int(window_hours or 24),
        "weights": normalize_weights(weights),
        "confidence": round(confidence, 4),
        "reason_chain": reasons,
        "sample_count": sample_count,
        "shadow_recommendation": snapshot["shadow_recommendation"],
    }
