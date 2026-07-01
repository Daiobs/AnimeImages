from django.conf import settings

from ..services.download_routing import (
    ROUTE_BLOCKED,
    ROUTE_DIRECT,
    ROUTE_PROXY,
    ROUTE_ZIP,
    item_is_image,
    item_is_video,
    provider_item_requires_server,
    safe_download_size,
)
from .policy_rules import HIGH_COST_BYTES, MEDIUM_COST_BYTES, clamp_confidence
from .policy_types import PolicyConflictResolution, PolicyScoreResult


POLICY_COST = "cost"
POLICY_PROVIDER = "provider"
POLICY_ROUTING = "routing"
DEFAULT_POLICY_WEIGHTS = {
    POLICY_COST: 0.4,
    POLICY_PROVIDER: 0.4,
    POLICY_ROUTING: 0.2,
}
ROUTE_TIEBREAK_PRIORITY = {
    ROUTE_BLOCKED: 4,
    ROUTE_ZIP: 3,
    ROUTE_PROXY: 2,
    ROUTE_DIRECT: 1,
}


def normalize_score(value, default=0.75):
    try:
        value = float(value)
    except (TypeError, ValueError):
        value = default
    return max(0.0, min(1.0, value))


def normalized_policy_weights(weights=None):
    weights = dict(weights or default_policy_weights())
    normalized = {
        POLICY_COST: max(0.0, float(weights.get(POLICY_COST, 0.0) or 0.0)),
        POLICY_PROVIDER: max(0.0, float(weights.get(POLICY_PROVIDER, 0.0) or 0.0)),
        POLICY_ROUTING: max(0.0, float(weights.get(POLICY_ROUTING, 0.0) or 0.0)),
    }
    total = sum(normalized.values())
    if total <= 0:
        return dict(DEFAULT_POLICY_WEIGHTS)
    return {key: value / total for key, value in normalized.items()}


def default_policy_weights():
    return {
        POLICY_COST: getattr(settings, "POLICY_ENGINE_COST_WEIGHT", 0.4),
        POLICY_PROVIDER: getattr(settings, "POLICY_ENGINE_PROVIDER_WEIGHT", 0.4),
        POLICY_ROUTING: getattr(settings, "POLICY_ENGINE_ROUTING_WEIGHT", 0.2),
    }


def routing_route(context):
    return str((context.routing_decision or {}).get("route_type") or ROUTE_ZIP)


def routing_error_code(context):
    return str((context.routing_decision or {}).get("error_code") or "")


def routing_cost(context):
    route = context.routing_decision or {}
    estimated_cost = route.get("estimated_cost") or {}
    server_bytes = safe_download_size(
        estimated_cost.get("server_bytes")
        or route.get("server_egress_estimated_bytes")
    )
    browser_bytes = safe_download_size(
        estimated_cost.get("browser_bytes")
        or route.get("browser_egress_estimated_bytes")
    )
    return server_bytes, browser_bytes


class ScoringPolicy:
    name = ""

    def evaluate(self, context):
        raise NotImplementedError


class CostPolicy(ScoringPolicy):
    name = POLICY_COST

    def evaluate(self, context):
        route = routing_route(context)
        if route == ROUTE_BLOCKED:
            return PolicyScoreResult(
                policy=self.name,
                route=ROUTE_BLOCKED,
                score=1.0,
                confidence=1.0,
                reason=routing_error_code(context) or "cost_guard_blocked",
            )

        server_bytes, browser_bytes = routing_cost(context)
        estimated = max(server_bytes, browser_bytes, context.estimated_bytes)
        if server_bytes == 0 and route == ROUTE_DIRECT:
            return PolicyScoreResult(
                policy=self.name,
                route=ROUTE_DIRECT,
                score=0.95,
                confidence=0.9,
                reason="zero_server_egress",
            )
        if server_bytes >= HIGH_COST_BYTES and context.item_count == 1:
            return PolicyScoreResult(
                policy=self.name,
                route=ROUTE_PROXY,
                score=0.86,
                confidence=0.82,
                reason="high_server_egress_single_item",
                metadata={"server_bytes": server_bytes},
            )
        if estimated >= MEDIUM_COST_BYTES:
            return PolicyScoreResult(
                policy=self.name,
                route=route,
                score=0.68,
                confidence=0.72,
                reason="medium_or_high_cost_keep_routing",
                metadata={"estimated_bytes": estimated},
            )
        return PolicyScoreResult(
            policy=self.name,
            route=route,
            score=0.82,
            confidence=0.78,
            reason="cost_within_normal_range",
        )


class ProviderPolicy(ScoringPolicy):
    name = POLICY_PROVIDER

    def evaluate(self, context):
        route = routing_route(context)
        if route == ROUTE_BLOCKED:
            return PolicyScoreResult(
                policy=self.name,
                route=ROUTE_BLOCKED,
                score=0.96,
                confidence=0.95,
                reason="provider_respects_blocked_route",
            )

        if any(provider_item_requires_server(item) for item in context.media_items):
            return PolicyScoreResult(
                policy=self.name,
                route=ROUTE_ZIP,
                score=0.95,
                confidence=0.9,
                reason="provider_requires_server_side_execution",
                metadata={"provider": context.provider_hint},
            )

        all_images = context.item_count > 0 and all(
            item_is_image(item) for item in context.media_items
        )
        all_videos = context.item_count > 0 and all(
            item_is_video(item) for item in context.media_items
        )
        if all_images and context.item_count <= 12:
            return PolicyScoreResult(
                policy=self.name,
                route=ROUTE_DIRECT,
                score=0.9,
                confidence=0.86,
                reason="provider_public_images_direct_safe",
                metadata={"provider": context.provider_hint},
            )
        if all_videos and context.item_count == 1:
            return PolicyScoreResult(
                policy=self.name,
                route=ROUTE_PROXY,
                score=0.78,
                confidence=0.74,
                reason="provider_public_single_video_proxy_safe",
                metadata={"provider": context.provider_hint},
            )
        return PolicyScoreResult(
            policy=self.name,
            route=ROUTE_ZIP,
            score=0.8,
            confidence=0.76,
            reason="provider_multi_or_mixed_requires_archive",
            metadata={"provider": context.provider_hint},
        )


class RoutingPolicy(ScoringPolicy):
    name = POLICY_ROUTING

    def evaluate(self, context):
        route = routing_route(context)
        routing_score = normalize_score(
            (context.routing_decision or {}).get("routing_score"),
            default=0.75,
        )
        routing_confidence = normalize_score(
            (context.routing_decision or {}).get("confidence"),
            default=0.75,
        )
        return PolicyScoreResult(
            policy=self.name,
            route=route,
            score=routing_score,
            confidence=routing_confidence,
            reason=(context.routing_decision or {}).get("reason")
            or (context.routing_decision or {}).get("route_reason")
            or "routing_engine_selected",
        )


DEFAULT_SCORING_POLICIES = (
    CostPolicy(),
    ProviderPolicy(),
    RoutingPolicy(),
)


def aggregate_policy_results(context, policies=None):
    return {
        result.policy: result
        for result in (
            policy.evaluate(context) for policy in tuple(policies or DEFAULT_SCORING_POLICIES)
        )
    }


def resolve_policy_conflicts(policy_results, weights=None):
    weights = normalized_policy_weights(weights)
    weighted_scores = {}
    policy_scores = {}
    reason_chain = []

    for policy_name, result in policy_results.items():
        weight = weights.get(policy_name, 0.0)
        contribution = weight * result.score
        weighted_scores[result.route] = weighted_scores.get(result.route, 0.0) + contribution
        policy_scores[policy_name] = result.score
        reason_chain.append(
            f"{policy_name}:{result.route}:{result.reason}"
        )

    if not weighted_scores:
        final_route = ROUTE_ZIP
    else:
        final_route = max(
            weighted_scores,
            key=lambda route: (
                weighted_scores[route],
                ROUTE_TIEBREAK_PRIORITY.get(route, 0),
            ),
        )

    confidence_numerator = 0.0
    confidence_denominator = 0.0
    for policy_name, result in policy_results.items():
        if result.route != final_route:
            continue
        weight = weights.get(policy_name, 0.0)
        confidence_numerator += weight * result.confidence
        confidence_denominator += weight
    if confidence_denominator <= 0:
        confidence = 0.5
    else:
        confidence = confidence_numerator / confidence_denominator

    return PolicyConflictResolution(
        final_route=final_route,
        confidence=round(clamp_confidence(confidence), 3),
        weighted_scores={key: round(value, 4) for key, value in weighted_scores.items()},
        policy_scores={key: round(value, 4) for key, value in policy_scores.items()},
        policy_weights={key: round(value, 4) for key, value in weights.items()},
        reason_chain=tuple(reason_chain),
    )
