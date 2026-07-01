from django.conf import settings

from ..services.download_routing import ROUTE_BLOCKED
from ..services.routing_v3 import decide_route_v3
from .policy_context import build_policy_context
from .policy_rules import DEFAULT_POLICY_RULES, clamp_confidence, summarize_media_reason
from .policy_scoring import aggregate_policy_results, resolve_policy_conflicts
from .policy_types import (
    COST_BALANCED,
    COST_LOW,
    COST_SAFE,
    COST_STRICT,
    POLICY_VERSION,
    POLICY_VERSION_V2,
    PolicyDecision,
)


def policy_v2_enabled():
    return (
        bool(getattr(settings, "POLICY_ENGINE_V2_ENABLED", True))
        and str(getattr(settings, "POLICY_ENGINE_VERSION", POLICY_VERSION_V2)).lower()
        == POLICY_VERSION_V2
    )


def build_routing_decision(
    request,
    media_items,
    *,
    requested_mode="auto",
    platform_key="",
    provider_hint="",
    routing_decision=None,
):
    if routing_decision is not None:
        return dict(routing_decision or {})
    return decide_route_v3(
        request,
        media_items,
        context={
            "requested_mode": requested_mode,
            "platform_key": platform_key,
            "provider": provider_hint,
        },
    )


def evaluate_download_policy_v1(
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
    routing_decision = build_routing_decision(
        request,
        media_items,
        requested_mode=requested_mode,
        platform_key=platform_key,
        provider_hint=provider_hint,
        routing_decision=routing_decision,
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


def evaluate_download_policy_v2(
    request,
    media_items,
    *,
    requested_mode="auto",
    platform_key="",
    provider_hint="",
    runtime_settings=None,
    routing_decision=None,
    policies=None,
    weights=None,
):
    routing_decision = build_routing_decision(
        request,
        media_items,
        requested_mode=requested_mode,
        platform_key=platform_key,
        provider_hint=provider_hint,
        routing_decision=routing_decision,
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
    policy_results = aggregate_policy_results(context, policies=policies)
    resolution = resolve_policy_conflicts(policy_results, weights=weights)
    allow_download = resolution.final_route != ROUTE_BLOCKED
    reason_chain = (
        summarize_media_reason(context),
        *resolution.reason_chain,
    )
    return PolicyDecision(
        allow_download=allow_download,
        route_type=resolution.final_route,
        provider=context.provider_hint or routing_decision.get("provider") or "native",
        cost_level=cost_level_from_results(resolution.final_route, policy_results),
        confidence=resolution.confidence,
        reason_chain=tuple(reason_chain),
        strategy_version=POLICY_VERSION_V2,
        routing_strategy_version=str(routing_decision.get("strategy_version") or ""),
        routing_score=float(routing_decision.get("routing_score") or 0),
        provider_score=float(routing_decision.get("provider_score") or 0),
        policy_scores=dict(resolution.policy_scores),
        policy_weights=dict(resolution.policy_weights),
        estimated_cost=dict(routing_decision.get("estimated_cost") or {}),
        error_code=str(routing_decision.get("error_code") or ""),
        warnings=tuple(routing_decision.get("warnings") or ()),
    )


def cost_level_from_results(final_route, policy_results):
    if final_route == ROUTE_BLOCKED:
        return COST_STRICT
    cost_result = policy_results.get("cost")
    reason = str(getattr(cost_result, "reason", "") or "")
    if "zero_server_egress" in reason:
        return COST_LOW
    if "high_server_egress" in reason:
        return COST_STRICT
    if "medium_or_high_cost" in reason:
        return COST_SAFE
    return COST_BALANCED


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
    policies=None,
    weights=None,
):
    if policy_v2_enabled():
        return evaluate_download_policy_v2(
            request,
            media_items,
            requested_mode=requested_mode,
            platform_key=platform_key,
            provider_hint=provider_hint,
            runtime_settings=runtime_settings,
            routing_decision=routing_decision,
            policies=policies,
            weights=weights,
        )
    return evaluate_download_policy_v1(
        request,
        media_items,
        requested_mode=requested_mode,
        platform_key=platform_key,
        provider_hint=provider_hint,
        runtime_settings=runtime_settings,
        routing_decision=routing_decision,
        rules=rules,
    )
