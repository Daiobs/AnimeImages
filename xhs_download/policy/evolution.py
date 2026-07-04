from .adaptive import BASE_WEIGHTS, build_adaptive_weight_recommendation, normalize_weights
from ..models import PolicyRuntimeSetting


POLICY_EVOLUTION_VERSION = "v4"
SAFETY_APPROVE = "approve"
SAFETY_REVIEW = "review"
SAFETY_REJECT = "reject"


def bounded_float(value, default=0.0):
    try:
        value = float(value)
    except (TypeError, ValueError):
        value = default
    return max(0.0, min(1.0, value))


def weight_delta(weights, baseline=None):
    baseline = baseline or BASE_WEIGHTS
    return {
        key: round(float(weights.get(key, 0) or 0) - float(baseline.get(key, 0) or 0), 4)
        for key in baseline
    }


def max_abs_weight_delta(delta):
    if not delta:
        return 0.0
    return max(abs(float(value or 0)) for value in delta.values())


def weights_are_valid(weights):
    if set(weights or {}) != set(BASE_WEIGHTS):
        return False
    try:
        values = [float(weights[key]) for key in BASE_WEIGHTS]
    except (TypeError, ValueError):
        return False
    if any(value < 0 or value > 1 for value in values):
        return False
    return abs(sum(values) - 1.0) <= 0.02


def build_adaptive_weight_suggestion(adaptive_recommendation, runtime_setting=None):
    runtime_setting = runtime_setting or PolicyRuntimeSetting.load()
    weights = normalize_weights((adaptive_recommendation or {}).get("weights") or BASE_WEIGHTS)
    delta = weight_delta(weights)
    confidence = bounded_float((adaptive_recommendation or {}).get("confidence"))
    sample_count = int((adaptive_recommendation or {}).get("sample_count") or 0)
    min_confidence = bounded_float(
        getattr(runtime_setting, "v4_adaptive_min_confidence", 0.65),
        default=0.65,
    )
    return {
        "kind": "adaptive_policy_weights",
        "policy_area": "policy_weights",
        "title": "调整 Policy Engine 权重",
        "body": "基于下载日志、推荐路线命中率和 Provider 反馈生成权重建议；v4 只输出建议，不自动修改生产配置。",
        "proposed_change": {
            "weights": weights,
            "delta": delta,
            "baseline": dict(BASE_WEIGHTS),
        },
        "confidence": confidence,
        "sample_count": sample_count,
        "min_confidence": min_confidence,
        "source": "policy_v4_adaptive_feedback",
        "reason_chain": list((adaptive_recommendation or {}).get("reason_chain") or ()),
        "automatic_execution": False,
    }


def validate_policy_suggestion(suggestion, *, runtime_setting=None):
    runtime_setting = runtime_setting or PolicyRuntimeSetting.load()
    reasons = ["policy-v4-safety-guard"]
    if bool((suggestion or {}).get("automatic_execution")):
        return {
            "decision": SAFETY_REJECT,
            "risk": "high",
            "confidence": 1.0,
            "reason_chain": reasons + ["automatic-execution-is-not-allowed-in-v4"],
        }

    kind = str((suggestion or {}).get("kind") or "")
    if kind != "adaptive_policy_weights":
        return {
            "decision": SAFETY_REVIEW,
            "risk": "medium",
            "confidence": 0.5,
            "reason_chain": reasons + ["unknown-suggestion-kind-requires-human-review"],
        }

    proposed = (suggestion or {}).get("proposed_change") or {}
    weights = proposed.get("weights") or {}
    if not weights_are_valid(weights):
        return {
            "decision": SAFETY_REJECT,
            "risk": "high",
            "confidence": 1.0,
            "reason_chain": reasons + ["invalid-policy-weight-shape"],
        }

    sample_count = int((suggestion or {}).get("sample_count") or 0)
    confidence = bounded_float((suggestion or {}).get("confidence"))
    min_confidence = bounded_float(
        getattr(runtime_setting, "v4_adaptive_min_confidence", 0.65),
        default=0.65,
    )
    max_delta = max_abs_weight_delta(proposed.get("delta") or {})

    if sample_count < 5:
        return {
            "decision": SAFETY_REVIEW,
            "risk": "medium",
            "confidence": confidence,
            "reason_chain": reasons + ["insufficient-feedback-sample"],
        }
    if confidence < min_confidence:
        return {
            "decision": SAFETY_REVIEW,
            "risk": "medium",
            "confidence": confidence,
            "reason_chain": reasons + ["confidence-below-runtime-threshold"],
        }
    if max_delta > 0.25:
        return {
            "decision": SAFETY_REVIEW,
            "risk": "medium",
            "confidence": confidence,
            "reason_chain": reasons + ["large-weight-change-requires-human-review"],
        }

    return {
        "decision": SAFETY_APPROVE,
        "risk": "low",
        "confidence": confidence,
        "reason_chain": reasons + ["low-risk-advisory-change"],
    }


def evaluate_policy_safety_guard(suggestions, *, runtime_setting=None):
    runtime_setting = runtime_setting or PolicyRuntimeSetting.load()
    reviewed = []
    summary = {
        SAFETY_APPROVE: 0,
        SAFETY_REVIEW: 0,
        SAFETY_REJECT: 0,
    }
    for suggestion in suggestions or ():
        safety = validate_policy_suggestion(suggestion, runtime_setting=runtime_setting)
        summary[safety["decision"]] += 1
        reviewed.append({**suggestion, "safety": safety})
    return {
        "version": POLICY_EVOLUTION_VERSION,
        "automatic_execution": False,
        "summary": summary,
        "suggestions": reviewed,
    }


def build_policy_evolution_recommendation(*, window_hours=24, runtime_setting=None):
    runtime_setting = runtime_setting or PolicyRuntimeSetting.load()
    adaptive = build_adaptive_weight_recommendation(window_hours=window_hours)
    suggestions = [build_adaptive_weight_suggestion(adaptive, runtime_setting=runtime_setting)]
    safety = evaluate_policy_safety_guard(suggestions, runtime_setting=runtime_setting)
    return {
        "version": POLICY_EVOLUTION_VERSION,
        "mode": "recommendation_only",
        "window_hours": int(window_hours or 24),
        "adaptive": adaptive,
        "suggestions": safety["suggestions"],
        "safety_summary": safety["summary"],
        "automatic_execution": False,
        "production_behavior_changed": False,
        "safety_note": "v4 只生成建议和安全判定；不会自动修改 Provider、Cost Guard、Routing 或 Policy runtime 配置。",
    }
