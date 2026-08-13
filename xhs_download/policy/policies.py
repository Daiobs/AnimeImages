from download_console.models import DownloadRuntimeSetting

from ..services.download_routing import (
    ROUTE_BLOCKED,
    ROUTE_DIRECT,
    ROUTE_PROXY,
    ROUTE_ZIP,
    download_cost_guard_block,
    estimate_media_items_bytes,
    item_is_image,
    item_is_video,
    provider_item_requires_server,
    request_is_mobile,
    safe_download_size,
    single_video_prefers_browser_direct,
)
from ..services.routing_v3 import decide_route_v3, infer_provider, provider_score
from .types import PolicyContext, PolicyResult


LOW_COST_BYTES = 50 * 1024 * 1024
HIGH_COST_BYTES = 300 * 1024 * 1024


def clamp_score(value, default=0.75):
    try:
        value = float(value)
    except (TypeError, ValueError):
        value = default
    return max(0.0, min(1.0, value))


def media_cost_level(media_items):
    estimated_bytes = estimate_media_items_bytes(media_items)
    if estimated_bytes >= HIGH_COST_BYTES:
        return "high"
    if estimated_bytes >= LOW_COST_BYTES:
        return "medium"
    return "low"


def route_for_media_shape(media_items):
    items = tuple(item for item in media_items or () if isinstance(item, dict))
    if not items:
        return ROUTE_BLOCKED
    if len(items) == 1 and item_is_image(items[0]):
        return ROUTE_DIRECT
    if len(items) == 1 and single_video_prefers_browser_direct(items[0]):
        return ROUTE_DIRECT
    if len(items) == 1 and item_is_video(items[0]):
        return ROUTE_PROXY
    return ROUTE_ZIP


class CostPolicy:
    name = "cost"

    def evaluate(self, context: PolicyContext):
        runtime_setting = context.runtime_setting or DownloadRuntimeSetting.load()
        guard_block = download_cost_guard_block(
            context.request,
            context.media_items,
            runtime_setting,
        )
        cost_level = media_cost_level(context.media_items)
        if guard_block:
            _, reason = guard_block
            return PolicyResult(
                policy=self.name,
                route=ROUTE_BLOCKED,
                score=1.0,
                confidence=1.0,
                reason=reason,
                provider=infer_provider(context.media_items),
                cost_level=cost_level,
            )

        estimated_bytes = estimate_media_items_bytes(context.media_items)
        if (
            len(context.media_items) == 1
            and single_video_prefers_browser_direct(context.media_items[0])
        ):
            route = ROUTE_DIRECT
            reason = "ordinary-provider-video-requires-browser-data-plane"
            score = 0.95
        elif estimated_bytes >= HIGH_COST_BYTES and len(context.media_items) == 1:
            route = ROUTE_PROXY
            reason = "high-cost-single-resource-prefers-non-zip"
            score = 0.88
        elif estimated_bytes >= HIGH_COST_BYTES:
            route = ROUTE_ZIP
            reason = "high-cost-multi-resource-requires-archive"
            score = 0.72
        else:
            route = route_for_media_shape(context.media_items)
            reason = f"{cost_level}-cost-default-shape-route"
            score = 0.82 if cost_level == "low" else 0.76

        return PolicyResult(
            policy=self.name,
            route=route,
            score=score,
            confidence=0.84,
            reason=reason,
            provider=infer_provider(context.media_items),
            cost_level=cost_level,
        )


class ProviderPolicy:
    name = "provider"

    def evaluate(self, context: PolicyContext):
        provider = context.provider_hint or infer_provider(context.media_items)
        if any(provider_item_requires_server(item) for item in context.media_items):
            return PolicyResult(
                policy=self.name,
                route=ROUTE_ZIP,
                score=0.94,
                confidence=0.92,
                reason="provider-requires-server-validation-or-merge",
                provider=provider,
                cost_level=media_cost_level(context.media_items),
            )

        route = route_for_media_shape(context.media_items)
        if route == ROUTE_DIRECT and request_is_mobile(context.request):
            reason = "mobile-single-image-can-use-direct"
            confidence = 0.82
        else:
            reason = "provider-has-no-server-only-constraint"
            confidence = 0.78

        return PolicyResult(
            policy=self.name,
            route=route,
            score=provider_score(provider),
            confidence=confidence,
            reason=reason,
            provider=provider,
            cost_level=media_cost_level(context.media_items),
        )


class RoutingPolicy:
    name = "routing"

    def evaluate(self, context: PolicyContext):
        result = decide_route_v3(
            context.request,
            context.media_items,
            context={
                "requested_mode": context.requested_mode,
                "platform_key": context.platform_key,
                "provider": context.provider_hint,
            },
        )
        return PolicyResult(
            policy=self.name,
            route=str(result.get("route_type") or ROUTE_ZIP),
            score=clamp_score(result.get("routing_score")),
            confidence=clamp_score(result.get("confidence")),
            reason=str(result.get("reason") or "routing-v3-feedback"),
            provider=str(result.get("provider") or infer_provider(context.media_items)),
            cost_level=media_cost_level(context.media_items),
        )


def estimated_server_savings(route_hint, policy_route):
    current_server = safe_download_size((route_hint or {}).get("server_egress_estimated_bytes"))
    current_browser = safe_download_size((route_hint or {}).get("browser_egress_estimated_bytes"))
    if policy_route in {ROUTE_DIRECT, ROUTE_PROXY}:
        return current_server
    if policy_route == ROUTE_ZIP and current_browser:
        return -current_browser
    return 0
