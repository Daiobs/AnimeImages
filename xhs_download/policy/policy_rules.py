from ..services.download_routing import (
    ROUTE_BLOCKED,
    ROUTE_DIRECT,
    ROUTE_PROXY,
    ROUTE_ZIP,
    item_type,
    provider_item_requires_server,
    safe_download_size,
)
from .policy_types import (
    COST_BALANCED,
    COST_LOW,
    COST_SAFE,
    COST_STRICT,
    RuleResult,
)


HIGH_COST_BYTES = 300 * 1024 * 1024
MEDIUM_COST_BYTES = 80 * 1024 * 1024


def clamp_confidence(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        value = 0.75
    return max(0.0, min(1.0, value))


class PolicyRule:
    name = "policy_rule"

    def evaluate(self, context):
        raise NotImplementedError


class CostGuardRule(PolicyRule):
    name = "cost_guard"

    def evaluate(self, context):
        route = context.routing_decision or {}
        if route.get("route_type") == ROUTE_BLOCKED:
            return RuleResult(
                rule=self.name,
                allow_download=False,
                route_type=ROUTE_BLOCKED,
                cost_level=COST_STRICT,
                reason=route.get("error_code") or "cost_guard_blocked",
                confidence_delta=0.2,
            )
        return RuleResult(
            rule=self.name,
            allow_download=True,
            reason="cost_guard_passed",
        )


class ProviderRule(PolicyRule):
    name = "provider_rule"

    def evaluate(self, context):
        requires_server = any(provider_item_requires_server(item) for item in context.media_items)
        if requires_server:
            return RuleResult(
                rule=self.name,
                provider=context.provider_hint,
                route_type=ROUTE_ZIP,
                reason="provider_requires_server_side_execution",
                confidence_delta=0.04,
            )
        return RuleResult(
            rule=self.name,
            provider=context.provider_hint,
            reason=f"provider_selected:{context.provider_hint or 'native'}",
        )


class RoutingRule(PolicyRule):
    name = "routing_rule"

    def evaluate(self, context):
        route = context.routing_decision or {}
        route_type = route.get("route_type") or ROUTE_ZIP
        reason = route.get("reason") or route.get("route_reason") or "routing_engine_suggestion"
        return RuleResult(
            rule=self.name,
            route_type=route_type,
            reason=f"routing:{route_type}:{reason}",
        )


class MobileRule(PolicyRule):
    name = "mobile_rule"

    def evaluate(self, context):
        if not context.is_mobile:
            return RuleResult(rule=self.name, reason="desktop_or_unknown_client")
        has_server_provider = any(provider_item_requires_server(item) for item in context.media_items)
        if (
            context.image_count == context.item_count
            and context.item_count <= 12
            and not has_server_provider
        ):
            return RuleResult(
                rule=self.name,
                route_type=ROUTE_DIRECT,
                reason="mobile_image_direct_preferred",
                confidence_delta=0.03,
            )
        return RuleResult(
            rule=self.name,
            reason="mobile_zip_warning_applies",
            confidence_delta=-0.02,
        )


class CostLevelRule(PolicyRule):
    name = "cost_level"

    def evaluate(self, context):
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
        total_bytes = max(server_bytes, browser_bytes, context.estimated_bytes)
        if route.get("route_type") == ROUTE_BLOCKED:
            level = COST_STRICT
        elif server_bytes >= HIGH_COST_BYTES:
            level = COST_STRICT
        elif server_bytes >= MEDIUM_COST_BYTES:
            level = COST_SAFE
        elif total_bytes >= MEDIUM_COST_BYTES:
            level = COST_BALANCED
        else:
            level = COST_LOW
        return RuleResult(
            rule=self.name,
            cost_level=level,
            reason=f"cost_level:{level}",
        )


DEFAULT_POLICY_RULES = (
    CostGuardRule(),
    RoutingRule(),
    ProviderRule(),
    MobileRule(),
    CostLevelRule(),
)


def summarize_media_reason(context):
    if context.item_count <= 0:
        return "empty_selection"
    if context.image_count == context.item_count:
        return "image_only"
    if context.video_count == context.item_count:
        return "video_only"
    types = sorted({item_type(item) for item in context.media_items if item_type(item)})
    return "mixed_media:" + ",".join(types)
