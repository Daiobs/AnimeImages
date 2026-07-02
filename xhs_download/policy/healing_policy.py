from dataclasses import dataclass

from ..services.download_routing import ROUTE_BLOCKED, ROUTE_DIRECT, ROUTE_PROXY, ROUTE_ZIP
from ..services.recovery_engine import (
    FAILURE_COST_BLOCKED,
    FAILURE_DIRECT_FAILED,
    FAILURE_MERGE_FAILED,
    FAILURE_PROVIDER_TIMEOUT,
    FAILURE_PROVIDER_UNAVAILABLE,
    FAILURE_PROXY_FAILED,
    FAILURE_ROUTING_FAILED,
    FAILURE_UNKNOWN,
    FAILURE_ZIP_FAILED,
)


HEAL_ACTION_NONE = "none"
HEAL_ACTION_RETRY_PROVIDER = "retry_provider"
HEAL_ACTION_SWITCH_PROVIDER = "switch_provider"
HEAL_ACTION_FALLBACK_ZIP = "fallback_zip"
HEAL_ACTION_FALLBACK_PROXY = "fallback_proxy"
HEAL_ACTION_FALLBACK_DIRECT = "fallback_direct"
HEAL_ACTION_DEGRADE_WAIT = "degrade_wait"
HEAL_ACTION_BLOCK_GRACEFULLY = "block_gracefully"

RISK_LOW = "low"
RISK_MEDIUM = "medium"
RISK_HIGH = "high"

DEFAULT_MAX_HEALING_DEPTH = 3


@dataclass(frozen=True)
class HealingPolicyDecision:
    action: str
    risk: str
    confidence: float
    reason: str
    fallback_to_route: str = ""
    retry_allowed: bool = False
    switch_provider_allowed: bool = False
    max_depth: int = DEFAULT_MAX_HEALING_DEPTH

    @property
    def can_heal(self):
        return self.action not in {"", HEAL_ACTION_NONE, HEAL_ACTION_BLOCK_GRACEFULLY}


PROVIDER_FALLBACK_ORDER = (
    "kukutool",
    "tikhub",
    "youtube_merge",
    "yt_dlp_merge",
    "yt_dlp_generic",
    "gallery_dl_generic",
    "native",
    "fallback",
)

PROVIDER_FALLBACK_CHAIN = {
    "kukutool": ("yt_dlp_generic", "native", "fallback"),
    "tikhub": ("kukutool", "native", "fallback"),
    "youtube_merge": ("yt_dlp_generic", "fallback"),
    "yt_dlp_merge": ("yt_dlp_generic", "fallback"),
    "yt_dlp_generic": ("native", "fallback"),
    "gallery_dl_generic": ("yt_dlp_generic", "fallback"),
    "mobile_feed": ("native", "fallback"),
    "mobile_share": ("native", "fallback"),
    "native_fast_feed": ("native", "fallback"),
    "native": ("fallback",),
}


def normalize_provider(provider):
    return str(provider or "").strip()


def next_provider(provider):
    provider = normalize_provider(provider)
    chain = PROVIDER_FALLBACK_CHAIN.get(provider)
    if chain:
        return chain[0]
    if not provider:
        return ""
    try:
        index = PROVIDER_FALLBACK_ORDER.index(provider)
    except ValueError:
        return "fallback"
    if index + 1 < len(PROVIDER_FALLBACK_ORDER):
        return PROVIDER_FALLBACK_ORDER[index + 1]
    return "fallback"


def provider_health_tier(stability_score):
    try:
        score = float(stability_score)
    except (TypeError, ValueError):
        score = 1.0
    if score < 0.1:
        return "quarantine"
    if score < 0.3:
        return "temporary_disable_recommended"
    if score < 0.5:
        return "degraded"
    return "healthy"


def decide_healing_policy(
    *,
    failure_type,
    route_type="",
    provider="",
    depth=0,
    stability_score=None,
):
    failure_type = str(failure_type or FAILURE_UNKNOWN)
    route_type = str(route_type or "")
    provider = normalize_provider(provider)
    depth = max(0, int(depth or 0))
    health_tier = provider_health_tier(stability_score)

    if depth >= DEFAULT_MAX_HEALING_DEPTH:
        return HealingPolicyDecision(
            action=HEAL_ACTION_BLOCK_GRACEFULLY,
            risk=RISK_HIGH,
            confidence=0.95,
            reason="healing_depth_limit_reached",
            fallback_to_route=ROUTE_BLOCKED,
        )

    if failure_type == FAILURE_PROVIDER_TIMEOUT:
        if provider and depth == 0 and health_tier == "healthy":
            return HealingPolicyDecision(
                action=HEAL_ACTION_RETRY_PROVIDER,
                risk=RISK_MEDIUM,
                confidence=0.72,
                reason="provider_timeout_retry_once",
                retry_allowed=True,
            )
        return HealingPolicyDecision(
            action=HEAL_ACTION_SWITCH_PROVIDER,
            risk=RISK_MEDIUM,
            confidence=0.82,
            reason=f"provider_timeout_switch_provider_{health_tier}",
            switch_provider_allowed=bool(next_provider(provider)),
        )

    if failure_type == FAILURE_PROVIDER_UNAVAILABLE:
        return HealingPolicyDecision(
            action=HEAL_ACTION_SWITCH_PROVIDER,
            risk=RISK_MEDIUM,
            confidence=0.86,
            reason=f"provider_unavailable_switch_provider_{health_tier}",
            switch_provider_allowed=bool(next_provider(provider)),
        )

    if failure_type == FAILURE_MERGE_FAILED:
        return HealingPolicyDecision(
            action=HEAL_ACTION_SWITCH_PROVIDER,
            risk=RISK_MEDIUM,
            confidence=0.78,
            reason="merge_failed_switch_provider",
            switch_provider_allowed=bool(next_provider(provider)),
        )

    if failure_type == FAILURE_ROUTING_FAILED:
        return HealingPolicyDecision(
            action=HEAL_ACTION_FALLBACK_ZIP,
            risk=RISK_LOW,
            confidence=0.88,
            reason="routing_failed_safe_zip",
            fallback_to_route=ROUTE_ZIP,
        )

    if failure_type == FAILURE_ZIP_FAILED or route_type == ROUTE_ZIP:
        return HealingPolicyDecision(
            action=HEAL_ACTION_FALLBACK_PROXY,
            risk=RISK_LOW,
            confidence=0.84,
            reason="zip_failed_proxy_fallback",
            fallback_to_route=ROUTE_PROXY,
        )

    if failure_type == FAILURE_PROXY_FAILED or route_type == ROUTE_PROXY:
        return HealingPolicyDecision(
            action=HEAL_ACTION_FALLBACK_DIRECT,
            risk=RISK_MEDIUM,
            confidence=0.76,
            reason="proxy_failed_direct_fallback_if_safe",
            fallback_to_route=ROUTE_DIRECT,
        )

    if failure_type == FAILURE_DIRECT_FAILED or route_type == ROUTE_DIRECT:
        return HealingPolicyDecision(
            action=HEAL_ACTION_FALLBACK_PROXY,
            risk=RISK_MEDIUM,
            confidence=0.7,
            reason="direct_failed_proxy_fallback",
            fallback_to_route=ROUTE_PROXY,
        )

    if failure_type == FAILURE_COST_BLOCKED:
        return HealingPolicyDecision(
            action=HEAL_ACTION_DEGRADE_WAIT,
            risk=RISK_HIGH,
            confidence=0.92,
            reason="cost_guard_blocked_wait_or_degrade",
            fallback_to_route=ROUTE_BLOCKED,
        )

    return HealingPolicyDecision(
        action=HEAL_ACTION_BLOCK_GRACEFULLY,
        risk=RISK_HIGH,
        confidence=0.5,
        reason="unknown_failure_block_gracefully",
        fallback_to_route=ROUTE_BLOCKED,
    )
