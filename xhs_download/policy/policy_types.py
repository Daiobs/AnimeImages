from dataclasses import dataclass, field


POLICY_VERSION_V1 = "policy_v1"
POLICY_VERSION_V2 = "policy_v2"
POLICY_VERSION = POLICY_VERSION_V1

POLICY_ALLOW = "allow"
POLICY_BLOCK = "block"

COST_LOW = "low_cost"
COST_BALANCED = "balanced"
COST_SAFE = "safe"
COST_STRICT = "strict"


@dataclass(frozen=True)
class RuleResult:
    rule: str
    reason: str
    allow_download: bool | None = None
    route_type: str = ""
    provider: str = ""
    cost_level: str = ""
    confidence_delta: float = 0.0
    metadata: dict = field(default_factory=dict)


@dataclass(frozen=True)
class PolicyDecision:
    allow_download: bool
    route_type: str
    provider: str
    cost_level: str
    confidence: float
    reason_chain: tuple[str, ...] = ()
    strategy_version: str = POLICY_VERSION
    routing_strategy_version: str = ""
    routing_score: float = 0.0
    provider_score: float = 0.0
    policy_scores: dict = field(default_factory=dict)
    policy_weights: dict = field(default_factory=dict)
    estimated_cost: dict = field(default_factory=dict)
    error_code: str = ""
    warnings: tuple[str, ...] = ()

    @property
    def decision(self):
        return POLICY_ALLOW if self.allow_download else POLICY_BLOCK

    def to_dict(self):
        return {
            "decision": self.decision,
            "allow_download": self.allow_download,
            "route_type": self.route_type,
            "provider": self.provider,
            "cost_level": self.cost_level,
            "confidence": self.confidence,
            "reason_chain": list(self.reason_chain),
            "strategy_version": self.strategy_version,
            "routing_strategy_version": self.routing_strategy_version,
            "routing_score": self.routing_score,
            "provider_score": self.provider_score,
            "policy_scores": dict(self.policy_scores or {}),
            "policy_weights": dict(self.policy_weights or {}),
            "estimated_cost": dict(self.estimated_cost or {}),
            "error_code": self.error_code,
            "warnings": list(self.warnings or ()),
        }


@dataclass(frozen=True)
class PolicyScoreResult:
    policy: str
    route: str
    score: float
    confidence: float
    reason: str
    metadata: dict = field(default_factory=dict)

    def to_dict(self):
        return {
            "policy": self.policy,
            "route": self.route,
            "score": self.score,
            "confidence": self.confidence,
            "reason": self.reason,
            "metadata": dict(self.metadata or {}),
        }


@dataclass(frozen=True)
class PolicyConflictResolution:
    final_route: str
    confidence: float
    weighted_scores: dict
    policy_scores: dict
    policy_weights: dict
    reason_chain: tuple[str, ...] = ()
