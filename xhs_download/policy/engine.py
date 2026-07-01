from django.conf import settings

from download_console.models import DownloadRuntimeSetting

from ..services.download_routing import (
    ROUTE_BLOCKED,
    decide_download_route,
    estimate_media_items_bytes,
)
from ..services.routing_v3 import infer_provider
from .policies import (
    CostPolicy,
    ProviderPolicy,
    RoutingPolicy,
    estimated_server_savings,
)
from .types import PolicyContext, PolicyDecision


DEFAULT_POLICY_WEIGHTS = {
    "cost": 0.4,
    "provider": 0.4,
    "routing": 0.2,
}
POLICY_ENGINE_VERSION = "v3.5"


def normalized_media_items(media_items):
    return tuple(item for item in media_items or () if isinstance(item, dict))


def build_policy_context(
    request,
    media_items,
    *,
    requested_mode="",
    platform_key="",
    route_hint=None,
    provider_hint="",
    runtime_setting=None,
):
    media_items = normalized_media_items(media_items)
    provider_hint = provider_hint or infer_provider(media_items)
    source_platform = ""
    for item in media_items:
        source_platform = str(item.get("source_platform") or item.get("platform") or "")
        if source_platform:
            break

    return PolicyContext(
        request=request,
        media_items=media_items,
        requested_mode=str(requested_mode or ""),
        platform_key=str(platform_key or ""),
        provider_hint=str(provider_hint or ""),
        source_platform=source_platform,
        runtime_setting=runtime_setting or DownloadRuntimeSetting.load(),
        route_hint=dict(route_hint or {}),
    )


def policy_engine_mode():
    mode = str(getattr(settings, "POLICY_ENGINE_MODE", "shadow") or "shadow").lower()
    if bool(getattr(settings, "POLICY_ENGINE_V35_ENABLED", False)):
        return "v3_5"
    return mode


def policy_engine_is_active():
    return policy_engine_mode() in {"v3_5", "v35", "active", "policy_v3_5"}


def policy_weights():
    configured = getattr(settings, "POLICY_ENGINE_WEIGHTS", None)
    if not isinstance(configured, dict):
        return dict(DEFAULT_POLICY_WEIGHTS)
    weights = dict(DEFAULT_POLICY_WEIGHTS)
    for key in tuple(weights):
        try:
            weights[key] = max(0.0, float(configured.get(key, weights[key])))
        except (TypeError, ValueError):
            pass
    total = sum(weights.values())
    if total <= 0:
        return dict(DEFAULT_POLICY_WEIGHTS)
    return {key: value / total for key, value in weights.items()}


def aggregate_weighted_routes(results, weights):
    weighted_routes = {}
    for result in results:
        weight = weights.get(result.policy, 0)
        weighted = float(result.score or 0) * float(result.confidence or 0) * weight
        weighted_routes[result.route] = weighted_routes.get(result.route, 0) + weighted
    return weighted_routes


def resolve_conflict(results, route_hint=None):
    results = tuple(results or ())
    cost_result = next((item for item in results if item.policy == "cost"), None)
    if cost_result and cost_result.route == ROUTE_BLOCKED:
        return cost_result.route, 1.0, "cost-hard-constraint"

    weights = policy_weights()
    weighted_routes = aggregate_weighted_routes(results, weights)
    if not weighted_routes:
        fallback = str((route_hint or {}).get("route_type") or ROUTE_BLOCKED)
        return fallback, 0.0, "routing-v2-fallback"

    route = max(weighted_routes, key=weighted_routes.get)
    confidence = max(0.0, min(1.0, weighted_routes[route]))
    return route, confidence, "weighted-policy-resolution"


def fallback_route_for(context):
    if context.route_hint:
        return str(context.route_hint.get("route_type") or "")
    route = decide_download_route(
        context.request,
        context.media_items,
        requested_mode=context.requested_mode,
        platform_key=context.platform_key,
    )
    return str(route.get("route_type") or "")


def decide_final_policy(context: PolicyContext):
    policies = (CostPolicy(), ProviderPolicy(), RoutingPolicy())
    results = tuple(policy.evaluate(context) for policy in policies)
    route, confidence, resolver_reason = resolve_conflict(results, context.route_hint)
    provider = next((result.provider for result in results if result.provider), "")
    cost_level = next((result.cost_level for result in results if result.cost_level), "low")
    fallback_route = fallback_route_for(context)
    active = policy_engine_is_active()
    decision_source = "policy_v3_5" if active else "policy_v3_5_shadow"
    if not active:
        decision_source = "routing_v2_fallback_with_policy_shadow"

    return PolicyDecision(
        allow_download=route != ROUTE_BLOCKED,
        route=route,
        provider=provider or "native",
        cost_level=cost_level,
        confidence=confidence,
        reason_chain=tuple([resolver_reason] + [result.reason for result in results]),
        policy_scores={result.policy: result.score for result in results},
        weighted_routes=aggregate_weighted_routes(results, policy_weights()),
        fallback_route=fallback_route,
        decision_source=decision_source,
        active=active,
        estimated_savings_bytes=estimated_server_savings(context.route_hint, route),
    )


def evaluate_policy_shadow(
    request,
    media_items,
    *,
    route_info=None,
    requested_mode="",
    platform_key="",
    provider_hint="",
):
    context = build_policy_context(
        request,
        media_items,
        requested_mode=requested_mode,
        platform_key=platform_key,
        route_hint=route_info or {},
        provider_hint=provider_hint,
    )
    decision = decide_final_policy(context)
    return {
        **decision.to_dict(),
        "version": POLICY_ENGINE_VERSION,
        "estimated_bytes": estimate_media_items_bytes(context.media_items),
    }
