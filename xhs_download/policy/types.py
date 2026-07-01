from dataclasses import dataclass, field


@dataclass(frozen=True)
class PolicyContext:
    request: object
    media_items: tuple
    requested_mode: str = ""
    platform_key: str = ""
    provider_hint: str = ""
    source_platform: str = ""
    runtime_setting: object = None
    route_hint: dict = field(default_factory=dict)


@dataclass(frozen=True)
class PolicyResult:
    policy: str
    route: str
    score: float
    confidence: float
    reason: str
    provider: str = ""
    cost_level: str = "low"

    def to_dict(self):
        return {
            "policy": self.policy,
            "route": self.route,
            "score": round(float(self.score or 0), 4),
            "confidence": round(float(self.confidence or 0), 4),
            "reason": self.reason,
            "provider": self.provider,
            "cost_level": self.cost_level,
        }


@dataclass(frozen=True)
class PolicyDecision:
    allow_download: bool
    route: str
    provider: str
    cost_level: str
    confidence: float
    reason_chain: tuple = field(default_factory=tuple)
    policy_scores: dict = field(default_factory=dict)
    weighted_routes: dict = field(default_factory=dict)
    fallback_route: str = ""
    decision_source: str = "policy_v3_5_shadow"
    active: bool = False
    estimated_savings_bytes: int = 0
    policy_weights: dict = field(default_factory=dict)
    weight_source: str = "static"
    weight_confidence: float = 1.0

    def to_dict(self):
        return {
            "allow_download": bool(self.allow_download),
            "route": self.route,
            "provider": self.provider,
            "cost_level": self.cost_level,
            "confidence": round(float(self.confidence or 0), 4),
            "reason_chain": list(self.reason_chain),
            "policy_scores": {
                key: round(float(value or 0), 4)
                for key, value in (self.policy_scores or {}).items()
            },
            "weighted_routes": {
                key: round(float(value or 0), 4)
                for key, value in (self.weighted_routes or {}).items()
            },
            "fallback_route": self.fallback_route,
            "decision_source": self.decision_source,
            "active": bool(self.active),
            "estimated_savings_bytes": int(self.estimated_savings_bytes or 0),
            "policy_weights": {
                key: round(float(value or 0), 4)
                for key, value in (self.policy_weights or {}).items()
            },
            "weight_source": self.weight_source,
            "weight_confidence": round(float(self.weight_confidence or 0), 4),
        }
