from django.conf import settings

from download_console.models import DownloadRuntimeSetting

from ..models import PolicyRuntimeMode, PolicyRuntimeSetting
from ..services.provider_access import (
    request_is_in_public_rollout,
    request_rollout_bucket,
    request_user_is_staff,
)
from ..services.download_routing import (
    ROUTE_BLOCKED,
    ROUTE_DIRECT,
    ROUTE_PROXY,
    ROUTE_ZIP,
    decide_download_route,
    estimate_media_items_bytes,
    is_http_url,
    item_is_image,
    item_provider,
    item_url,
    provider_item_can_direct_download,
    route_payload,
    single_item_can_attachment_download,
    single_video_prefers_browser_direct,
    single_video_can_server_proxy_download,
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
POLICY_ENGINE_ROLLOUT_ID = "policy_engine_v35"


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


def load_policy_runtime_setting():
    try:
        return PolicyRuntimeSetting.load()
    except Exception:
        return None


def policy_engine_mode(context=None):
    mode = str(getattr(settings, "POLICY_ENGINE_MODE", "shadow") or "shadow").lower()
    if bool(getattr(settings, "POLICY_ENGINE_V35_ENABLED", False)):
        return "v3_5"
    runtime_setting = load_policy_runtime_setting()
    if runtime_setting is not None:
        return str(runtime_setting.v35_mode or PolicyRuntimeMode.SHADOW).lower()
    return mode


def policy_engine_rollout_percent():
    runtime_setting = load_policy_runtime_setting()
    if runtime_setting is None:
        return 0
    try:
        return min(100, max(0, int(runtime_setting.v35_rollout_percent or 0)))
    except (TypeError, ValueError):
        return 0


def policy_engine_rollout_bucket(request):
    return request_rollout_bucket(POLICY_ENGINE_ROLLOUT_ID, request)


def policy_engine_is_active(context=None):
    mode = policy_engine_mode(context)
    if mode in {"v3_5", "v35", "active", "policy_v3_5"}:
        return True
    request = getattr(context, "request", None)
    if mode == PolicyRuntimeMode.STAFF:
        return request_user_is_staff(request)
    if mode == PolicyRuntimeMode.PUBLIC:
        return True
    if mode == PolicyRuntimeMode.PUBLIC_GRAY:
        return request_is_in_public_rollout(
            POLICY_ENGINE_ROLLOUT_ID,
            request,
            policy_engine_rollout_percent(),
        )
    return False


def normalize_policy_weights(weights):
    normalized = {}
    for key, default in DEFAULT_POLICY_WEIGHTS.items():
        try:
            normalized[key] = max(0.0, float((weights or {}).get(key, default)))
        except (TypeError, ValueError):
            normalized[key] = default
    total = sum(normalized.values())
    if total <= 0:
        return dict(DEFAULT_POLICY_WEIGHTS)
    return {key: value / total for key, value in normalized.items()}


def configured_policy_weights():
    configured = getattr(settings, "POLICY_ENGINE_WEIGHTS", None)
    if not isinstance(configured, dict):
        return dict(DEFAULT_POLICY_WEIGHTS)
    return normalize_policy_weights(configured)


def policy_weight_diagnostics():
    static_weights = configured_policy_weights()
    runtime_setting = load_policy_runtime_setting()
    if not runtime_setting or not bool(runtime_setting.v4_auto_weights_enabled):
        return {
            "weights": static_weights,
            "source": "static",
            "confidence": 1.0,
            "reason_chain": ["static-policy-weights"],
        }

    try:
        from .adaptive import get_adaptive_weight_recommendation

        recommendation = get_adaptive_weight_recommendation(
            window_hours=runtime_setting.v4_adaptive_window_hours,
        )
    except Exception:
        return {
            "weights": static_weights,
            "source": "static_adaptive_unavailable",
            "confidence": 0.0,
            "reason_chain": ["adaptive-weight-recommendation-unavailable"],
        }

    try:
        confidence = max(0.0, min(1.0, float(recommendation.get("confidence") or 0)))
    except (TypeError, ValueError):
        confidence = 0.0
    try:
        min_confidence = max(
            0.0,
            min(1.0, float(runtime_setting.v4_adaptive_min_confidence or 0.65)),
        )
    except (TypeError, ValueError):
        min_confidence = 0.65
    if confidence < min_confidence or int(recommendation.get("sample_count") or 0) <= 0:
        return {
            "weights": static_weights,
            "source": "static_adaptive_confidence_too_low",
            "confidence": confidence,
            "reason_chain": recommendation.get("reason_chain") or [],
        }

    return {
        "weights": normalize_policy_weights(recommendation.get("weights") or static_weights),
        "source": "policy_v4_adaptive",
        "confidence": confidence,
        "reason_chain": recommendation.get("reason_chain") or [],
    }


def policy_weights():
    return policy_weight_diagnostics()["weights"]


def aggregate_weighted_routes(results, weights):
    weighted_routes = {}
    for result in results:
        weight = weights.get(result.policy, 0)
        weighted = float(result.score or 0) * float(result.confidence or 0) * weight
        weighted_routes[result.route] = weighted_routes.get(result.route, 0) + weighted
    return weighted_routes


def resolve_conflict(results, route_hint=None, weights=None):
    results = tuple(results or ())
    cost_result = next((item for item in results if item.policy == "cost"), None)
    if cost_result and cost_result.route == ROUTE_BLOCKED:
        return cost_result.route, 1.0, "cost-hard-constraint"

    weights = weights or policy_weights()
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
    weight_info = policy_weight_diagnostics()
    weights = weight_info["weights"]
    route, confidence, resolver_reason = resolve_conflict(
        results,
        context.route_hint,
        weights=weights,
    )
    provider = next((result.provider for result in results if result.provider), "")
    cost_level = next((result.cost_level for result in results if result.cost_level), "low")
    fallback_route = fallback_route_for(context)
    active = policy_engine_is_active(context)
    decision_source = "policy_v3_5" if active else "policy_v3_5_shadow"
    if not active:
        decision_source = "routing_v2_fallback_with_policy_shadow"

    return PolicyDecision(
        allow_download=route != ROUTE_BLOCKED,
        route=route,
        provider=provider or "native",
        cost_level=cost_level,
        confidence=confidence,
        reason_chain=tuple(
            [resolver_reason]
            + [result.reason for result in results]
            + list(weight_info.get("reason_chain") or [])
        ),
        policy_scores={result.policy: result.score for result in results},
        weighted_routes=aggregate_weighted_routes(results, weights),
        fallback_route=fallback_route,
        decision_source=decision_source,
        active=active,
        estimated_savings_bytes=estimated_server_savings(context.route_hint, route),
        policy_weights=weights,
        weight_source=weight_info["source"],
        weight_confidence=weight_info["confidence"],
    )


def route_can_be_browser_direct(media_items):
    if len(media_items) != 1:
        return False
    item = media_items[0]
    if item_is_image(item):
        return (
            is_http_url(item_url(item))
            and not item_provider(item)
            and provider_item_can_direct_download(item)
        )
    return single_video_prefers_browser_direct(item)


def build_policy_route_payload(context, decision):
    media_items = list(context.media_items)
    estimated_bytes = estimate_media_items_bytes(media_items)
    warnings = list((context.route_hint or {}).get("warnings") or ())
    if decision.route == ROUTE_BLOCKED:
        return route_payload(
            route_type=ROUTE_BLOCKED,
            route_reason="Policy Engine 拦截下载请求",
            download_strategy="blocked_by_policy_engine",
            media_items=media_items,
            estimated_bytes=estimated_bytes,
            error_code="POLICY_ENGINE_BLOCKED",
            warnings=warnings,
        )
    if decision.route == ROUTE_ZIP:
        return route_payload(
            route_type=ROUTE_ZIP,
            route_reason="Policy Engine 选择服务端打包下载",
            download_strategy="server_zip_policy",
            media_items=media_items,
            estimated_bytes=estimated_bytes,
            warnings=warnings,
        )
    if (
        decision.route == ROUTE_PROXY
        and len(media_items) == 1
        and (
            single_item_can_attachment_download(media_items[0])
            or single_video_can_server_proxy_download(media_items[0])
        )
    ):
        return route_payload(
            route_type=ROUTE_PROXY,
            route_reason="Policy Engine 选择单文件服务端下载",
            download_strategy="server_single_attachment",
            media_items=media_items,
            estimated_bytes=estimated_bytes,
            warnings=warnings,
        )
    if decision.route == ROUTE_DIRECT and route_can_be_browser_direct(media_items):
        return route_payload(
            route_type=ROUTE_DIRECT,
            route_reason="Policy Engine 选择浏览器直连下载",
            download_strategy="browser_direct",
            media_items=media_items,
            estimated_bytes=estimated_bytes,
            warnings=warnings,
        )
    return None


def attach_policy_metadata(route_info, decision, request=None):
    updated = dict(route_info or {})
    updated["policy_engine_version"] = POLICY_ENGINE_VERSION
    updated["policy_active"] = bool(decision.active)
    updated["policy_decision_source"] = decision.decision_source
    updated["final_policy_route"] = decision.route
    updated["policy_confidence"] = decision.confidence
    updated["policy_estimated_savings_bytes"] = decision.estimated_savings_bytes
    updated["policy_scores"] = dict(decision.policy_scores or {})
    updated["policy_decision"] = {
        **decision.to_dict(),
        "version": POLICY_ENGINE_VERSION,
        "rollout_percent": policy_engine_rollout_percent(),
        "rollout_bucket": policy_engine_rollout_bucket(request),
    }
    return updated


def apply_policy_decision_to_route(
    request,
    media_items,
    route_info,
    *,
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
    if not decision.active:
        return attach_policy_metadata(route_info, decision, request)

    policy_route = build_policy_route_payload(context, decision)
    if policy_route is None:
        fallback = attach_policy_metadata(route_info, decision, request)
        fallback["policy_fallback_reason"] = "policy-route-not-executable-by-current-routing-layer"
        return fallback

    policy_route["strategy_version"] = POLICY_ENGINE_VERSION
    return attach_policy_metadata(policy_route, decision, request)


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
        "rollout_percent": policy_engine_rollout_percent(),
        "rollout_bucket": policy_engine_rollout_bucket(request),
    }
