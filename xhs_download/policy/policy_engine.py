from .policy_context import build_policy_context
from .policy_rules import DEFAULT_POLICY_RULES, clamp_confidence, summarize_media_reason
from .policy_types import COST_BALANCED, POLICY_VERSION, PolicyDecision
from ..services.routing_v3 import decide_route_v3
from ..services.download_routing import ROUTE_BLOCKED


def evaluate_download_policy(
    request,
    media_items,
    *,
    requested_mode="auto",
    platform_key="",
    provider_hint="",
    runtime_settings=None,
    routing_decision=None,
    rules=None,
):
    if routing_decision is None:
        routing_decision = decide_route_v3(
            request,
            media_items,
            context={
                "requested_mode": requested_mode,
                "platform_key": platform_key,
                "provider": provider_hint,
            },
        )
    context = build_policy_context(
        request,
        media_items,
        platform=platform_key,
        provider_hint=provider_hint,
        runtime_settings=runtime_settings,
        requested_mode=requested_mode,
        routing_decision=routing_decision,
    )
    rules = tuple(rules or DEFAULT_POLICY_RULES)
    allow_download = routing_decision.get("route_type") != ROUTE_BLOCKED
    route_type = routing_decision.get("route_type") or ""
    provider = context.provider_hint or routing_decision.get("provider") or "native"
    cost_level = COST_BALANCED
    confidence = float(routing_decision.get("confidence") or 0.75)
    reason_chain = [summarize_media_reason(context)]

    for rule in rules:
        result = rule.evaluate(context)
        if result.allow_download is not None:
            allow_download = bool(result.allow_download)
        if result.route_type:
            route_type = result.route_type
        if result.provider:
            provider = result.provider
        if result.cost_level:
            cost_level = result.cost_level
        confidence += result.confidence_delta
        if result.reason:
            reason_chain.append(result.reason)

    if not allow_download:
        route_type = ROUTE_BLOCKED
        confidence = max(confidence, 0.95)

    return PolicyDecision(
        allow_download=allow_download,
        route_type=route_type,
        provider=provider or "native",
        cost_level=cost_level,
        confidence=round(clamp_confidence(confidence), 3),
        reason_chain=tuple(reason_chain),
        strategy_version=POLICY_VERSION,
        routing_strategy_version=str(routing_decision.get("strategy_version") or ""),
        routing_score=float(routing_decision.get("routing_score") or 0),
        provider_score=float(routing_decision.get("provider_score") or 0),
        estimated_cost=dict(routing_decision.get("estimated_cost") or {}),
        error_code=str(routing_decision.get("error_code") or ""),
        warnings=tuple(routing_decision.get("warnings") or ()),
    )
