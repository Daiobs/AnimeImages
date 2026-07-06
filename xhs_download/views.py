import json
import logging
import time
from pathlib import Path
from urllib.parse import urlencode, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from django.conf import settings
from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Avg, Count, Q, Sum
from django.db.models.functions import TruncHour
from django.http import FileResponse, Http404, HttpResponse, JsonResponse, StreamingHttpResponse
from django.shortcuts import redirect, render
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.http import content_disposition_header
from django.urls import reverse
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_GET, require_POST

from download_console.models import DownloadRuntimeSetting
from djangoProject.health import get_runtime_readiness

from .models import (
    DownloadRequestLog,
    KukuToolDownloadDeliveryMode,
    ObservabilityStatus,
    ParseRequestLog,
    PolicyControlRecord,
    PolicyLearningRun,
    PolicyOptimizationProposal,
    PolicyOptimizationStatus,
    PolicyRuntimeMode,
    PolicyRuntimeSetting,
    ProviderAccessMode,
    ProviderAdminAuditLog,
    ProviderHealthTestSetting,
    ProviderHealthTestRun,
    ProviderHealthTestStatus,
    ProviderRuntimeSetting,
    RoutingDecisionLog,
    SelfHealingEvent,
)
from .policy.adaptive import build_adaptive_weight_recommendation
from .policy.engine import (
    apply_policy_decision_to_route,
    evaluate_policy_shadow,
    policy_weight_diagnostics,
)
from .policy.evolution import build_policy_evolution_recommendation
from .policy.learning import build_policy_learning_snapshot
from .services.common import (
    XhsDownloadError,
    XhsError,
    clean_filename,
    XhsProviderTimeoutError,
    decode_url,
    encode_url,
    validate_media_url,
)
from .services.env_config import (
    runtime_bool as env_runtime_bool,
    runtime_float as env_runtime_float,
    runtime_int as env_runtime_int,
)
from .services.download_tasks import (
    create_download_task,
    get_download_task,
    pop_download_task,
    serialize_task,
)
from .services.feedback_loop import build_feedback_loop_context
from .services.approval_gate import (
    approve_policy_change,
    reject_policy_change,
    rollback_policy_change,
)
from .services.download_routing import (
    ROUTE_BLOCKED,
    ROUTE_DIRECT,
    ROUTE_PROXY,
    ROUTE_ZIP,
    decide_download_route,
    request_is_mobile,
)
from .services.downloads import (
    CHUNK_SIZE,
    MAX_IMAGE_BYTES,
    MAX_VIDEO_BYTES,
    build_download_archive_file,
    fetch_media_response,
    guess_image_extension,
    guess_video_extension_from_response,
    invalidate_external_provider_caches,
    is_recoverable_provider_download_error,
    parse_content_length,
    provider_manifest_allows_public_download,
    probe_media_size,
    public_download_error_message,
    read_limited_response,
    response_chunk_looks_like_html,
    save_media_items_to_server,
    validate_response_content_type,
)
from .services.platforms.douyin_kukutool import (
    get_kukutool_provider_health,
    is_byte_cdn_url,
    set_kukutool_admin_disabled,
)
from .services.platforms.gallerydl_generic import GalleryDlGenericProvider
from .services.platforms.kukutool_generic import KukutoolGenericProvider
from .services.platforms.douyin import DouyinAdapter
from .services.platforms.douyin_providers import DouyinTianTianJieXiProvider
from .services.platforms.tiantianjiexi_generic import (
    TianTianJieXiGenericProvider,
    TianTianJieXiResultNormalizer,
)
from .services.platforms.third_party import (
    GenericThirdPartyAdapter,
    THIRD_PARTY_PLATFORM_RULES,
    build_third_party_rollout_status,
)
from .services.platforms.ytdlp_generic import YtDlpGenericProvider
from .services.provider_access import (
    PROVIDER_DOUYIN_ULTRA,
    PROVIDER_THIRD_PARTY,
    PROVIDER_YOUTUBE_MERGE,
    provider_access_summary,
    provider_is_allowed_for_request,
    third_party_public_access_summary,
)
from .services.provider_strategy import (
    build_download_strategy,
    build_download_strategy_matrix,
)
from .services.provider_runtime_config import runtime_float
from .services.provider_recovery import (
    get_kukutool_manifest_identity,
    iter_kukutool_download_recovery_urls,
)
from .services.registry import get_adapter_for_url, parse_media_page
from .services.provider_manifest import build_provider_manifest_token
from .services.observability import (
    provider_runtime_snapshot,
    record_download_request_log,
    record_parse_request_log,
    record_provider_admin_audit_log,
    sanitize_observability_text,
)
from .services.recovery_engine import (
    FAILURE_PROVIDER_TIMEOUT,
    proxy_failure_recovery,
    safe_routing_decision,
    zip_failure_recovery,
)
from .services.self_healing_engine import SelfHealingEngine, attach_healing_to_route_info
from .services.youtube_merge import build_youtube_merge_plan, get_youtube_merge_health
from .services.admin_console import build_admin_console_context
from .services.production_intelligence import build_observability_v2_context
from .services.policy_control_layer import (
    build_policy_control_preview,
    create_controlled_policy_proposals,
)
from .services.control_convergence_engine import build_control_convergence_context
from .services.stabilization_engine import build_stability_v6_context
from .services.autonomous_evolution_engine import build_autonomous_evolution_context
from .services.governance_engine import build_governance_v8_context
from .services.control_plane import build_control_plane_context
from .services.rollout_guard import build_rollout_guard_context
from .services.routing_v3 import decide_route_v3
from .services.rate_limits import (
    acquire_concurrency,
    consume_daily,
    consume_fixed_window,
    get_client_identity,
    has_daily_capacity,
    release_concurrency,
)
from .services.region_routing import get_request_country_code


logger = logging.getLogger(__name__)

ADMIN_LOG_PAGE_SIZE = 50
ADMIN_DISPLAY_TIME_ZONE = "Asia/Shanghai"


def admin_display_timezone():
    timezone_name = str(getattr(settings, "ADMIN_DISPLAY_TIME_ZONE", ADMIN_DISPLAY_TIME_ZONE) or ADMIN_DISPLAY_TIME_ZONE)
    try:
        return ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        return ZoneInfo(ADMIN_DISPLAY_TIME_ZONE)


def format_admin_datetime(value):
    if not value:
        return ""
    return timezone.localtime(value, admin_display_timezone()).strftime("%Y-%m-%d %H:%M:%S")


def paginate_admin_queryset(request, queryset, page_size=ADMIN_LOG_PAGE_SIZE):
    paginator = Paginator(queryset, page_size)
    page_obj = paginator.get_page(request.GET.get("page"))
    query_params = request.GET.copy()
    query_params.pop("page", None)
    return page_obj, query_params.urlencode()
PREVIEW_MAX_IMAGE_BYTES = 8 * 1024 * 1024
SIGNED_THIRD_PARTY_VIDEO_PROVIDERS = {
    "kukutool_generic",
    "tiantianjiexi_generic",
    "gallery_dl_generic",
    "yt_dlp_generic",
    "youtube_merge",
    "yt_dlp_merge",
}


def format_file_size(size_bytes):
    try:
        size_bytes = int(size_bytes or 0)
    except (TypeError, ValueError):
        size_bytes = 0
    if size_bytes <= 0:
        return "大小未知"

    units = ("B", "KB", "MB", "GB")
    value = float(size_bytes)
    unit_index = 0
    while value >= 1024 and unit_index < len(units) - 1:
        value /= 1024
        unit_index += 1

    if unit_index == 0:
        return f"{int(value)} {units[unit_index]}"
    return f"{value:.1f} {units[unit_index]}"


def elapsed_ms(started_at):
    try:
        return int((time.monotonic() - float(started_at or 0)) * 1000)
    except (TypeError, ValueError):
        return 0


def get_video_size_bytes(asset):
    if asset.size_bytes:
        return asset.size_bytes
    if asset.provider in SIGNED_THIRD_PARTY_VIDEO_PROVIDERS:
        return 0
    return probe_media_size(asset.url)


def build_provider_preview_query(asset, preview_url, asset_id_suffix=""):
    if not asset.provider or not preview_url:
        return ""
    if not asset.asset_id or not asset.provider_resource_id or not asset.provider_cache_key:
        return ""

    asset_id = asset.asset_id
    media_type = asset.media_type
    if asset_id_suffix:
        asset_id = f"{asset_id}:{asset_id_suffix}"
        media_type = "image"
    item = {
        "type": media_type,
        "url": preview_url,
        "asset_id": asset_id,
        "provider": asset.provider,
        "provider_resource_id": asset.provider_resource_id,
        "provider_cache_key": asset.provider_cache_key,
    }
    token = build_provider_manifest_token(
        asset_id=asset_id,
        provider=asset.provider,
        provider_resource_id=asset.provider_resource_id,
        provider_cache_key=asset.provider_cache_key,
        url=preview_url,
        media_type=media_type,
    )
    item["provider_manifest_token"] = token
    if not provider_manifest_allows_public_download(item):
        return ""
    return urlencode(
        {
            "type": media_type,
            "asset_id": asset_id,
            "provider": asset.provider,
            "provider_resource_id": asset.provider_resource_id,
            "provider_cache_key": asset.provider_cache_key,
            "provider_manifest_token": token,
        }
    )


def build_provider_preview_item(request, url):
    item = {
        "type": request.GET.get("type", ""),
        "url": url,
        "asset_id": request.GET.get("asset_id", ""),
        "provider": request.GET.get("provider", ""),
        "provider_resource_id": request.GET.get("provider_resource_id", ""),
        "provider_cache_key": request.GET.get("provider_cache_key", ""),
        "provider_manifest_token": request.GET.get("provider_manifest_token", ""),
    }
    return item if provider_manifest_allows_public_download(item) else None


PLATFORM_PAGE_CONFIG = {
    "home": {
        "title": "星空的高清下载小站",
        "display_name": "首页",
        "platform_id": "",
        "theme": "home",
        "heading": "高清素材解析下载",
        "description": "无需登录解析去水印，支持视频、图片、批量无水印保存",
        "label": "素材链接",
        "placeholder": "请将复制的链接粘贴到这里",
        "home_route": "home",
        "results_route": "home_results",
    },
    "xhs": {
        "title": "小红书高清下载",
        "display_name": "小红书",
        "platform_id": "xiaohongshu",
        "theme": "xhs",
        "heading": "小红书高清素材下载",
        "description": "粘贴小红书链接，解析图片/视频内容，保存原始素材文件。",
        "label": "小红书链接",
        "placeholder": "粘贴小红书链接...",
        "home_route": "xhs_home",
        "results_route": "xhs_results",
    },
    "tiktok": {
        "title": "抖音高清下载",
        "display_name": "抖音",
        "platform_id": "douyin",
        "theme": "tiktok",
        "heading": "抖音高清素材下载",
        "description": "粘贴抖音链接，解析图文图片或视频内容，保存原始素材文件。",
        "label": "抖音链接",
        "placeholder": "粘贴抖音链接或分享文案...",
        "home_route": "tiktok_home",
        "results_route": "tiktok_results",
    },
    "bilibili": {
        "title": "哔哩哔哩高清下载",
        "display_name": "哔哩哔哩",
        "platform_id": "bilibili",
        "theme": "bilibili",
        "heading": "哔哩哔哩高清素材下载",
        "description": "粘贴哔哩哔哩视频链接，解析视频内容和封面素材，保存原始文件。",
        "label": "哔哩哔哩链接",
        "placeholder": "粘贴哔哩哔哩链接...",
        "home_route": "bilibili_home",
        "results_route": "bilibili_results",
    },
}

PLATFORM_DISPLAY_NAMES = {
    "xiaohongshu": "小红书",
    "douyin": "抖音",
    "bilibili": "哔哩哔哩",
}
RESULT_THEME_BY_PLATFORM = {
    "xiaohongshu": "xhs",
    "rednote": "xhs",
    "douyin": "tiktok",
    "tiktok": "tiktok",
    "bilibili": "bilibili",
}
PLATFORM_KEY_BY_PLATFORM = {
    "xiaohongshu": "xhs",
    "rednote": "xhs",
    "douyin": "tiktok",
    "tiktok": "tiktok",
    "bilibili": "bilibili",
}


def request_flag(value):
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def ultra_hd_is_available(request=None):
    if not provider_is_allowed_for_request(PROVIDER_DOUYIN_ULTRA, request):
        return False
    health = get_kukutool_provider_health()
    return health["available"]


def youtube_merge_mode_is_available(request):
    health = get_youtube_merge_health()
    if not health.get("implemented") or not health.get("ready"):
        return False
    return provider_is_allowed_for_request(PROVIDER_YOUTUBE_MERGE, request)


def third_party_provider_is_available(request):
    return provider_is_allowed_for_request(PROVIDER_THIRD_PARTY, request)


def build_third_party_provider_summary(
    kukutool_health,
    tiantianjiexi_health,
    gallerydl_health,
    ytdlp_health,
):
    kukutool_enabled = bool(kukutool_health.get("provider_enabled"))
    kukutool_circuit_open = bool((kukutool_health.get("circuit") or {}).get("open"))
    provider_states = {
        "kukutool_generic": {
            "enabled": kukutool_enabled,
            "ready": bool(kukutool_enabled and not kukutool_circuit_open),
            "issue": "circuit_open" if kukutool_circuit_open else "",
        },
        "tiantianjiexi_generic": {
            "enabled": bool(tiantianjiexi_health.get("enabled")),
            "ready": bool(
                tiantianjiexi_health.get("enabled")
                and not (tiantianjiexi_health.get("circuit") or {}).get("open")
            ),
            "issue": (
                "circuit_open"
                if (tiantianjiexi_health.get("circuit") or {}).get("open")
                else (
                    ""
                    if tiantianjiexi_health.get("api_configured")
                    else "api_not_configured"
                )
            ),
        },
        "gallery_dl_generic": {
            "enabled": bool(gallerydl_health.get("enabled")),
            "ready": bool(gallerydl_health.get("enabled") and gallerydl_health.get("ready")),
            "issue": (
                gallerydl_health.get("version_probe_error")
                or gallerydl_health.get("error")
                or ""
            ),
        },
        "yt_dlp_generic": {
            "enabled": bool(ytdlp_health.get("enabled")),
            "ready": bool(ytdlp_health.get("enabled") and ytdlp_health.get("ready")),
            "issue": (
                ytdlp_health.get("version_probe_error")
                or ytdlp_health.get("error")
                or ""
            ),
        },
    }
    enabled_provider_ids = tuple(
        provider_id
        for provider_id, state in provider_states.items()
        if state["enabled"]
    )
    ready_provider_ids = tuple(
        provider_id
        for provider_id, state in provider_states.items()
        if state["ready"]
    )
    global_enabled = bool(getattr(settings, "THIRD_PARTY_PROVIDER_ENABLED", False))
    public_access_enabled = global_enabled
    staff_only = False
    issue = ""
    if global_enabled and not enabled_provider_ids:
        issue = "no_provider_enabled"
    elif global_enabled and not ready_provider_ids:
        issue = "no_ready_provider"
    rollout = build_third_party_rollout_status(
        enabled=global_enabled,
        public_access_enabled=public_access_enabled,
        staff_only=staff_only,
        issue=issue,
    )
    return {
        "enabled": global_enabled,
        "public_access_enabled": public_access_enabled,
        "staff_only": staff_only,
        "configured": bool(global_enabled and enabled_provider_ids),
        "public_ready": bool(
            global_enabled
            and ready_provider_ids
        ),
        "staff_ready": bool(
            global_enabled and ready_provider_ids
        ),
        "enabled_provider_ids": enabled_provider_ids,
        "ready_provider_ids": ready_provider_ids,
        "provider_states": provider_states,
        "issue": issue,
        "rollout": rollout,
    }


def provider_settings_admin_required(request):
    return bool(
        getattr(request.user, "is_active", False)
        and getattr(request.user, "is_staff", False)
    )


def policy_engine_view_allowed(request):
    return observability_access_allowed(
        request,
        "xhs_download.view_policyruntimesetting",
        "xhs_download.change_policyruntimesetting",
    )


def policy_engine_change_allowed(request):
    user = getattr(request, "user", None)
    return bool(
        getattr(user, "is_active", False)
        and getattr(user, "is_staff", False)
        and (
            getattr(user, "is_superuser", False)
            or user.has_perm("xhs_download.change_policyruntimesetting")
        )
    )


def policy_runtime_snapshot(setting):
    return {
        "v35_mode": setting.v35_mode,
        "v35_rollout_percent": setting.v35_rollout_percent,
        "v4_adaptive_shadow_enabled": setting.v4_adaptive_shadow_enabled,
        "v4_auto_weights_enabled": setting.v4_auto_weights_enabled,
        "v4_adaptive_window_hours": setting.v4_adaptive_window_hours,
        "v4_adaptive_min_confidence": setting.v4_adaptive_min_confidence,
    }


def clamp_int(value, *, minimum=0, maximum=100, default=0):
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = default
    return min(maximum, max(minimum, value))


def clamp_float(value, *, minimum=0.0, maximum=1.0, default=0.0):
    try:
        value = float(value)
    except (TypeError, ValueError):
        value = default
    return min(maximum, max(minimum, value))


def policy_learning_card(snapshot):
    recommendation = snapshot.get("shadow_recommendation") or {}
    return {
        "window_hours": snapshot.get("window_hours"),
        "sample_count": snapshot.get("sample_count"),
        "download_count": snapshot.get("download_count"),
        "parse_count": snapshot.get("parse_count"),
        "route": recommendation.get("route") or "-",
        "reason": recommendation.get("reason") or "-",
        "confidence": snapshot.get("confidence"),
        "estimated_savings": format_file_size(snapshot.get("estimated_savings_bytes")),
    }


def build_policy_engine_context(request, runtime_setting):
    weight_info = policy_weight_diagnostics()
    adaptive = build_adaptive_weight_recommendation(
        window_hours=runtime_setting.v4_adaptive_window_hours,
    )
    evolution = build_policy_evolution_recommendation(
        window_hours=runtime_setting.v4_adaptive_window_hours,
        runtime_setting=runtime_setting,
    )
    feedback = adaptive.get("feedback_summary") or {}
    recent_decisions = [
        {
            "created_at": format_admin_datetime(item.created_at),
            "status": item.status,
            "actual_route": item.route_type or "-",
            "recommended_route": item.final_policy_route or "-",
            "policy_active": item.policy_active,
            "weight_source": item.policy_decision_source or "-",
            "provider": item.provider or "-",
            "actual_bytes": format_file_size(item.actual_bytes),
            "duration_ms": item.duration_ms,
            "request_id": item.request_id,
        }
        for item in DownloadRequestLog.objects.exclude(final_policy_route="")
        .order_by("-created_at")[:10]
    ]
    recent_optimizations = [
        {
            "created_at": format_admin_datetime(item.created_at),
            "kind": item.kind,
            "status": item.status,
            "risk": item.risk or "-",
            "safety_decision": item.safety_decision or "-",
            "confidence": item.confidence,
            "sample_count": item.sample_count,
            "automatic_execution": item.automatic_execution,
            "runtime_mutated": item.runtime_mutated,
        }
        for item in PolicyOptimizationProposal.objects.order_by("-created_at")[:10]
    ]
    return {
        "title": "Policy Engine",
        "runtime_setting": runtime_setting,
        "policy_modes": PolicyRuntimeMode.choices,
        "can_change_policy": policy_engine_change_allowed(request),
        "weight_info": weight_info,
        "adaptive": adaptive,
        "evolution": evolution,
        "feedback": feedback,
        "learning_cards": [
            policy_learning_card(build_policy_learning_snapshot(window_hours=24)),
            policy_learning_card(build_policy_learning_snapshot(window_hours=24 * 7)),
        ],
        "recent_decisions": recent_decisions,
        "recent_optimizations": recent_optimizations,
    }


@csrf_protect
def policy_engine_view(request):
    if not policy_engine_view_allowed(request):
        return JsonResponse({"detail": "Not found."}, status=404)

    runtime_setting = PolicyRuntimeSetting.load()
    if request.method == "POST":
        if not policy_engine_change_allowed(request):
            return JsonResponse({"detail": "Not found."}, status=404)
        before_snapshot = policy_runtime_snapshot(runtime_setting)
        valid_modes = {choice[0] for choice in PolicyRuntimeMode.choices}
        v35_mode = request.POST.get("v35_mode", PolicyRuntimeMode.SHADOW)
        if v35_mode not in valid_modes:
            v35_mode = PolicyRuntimeMode.SHADOW
        runtime_setting.v35_mode = v35_mode
        runtime_setting.v35_rollout_percent = clamp_int(
            request.POST.get("v35_rollout_percent"),
            minimum=0,
            maximum=100,
            default=0,
        )
        runtime_setting.v4_adaptive_shadow_enabled = (
            request.POST.get("v4_adaptive_shadow_enabled") == "1"
        )
        runtime_setting.v4_auto_weights_enabled = (
            request.POST.get("v4_auto_weights_enabled") == "1"
        )
        runtime_setting.v4_adaptive_window_hours = clamp_int(
            request.POST.get("v4_adaptive_window_hours"),
            minimum=1,
            maximum=168,
            default=24,
        )
        runtime_setting.v4_adaptive_min_confidence = clamp_float(
            request.POST.get("v4_adaptive_min_confidence"),
            minimum=0.0,
            maximum=1.0,
            default=0.65,
        )
        runtime_setting.save(
            update_fields=[
                "v35_mode",
                "v35_rollout_percent",
                "v4_adaptive_shadow_enabled",
                "v4_auto_weights_enabled",
                "v4_adaptive_window_hours",
                "v4_adaptive_min_confidence",
                "updated_at",
            ]
        )
        after_snapshot = policy_runtime_snapshot(runtime_setting)
        if before_snapshot != after_snapshot:
            record_provider_admin_audit_log(
                request,
                action="policy_runtime_update",
                before_snapshot=before_snapshot,
                after_snapshot=after_snapshot,
            )
        messages.success(request, "Policy Engine 运行时配置已保存。")
        return redirect("policy_engine")

    return render(
        request,
        "admin/policy_engine.html",
        build_policy_engine_context(request, runtime_setting),
    )


def build_provider_dashboard_rows(runtime_setting, request):
    kukutool_health = get_kukutool_provider_health()
    third_party_health = KukutoolGenericProvider().health()
    tiantianjiexi_health = TianTianJieXiGenericProvider().health()
    gallerydl_health = GalleryDlGenericProvider().health()
    ytdlp_health = YtDlpGenericProvider().health()
    youtube_health = get_youtube_merge_health()
    third_party_summary = build_third_party_provider_summary(
        third_party_health,
        tiantianjiexi_health,
        gallerydl_health,
        ytdlp_health,
    )
    access = {
        PROVIDER_DOUYIN_ULTRA: provider_access_summary(
            PROVIDER_DOUYIN_ULTRA,
            request,
            runtime_setting,
        ),
        PROVIDER_YOUTUBE_MERGE: provider_access_summary(
            PROVIDER_YOUTUBE_MERGE,
            request,
            runtime_setting,
        ),
        PROVIDER_THIRD_PARTY: provider_access_summary(
            PROVIDER_THIRD_PARTY,
            request,
            runtime_setting,
        ),
    }
    rows = [
        {
            "id": PROVIDER_DOUYIN_ULTRA,
            "name": "抖音超高清",
            "description": "优先使用天天解析，并与 KuKuTool 竞速获取抖音原画 / 超高清候选资源。",
            "mode_field": "douyin_ultra_mode",
            "rollout_field": "douyin_ultra_rollout_percent",
            "disabled_reason_field": "douyin_ultra_disabled_reason",
            "current_mode": runtime_setting.douyin_ultra_mode,
            "current_rollout_percent": runtime_setting.douyin_ultra_rollout_percent,
            "disabled_reason": runtime_setting.douyin_ultra_disabled_reason,
            "health_ready": bool(
                kukutool_health.get("available")
                or tiantianjiexi_health.get("enabled")
            ),
            "health_label": (
                "可用"
                if (
                    kukutool_health.get("available")
                    or tiantianjiexi_health.get("enabled")
                )
                else "不可用"
            ),
            "issue": (
                "天天解析和 KuKuTool 均不可用"
                if (
                    not tiantianjiexi_health.get("enabled")
                    and not kukutool_health.get("available")
                )
                else "天天解析熔断中"
                if (tiantianjiexi_health.get("circuit") or {}).get("open")
                else "KuKuTool 熔断中"
                if (kukutool_health.get("circuit") or {}).get("open")
                else (kukutool_health.get("runtime") or {}).get("error")
                or (kukutool_health.get("sidecar") or {}).get("error")
                or ""
            ),
            "access": access[PROVIDER_DOUYIN_ULTRA],
        },
        {
            "id": PROVIDER_YOUTUBE_MERGE,
            "name": "YouTube 高清合并",
            "description": "通过 yt-dlp + ffmpeg 生成 1080p 或最高画质单文件。",
            "mode_field": "youtube_merge_mode",
            "rollout_field": "youtube_merge_rollout_percent",
            "disabled_reason_field": "youtube_merge_disabled_reason",
            "current_mode": runtime_setting.youtube_merge_mode,
            "current_rollout_percent": runtime_setting.youtube_merge_rollout_percent,
            "disabled_reason": runtime_setting.youtube_merge_disabled_reason,
            "health_ready": bool(youtube_health.get("ready")),
            "health_label": "可用" if youtube_health.get("ready") else "不可用",
            "issue": (
                (youtube_health.get("ffmpeg") or {}).get("version_probe_error")
                or (youtube_health.get("ytdlp") or {}).get("version_probe_error")
                or ""
            ),
            "access": access[PROVIDER_YOUTUBE_MERGE],
        },
        {
            "id": PROVIDER_THIRD_PARTY,
            "name": "通用第三方 Provider",
            "description": "用于 yt-dlp、gallery-dl 和长尾平台的通用解析入口。",
            "mode_field": "third_party_mode",
            "rollout_field": "third_party_rollout_percent",
            "disabled_reason_field": "third_party_disabled_reason",
            "current_mode": runtime_setting.third_party_mode,
            "current_rollout_percent": runtime_setting.third_party_rollout_percent,
            "disabled_reason": runtime_setting.third_party_disabled_reason,
            "health_ready": bool(third_party_summary.get("staff_ready")),
            "health_label": (
                "可用" if third_party_summary.get("staff_ready") else "不可用"
            ),
            "issue": third_party_summary.get("issue") or "",
            "access": access[PROVIDER_THIRD_PARTY],
        },
    ]
    return rows, {
        "kukutool": kukutool_health,
        "tiantianjiexi": tiantianjiexi_health,
        "youtube_merge": youtube_health,
        "third_party": third_party_summary,
        "third_party_tiantianjiexi": tiantianjiexi_health,
    }


@csrf_protect
def provider_settings_view(request):
    if not provider_settings_admin_required(request):
        return JsonResponse({"detail": "Not found."}, status=404)

    runtime_setting = ProviderRuntimeSetting.load()
    valid_modes = {choice[0] for choice in ProviderAccessMode.choices}
    valid_kukutool_delivery_modes = {choice[0] for choice in KukuToolDownloadDeliveryMode.choices}
    if request.method == "POST":
        before_snapshot = provider_runtime_snapshot(runtime_setting)
        for field_name in (
            "douyin_ultra_mode",
            "youtube_merge_mode",
            "third_party_mode",
        ):
            value = request.POST.get(field_name, ProviderAccessMode.DISABLED)
            if value not in valid_modes:
                value = ProviderAccessMode.DISABLED
            setattr(runtime_setting, field_name, value)
        for field_name in (
            "douyin_ultra_rollout_percent",
            "youtube_merge_rollout_percent",
            "third_party_rollout_percent",
        ):
            try:
                rollout_percent = int(request.POST.get(field_name, "0") or 0)
            except (TypeError, ValueError):
                rollout_percent = 0
            setattr(runtime_setting, field_name, min(100, max(0, rollout_percent)))
        runtime_setting.douyin_ultra_total_timeout_seconds = clamp_float(
            request.POST.get("douyin_ultra_total_timeout_seconds"),
            minimum=1.0,
            maximum=30.0,
            default=runtime_setting.douyin_ultra_total_timeout_seconds,
        )
        runtime_setting.douyin_ultra_kukutool_grace_seconds = clamp_float(
            request.POST.get("douyin_ultra_kukutool_grace_seconds"),
            minimum=0.0,
            maximum=10.0,
            default=runtime_setting.douyin_ultra_kukutool_grace_seconds,
        )
        runtime_setting.kukutool_download_timeout_seconds = clamp_float(
            request.POST.get("kukutool_download_timeout_seconds"),
            minimum=1.0,
            maximum=180.0,
            default=runtime_setting.kukutool_download_timeout_seconds,
        )
        kukutool_delivery_mode = request.POST.get(
            "kukutool_download_delivery_mode",
            runtime_setting.kukutool_download_delivery_mode,
        )
        if kukutool_delivery_mode not in valid_kukutool_delivery_modes:
            kukutool_delivery_mode = KukuToolDownloadDeliveryMode.SERVER_STREAM
        runtime_setting.kukutool_download_delivery_mode = kukutool_delivery_mode
        runtime_setting.youtube_frontend_timeout_seconds = clamp_float(
            request.POST.get("youtube_frontend_timeout_seconds"),
            minimum=3.0,
            maximum=60.0,
            default=runtime_setting.youtube_frontend_timeout_seconds,
        )
        for field_name in (
            "douyin_ultra_disabled_reason",
            "youtube_merge_disabled_reason",
            "third_party_disabled_reason",
            "third_party_allowed_public_hosts",
            "third_party_blocked_hosts",
            "third_party_allowed_public_platforms",
            "third_party_blocked_platforms",
        ):
            setattr(runtime_setting, field_name, str(request.POST.get(field_name, "") or "").strip())
        runtime_setting.kukutool_disabled = request.POST.get("kukutool_disabled") == "1"
        runtime_setting.save(
            update_fields=[
                "douyin_ultra_mode",
                "youtube_merge_mode",
                "third_party_mode",
                "douyin_ultra_rollout_percent",
                "youtube_merge_rollout_percent",
                "third_party_rollout_percent",
                "douyin_ultra_disabled_reason",
                "youtube_merge_disabled_reason",
                "third_party_disabled_reason",
                "third_party_allowed_public_hosts",
                "third_party_blocked_hosts",
                "third_party_allowed_public_platforms",
                "third_party_blocked_platforms",
                "kukutool_disabled",
                "douyin_ultra_total_timeout_seconds",
                "douyin_ultra_kukutool_grace_seconds",
                "kukutool_download_timeout_seconds",
                "kukutool_download_delivery_mode",
                "youtube_frontend_timeout_seconds",
                "updated_at",
            ]
        )
        after_snapshot = provider_runtime_snapshot(runtime_setting)
        if before_snapshot != after_snapshot:
            record_provider_admin_audit_log(
                request,
                action="provider_runtime_update",
                before_snapshot=before_snapshot,
                after_snapshot=after_snapshot,
            )
        messages.success(request, "Provider 运行时配置已保存。")
        return redirect("provider_settings")

    rows, health = build_provider_dashboard_rows(runtime_setting, request)
    return render(
        request,
        "admin/provider_settings.html",
        {
            "title": "Provider 管理后台",
            "runtime_setting": runtime_setting,
            "access_modes": ProviderAccessMode.choices,
            "provider_rows": rows,
            "provider_health": health,
            "third_party_platform_rules": [
                rule.to_dict() for rule in THIRD_PARTY_PLATFORM_RULES
            ],
        },
    )


@require_GET
def provider_health_view(request):
    if not request.user.is_active or not request.user.is_staff:
        return JsonResponse({"detail": "Not found."}, status=404)
    health = get_kukutool_provider_health()
    third_party_health = KukutoolGenericProvider().health(
        source_key=request.GET.get("source_key", "")
    )
    tiantianjiexi_health = TianTianJieXiGenericProvider().health(
        source_key=request.GET.get("source_key", "")
    )
    gallerydl_health = GalleryDlGenericProvider().health()
    ytdlp_health = YtDlpGenericProvider().health()
    health["third_party"] = third_party_health
    health["third_party_tiantianjiexi"] = tiantianjiexi_health
    health["third_party_gallerydl"] = gallerydl_health
    health["third_party_ytdlp"] = ytdlp_health
    health["youtube_merge"] = get_youtube_merge_health()
    health["third_party_summary"] = build_third_party_provider_summary(
        third_party_health,
        tiantianjiexi_health,
        gallerydl_health,
        ytdlp_health,
    )
    runtime_setting = ProviderRuntimeSetting.load()
    health["provider_access"] = {
        PROVIDER_DOUYIN_ULTRA: provider_access_summary(
            PROVIDER_DOUYIN_ULTRA,
            request,
            runtime_setting,
        ),
        PROVIDER_YOUTUBE_MERGE: provider_access_summary(
            PROVIDER_YOUTUBE_MERGE,
            request,
            runtime_setting,
        ),
        PROVIDER_THIRD_PARTY: provider_access_summary(
            PROVIDER_THIRD_PARTY,
            request,
            runtime_setting,
        ),
    }
    response = JsonResponse(health)
    response["Cache-Control"] = "no-store"
    return response


@require_POST
@csrf_protect
def provider_control_view(request):
    if not request.user.is_active or not request.user.is_staff:
        return JsonResponse({"detail": "Not found."}, status=404)
    try:
        payload = json.loads(request.body or "{}")
    except (TypeError, ValueError):
        return JsonResponse({"detail": "Invalid JSON."}, status=400)

    action = str(payload.get("action") or "").strip().lower()
    if action not in {"disable", "enable"}:
        return JsonResponse(
            {"detail": "action must be enable or disable."},
            status=400,
        )
    set_kukutool_admin_disabled(action == "disable")
    health = get_kukutool_provider_health()
    third_party_health = KukutoolGenericProvider().health()
    tiantianjiexi_health = TianTianJieXiGenericProvider().health()
    gallerydl_health = GalleryDlGenericProvider().health()
    ytdlp_health = YtDlpGenericProvider().health()
    health["third_party"] = third_party_health
    health["third_party_tiantianjiexi"] = tiantianjiexi_health
    health["third_party_gallerydl"] = gallerydl_health
    health["third_party_ytdlp"] = ytdlp_health
    health["youtube_merge"] = get_youtube_merge_health()
    health["third_party_summary"] = build_third_party_provider_summary(
        third_party_health,
        tiantianjiexi_health,
        gallerydl_health,
        ytdlp_health,
    )
    runtime_setting = ProviderRuntimeSetting.load()
    health["provider_access"] = {
        PROVIDER_DOUYIN_ULTRA: provider_access_summary(
            PROVIDER_DOUYIN_ULTRA,
            request,
            runtime_setting,
        ),
        PROVIDER_YOUTUBE_MERGE: provider_access_summary(
            PROVIDER_YOUTUBE_MERGE,
            request,
            runtime_setting,
        ),
        PROVIDER_THIRD_PARTY: provider_access_summary(
            PROVIDER_THIRD_PARTY,
            request,
            runtime_setting,
        ),
    }
    response = JsonResponse(health)
    response["Cache-Control"] = "no-store"
    return response


def admin_ops_required(request):
    return observability_access_allowed(
        request,
        "xhs_download.view_parserequestlog",
        "xhs_download.view_downloadrequestlog",
        "xhs_download.view_provideradminauditlog",
    )


def observability_v2_required(request):
    return observability_access_allowed(
        request,
        "xhs_download.view_downloadrequestlog",
        "xhs_download.view_parserequestlog",
        "xhs_download.view_routingdecisionlog",
    )


def feedback_loop_required(request):
    return observability_access_allowed(
        request,
        "xhs_download.view_downloadrequestlog",
        "xhs_download.view_parserequestlog",
        "xhs_download.view_providerhealthtestrun",
        "xhs_download.view_routingdecisionlog",
    )


def policy_control_view_allowed(request):
    return observability_access_allowed(
        request,
        "xhs_download.view_policyoptimizationproposal",
        "xhs_download.change_policyoptimizationproposal",
    )


def policy_control_change_allowed(request):
    user = getattr(request, "user", None)
    return bool(
        getattr(user, "is_active", False)
        and getattr(user, "is_staff", False)
        and (
            getattr(user, "is_superuser", False)
            or user.has_perm("xhs_download.change_policyoptimizationproposal")
        )
    )


def control_convergence_view_allowed(request):
    return observability_access_allowed(
        request,
        "xhs_download.view_policycontrolrecord",
        "xhs_download.view_policyoptimizationproposal",
    )


def stability_v6_view_allowed(request):
    return observability_access_allowed(
        request,
        "xhs_download.view_downloadrequestlog",
        "xhs_download.view_policycontrolrecord",
        "xhs_download.view_routingdecisionlog",
    )


def autonomous_evolution_view_allowed(request):
    return observability_access_allowed(
        request,
        "xhs_download.view_downloadrequestlog",
        "xhs_download.view_policycontrolrecord",
        "xhs_download.view_policyoptimizationproposal",
    )


def governance_v8_view_allowed(request):
    return observability_access_allowed(
        request,
        "xhs_download.view_downloadrequestlog",
        "xhs_download.view_policycontrolrecord",
        "xhs_download.view_policyoptimizationproposal",
    )


def observability_access_allowed(request, *permissions):
    user = getattr(request, "user", None)
    if not (
        getattr(user, "is_active", False)
        and getattr(user, "is_staff", False)
    ):
        return False
    if getattr(user, "is_superuser", False):
        return True
    return any(user.has_perm(permission) for permission in permissions)


def admin_console_required(request):
    return observability_access_allowed(
        request,
        "xhs_download.view_parserequestlog",
        "xhs_download.view_downloadrequestlog",
        "xhs_download.view_providerhealthtestrun",
        "xhs_download.view_provideradminauditlog",
        "xhs_download.view_selfhealingevent",
    )


def control_plane_required(request):
    return observability_access_allowed(
        request,
        "xhs_download.view_parserequestlog",
        "xhs_download.view_downloadrequestlog",
        "xhs_download.view_providerhealthtestrun",
        "xhs_download.view_provideradminauditlog",
    )


@require_GET
def control_plane_view(request):
    if not control_plane_required(request):
        return JsonResponse({"detail": "Not found."}, status=404)
    try:
        context = build_control_plane_context(request)
    except Exception as error:
        logger.exception("control_plane_view_failed")
        context = {
            "title": "Production Control Plane",
            "version": "production_control_plane_v1",
            "request_id": str(getattr(request, "request_id", "") or ""),
            "system_status": {
                "available": False,
                "risk_level": "danger",
                "error": type(error).__name__,
            },
            "core_metrics": {"available": False},
            "provider_status": {"available": False, "rows": []},
            "download_cost": {"available": False, "route_distribution": [], "runtime": {}},
            "policy_governance": {"available": False, "recent_events": []},
            "actions": {
                "items": [{
                    "level": "danger",
                    "title": "控制平面加载失败",
                    "body": "请查看 Django 日志中的 control_plane_view_failed。",
                    "url": "",
                }]
            },
            "links": {},
        }
    return render(request, "admin/control_plane.html", context)


@require_GET
def admin_console_view(request):
    if not admin_console_required(request):
        return JsonResponse({"detail": "Not found."}, status=404)
    context = build_admin_console_context()
    context["title"] = "运维首页"
    return render(request, "admin/console.html", context)


@require_GET
def rollout_guard_view(request):
    if not admin_console_required(request):
        return JsonResponse({"detail": "Not found."}, status=404)
    context = build_rollout_guard_context(request)
    return render(request, "admin/rollout_guard.html", context)


def self_healing_access_allowed(request):
    return observability_access_allowed(
        request,
        "xhs_download.view_selfhealingevent",
    )


def self_healing_execute_allowed(request):
    return observability_access_allowed(
        request,
        "xhs_download.add_selfhealingevent",
    )


@csrf_protect
def self_healing_run_view(request):
    if not self_healing_access_allowed(request):
        return JsonResponse({"detail": "Not found."}, status=404)
    if request.method == "POST":
        if not self_healing_execute_allowed(request):
            return JsonResponse({"detail": "Not found."}, status=404)
        provider = str(request.POST.get("provider") or "kukutool").strip()[:80]
        outcome = SelfHealingEngine(
            request,
            context={
                "manual": True,
                "source": "admin_self_healing_run",
            },
        ).handle_failure(
            trigger="manual_diagnostics",
            route_result={"route_type": "", "provider": provider},
            provider=provider,
            error=XhsProviderTimeoutError("manual self-healing diagnostic provider timeout"),
            depth=1,
        )
        messages.success(
            request,
            f"Self-Healing 诊断已记录：{outcome.action} / {outcome.failure_type}",
        )
        return redirect("self_healing_run")

    recent_events = [
        {
            "created_at": format_admin_datetime(event.created_at),
            "trigger": event.trigger,
            "failure_type": event.failure_type,
            "action": event.action,
            "healed": event.healed,
            "provider": event.provider,
            "switched_to_provider": event.switched_to_provider,
            "risk": event.risk,
            "confidence": event.confidence,
            "duration_ms": event.duration_ms,
            "request_id": event.request_id,
        }
        for event in SelfHealingEvent.objects.order_by("-created_at")[:50]
    ]
    return render(
        request,
        "admin/self_healing_run.html",
        {
            "title": "Self-Healing",
            "recent_events": recent_events,
            "can_execute": self_healing_execute_allowed(request),
        },
    )


def provider_health_tests_view_allowed(request):
    return observability_access_allowed(
        request,
        "xhs_download.view_providerhealthtestrun",
    )


def provider_health_tests_execute_allowed(request):
    return observability_access_allowed(
        request,
        "xhs_download.add_providerhealthtestrun",
    )


PROVIDER_HEALTH_TESTS = (
    {
        "key": "health_ready",
        "name": "Runtime Readiness",
        "description": "验证 database/cache 是否可用。",
        "provider": "runtime",
        "provider_access_id": "",
        "url_field": "",
    },
    {
        "key": "douyin_native_parse",
        "name": "抖音普通解析",
        "description": "使用普通抖音 adapter 解析测试 URL，不强制 KuKuTool。",
        "provider": "native",
        "provider_access_id": "",
        "url_field": "douyin_url",
    },
    {
        "key": "douyin_kukutool_ultra",
        "name": "抖音 KuKuTool 超清",
        "description": "开启 prefer_ultra_hd=True 测试抖音超清链路。",
        "provider": "kukutool",
        "provider_access_id": PROVIDER_DOUYIN_ULTRA,
        "url_field": "douyin_url",
    },
    {
        "key": "tiantianjiexi_account_status",
        "name": "天天解析账号状态",
        "description": "只查询天天解析账号状态，不解析真实视频，不消耗解析额度。",
        "provider": "tiantianjiexi_generic",
        "provider_access_id": PROVIDER_THIRD_PARTY,
        "url_field": "",
    },
    {
        "key": "douyin_tiantianjiexi_ultra",
        "name": "抖音天天解析超清",
        "description": "手动调用天天解析解析抖音测试 URL，用于超清链路 smoke。",
        "provider": "tiantianjiexi",
        "provider_access_id": PROVIDER_DOUYIN_ULTRA,
        "url_field": "douyin_url",
    },
    {
        "key": "youtube_merge",
        "name": "YouTube 高清合并",
        "description": "验证 yt-dlp + ffmpeg health，并只做候选规划，不下载大文件。",
        "provider": "youtube_merge",
        "provider_access_id": PROVIDER_YOUTUBE_MERGE,
        "url_field": "youtube_url",
    },
    {
        "key": "third_party_ytdlp",
        "name": "第三方 yt-dlp",
        "description": "验证通用 yt-dlp provider health，并轻量解析测试 URL。",
        "provider": "yt_dlp_generic",
        "provider_access_id": PROVIDER_THIRD_PARTY,
        "url_field": "third_party_url",
    },
    {
        "key": "third_party_tiantianjiexi",
        "name": "第三方天天解析",
        "description": "手动调用天天解析解析第三方测试 URL。",
        "provider": "tiantianjiexi_generic",
        "provider_access_id": PROVIDER_THIRD_PARTY,
        "url_field": "third_party_url",
    },
    {
        "key": "gallery_dl",
        "name": "gallery-dl",
        "description": "验证 gallery-dl provider health，并轻量解析测试 URL。",
        "provider": "gallery_dl_generic",
        "provider_access_id": PROVIDER_THIRD_PARTY,
        "url_field": "gallerydl_url",
    },
)


def provider_health_test_by_key(test_key):
    for item in PROVIDER_HEALTH_TESTS:
        if item["key"] == test_key:
            return item
    return None


def provider_test_url_host(test_url):
    parsed = urlparse(str(test_url or ""))
    return (parsed.hostname or "").strip().lower()


def provider_health_tests_manage_config_allowed(request):
    return observability_access_allowed(
        request,
        "xhs_download.change_providerhealthtestsetting",
    )


def provider_test_url_for(item, test_setting=None):
    field_name = item.get("url_field") or ""
    if not field_name:
        return ""
    test_setting = test_setting or ProviderHealthTestSetting.load()
    return str(getattr(test_setting, field_name, "") or "").strip()


def update_provider_health_test_setting(request):
    setting = ProviderHealthTestSetting.load()
    invalid_labels = []
    labels = {
        "douyin_url": "抖音测试 URL",
        "youtube_url": "YouTube 测试 URL",
        "third_party_url": "第三方 yt-dlp 测试 URL",
        "gallerydl_url": "gallery-dl 测试 URL",
    }
    for field_name in ("douyin_url", "youtube_url", "third_party_url", "gallerydl_url"):
        value = str(request.POST.get(field_name, "") or "").strip()
        if value:
            parsed = urlparse(value)
            if parsed.scheme not in ("http", "https") or not parsed.hostname:
                invalid_labels.append(labels[field_name])
                value = ""
        setattr(setting, field_name, value)
    setting.save(update_fields=("douyin_url", "youtube_url", "third_party_url", "gallerydl_url", "updated_at"))
    return setting, invalid_labels


def download_runtime_summary(setting):
    return (
        ("每分钟下载请求数 / IP", setting.rate_limit_per_minute),
        ("每日下载请求数 / IP", setting.daily_request_limit_per_ip),
        ("每日全站下载请求数", setting.global_daily_request_limit),
        ("并发下载数 / IP", setting.max_concurrent_per_ip),
        ("每日下载流量 / IP", format_file_size(setting.daily_bytes_limit_per_ip)),
        ("每日全站下载流量", format_file_size(setting.global_daily_bytes_limit)),
    )


def provider_test_runtime_info(item, request, runtime_setting):
    provider_id = item.get("provider_access_id") or ""
    if not provider_id:
        return {
            "runtime_mode": "",
            "runtime_label": "不适用",
            "hard_enabled": True,
            "access_reason": "",
        }
    access = provider_access_summary(provider_id, request, runtime_setting)
    return {
        "runtime_mode": access["effective_mode"],
        "runtime_label": access["effective_label"],
        "hard_enabled": bool(access["hard_enabled"]),
        "access_reason": access["access_reason"],
    }


def provider_health_test_record(
    request,
    item,
    *,
    status,
    started_at,
    provider="",
    error_type="",
    error_message="",
    test_url="",
    runtime_mode="",
    hard_enabled=False,
):
    user = getattr(request, "user", None)
    return ProviderHealthTestRun.objects.create(
        request_id=str(getattr(request, "request_id", "") or ""),
        test_key=item["key"],
        status=status,
        provider=provider or item.get("provider", ""),
        duration_ms=elapsed_ms(started_at),
        error_type=str(error_type or "")[:120],
        error_message_short=sanitize_observability_text(error_message),
        runtime_mode=str(runtime_mode or "")[:32],
        hard_enabled=bool(hard_enabled),
        is_staff=bool(getattr(user, "is_active", False) and getattr(user, "is_staff", False)),
        actor_id=getattr(user, "id", None),
        actor_username=str(getattr(user, "get_username", lambda: "")() or "")[:150],
        test_url_host=provider_test_url_host(test_url)[:255],
    )


def provider_asset_manifest_validation_error(result, allowed_providers, *, require_video=True):
    if not result or not getattr(result, "assets", ()):
        return "未生成 ParseResult 资源"
    assets = tuple(result.assets or ())
    if require_video:
        assets = tuple(asset for asset in assets if asset.media_type == "video")
        if not assets:
            return "未生成视频资源"
    matching_assets = tuple(
        asset for asset in assets if str(asset.provider or "") in allowed_providers
    )
    if not matching_assets:
        return "资源 provider 与预期不一致"
    asset = matching_assets[0]
    if not asset.provider_manifest_token:
        return "资源缺少 provider_manifest_token"
    if not asset.provider_resource_id:
        return "资源缺少 provider_resource_id"
    if not asset.provider_cache_key:
        return "资源缺少 provider_cache_key"
    item = asset.to_download_item()
    if not provider_manifest_allows_public_download(item):
        return "Provider manifest 未通过下载校验"
    return ""


def build_tiantianjiexi_third_party_parse_result(test_url, timeout_seconds=10.0):
    adapter = GenericThirdPartyAdapter()
    rule = adapter.get_rule_for_url(test_url)
    if not rule:
        raise XhsParseError("第三方测试 URL 不在支持的平台规则内")
    payload = TianTianJieXiGenericProvider().parse(
        test_url,
        timeout_seconds=timeout_seconds,
    )
    normalizer = TianTianJieXiResultNormalizer(platform_id=adapter.platform_id)
    return normalizer.normalize(
        rule=rule,
        platform_name=rule.display_name,
        source_url=test_url,
        payload=payload,
    )


def run_provider_health_test(request, item):
    started_at = time.monotonic()
    runtime_setting = ProviderRuntimeSetting.load()
    test_setting = ProviderHealthTestSetting.load()
    runtime_info = provider_test_runtime_info(item, request, runtime_setting)
    test_url = provider_test_url_for(item, test_setting)
    provider = item.get("provider", "")

    def record(status, error_type="", error_message=""):
        return provider_health_test_record(
            request,
            item,
            status=status,
            started_at=started_at,
            provider=provider,
            error_type=error_type,
            error_message=error_message,
            test_url=test_url,
            runtime_mode=runtime_info["runtime_mode"],
            hard_enabled=runtime_info["hard_enabled"],
        )

    try:
        if item["key"] == "health_ready":
            readiness = get_runtime_readiness()
            if readiness["ready"]:
                return record(ProviderHealthTestStatus.SUCCESS)
            return record(
                ProviderHealthTestStatus.UNAVAILABLE,
                error_type="RuntimeUnavailable",
                error_message=json.dumps(readiness.get("components", {}), ensure_ascii=False),
            )

        if item["key"] == "tiantianjiexi_account_status":
            if not env_runtime_bool("TIANTIANJIEXI_PROVIDER_ENABLED", False):
                return record(
                    ProviderHealthTestStatus.UNAVAILABLE,
                    error_type="CapabilityDisabled",
                    error_message="TIANTIANJIEXI_PROVIDER_ENABLED 未启用",
                )
            health = TianTianJieXiGenericProvider().health()
            if health.get("account_status_available"):
                return record(ProviderHealthTestStatus.SUCCESS)
            return record(
                ProviderHealthTestStatus.UNAVAILABLE,
                error_type="AccountStatusUnavailable",
                error_message=json.dumps(health, ensure_ascii=False),
            )

        if not test_url:
            return record(
                ProviderHealthTestStatus.SKIPPED,
                error_type="TestUrlMissing",
                error_message="未配置测试 URL",
            )

        if item["key"] == "douyin_native_parse":
            result = parse_media_page(test_url, prefer_ultra_hd=False)
            if result.assets:
                return record(ProviderHealthTestStatus.SUCCESS)
            return record(
                ProviderHealthTestStatus.UNAVAILABLE,
                error_type="NoAssets",
                error_message="未解析到资源",
            )

        if item["key"] == "douyin_kukutool_ultra":
            if not (
                getattr(settings, "ULTRA_HD_PROVIDER_ENABLED", False)
                and getattr(settings, "KUKUTOOL_PROVIDER_ENABLED", False)
            ):
                return record(
                    ProviderHealthTestStatus.UNAVAILABLE,
                    error_type="CapabilityDisabled",
                    error_message="ULTRA_HD_PROVIDER_ENABLED 或 KUKUTOOL_PROVIDER_ENABLED 未启用",
                )
            health = get_kukutool_provider_health()
            if not health.get("available"):
                return record(
                    ProviderHealthTestStatus.UNAVAILABLE,
                    error_type="ProviderUnavailable",
                    error_message=json.dumps(health, ensure_ascii=False),
                )
            result = parse_media_page(test_url, prefer_ultra_hd=True)
            if result.assets:
                return record(ProviderHealthTestStatus.SUCCESS)
            return record(
                ProviderHealthTestStatus.UNAVAILABLE,
                error_type="NoAssets",
                error_message="未解析到超清资源",
            )

        if item["key"] == "douyin_tiantianjiexi_ultra":
            if not (
                getattr(settings, "ULTRA_HD_PROVIDER_ENABLED", False)
                and env_runtime_bool("THIRD_PARTY_PROVIDER_ENABLED", False)
                and env_runtime_bool("TIANTIANJIEXI_PROVIDER_ENABLED", False)
            ):
                return record(
                    ProviderHealthTestStatus.UNAVAILABLE,
                    error_type="CapabilityDisabled",
                    error_message="ULTRA_HD_PROVIDER_ENABLED、THIRD_PARTY_PROVIDER_ENABLED 或 TIANTIANJIEXI_PROVIDER_ENABLED 未启用",
                )
            adapter = DouyinAdapter()
            deadline = time.monotonic() + 10.0
            context = adapter.build_parse_context(
                test_url,
                fetch_direct_page=not bool(adapter.supports_url(test_url)),
                fast_short_link=True,
                request_deadline=deadline,
            )
            result = DouyinTianTianJieXiProvider().parse(adapter, context)
            validation_error = provider_asset_manifest_validation_error(
                result,
                {"tiantianjiexi", "tiantianjiexi_generic"},
                require_video=True,
            )
            metadata = result.metadata or {} if result else {}
            selected_provider = metadata.get("selected_provider") or metadata.get("source_provider")
            if not validation_error and selected_provider in {"tiantianjiexi", "tiantianjiexi_generic"}:
                return record(ProviderHealthTestStatus.SUCCESS)
            return record(
                ProviderHealthTestStatus.UNAVAILABLE,
                error_type="InvalidParseResult",
                error_message=validation_error or "天天解析 ParseResult provider 不匹配",
            )

        if item["key"] == "youtube_merge":
            health = get_youtube_merge_health()
            if not (health.get("implemented") and health.get("ready")):
                return record(
                    ProviderHealthTestStatus.UNAVAILABLE,
                    error_type="ProviderUnavailable",
                    error_message=json.dumps(health, ensure_ascii=False),
                )
            payload = YtDlpGenericProvider().parse(
                test_url,
                timeout_seconds=min(10.0, float(health.get("timeout_seconds") or 10.0)),
            )
            plan = build_youtube_merge_plan(
                payload,
                max_height=int(getattr(settings, "YTDLP_MERGE_DEFAULT_MAX_HEIGHT", 1080) or 1080),
            )
            if plan.get("available"):
                return record(ProviderHealthTestStatus.SUCCESS)
            return record(
                ProviderHealthTestStatus.UNAVAILABLE,
                error_type=str(plan.get("reason") or "MergeUnavailable"),
                error_message=json.dumps(plan, ensure_ascii=False),
            )

        if item["key"] == "third_party_ytdlp":
            if not env_runtime_bool("THIRD_PARTY_PROVIDER_ENABLED", False):
                return record(
                    ProviderHealthTestStatus.UNAVAILABLE,
                    error_type="CapabilityDisabled",
                    error_message="THIRD_PARTY_PROVIDER_ENABLED 未启用",
                )
            health = YtDlpGenericProvider().health(force_version_probe=True)
            if not health.get("ready"):
                return record(
                    ProviderHealthTestStatus.UNAVAILABLE,
                    error_type="ProviderUnavailable",
                    error_message=json.dumps(health, ensure_ascii=False),
                )
            payload = YtDlpGenericProvider().parse(test_url, timeout_seconds=10.0)
            if payload:
                return record(ProviderHealthTestStatus.SUCCESS)
            return record(
                ProviderHealthTestStatus.UNAVAILABLE,
                error_type="EmptyPayload",
                error_message="yt-dlp 返回空结果",
            )

        if item["key"] == "gallery_dl":
            if not env_runtime_bool("GALLERYDL_PROVIDER_ENABLED", False):
                return record(
                    ProviderHealthTestStatus.UNAVAILABLE,
                    error_type="CapabilityDisabled",
                    error_message="GALLERYDL_PROVIDER_ENABLED 未启用",
                )
            health = GalleryDlGenericProvider().health(force_version_probe=True)
            if not health.get("ready"):
                return record(
                    ProviderHealthTestStatus.UNAVAILABLE,
                    error_type="ProviderUnavailable",
                    error_message=json.dumps(health, ensure_ascii=False),
                )
            payload = GalleryDlGenericProvider().parse(test_url, timeout_seconds=10.0)
            if payload:
                return record(ProviderHealthTestStatus.SUCCESS)
            return record(
                ProviderHealthTestStatus.UNAVAILABLE,
                error_type="EmptyPayload",
                error_message="gallery-dl 返回空结果",
            )

        if item["key"] == "third_party_tiantianjiexi":
            if not (
                env_runtime_bool("THIRD_PARTY_PROVIDER_ENABLED", False)
                and env_runtime_bool("TIANTIANJIEXI_PROVIDER_ENABLED", False)
            ):
                return record(
                    ProviderHealthTestStatus.UNAVAILABLE,
                    error_type="CapabilityDisabled",
                    error_message="THIRD_PARTY_PROVIDER_ENABLED 或 TIANTIANJIEXI_PROVIDER_ENABLED 未启用",
                )
            result = build_tiantianjiexi_third_party_parse_result(
                test_url,
                timeout_seconds=10.0,
            )
            validation_error = provider_asset_manifest_validation_error(
                result,
                {"tiantianjiexi_generic"},
                require_video=False,
            )
            if not validation_error:
                return record(ProviderHealthTestStatus.SUCCESS)
            return record(
                ProviderHealthTestStatus.UNAVAILABLE,
                error_type="InvalidParseResult",
                error_message=validation_error,
            )

        return record(
            ProviderHealthTestStatus.FAILED,
            error_type="UnknownTest",
            error_message=f"Unknown test key: {item['key']}",
        )
    except Exception as error:
        return record(
            ProviderHealthTestStatus.FAILED,
            error_type=type(error).__name__,
            error_message=str(error),
        )


def build_provider_health_test_cards(request):
    runtime_setting = ProviderRuntimeSetting.load()
    test_setting = ProviderHealthTestSetting.load()
    last_runs = {}
    for run in ProviderHealthTestRun.objects.order_by("-created_at")[:200]:
        last_runs.setdefault(run.test_key, run)
    cards = []
    for item in PROVIDER_HEALTH_TESTS:
        runtime_info = provider_test_runtime_info(item, request, runtime_setting)
        test_url = provider_test_url_for(item, test_setting)
        cards.append(
            {
                **item,
                **runtime_info,
                "url_configured": bool(test_url) or not item.get("url_field"),
                "test_url_host": provider_test_url_host(test_url),
                "last_run": last_runs.get(item["key"]),
            }
        )
    return cards


def provider_health_tests_is_ajax(request):
    return request.headers.get("x-requested-with") == "XMLHttpRequest"


def provider_health_tests_context(request):
    readiness = get_runtime_readiness()
    runtime_setting = ProviderRuntimeSetting.load()
    test_setting = ProviderHealthTestSetting.load()
    rows, health = build_provider_dashboard_rows(runtime_setting, request)
    download_setting = DownloadRuntimeSetting.load()
    recent_runs_page, recent_runs_querystring = paginate_admin_queryset(
        request,
        ProviderHealthTestRun.objects.all(),
    )
    return {
        "title": "Provider 健康测试",
        "request_id": str(getattr(request, "request_id", "") or ""),
        "admin_time_zone": getattr(admin_display_timezone(), "key", ADMIN_DISPLAY_TIME_ZONE),
        "readiness": readiness,
        "provider_rows": rows,
        "provider_health": health,
        "download_setting": download_setting,
        "download_summary": download_runtime_summary(download_setting),
        "test_cards": build_provider_health_test_cards(request),
        "test_setting": test_setting,
        "can_manage_test_urls": provider_health_tests_manage_config_allowed(request),
        "recent_runs": recent_runs_page,
        "recent_runs_page": recent_runs_page,
        "recent_runs_querystring": recent_runs_querystring,
        "can_execute_tests": provider_health_tests_execute_allowed(request),
    }


def provider_health_tests_render(request):
    return render(request, "admin/provider_health_tests.html", provider_health_tests_context(request))


@csrf_protect
def provider_health_tests_view(request):
    if request.method == "POST":
        if request.POST.get("action") == "save_test_urls":
            if not provider_health_tests_manage_config_allowed(request):
                return JsonResponse({"detail": "Not found."}, status=404)
            _setting, invalid_labels = update_provider_health_test_setting(request)
            if invalid_labels:
                messages.warning(
                    request,
                    f"Provider 测试 URL 已保存；以下输入不是有效 http/https URL，已清空：{'、'.join(invalid_labels)}。",
                )
            else:
                messages.info(request, "Provider 测试 URL 已保存。")
            if provider_health_tests_is_ajax(request):
                return provider_health_tests_render(request)
            return redirect("provider_health_tests")

        if not provider_health_tests_execute_allowed(request):
            return JsonResponse({"detail": "Not found."}, status=404)

        if request.POST.get("action") == "run_all_tests":
            runs = [run_provider_health_test(request, item) for item in PROVIDER_HEALTH_TESTS]
            status_counts = {}
            for run in runs:
                status_counts[run.status] = status_counts.get(run.status, 0) + 1
            summary = "，".join(f"{status} {count}" for status, count in sorted(status_counts.items()))
            messages.info(request, f"Provider 一键测试完成：{summary}")
            if provider_health_tests_is_ajax(request):
                return provider_health_tests_render(request)
            return redirect("provider_health_tests")

        item = provider_health_test_by_key(request.POST.get("test_key"))
        if item is None:
            return JsonResponse({"detail": "Unknown test."}, status=400)
        run = run_provider_health_test(request, item)
        messages.info(
            request,
            f"{item['name']} 测试完成：{run.get_status_display()}",
        )
        if provider_health_tests_is_ajax(request):
            return provider_health_tests_render(request)
        return redirect("provider_health_tests")

    if not provider_health_tests_view_allowed(request):
        return JsonResponse({"detail": "Not found."}, status=404)

    return provider_health_tests_render(request)


def top_values(queryset, field_name, limit=10):
    return list(
        queryset.exclude(**{field_name: ""})
        .values(field_name)
        .annotate(count=Count("id"))
        .order_by("-count", field_name)[:limit]
    )


def percent(numerator, denominator):
    denominator = int(denominator or 0)
    if denominator <= 0:
        return 0
    return round((int(numerator or 0) / denominator) * 100, 1)


@require_GET
def ops_dashboard_view(request):
    if not admin_ops_required(request):
        return JsonResponse({"detail": "Not found."}, status=404)

    now = timezone.now()
    display_tz = admin_display_timezone()
    today_start = timezone.localtime(now, display_tz).replace(hour=0, minute=0, second=0, microsecond=0)
    recent_24h = now - timezone.timedelta(hours=24)
    recent_7d = now - timezone.timedelta(days=7)

    today_parse = ParseRequestLog.objects.filter(created_at__gte=today_start)
    recent_parse = ParseRequestLog.objects.filter(created_at__gte=recent_24h)
    today_download = DownloadRequestLog.objects.filter(created_at__gte=today_start)
    recent_download = DownloadRequestLog.objects.filter(created_at__gte=recent_24h)
    seven_day_parse = ParseRequestLog.objects.filter(created_at__gte=recent_7d)

    today_parse_count = today_parse.count()
    today_parse_success = today_parse.filter(status=ObservabilityStatus.SUCCESS).count()
    recent_parse_count = recent_parse.count()
    recent_provider_success = recent_parse.filter(
        status=ObservabilityStatus.SUCCESS,
    ).exclude(selected_provider="").count()
    recent_provider_total = recent_parse.exclude(selected_provider="").count()

    hourly_parse = list(
        recent_parse.annotate(hour=TruncHour("created_at", tzinfo=display_tz))
        .values("hour")
        .annotate(count=Count("id"), avg_duration_ms=Avg("duration_ms"))
        .order_by("hour")
    )
    for row in hourly_parse:
        row["hour_display"] = format_admin_datetime(row["hour"])[:13] + ":00"

    context = {
        "title": "运营看板",
        "admin_time_zone": getattr(display_tz, "key", ADMIN_DISPLAY_TIME_ZONE),
        "cards": [
            {"label": "今日解析次数", "value": today_parse_count},
            {"label": "今日成功率", "value": f"{percent(today_parse_success, today_parse_count)}%"},
            {"label": "今日失败数", "value": today_parse.exclude(status=ObservabilityStatus.SUCCESS).count()},
            {"label": "今日下载次数", "value": today_download.count()},
            {
                "label": "今日下载估算流量",
                "value": format_file_size(today_download.aggregate(total=Sum("estimated_bytes"))["total"] or 0),
            },
            {
                "label": "今日下载实际流量",
                "value": format_file_size(today_download.aggregate(total=Sum("actual_bytes"))["total"] or 0),
            },
            {
                "label": "24h 平均解析耗时",
                "value": f"{int(recent_parse.aggregate(avg=Avg('duration_ms'))['avg'] or 0)} ms",
            },
            {
                "label": "24h Provider 成功率",
                "value": f"{percent(recent_provider_success, recent_provider_total)}%",
            },
        ],
        "failure_top": top_values(
            recent_parse.exclude(status=ObservabilityStatus.SUCCESS),
            "error_code",
        ),
        "platform_top": top_values(recent_parse, "source_platform"),
        "provider_top": top_values(recent_parse, "selected_provider"),
        "download_type_top": top_values(recent_download, "download_type"),
        "hourly_parse": hourly_parse,
        "seven_day_parse_count": seven_day_parse.count(),
        "recent_audit_logs": [
            {
                "actor_username": item.actor_username,
                "action": item.action,
                "created_at": format_admin_datetime(item.created_at),
            }
            for item in ProviderAdminAuditLog.objects.all()[:10]
        ],
    }
    return render(request, "admin/ops_dashboard.html", context)


@require_GET
def observability_v2_view(request):
    if not observability_v2_required(request):
        return JsonResponse({"detail": "Not found."}, status=404)
    context = build_observability_v2_context(
        window_hours=request.GET.get("window") or 24,
    )
    return render(request, "admin/observability_v2.html", context)


@require_GET
def feedback_loop_view(request):
    if not feedback_loop_required(request):
        return JsonResponse({"detail": "Not found."}, status=404)
    context = build_feedback_loop_context(
        window_hours=request.GET.get("window") or 24,
    )
    return render(request, "admin/feedback_loop.html", context)


@csrf_protect
def policy_control_view(request):
    if not policy_control_view_allowed(request):
        return JsonResponse({"detail": "Not found."}, status=404)

    if request.method == "POST":
        if not policy_control_change_allowed(request):
            return JsonResponse({"detail": "Not found."}, status=404)
        action = str(request.POST.get("action") or "").strip()
        proposal_id = request.POST.get("proposal_id")
        actor_username = str(getattr(request.user, "username", "") or "")
        if action == "generate":
            result = create_controlled_policy_proposals(
                window_hours=clamp_int(request.POST.get("window"), minimum=1, maximum=168, default=24),
                actor_username=actor_username,
            )
            messages.success(
                request,
                f"Policy Control 已生成 {result['proposal_count']} 条受控建议。",
            )
        elif action in {"approve", "reject", "rollback"}:
            try:
                proposal = PolicyOptimizationProposal.objects.get(pk=proposal_id)
            except (PolicyOptimizationProposal.DoesNotExist, TypeError, ValueError):
                messages.error(request, "Policy 优化记录不存在。")
                return redirect("policy_control")
            if action == "approve":
                result = approve_policy_change(proposal, actor_username=actor_username)
                messages.success(request, f"审批结果：{result['reason']}")
            elif action == "reject":
                result = reject_policy_change(proposal, actor_username=actor_username)
                messages.success(request, f"拒绝结果：{result['reason']}")
            else:
                result = rollback_policy_change(proposal, actor_username=actor_username)
                messages.success(request, f"回滚结果：{result['reason']}")
        return redirect("policy_control")

    window_hours = clamp_int(request.GET.get("window"), minimum=1, maximum=168, default=24)
    preview = build_policy_control_preview(window_hours=window_hours)
    proposals = PolicyOptimizationProposal.objects.all()
    context = {
        "title": "Policy Control",
        "window_hours": int(preview["window_hours"]),
        "preview": preview,
        "control": preview["control"],
        "can_change_policy_control": policy_control_change_allowed(request),
        "pending_approvals": proposals.filter(status=PolicyOptimizationStatus.NEEDS_APPROVAL)[:20],
        "approved_changes": proposals.filter(status__in=[
            PolicyOptimizationStatus.AUTO_APPLIED,
            PolicyOptimizationStatus.APPROVED,
            PolicyOptimizationStatus.APPLIED,
        ])[:20],
        "rejected_changes": proposals.filter(status=PolicyOptimizationStatus.REJECTED)[:20],
        "rolled_back_changes": proposals.filter(status=PolicyOptimizationStatus.ROLLED_BACK)[:20],
        "recent_proposals": proposals[:50],
        "status_counts": {
            "pending": proposals.filter(status=PolicyOptimizationStatus.NEEDS_APPROVAL).count(),
            "approved": proposals.filter(status__in=[
                PolicyOptimizationStatus.AUTO_APPLIED,
                PolicyOptimizationStatus.APPROVED,
                PolicyOptimizationStatus.APPLIED,
            ]).count(),
            "rejected": proposals.filter(status=PolicyOptimizationStatus.REJECTED).count(),
            "rolled_back": proposals.filter(status=PolicyOptimizationStatus.ROLLED_BACK).count(),
        },
    }
    return render(request, "admin/policy_control.html", context)


@csrf_protect
def control_convergence_view(request):
    if not control_convergence_view_allowed(request):
        return JsonResponse({"detail": "Not found."}, status=404)
    window_hours = clamp_int(request.GET.get("window"), minimum=1, maximum=168, default=24)
    return render(
        request,
        "admin/control_convergence.html",
        build_control_convergence_context(window_hours=window_hours),
    )


@csrf_protect
def stability_v6_view(request):
    if not stability_v6_view_allowed(request):
        return JsonResponse({"detail": "Not found."}, status=404)
    window_hours = clamp_int(request.GET.get("window"), minimum=1, maximum=168, default=24)
    return render(
        request,
        "admin/stability_v6.html",
        build_stability_v6_context(window_hours=window_hours),
    )


@csrf_protect
def autonomous_evolution_view(request):
    if not autonomous_evolution_view_allowed(request):
        return JsonResponse({"detail": "Not found."}, status=404)
    window_hours = clamp_int(request.GET.get("window"), minimum=1, maximum=168, default=24)
    return render(
        request,
        "admin/autonomous_evolution.html",
        build_autonomous_evolution_context(window_hours=window_hours),
    )


@csrf_protect
def governance_v8_view(request):
    if not governance_v8_view_allowed(request):
        return JsonResponse({"detail": "Not found."}, status=404)
    window_hours = clamp_int(request.GET.get("window"), minimum=1, maximum=168, default=24)
    return render(
        request,
        "admin/governance_v8.html",
        build_governance_v8_context(window_hours=window_hours),
    )


def filtered_log_queryset(model, request, filter_fields):
    queryset = model.objects.all()
    for field in filter_fields:
        value = str(request.GET.get(field, "") or "").strip()
        if value:
            queryset = queryset.filter(**{field: value})
    search = str(request.GET.get("q", "") or "").strip()
    if search and model is ParseRequestLog:
        queryset = queryset.filter(
            Q(request_id__icontains=search)
            | Q(query_host__icontains=search)
            | Q(error_type__icontains=search)
        )
    elif search and model is DownloadRequestLog:
        queryset = queryset.filter(
            Q(request_id__icontains=search)
            | Q(provider__icontains=search)
            | Q(error_type__icontains=search)
        )
    elif search and model is ProviderAdminAuditLog:
        queryset = queryset.filter(
            Q(request_id__icontains=search)
            | Q(actor_username__icontains=search)
            | Q(action__icontains=search)
        )
    return queryset


def filter_values_for_request(request, filter_fields):
    return {field: str(request.GET.get(field, "") or "").strip() for field in filter_fields}


def logs_center_required(request):
    return observability_access_allowed(
        request,
        "xhs_download.view_parserequestlog",
        "xhs_download.view_downloadrequestlog",
        "xhs_download.view_provideradminauditlog",
        "xhs_download.view_providerhealthtestrun",
        "xhs_download.view_selfhealingevent",
        "xhs_download.view_routingdecisionlog",
    )


def policy_center_required(request):
    return observability_access_allowed(
        request,
        "xhs_download.view_policylearningrun",
        "xhs_download.view_policycontrolrecord",
        "xhs_download.view_policyoptimizationproposal",
        "xhs_download.view_downloadrequestlog",
        "xhs_download.view_routingdecisionlog",
    )


def safe_recent_count(model, since, **filters):
    try:
        return model.objects.filter(created_at__gte=since, **filters).count()
    except Exception:
        logger.exception("admin_center_count_failed", extra={"model": getattr(model, "__name__", "")})
        return None


def center_count_label(value):
    return "暂无数据" if value is None else str(value)


@require_GET
def logs_center_view(request):
    if not logs_center_required(request):
        return JsonResponse({"detail": "Not found."}, status=404)
    since = timezone.now() - timezone.timedelta(hours=24)
    cards = (
        {
            "title": "解析日志",
            "description": "查看解析入口、平台识别、Provider 选择和失败原因。",
            "url_name": "parse_logs",
            "count": safe_recent_count(ParseRequestLog, since),
            "warning_count": safe_recent_count(ParseRequestLog, since, status=ObservabilityStatus.FAILED),
            "warning_label": "失败",
        },
        {
            "title": "下载日志",
            "description": "查看下载路由、服务端流量、失败类型和恢复情况。",
            "url_name": "download_logs",
            "count": safe_recent_count(DownloadRequestLog, since),
            "warning_count": safe_recent_count(DownloadRequestLog, since, status=ObservabilityStatus.FAILED),
            "warning_label": "失败",
        },
        {
            "title": "Provider 审计日志",
            "description": "查看 Provider runtime 设置变更和后台操作审计。",
            "url_name": "provider_audit_logs",
            "count": safe_recent_count(ProviderAdminAuditLog, since),
            "warning_count": None,
            "warning_label": "",
        },
        {
            "title": "Provider 健康测试记录",
            "description": "查看后台手动健康测试结果和 Provider 可用性诊断。",
            "url_name": "provider_health_tests",
            "count": safe_recent_count(ProviderHealthTestRun, since),
            "warning_count": safe_recent_count(ProviderHealthTestRun, since, status=ProviderHealthTestStatus.FAILED),
            "warning_label": "失败",
        },
        {
            "title": "Self-Healing Events",
            "description": "查看下载失败恢复、Provider fallback 和自愈动作记录。",
            "url_name": "self_healing_run",
            "count": safe_recent_count(SelfHealingEvent, since),
            "warning_count": safe_recent_count(SelfHealingEvent, since, healed=False),
            "warning_label": "未恢复",
        },
        {
            "title": "Observability v2",
            "description": "进入观察面板，关联 request_id、路由和关键运行信号。",
            "url_name": "observability_v2",
            "count": safe_recent_count(RoutingDecisionLog, since),
            "warning_count": safe_recent_count(RoutingDecisionLog, since, success=False),
            "warning_label": "失败路由",
        },
    )
    return render(
        request,
        "admin/logs_center.html",
        {
            "title": "日志中心",
            "cards": [
                {
                    **card,
                    "count_label": center_count_label(card["count"]),
                    "warning_count_label": center_count_label(card["warning_count"]),
                    "url": reverse(card["url_name"]),
                }
                for card in cards
            ],
        },
    )


@require_GET
def policy_center_view(request):
    if not policy_center_required(request):
        return JsonResponse({"detail": "Not found."}, status=404)
    since = timezone.now() - timezone.timedelta(hours=24)
    cards = (
        {
            "title": "Policy Engine",
            "description": "查看策略决策、shadow learning 和当前策略运行态。",
            "url_name": "policy_engine",
            "count": safe_recent_count(PolicyLearningRun, since),
        },
        {
            "title": "Feedback Loop",
            "description": "查看 Observation → Feedback → Insight → Suggestion 的闭环入口。",
            "url_name": "feedback_loop",
            "count": safe_recent_count(DownloadRequestLog, since),
        },
        {
            "title": "Policy Control",
            "description": "查看策略审批、风险控制和受控执行记录。",
            "url_name": "policy_control",
            "count": safe_recent_count(PolicyControlRecord, since),
        },
        {
            "title": "Control Convergence",
            "description": "查看控制面收敛和策略一致性入口。",
            "url_name": "control_convergence",
            "count": safe_recent_count(PolicyControlRecord, since),
        },
        {
            "title": "Stability v6",
            "description": "查看稳定性观察入口，低频诊断集中放在这里。",
            "url_name": "stability_v6",
            "count": safe_recent_count(RoutingDecisionLog, since),
        },
        {
            "title": "Autonomous Evolution",
            "description": "查看自治演进提案入口，不在中心页执行演进计算。",
            "url_name": "autonomous_evolution",
            "count": safe_recent_count(PolicyOptimizationProposal, since),
        },
        {
            "title": "Governance v8",
            "description": "查看治理判断入口，不在中心页执行 Governance 重计算。",
            "url_name": "governance_v8",
            "count": safe_recent_count(PolicyOptimizationProposal, since),
        },
        {
            "title": "Rollout Guard",
            "description": "查看放量守卫和公开灰度相关入口。",
            "url_name": "rollout_guard",
            "count": safe_recent_count(ProviderAdminAuditLog, since),
        },
    )
    return render(
        request,
        "admin/policy_center.html",
        {
            "title": "策略中心",
            "cards": [
                {
                    **card,
                    "count_label": center_count_label(card["count"]),
                    "url": reverse(card["url_name"]),
                }
                for card in cards
            ],
        },
    )


@require_GET
def parse_logs_view(request):
    if not observability_access_allowed(request, "xhs_download.view_parserequestlog"):
        return JsonResponse({"detail": "Not found."}, status=404)
    filter_fields = ("status", "source_platform", "selected_provider", "is_staff", "error_type")
    queryset = filtered_log_queryset(ParseRequestLog, request, filter_fields)
    page_obj, page_querystring = paginate_admin_queryset(request, queryset)
    return render(
        request,
        "admin/observability_log_list.html",
        {
            "title": "解析日志",
            "nav_title": "解析日志",
            "search_placeholder": "搜索 request_id / host / error_type",
            "filter_fields": filter_fields,
            "filter_values": filter_values_for_request(request, filter_fields),
            "page_obj": page_obj,
            "page_querystring": page_querystring,
            "headers": (
                "created_at",
                "status",
                "query_host",
                "source_platform",
                "selected_provider",
                "duration_ms",
                "image_count",
                "video_count",
                "error_type",
                "error_message_short",
                "request_id",
            ),
            "rows": [
                {
                    "created_at": format_admin_datetime(item.created_at),
                    "status": item.status,
                    "query_host": item.query_host,
                    "source_platform": item.source_platform,
                    "selected_provider": item.selected_provider,
                    "duration_ms": item.duration_ms,
                    "image_count": item.image_count,
                    "video_count": item.video_count,
                    "error_type": item.error_type,
                    "error_message_short": item.error_message_short,
                    "request_id": item.request_id,
                }
                for item in page_obj
            ],
        },
    )


@require_GET
def download_logs_view(request):
    if not observability_access_allowed(request, "xhs_download.view_downloadrequestlog"):
        return JsonResponse({"detail": "Not found."}, status=404)
    filter_fields = ("status", "download_type", "route_type", "provider", "source_platform", "error_type")
    queryset = filtered_log_queryset(DownloadRequestLog, request, filter_fields)
    page_obj, page_querystring = paginate_admin_queryset(request, queryset)
    return render(
        request,
        "admin/observability_log_list.html",
        {
            "title": "下载日志",
            "nav_title": "下载日志",
            "search_placeholder": "搜索 request_id / provider / error_type",
            "filter_fields": filter_fields,
            "filter_values": filter_values_for_request(request, filter_fields),
            "page_obj": page_obj,
            "page_querystring": page_querystring,
            "headers": (
                "created_at",
                "status",
                "download_type",
                "route_type",
                "item_count",
                "actual_bytes",
                "provider",
                "source_platform",
                "duration_ms",
                "error_type",
                "request_id",
            ),
            "rows": [
                {
                    "created_at": format_admin_datetime(item.created_at),
                    "status": item.status,
                    "download_type": item.download_type,
                    "route_type": item.route_type,
                    "item_count": item.item_count,
                    "actual_bytes": format_file_size(item.actual_bytes),
                    "provider": item.provider,
                    "source_platform": item.source_platform,
                    "duration_ms": item.duration_ms,
                    "error_type": item.error_type,
                    "request_id": item.request_id,
                }
                for item in page_obj
            ],
        },
    )


@require_GET
def provider_audit_logs_view(request):
    if not observability_access_allowed(request, "xhs_download.view_provideradminauditlog"):
        return JsonResponse({"detail": "Not found."}, status=404)
    filter_fields = ("action", "actor_username")
    queryset = filtered_log_queryset(ProviderAdminAuditLog, request, filter_fields)
    page_obj, page_querystring = paginate_admin_queryset(request, queryset)
    return render(
        request,
        "admin/observability_log_list.html",
        {
            "title": "Provider 审计",
            "nav_title": "Provider 审计",
            "search_placeholder": "搜索 request_id / actor / action",
            "filter_fields": filter_fields,
            "filter_values": filter_values_for_request(request, filter_fields),
            "page_obj": page_obj,
            "page_querystring": page_querystring,
            "headers": ("created_at", "actor_username", "action", "changed_fields", "request_id"),
            "rows": [
                {
                    "created_at": format_admin_datetime(item.created_at),
                    "actor_username": item.actor_username,
                    "action": item.action,
                    "changed_fields": ", ".join(item.changed_fields or ()),
                    "request_id": item.request_id,
                }
                for item in page_obj
            ],
        },
    )


def limit_error_response(message, retry_after=0, error_code="RATE_LIMITED"):
    response = JsonResponse({"status": "error", "message": message}, status=429)
    if retry_after:
        response["Retry-After"] = str(retry_after)
    response["X-Error-Code"] = error_code
    return response


def download_guard_error_response(message, *, status=403, error_code):
    response = JsonResponse({"status": "error", "message": message}, status=status)
    response["X-Error-Code"] = error_code
    return response


def get_download_runtime_setting():
    return DownloadRuntimeSetting.load()


def safe_download_size(value):
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def is_staff_request(request):
    user = getattr(request, "user", None)
    return bool(getattr(user, "is_active", False) and getattr(user, "is_staff", False))


def download_item_type(item):
    if not isinstance(item, dict):
        return ""
    return str(item.get("type") or item.get("media_type") or "").lower().strip()


def download_item_is_video(item):
    return download_item_type(item) == "video"


def estimated_media_items_bytes(media_items):
    return sum(
        safe_download_size(item.get("size_bytes"))
        for item in media_items or ()
        if isinstance(item, dict)
    )


def staff_can_bypass_single_download_limits(request, runtime_setting):
    return bool(
        is_staff_request(request)
        and getattr(runtime_setting, "staff_large_download_exempt", False)
    )


def check_download_cost_preflight(request, media_items, runtime_setting):
    has_video = any(download_item_is_video(item) for item in media_items or ())
    if has_video and not is_staff_request(request) and not runtime_setting.visitor_video_download_enabled:
        return (
            "VISITOR_VIDEO_DOWNLOAD_DISABLED",
            "当前环境暂不允许访客下载视频资源。",
            403,
        )

    if not staff_can_bypass_single_download_limits(request, runtime_setting):
        single_video_limit = safe_download_size(runtime_setting.single_video_bytes_limit)
        if single_video_limit > 0:
            for item in media_items or ():
                if not download_item_is_video(item):
                    continue
                size_bytes = safe_download_size(item.get("size_bytes") if isinstance(item, dict) else 0)
                if size_bytes and size_bytes > single_video_limit:
                    return (
                        "SINGLE_VIDEO_SIZE_LIMIT",
                        f"视频资源过大，当前单个视频限制为 {format_file_size(single_video_limit)}。",
                        413,
                    )

        single_archive_limit = safe_download_size(runtime_setting.single_archive_bytes_limit)
        estimated_bytes = estimated_media_items_bytes(media_items)
        if single_archive_limit > 0 and estimated_bytes > single_archive_limit:
            return (
                "SINGLE_ARCHIVE_SIZE_LIMIT",
                f"本次下载资源过大，当前单次打包限制为 {format_file_size(single_archive_limit)}。",
                413,
            )

    return None


def check_single_archive_actual_size(request, runtime_setting, archive_size):
    single_archive_limit = safe_download_size(runtime_setting.single_archive_bytes_limit)
    if (
        single_archive_limit > 0
        and archive_size > single_archive_limit
        and not staff_can_bypass_single_download_limits(request, runtime_setting)
    ):
        return (
            "SINGLE_ARCHIVE_SIZE_LIMIT",
            f"打包后的文件过大，当前单次打包限制为 {format_file_size(single_archive_limit)}。",
            413,
        )
    return None


def check_download_request_limits(request):
    identity = get_client_identity(request)
    runtime_setting = get_download_runtime_setting()
    checks = (
        (
            lambda: consume_fixed_window(
                "download-minute-ip",
                identity,
                runtime_setting.rate_limit_per_minute,
                60,
            ),
            "DOWNLOAD_MINUTE_RATE_LIMIT",
            "下载请求过于频繁，请稍后再试。",
        ),
        (
            lambda: consume_daily(
                "download-requests-ip",
                identity,
                runtime_setting.daily_request_limit_per_ip,
            ),
            "DOWNLOAD_DAILY_REQUEST_LIMIT_IP",
            "今日下载请求次数已达到限制，请明天再试。",
        ),
        (
            lambda: consume_daily(
                "download-requests-global",
                "all",
                runtime_setting.global_daily_request_limit,
            ),
            "DOWNLOAD_DAILY_REQUEST_LIMIT_GLOBAL",
            "今日全站下载请求次数已达到限制，请稍后再试。",
        ),
        (
            lambda: has_daily_capacity(
                "download-bytes-ip",
                identity,
                runtime_setting.daily_bytes_limit_per_ip,
            ),
            "DOWNLOAD_DAILY_BYTES_LIMIT_IP",
            "今日下载流量已达到限制，请明天再试。",
        ),
        (
            lambda: has_daily_capacity(
                "download-bytes-global",
                "all",
                runtime_setting.global_daily_bytes_limit,
            ),
            "DOWNLOAD_DAILY_BYTES_LIMIT_GLOBAL",
            "今日全站下载流量已达到限制，请稍后再试。",
        ),
    )
    for check, error_code, message in checks:
        result = check()
        if not result.allowed:
            return identity, limit_error_response(
                message,
                result.retry_after,
                error_code=error_code,
            )
    return identity, None


def record_download_archive_bytes(identity, archive_file, archive_size=None):
    if archive_size is None:
        archive_size = file_size_for_observability(archive_file)
    if archive_size <= 0:
        return True, ""

    runtime_setting = get_download_runtime_setting()
    per_ip = consume_daily(
        "download-bytes-ip",
        identity,
        runtime_setting.daily_bytes_limit_per_ip,
        amount=archive_size,
    )
    if not per_ip.allowed:
        return False, "DOWNLOAD_DAILY_BYTES_LIMIT_IP"
    global_result = consume_daily(
        "download-bytes-global",
        "all",
        runtime_setting.global_daily_bytes_limit,
        amount=archive_size,
    )
    if not global_result.allowed:
        return False, "DOWNLOAD_DAILY_BYTES_LIMIT_GLOBAL"
    return True, ""


def file_size_for_observability(file_obj):
    try:
        name = getattr(file_obj, "name", "")
        if name:
            return Path(name).stat().st_size
    except (OSError, TypeError, ValueError):
        pass
    try:
        current = file_obj.tell()
        file_obj.seek(0, 2)
        size = file_obj.tell()
        file_obj.seek(current)
        return size
    except Exception:
        return 0


def log_download_request_event(
    request,
    *,
    download_type,
    media_items=None,
    status=ObservabilityStatus.SUCCESS,
    started_at=None,
    actual_bytes=0,
    error_code="",
    error=None,
    error_message_short="",
    route_info=None,
):
    duration_ms = (time.monotonic() - started_at) * 1000 if started_at else 0
    route_info = route_info or {}
    policy_shadow = route_info.get("policy_decision") or {}
    if media_items:
        if not policy_shadow:
            try:
                policy_shadow = evaluate_policy_shadow(
                    request,
                    media_items,
                    route_info=route_info,
                    requested_mode=download_type,
                    provider_hint=route_info.get("provider", ""),
                )
            except Exception:
                logger.exception("policy_shadow_evaluation_failed")
                policy_shadow = {}
    policy_scores = policy_shadow.get("policy_scores") or {}
    recovery = route_info.get("recovery") or {}
    healing = route_info.get("healing") or {}
    record_download_request_log(
        request,
        download_type=download_type,
        media_items=media_items or (),
        status=status,
        estimated_bytes=route_info.get("estimated_bytes"),
        actual_bytes=actual_bytes,
        route_type=route_info.get("route_type", ""),
        route_reason=route_info.get("route_reason", ""),
        server_egress_estimated_bytes=route_info.get("server_egress_estimated_bytes", 0),
        browser_egress_estimated_bytes=route_info.get("browser_egress_estimated_bytes", 0),
        duration_ms=duration_ms,
        error_code=error_code,
        error_type=type(error).__name__ if error is not None else "",
        error_message_short=(
            sanitize_observability_text(error_message_short)
            if error_message_short
            else sanitize_observability_text(error)
            if error is not None
            else ""
        ),
        policy_cost_score=policy_scores.get("cost", 0),
        policy_provider_score=policy_scores.get("provider", 0),
        policy_routing_score=policy_scores.get("routing", 0),
        final_policy_route=policy_shadow.get("route", ""),
        policy_confidence=policy_shadow.get("confidence", 0),
        policy_active=bool(policy_shadow.get("active")),
        policy_decision_source=policy_shadow.get("decision_source", ""),
        policy_estimated_savings_bytes=policy_shadow.get("estimated_savings_bytes", 0),
        recovery_count=recovery.get("recovery_count", 0),
        fallback_count=recovery.get("fallback_count", 0),
        recovery_action=recovery.get("recovery_action", ""),
        recovery_reason=recovery.get("recovery_reason", ""),
        fallback_from_route=recovery.get("fallback_from_route", ""),
        fallback_to_route=recovery.get("fallback_to_route", ""),
        failure_type=recovery.get("failure_type", ""),
        auto_recovery_count=healing.get("auto_recovery_count", 0),
        provider_auto_switch_count=healing.get("provider_auto_switch_count", 0),
        failure_to_recovery_ms=healing.get("failure_to_recovery_ms", 0),
        healing_action=healing.get("healing_action", ""),
        healing_risk=healing.get("healing_risk", ""),
        healing_confidence=healing.get("healing_confidence", 0),
    )


def blocked_download_route_info(media_items, reason, error_code=""):
    estimated_bytes = estimated_media_items_bytes(media_items)
    return {
        "route_type": ROUTE_BLOCKED,
        "route_reason": sanitize_observability_text(reason, limit=200),
        "download_strategy": "blocked",
        "estimated_bytes": estimated_bytes,
        "server_egress_estimated_bytes": 0,
        "browser_egress_estimated_bytes": 0,
        "items": [],
        "warnings": [],
        "error_code": error_code,
    }


def zip_download_route_info(request, media_items, reason="默认兼容旧 ZIP 下载流程", *, record_healing=True):
    route_info = safe_routing_decision(
        request,
        media_items,
        lambda: decide_download_route(
            request,
            media_items,
            requested_mode="zip",
        ),
        reason="ZIP 兼容路由异常，已降级为 ZIP 下载",
    )
    if record_healing and route_info.get("recovery"):
        route_info = attach_self_healing_event(
            request,
            route_info,
            media_items,
            trigger="routing_exception",
            error=RuntimeError(route_info.get("route_reason", "")),
        )
    if route_info.get("route_type") == ROUTE_BLOCKED:
        route_info["route_reason"] = sanitize_observability_text(reason, limit=200)
        return route_info
    route_info["route_type"] = ROUTE_ZIP
    route_info["route_reason"] = sanitize_observability_text(reason, limit=200)
    route_info["download_strategy"] = "server_zip"
    route_info["server_egress_estimated_bytes"] = route_info.get("estimated_bytes", 0)
    route_info["browser_egress_estimated_bytes"] = 0
    return route_info


def attach_self_healing_event(
    request,
    route_info,
    media_items,
    *,
    trigger,
    error=None,
    error_code="",
    provider="",
    depth=0,
    force_healed=None,
):
    route_info = dict(route_info or {})
    provider = provider or route_info.get("provider", "")
    if not provider:
        for item in media_items or ():
            if isinstance(item, dict) and item.get("provider"):
                provider = item.get("provider")
                break
    context = {
        "item_count": len([item for item in media_items or () if isinstance(item, dict)]),
        "route_type": route_info.get("route_type", ""),
        "download_strategy": route_info.get("download_strategy", ""),
        "error_code": error_code or route_info.get("error_code", ""),
    }
    recovery = route_info.get("recovery") or {}
    failure_type = recovery.get("failure_type", "")
    if failure_type == FAILURE_PROVIDER_TIMEOUT and depth == 0:
        # Download recovery already reached this point after the provider path failed;
        # treat the retry budget as spent and move to the next provider.
        depth = 1
    outcome = SelfHealingEngine(request, context=context).handle_failure(
        trigger=trigger,
        route_result=route_info,
        provider=provider,
        error=error,
        error_code=error_code or route_info.get("error_code", ""),
        failure_type=failure_type,
        depth=depth,
        force_healed=force_healed,
    )
    return attach_healing_to_route_info(route_info, outcome)


def single_attachment_download_filename(item, base_filename, final_url="", content_type=""):
    raw_filename = str(item.get("filename") or "").strip()
    if raw_filename:
        stem = Path(raw_filename).stem or raw_filename
    else:
        stem = base_filename or str(item.get("type") or "media")
    media_type = str(item.get("type") or "").lower()
    if media_type == "image":
        ext = guess_image_extension(final_url or item.get("url", ""), content_type)
    else:
        ext = guess_video_extension_from_response(final_url or item.get("url", ""), content_type)
    return clean_filename(f"{stem}.{ext}")


def open_single_attachment_upstream(item, media_type, request_timeout):
    url = item.get("url")
    if not url:
        raise XhsDownloadError("资源 URL 为空")

    recovery_enabled = bool(
        media_type == "video"
        and isinstance(item, dict)
        and get_kukutool_manifest_identity(item)
    )
    provider_item = item if provider_manifest_allows_public_download(item) else None
    attempted_urls = set()
    candidates = [url]
    recovery_urls = None
    last_error = None

    while candidates or recovery_urls is not None:
        if not candidates:
            try:
                candidates.append(next(recovery_urls))
            except StopIteration:
                recovery_urls = None
                continue

        candidate_url = candidates.pop(0)
        if not candidate_url or candidate_url in attempted_urls:
            continue
        attempted_urls.add(candidate_url)

        candidate_provider_item = provider_item
        if recovery_enabled and not is_byte_cdn_url(candidate_url):
            candidate_provider_item = None

        try:
            return fetch_media_response(
                candidate_url,
                timeout=request_timeout,
                retries=1 if recovery_enabled else 3,
                provider_signed=candidate_provider_item,
            )
        except Exception as error:
            last_error = error
            if provider_item and is_recoverable_provider_download_error(error):
                invalidate_external_provider_caches(provider_item)
            if (
                recovery_enabled
                and recovery_urls is None
                and len(attempted_urls) == 1
                and is_recoverable_provider_download_error(error)
            ):
                recovery_urls = iter(iter_kukutool_download_recovery_urls(item) or ())

    raise last_error or XhsDownloadError("资源下载失败")


def build_single_attachment_response(item, base_filename):
    media_type = str(item.get("type") or "").lower() if isinstance(item, dict) else ""
    if media_type not in {"image", "video"}:
        raise XhsDownloadError("单文件下载仅支持图片或视频资源")

    max_bytes = MAX_IMAGE_BYTES if media_type == "image" else MAX_VIDEO_BYTES
    request_timeout = 20 if media_type == "image" else 60
    if provider_manifest_allows_public_download(item) and media_type == "video":
        request_timeout = max(
            request_timeout,
            runtime_float(
                "kukutool_download_timeout_seconds",
                getattr(settings, "KUKUTOOL_DOWNLOAD_TIMEOUT_SECONDS", 8.0)
                or 8.0,
                minimum=1.0,
                maximum=180.0,
            ),
        )
    upstream = open_single_attachment_upstream(item, media_type, request_timeout)
    try:
        content_type = upstream.headers.get("Content-Type", "")
        validate_response_content_type(content_type, media_type)
        content_length = parse_content_length(upstream)
        if content_length and content_length > max_bytes:
            raise XhsDownloadError("资源超过允许的大小限制")

        upstream_chunks = upstream.iter_content(chunk_size=CHUNK_SIZE)
        first_chunk = b""
        for chunk in upstream_chunks:
            if chunk:
                first_chunk = chunk
                break
        if first_chunk and response_chunk_looks_like_html(first_chunk):
            raise XhsDownloadError("资源类型异常：HTML response body")
        if len(first_chunk) > max_bytes:
            raise XhsDownloadError("资源超过允许的大小限制")

        def stream_media():
            total = 0
            try:
                if first_chunk:
                    total += len(first_chunk)
                    yield first_chunk
                for chunk in upstream_chunks:
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > max_bytes:
                        raise XhsDownloadError("资源超过允许的大小限制")
                    yield chunk
            finally:
                upstream.close()

        filename = single_attachment_download_filename(
            item,
            base_filename,
            final_url=getattr(upstream, "url", "") or item.get("url", ""),
            content_type=content_type,
        )
        response = StreamingHttpResponse(
            stream_media(),
            content_type=content_type or "application/octet-stream",
        )
        disposition = content_disposition_header(True, filename)
        if disposition:
            response["Content-Disposition"] = disposition
        if content_length:
            response["Content-Length"] = str(content_length)
        response["X-Saved-Count"] = "1"
        response["X-Failed-Count"] = "0"
        return response, content_length or estimated_media_items_bytes([item])
    except Exception:
        upstream.close()
        raise


def apply_route_v3_metadata(request, media_items, route_info, *, requested_mode="", platform_key=""):
    v3_route = decide_route_v3(
        request,
        media_items,
        context={
            "requested_mode": requested_mode,
            "platform_key": platform_key,
        },
    )
    updated = dict(route_info or {})
    updated["route_type"] = v3_route.get("route_type", updated.get("route_type", ""))
    updated["route_reason"] = v3_route.get("reason", updated.get("route_reason", ""))
    updated["download_strategy"] = v3_route.get("download_strategy", updated.get("download_strategy", ""))
    updated["server_egress_estimated_bytes"] = v3_route.get("estimated_cost", {}).get(
        "server_bytes",
        updated.get("server_egress_estimated_bytes", 0),
    )
    updated["browser_egress_estimated_bytes"] = v3_route.get("estimated_cost", {}).get(
        "browser_bytes",
        updated.get("browser_egress_estimated_bytes", 0),
    )
    updated["strategy_version"] = v3_route.get("strategy_version", "v3")
    updated["confidence"] = v3_route.get("confidence", 0)
    updated["routing_score"] = v3_route.get("routing_score", 0)
    updated["provider_score"] = v3_route.get("provider_score", 0)
    return updated


def download_items_have_invalid_provider_manifest(media_items):
    for item in media_items or ():
        if isinstance(item, dict) and item.get("provider"):
            if not provider_manifest_allows_public_download(item):
                return True
    return False


def check_preview_request_limits(request):
    identity = get_client_identity(request)
    checks = (
        lambda: consume_fixed_window(
            "preview-minute-ip",
            identity,
            settings.PREVIEW_RATE_LIMIT_PER_MINUTE,
            60,
        ),
        lambda: consume_daily(
            "preview-requests-ip",
            identity,
            settings.PREVIEW_DAILY_REQUEST_LIMIT_PER_IP,
        ),
        lambda: consume_daily(
            "preview-requests-global",
            "all",
            settings.PREVIEW_GLOBAL_DAILY_REQUEST_LIMIT,
        ),
        lambda: has_daily_capacity(
            "preview-bytes-ip",
            identity,
            settings.PREVIEW_DAILY_BYTES_LIMIT_PER_IP,
        ),
        lambda: has_daily_capacity(
            "preview-bytes-global",
            "all",
            settings.PREVIEW_GLOBAL_DAILY_BYTES_LIMIT,
        ),
    )
    for check in checks:
        result = check()
        if not result.allowed:
            return identity, result
    return identity, None


def record_preview_bytes(identity, size_bytes):
    per_ip = consume_daily(
        "preview-bytes-ip",
        identity,
        settings.PREVIEW_DAILY_BYTES_LIMIT_PER_IP,
        amount=size_bytes,
    )
    if not per_ip.allowed:
        return False
    global_result = consume_daily(
        "preview-bytes-global",
        "all",
        settings.PREVIEW_GLOBAL_DAILY_BYTES_LIMIT,
        amount=size_bytes,
    )
    return global_result.allowed


def get_platform_page_config(platform_key):
    return PLATFORM_PAGE_CONFIG.get(platform_key, PLATFORM_PAGE_CONFIG["home"])


def get_result_theme(platform_key, result_platform="", result_metadata=None):
    page_theme = get_platform_page_config(platform_key)["theme"]
    if platform_key == "home":
        return page_theme

    source_platform = (result_metadata or {}).get("source_platform") or result_platform
    return RESULT_THEME_BY_PLATFORM.get(source_platform, page_theme)


def get_platform_key_for_result(result_platform="", result_metadata=None):
    source_platform = (result_metadata or {}).get("source_platform") or result_platform
    return PLATFORM_KEY_BY_PLATFORM.get(source_platform, "")


def get_page_payload(platform_key):
    page_config = get_platform_page_config(platform_key)
    return {
        "key": platform_key,
        "theme": page_config["theme"],
        "theme_class": f"theme-{page_config['theme']}",
        "title": page_config["title"],
        "heading": page_config["heading"],
        "description": page_config["description"],
        "label": page_config["label"],
        "placeholder": page_config["placeholder"],
        "home_url": reverse(page_config["home_route"]),
        "parse_action_url": reverse(page_config["results_route"]),
        "platform_id": page_config["platform_id"],
        "display_name": page_config["display_name"],
    }


def build_asset_strategy_fields(asset, result):
    return build_download_strategy(
        asset,
        result_platform=getattr(result, "platform", ""),
        result_metadata=getattr(result, "metadata", None) or {},
    )


def build_image_item(asset, result):
    return {
        **asset.to_download_item(),
        **build_asset_strategy_fields(asset, result),
        "type": "image",
        "url": asset.url,
        "encoded_url": encode_url(asset.url),
        "encoded_preview_url": encode_url(asset.preview_url or asset.url),
        "preview_provider_query": build_provider_preview_query(
            asset,
            asset.preview_url or asset.url,
        ),
    }


def build_video_item(asset, result):
    size_bytes = get_video_size_bytes(asset)
    return {
        **asset.to_download_item(),
        **build_asset_strategy_fields(asset, result),
        "type": "video",
        "video_url": asset.url,
        "cover_url": asset.cover_url,
        "size_bytes": size_bytes,
        "size_label": format_file_size(size_bytes),
        "encoded_cover_url": encode_url(asset.cover_url) if asset.cover_url else "",
        "encoded_preview_cover_url": (
            encode_url(asset.cover_preview_url or asset.cover_url)
            if asset.cover_url
            else ""
        ),
        "cover_preview_provider_query": build_provider_preview_query(
            asset,
            asset.cover_preview_url or asset.cover_url,
            "cover",
        ),
    }


def build_result_display_context(result, *, platform_key="home"):
    result_metadata = result.metadata or {}
    return {
        "platform": result.platform,
        "platform_name": (
            result_metadata.get("source_platform_name")
            or PLATFORM_DISPLAY_NAMES.get(result.platform, result.platform or "未知平台")
        ),
        "result_theme": get_result_theme(
            platform_key,
            result.platform,
            result_metadata,
        ),
        "post_type": result.post_type,
        "base_filename": result.base_filename,
        "image_items": [build_image_item(asset, result) for asset in result.image_assets],
        "video_items": [build_video_item(asset, result) for asset in result.video_assets],
        "metadata": result_metadata,
    }


def query_uses_third_party_provider(query):
    try:
        return getattr(get_adapter_for_url(query), "platform_id", "") == "third_party"
    except XhsError:
        return False


def log_parse_request_event(
    request,
    event,
    *,
    query,
    platform_key,
    requested_ultra_hd,
    prefer_ultra_hd,
    requested_youtube_merge,
    prefer_youtube_merge,
    duration_ms,
    result=None,
    context=None,
    error=None,
    status=None,
    error_code="",
    error_message_short="",
):
    parsed = urlparse(str(query or ""))
    metadata = getattr(result, "metadata", None) or {}
    image_count = len(context.get("image_items", ())) if context else 0
    video_count = len(context.get("video_items", ())) if context else 0
    source_platform = metadata.get("source_platform", "")
    selected_provider = metadata.get("selected_provider", "")
    provider_id = ""
    if source_platform == "third_party" or selected_provider in {
        "kukutool_generic",
        "gallery_dl_generic",
        "yt_dlp_generic",
        "youtube_merge",
        "yt_dlp_merge",
    } or query_uses_third_party_provider(query):
        provider_id = PROVIDER_THIRD_PARTY
    elif requested_youtube_merge or prefer_youtube_merge:
        provider_id = PROVIDER_YOUTUBE_MERGE
    elif requested_ultra_hd or prefer_ultra_hd:
        provider_id = PROVIDER_DOUYIN_ULTRA
    access = provider_access_summary(provider_id, request) if provider_id else {}
    if status is None:
        status = (
            ObservabilityStatus.FAILED
            if error is not None or (context and context.get("error_message"))
            else ObservabilityStatus.SUCCESS
        )
    extra = {
        "request_id": getattr(request, "request_id", ""),
        "event": event,
        "page_platform": platform_key,
        "query_host": parsed.hostname or "",
        "requested_ultra_hd": bool(requested_ultra_hd),
        "prefer_ultra_hd": bool(prefer_ultra_hd),
        "requested_youtube_merge": bool(requested_youtube_merge),
        "prefer_youtube_merge": bool(prefer_youtube_merge),
        "duration_ms": int(duration_ms),
        "status": status,
        "platform": getattr(result, "platform", "") if result else "",
        "post_type": getattr(result, "post_type", "") if result else "",
        "source_platform": source_platform,
        "provider": selected_provider,
        "selected_provider": selected_provider,
        "selected_provider_duration_ms": metadata.get(
            "selected_provider_duration_ms",
            0,
        ),
        "provider_total_duration_ms": metadata.get(
            "provider_total_duration_ms",
            0,
        ),
        "provider_fallback_count": metadata.get("provider_fallback_count", 0),
        "image_count": image_count,
        "video_count": video_count,
    }
    if error is not None:
        extra["error_type"] = type(error).__name__
        extra["error"] = sanitize_observability_text(error)
    elif error_message_short:
        extra["error_type"] = error_code
        extra["error"] = sanitize_observability_text(error_message_short)
    record_parse_request_log(
        request,
        query=query,
        page_platform=platform_key,
        query_host=parsed.hostname or "",
        source_platform=source_platform,
        source_platform_name=metadata.get("source_platform_name", ""),
        result_platform=getattr(result, "platform", "") if result else "",
        post_type=getattr(result, "post_type", "") if result else "",
        selected_provider=selected_provider,
        provider_label=metadata.get("provider_label", ""),
        requested_ultra_hd=requested_ultra_hd,
        prefer_ultra_hd=prefer_ultra_hd,
        requested_youtube_merge=requested_youtube_merge,
        prefer_youtube_merge=prefer_youtube_merge,
        status=status,
        error_code=error_code,
        error_type=type(error).__name__ if error is not None else "",
        error_message_short=sanitize_observability_text(
            error_message_short or (str(error) if error is not None else "")
        ),
        duration_ms=duration_ms,
        selected_provider_duration_ms=metadata.get("selected_provider_duration_ms", 0),
        provider_total_duration_ms=metadata.get("provider_total_duration_ms", 0),
        provider_fallback_count=metadata.get("provider_fallback_count", 0),
        image_count=image_count,
        video_count=video_count,
        runtime_mode=access.get("effective_mode", ""),
        rollout_percent=access.get("rollout_percent", 0),
        rollout_bucket=access.get("rollout_bucket") or 0,
    )
    logger.info(event, extra=extra)


def search_view(request, platform_key="home"):
    return render(
        request,
        "xhs_download.html",
        get_download_settings_context(platform_key, request),
    )


def server_save_mode_is_enabled():
    return bool(settings.XHS_ENABLE_SERVER_SAVE_MODE and settings.XHS_ALLOW_SERVER_SAVE)


def get_download_settings_context(platform_key="home", request=None):
    page_config = get_platform_page_config(platform_key)
    return {
        "platform_key": platform_key,
        "theme_class": f"theme-{page_config['theme']}",
        "result_theme": page_config["theme"],
        "page_platform_id": page_config["platform_id"],
        "page_platform_name": page_config["display_name"],
        "page_title": page_config["title"],
        "page_heading": page_config["heading"],
        "page_description": page_config["description"],
        "link_label": page_config["label"],
        "link_placeholder": page_config["placeholder"],
        "page_home_url": reverse(page_config["home_route"]),
        "parse_action_url": reverse(page_config["results_route"]),
        "proxy_image_url": reverse("media_proxy_image"),
        "download_url": reverse("media_download_images_api"),
        "download_task_url": (
            reverse("media_download_task_create")
            if settings.DOWNLOAD_ASYNC_TASKS_ENABLED
            else ""
        ),
        "download_async_tasks_enabled": settings.DOWNLOAD_ASYNC_TASKS_ENABLED,
        "xhs_allow_server_save": settings.XHS_ALLOW_SERVER_SAVE,
        "xhs_show_server_save_mode": server_save_mode_is_enabled(),
        "xhs_allow_absolute_save_path": settings.XHS_ALLOW_ABSOLUTE_SAVE_PATH,
        "xhs_default_server_save_dir": settings.XHS_DEFAULT_SERVER_SAVE_DIR,
        "ultra_hd_available": ultra_hd_is_available(request),
        "youtube_merge_mode_available": (
            youtube_merge_mode_is_available(request) if request is not None else False
        ),
    }


def build_search_results_context(request, platform_key="home"):
    query = request.GET.get("q", "").strip()
    requested_ultra_hd = request_flag(request.GET.get("ultra_hd"))
    prefer_ultra_hd = requested_ultra_hd and ultra_hd_is_available(request)
    requested_youtube_merge = request_flag(request.GET.get("youtube_merge"))
    prefer_youtube_merge = (
        requested_youtube_merge and youtube_merge_mode_is_available(request)
    )

    context = {
        **get_download_settings_context(platform_key, request),
        "query": query,
        "prefer_ultra_hd": prefer_ultra_hd,
        "prefer_youtube_merge": prefer_youtube_merge,
        "platform": "",
        "platform_name": "",
        "platform_notice": "",
        "provider_notice": "",
        "post_type": "",
        "image_items": [],
        "video_items": [],
        "is_mobile_request": request_is_mobile(request),
        "error_message": None,
        "base_filename": "",
    }

    if not query:
        return context, 200

    parse_started_at = time.monotonic()

    if query_uses_third_party_provider(query):
        if not third_party_provider_is_available(request):
            access = provider_access_summary(PROVIDER_THIRD_PARTY, request)
            if access["hard_enabled"] and access["effective_mode"] in {
                ProviderAccessMode.STAFF,
                ProviderAccessMode.PUBLIC_GRAY,
            }:
                context["error_message"] = "第三方通用解析暂未向该用户开放。"
                log_parse_request_event(
                    request,
                    "media_parse_blocked",
                    query=query,
                    platform_key=platform_key,
                    requested_ultra_hd=requested_ultra_hd,
                    prefer_ultra_hd=prefer_ultra_hd,
                    requested_youtube_merge=requested_youtube_merge,
                    prefer_youtube_merge=prefer_youtube_merge,
                    duration_ms=(time.monotonic() - parse_started_at) * 1000,
                    context=context,
                    status=ObservabilityStatus.BLOCKED,
                    error_code="THIRD_PARTY_ROLLOUT_MISS",
                    error_message_short=context["error_message"],
                )
                return context, 403
            context["error_message"] = "第三方通用解析暂未开放。"
            log_parse_request_event(
                request,
                "media_parse_blocked",
                query=query,
                platform_key=platform_key,
                requested_ultra_hd=requested_ultra_hd,
                prefer_ultra_hd=prefer_ultra_hd,
                requested_youtube_merge=requested_youtube_merge,
                prefer_youtube_merge=prefer_youtube_merge,
                duration_ms=(time.monotonic() - parse_started_at) * 1000,
                context=context,
                status=ObservabilityStatus.BLOCKED,
                error_code="THIRD_PARTY_NOT_OPEN",
                error_message_short=context["error_message"],
            )
            return context, 403
        third_party_gate = third_party_public_access_summary(query, request)
        if not third_party_gate["allowed"]:
            context["error_message"] = third_party_gate["message"]
            context["provider_notice"] = third_party_gate.get("error_code", "")
            log_parse_request_event(
                request,
                "media_parse_blocked",
                query=query,
                platform_key=platform_key,
                requested_ultra_hd=requested_ultra_hd,
                prefer_ultra_hd=prefer_ultra_hd,
                requested_youtube_merge=requested_youtube_merge,
                prefer_youtube_merge=prefer_youtube_merge,
                duration_ms=(time.monotonic() - parse_started_at) * 1000,
                context=context,
                status=ObservabilityStatus.BLOCKED,
                error_code=third_party_gate.get("error_code", ""),
                error_message_short=context["error_message"],
            )
            return context, 403
        try:
            limit = consume_fixed_window(
                "third-party-provider-minute-ip",
                get_client_identity(request),
                settings.THIRD_PARTY_RATE_LIMIT_PER_MINUTE,
                60,
            )
        except Exception:
            logger.exception("Third-party provider rate-limit cache failed")
            context["error_message"] = "第三方解析保护服务暂时不可用，请稍后再试。"
            log_parse_request_event(
                request,
                "media_parse_failed",
                query=query,
                platform_key=platform_key,
                requested_ultra_hd=requested_ultra_hd,
                prefer_ultra_hd=prefer_ultra_hd,
                requested_youtube_merge=requested_youtube_merge,
                prefer_youtube_merge=prefer_youtube_merge,
                duration_ms=(time.monotonic() - parse_started_at) * 1000,
                context=context,
                status=ObservabilityStatus.FAILED,
                error_code="RATE_LIMIT_CACHE_ERROR",
                error_message_short=context["error_message"],
            )
            return context, 503
        if not limit.allowed:
            context["error_message"] = "第三方解析请求过于频繁，请稍后再试。"
            context["retry_after"] = limit.retry_after
            log_parse_request_event(
                request,
                "media_parse_rate_limited",
                query=query,
                platform_key=platform_key,
                requested_ultra_hd=requested_ultra_hd,
                prefer_ultra_hd=prefer_ultra_hd,
                requested_youtube_merge=requested_youtube_merge,
                prefer_youtube_merge=prefer_youtube_merge,
                duration_ms=(time.monotonic() - parse_started_at) * 1000,
                context=context,
                status=ObservabilityStatus.RATE_LIMITED,
                error_code="RATE_LIMITED",
                error_message_short=context["error_message"],
            )
            return context, 429
        try:
            global_limit = consume_fixed_window(
                "third-party-provider-minute-global",
                "all",
                settings.THIRD_PARTY_GLOBAL_REQUEST_LIMIT_PER_MINUTE,
                60,
            )
        except Exception:
            logger.exception("Third-party provider global rate-limit cache failed")
            context["error_message"] = "第三方解析保护服务暂时不可用，请稍后再试。"
            log_parse_request_event(
                request,
                "media_parse_failed",
                query=query,
                platform_key=platform_key,
                requested_ultra_hd=requested_ultra_hd,
                prefer_ultra_hd=prefer_ultra_hd,
                requested_youtube_merge=requested_youtube_merge,
                prefer_youtube_merge=prefer_youtube_merge,
                duration_ms=(time.monotonic() - parse_started_at) * 1000,
                context=context,
                status=ObservabilityStatus.FAILED,
                error_code="RATE_LIMIT_CACHE_ERROR",
                error_message_short=context["error_message"],
            )
            return context, 503
        if not global_limit.allowed:
            context["error_message"] = "第三方解析当前排队请求较多，请稍后再试。"
            context["retry_after"] = global_limit.retry_after
            log_parse_request_event(
                request,
                "media_parse_rate_limited",
                query=query,
                platform_key=platform_key,
                requested_ultra_hd=requested_ultra_hd,
                prefer_ultra_hd=prefer_ultra_hd,
                requested_youtube_merge=requested_youtube_merge,
                prefer_youtube_merge=prefer_youtube_merge,
                duration_ms=(time.monotonic() - parse_started_at) * 1000,
                context=context,
                status=ObservabilityStatus.RATE_LIMITED,
                error_code="RATE_LIMITED",
                error_message_short=context["error_message"],
            )
            return context, 429

    if prefer_ultra_hd:
        try:
            limit = consume_fixed_window(
                "ultra-hd-minute-ip",
                get_client_identity(request),
                settings.ULTRA_HD_RATE_LIMIT_PER_MINUTE,
                60,
            )
        except Exception:
            logger.exception("Ultra-HD rate-limit cache failed")
            prefer_ultra_hd = False
            context["prefer_ultra_hd"] = False
            context["provider_notice"] = "超高清成本控制暂时不可用，本次已回退本地解析。"
            limit = None
        if limit is not None and not limit.allowed:
            context["error_message"] = "超高清解析请求过于频繁，请稍后再试。"
            context["retry_after"] = limit.retry_after
            log_parse_request_event(
                request,
                "media_parse_rate_limited",
                query=query,
                platform_key=platform_key,
                requested_ultra_hd=requested_ultra_hd,
                prefer_ultra_hd=prefer_ultra_hd,
                requested_youtube_merge=requested_youtube_merge,
                prefer_youtube_merge=prefer_youtube_merge,
                duration_ms=(time.monotonic() - parse_started_at) * 1000,
                context=context,
                status=ObservabilityStatus.RATE_LIMITED,
                error_code="RATE_LIMITED",
                error_message_short=context["error_message"],
            )
            return context, 429
    elif requested_ultra_hd:
        context["provider_notice"] = "当前环境未向访客开放超高清 Provider，本次使用本地解析。"

    if requested_youtube_merge and not prefer_youtube_merge:
        context["provider_notice"] = "当前环境未向该用户开放 YouTube 最高画质，本次默认优先 1080p。"

    try:
        client_country_code = (
            get_request_country_code(request)
            if prefer_ultra_hd
            else ""
        )
        parse_options = {"prefer_ultra_hd": prefer_ultra_hd}
        if prefer_youtube_merge:
            parse_options["prefer_youtube_merge"] = True
        if client_country_code:
            parse_options["client_country_code"] = client_country_code
        result = parse_media_page(query, **parse_options)
        result_metadata = result.metadata or {}
        display_context = build_result_display_context(result, platform_key=platform_key)
        context["platform"] = result.platform
        target_platform_key = get_platform_key_for_result(result.platform, result_metadata)
        if (
            platform_key != "home"
            and target_platform_key
            and target_platform_key != platform_key
        ):
            context["target_page"] = get_page_payload(target_platform_key)
        context["result_theme"] = display_context["result_theme"]
        context["platform_name"] = display_context["platform_name"]
        if context["page_platform_id"] and context["page_platform_id"] != result.platform:
            context["platform_notice"] = f"你粘贴的是{context['platform_name']}链接，已自动使用对应解析器。"
        if result.platform == "third_party":
            provider_label = result_metadata.get("provider_label") or "第三方 Provider"
            context["provider_notice"] = f"本次由 {provider_label} 第三方解析，资源可用性取决于上游平台。"
            if result_metadata.get("selected_provider") == "youtube_merge":
                context["provider_notice"] = "本次使用 YouTube 最高画质任务；下载时将在服务端合并音频和视频，文件可能更大。"
            elif result_metadata.get("selected_provider") == "yt_dlp_merge":
                context["provider_notice"] = "本次使用 yt-dlp 高清合并；默认优先 1080p，下载时将在服务端合并音频和视频。"
        if prefer_ultra_hd and result.platform == "douyin":
            ultra_provider = (
                result_metadata.get("ultra_race_winner")
                or result_metadata.get("selected_provider")
                or result_metadata.get("source_provider")
            )
            ultra_provider_label = {
                "tiantianjiexi": "天天解析",
                "kukutool": "KuKuTool",
                "native": "本地解析",
                "mobile_feed": "本地解析",
                "mobile_share": "本地解析",
            }.get(ultra_provider, "")
            if ultra_provider_label in {"天天解析", "KuKuTool"}:
                context["provider_notice"] = (
                    f"本次由 {ultra_provider_label} 返回抖音原画 / 超高清资源；"
                    "若上游不可用会自动回退本地解析。"
                )
            else:
                context["provider_notice"] = "本次未命中超高清上游，已自动回退本地解析。"
        context["post_type"] = display_context["post_type"]
        context["base_filename"] = display_context["base_filename"]
        context["image_items"] = display_context["image_items"]
        context["video_items"] = display_context["video_items"]

        if not context["image_items"] and not context["video_items"]:
            context["error_message"] = "没有解析到图片或视频。"
        log_parse_request_event(
            request,
            "media_parse_completed",
            query=query,
            platform_key=platform_key,
            requested_ultra_hd=requested_ultra_hd,
            prefer_ultra_hd=prefer_ultra_hd,
            requested_youtube_merge=requested_youtube_merge,
            prefer_youtube_merge=prefer_youtube_merge,
            duration_ms=(time.monotonic() - parse_started_at) * 1000,
            result=result,
            context=context,
        )

    except XhsProviderTimeoutError as error:
        context["error_message"] = str(error)
        log_parse_request_event(
            request,
            "media_parse_failed",
            query=query,
            platform_key=platform_key,
            requested_ultra_hd=requested_ultra_hd,
            prefer_ultra_hd=prefer_ultra_hd,
            requested_youtube_merge=requested_youtube_merge,
            prefer_youtube_merge=prefer_youtube_merge,
            duration_ms=(time.monotonic() - parse_started_at) * 1000,
            error=error,
            status=ObservabilityStatus.FAILED,
            error_code="PROVIDER_TIMEOUT",
        )
    except XhsError as error:
        context["error_message"] = f"解析失败：{error}"
        log_parse_request_event(
            request,
            "media_parse_failed",
            query=query,
            platform_key=platform_key,
            requested_ultra_hd=requested_ultra_hd,
            prefer_ultra_hd=prefer_ultra_hd,
            requested_youtube_merge=requested_youtube_merge,
            prefer_youtube_merge=prefer_youtube_merge,
            duration_ms=(time.monotonic() - parse_started_at) * 1000,
            error=error,
            status=ObservabilityStatus.FAILED,
        )
    except requests.RequestException as error:
        context["error_message"] = f"请求页面失败：{error}"
        log_parse_request_event(
            request,
            "media_parse_failed",
            query=query,
            platform_key=platform_key,
            requested_ultra_hd=requested_ultra_hd,
            prefer_ultra_hd=prefer_ultra_hd,
            requested_youtube_merge=requested_youtube_merge,
            prefer_youtube_merge=prefer_youtube_merge,
            duration_ms=(time.monotonic() - parse_started_at) * 1000,
            error=error,
            status=ObservabilityStatus.FAILED,
        )

    return context, 200


def request_wants_results_partial(request):
    return (
        request.headers.get("x-requested-with") == "XMLHttpRequest"
        or request.GET.get("_partial") == "1"
    )


def render_results_partial_response(request, context, status_code=200):
    has_results = bool(context.get("image_items") or context.get("video_items"))
    has_content = has_results or bool(
        context.get("error_message")
        or context.get("platform_notice")
        or context.get("provider_notice")
    )
    html = render_to_string(
        "partials/xhs_results_region.html",
        context,
        request=request,
    )
    response = JsonResponse(
        {
            "status": "success" if status_code < 400 else "error",
            "html": html,
            "has_results": has_results,
            "has_content": has_content,
            "base_filename": context.get("base_filename") or "xhs_image",
            "result_theme": context.get("result_theme") or "home",
            "target_page": context.get("target_page") or None,
            "message": context.get("error_message") or "",
        },
        status=status_code,
    )
    if context.get("retry_after"):
        response["Retry-After"] = str(context["retry_after"])
    response["Cache-Control"] = "no-store"
    return response


def search_results_view(request, platform_key="home"):
    context, status_code = build_search_results_context(request, platform_key)
    if request_wants_results_partial(request):
        return render_results_partial_response(request, context, status_code)

    response = render(request, "xhs_download.html", context, status=status_code)
    if context.get("retry_after"):
        response["Retry-After"] = str(context["retry_after"])
    return response


def batch_parse_user_is_staff(request):
    user = getattr(request, "user", None)
    return bool(
        getattr(user, "is_active", False)
        and getattr(user, "is_staff", False)
    )


def batch_parse_access_allowed(request):
    return batch_parse_user_is_staff(request) or bool(
        env_runtime_bool("BATCH_PARSE_PUBLIC_ENABLED", False)
    )


def batch_parse_max_links(request):
    global_max = max(1, env_runtime_int("BATCH_PARSE_MAX_LINKS", 20))
    if batch_parse_user_is_staff(request):
        staff_max = max(
            1,
            env_runtime_int("BATCH_PARSE_STAFF_MAX_LINKS", global_max),
        )
        return min(global_max, staff_max)
    public_max = max(
        1,
        env_runtime_int("BATCH_PARSE_PUBLIC_MAX_LINKS", 3),
    )
    return min(global_max, public_max)


def batch_parse_total_timeout_seconds():
    return max(
        1.0,
        env_runtime_float("BATCH_PARSE_TOTAL_TIMEOUT_SECONDS", 30.0),
    )


def batch_parse_per_link_timeout_seconds():
    return max(
        0.5,
        env_runtime_float("BATCH_PARSE_PER_LINK_TIMEOUT_SECONDS", 8.0),
    )


def batch_parse_rate_limit_results(request, link_count):
    identity = get_client_identity(request)
    amount = max(1, int(link_count or 1))
    return (
        consume_fixed_window(
            "batch-parse-minute-ip",
            identity,
            env_runtime_int("BATCH_PARSE_MINUTE_IP_LIMIT", 3),
            60,
            amount=amount,
        ),
        consume_fixed_window(
            "batch-parse-global-minute",
            "global",
            env_runtime_int("BATCH_PARSE_GLOBAL_MINUTE_LIMIT", 30),
            60,
            amount=amount,
        ),
        consume_daily(
            "batch-parse-daily-ip",
            identity,
            env_runtime_int("BATCH_PARSE_DAILY_IP_LIMIT", 20),
            amount=amount,
        ),
    )


def split_batch_links(value, max_links=None):
    max_links = max(1, int(max_links or env_runtime_int("BATCH_PARSE_MAX_LINKS", 20) or 20))
    links = []
    seen = set()
    for raw_line in str(value or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line in seen:
            continue
        seen.add(line)
        links.append(line)
        if len(links) >= max_links:
            break
    return links


def build_batch_skipped_row(source_text, reason, started_at=None):
    started_at = started_at or time.monotonic()
    return {
        "status": "skipped",
        "source_text": source_text,
        "duration_ms": int((time.monotonic() - started_at) * 1000),
        "error_message": sanitize_observability_text(reason),
    }


def batch_link_host(url):
    try:
        return urlparse(str(url or "")).hostname or ""
    except Exception:
        return ""


def enrich_batch_media_items(row):
    for item in row.get("video_items") or ():
        item["direct_host"] = batch_link_host(item.get("video_url") or item.get("url"))
        item["direct_url"] = item.get("video_url") or item.get("url") or ""
    for item in row.get("image_items") or ():
        item["direct_host"] = batch_link_host(item.get("url"))
        item["direct_url"] = item.get("url") or ""


def build_batch_summary(rows):
    summary = {
        "total_count": len(rows),
        "success_count": 0,
        "failed_count": 0,
        "skipped_count": 0,
        "video_count": 0,
        "image_count": 0,
        "total_duration_ms": 0,
        "provider_counts": {},
        "platform_counts": {},
    }
    for row in rows:
        status = row.get("status") or ""
        if status == "success":
            summary["success_count"] += 1
            videos = row.get("video_items") or ()
            images = row.get("image_items") or ()
            summary["video_count"] += len(videos)
            summary["image_count"] += len(images)
            provider = row.get("selected_provider") or row.get("provider_label") or "native"
            platform = row.get("platform_name") or row.get("platform") or "未知平台"
            summary["provider_counts"][provider] = summary["provider_counts"].get(provider, 0) + 1
            summary["platform_counts"][platform] = summary["platform_counts"].get(platform, 0) + 1
        elif status == "skipped":
            summary["skipped_count"] += 1
        else:
            summary["failed_count"] += 1
        summary["total_duration_ms"] += int(row.get("duration_ms") or 0)
    return summary


def build_batch_result_row(request, source_text, *, prefer_ultra_hd, prefer_youtube_merge, deadline=None):
    started_at = time.monotonic()
    if deadline is not None and time.monotonic() >= deadline:
        return build_batch_skipped_row(source_text, "批量解析总预算已用尽，本条已跳过。", started_at)
    try:
        remaining_timeout = batch_parse_per_link_timeout_seconds()
        if deadline is not None:
            remaining_timeout = min(
                remaining_timeout,
                max(0.0, deadline - time.monotonic()),
            )
        if remaining_timeout <= 0:
            return build_batch_skipped_row(source_text, "批量解析总预算已用尽，本条已跳过。", started_at)
        if query_uses_third_party_provider(source_text) and not third_party_provider_is_available(request):
            raise XhsParseError("第三方通用解析暂未开放。")
        result = parse_media_page(
            source_text,
            prefer_ultra_hd=prefer_ultra_hd,
            client_country_code=get_request_country_code(request) if prefer_ultra_hd else "",
            prefer_youtube_merge=prefer_youtube_merge,
            timeout_seconds=remaining_timeout,
            deadline_monotonic=(
                time.monotonic() + remaining_timeout
                if deadline is None
                else min(deadline, time.monotonic() + remaining_timeout)
            ),
        )
        display_context = build_result_display_context(result, platform_key="home")
        metadata = result.metadata or {}
        row = {
            "status": "success",
            "source_text": source_text,
            "duration_ms": int((time.monotonic() - started_at) * 1000),
            "provider_label": metadata.get("provider_label") or metadata.get("selected_provider") or "",
            "selected_provider": metadata.get("selected_provider") or metadata.get("source_provider") or "",
            **display_context,
        }
        enrich_batch_media_items(row)
        return row
    except XhsProviderTimeoutError as error:
        return {
            "status": "failed",
            "source_text": source_text,
            "duration_ms": int((time.monotonic() - started_at) * 1000),
            "error_message": sanitize_observability_text(str(error)),
        }
    except XhsError as error:
        return {
            "status": "failed",
            "source_text": source_text,
            "duration_ms": int((time.monotonic() - started_at) * 1000),
            "error_message": sanitize_observability_text(f"解析失败：{error}"),
        }
    except requests.RequestException as error:
        return {
            "status": "failed",
            "source_text": source_text,
            "duration_ms": int((time.monotonic() - started_at) * 1000),
            "error_message": sanitize_observability_text(f"请求失败：{error}"),
        }
    except Exception as error:
        return {
            "status": "failed",
            "source_text": source_text,
            "duration_ms": int((time.monotonic() - started_at) * 1000),
            "error_message": sanitize_observability_text(f"{type(error).__name__}: {error}"),
        }


def batch_parse_view(request):
    if not batch_parse_access_allowed(request):
        raise Http404("Batch parse is not available.")

    raw_links = request.POST.get("links", "") if request.method == "POST" else ""
    prefer_ultra_hd = (
        request.method == "POST"
        and request_flag(request.POST.get("ultra_hd"))
        and ultra_hd_is_available(request)
    )
    prefer_youtube_merge = (
        request.method == "POST"
        and request_flag(request.POST.get("youtube_merge"))
        and youtube_merge_mode_is_available(request)
    )
    max_links = batch_parse_max_links(request)
    links = split_batch_links(raw_links, max_links=max_links)
    rows = []
    batch_error = ""
    if request.method == "POST":
        limits = batch_parse_rate_limit_results(request, len(links) or 1)
        blocked_limit = next((limit for limit in limits if not limit.allowed), None)
        if blocked_limit:
            batch_error = f"批量解析请求过于频繁，请约 {blocked_limit.retry_after} 秒后再试。"
        else:
            deadline = time.monotonic() + batch_parse_total_timeout_seconds()
            for link in links:
                if time.monotonic() >= deadline:
                    rows.append(build_batch_skipped_row(link, "批量解析总预算已用尽，本条已跳过。"))
                    continue
                rows.append(
                    build_batch_result_row(
                        request,
                        link,
                        prefer_ultra_hd=prefer_ultra_hd,
                        prefer_youtube_merge=prefer_youtube_merge,
                        deadline=deadline,
                    )
                )

    return render(
        request,
        "batch_parse.html",
        {
            "theme_class": "theme-home",
            "page_title": "批量解析",
            "links_text": raw_links,
            "links": links,
            "rows": rows,
            "batch_summary": build_batch_summary(rows),
            "max_links": max_links,
            "batch_per_link_timeout_seconds": batch_parse_per_link_timeout_seconds(),
            "batch_total_timeout_seconds": batch_parse_total_timeout_seconds(),
            "batch_error": batch_error,
            "batch_staff_only": not env_runtime_bool("BATCH_PARSE_PUBLIC_ENABLED", False),
            "batch_direct_links_allowed": batch_parse_user_is_staff(request),
            "prefer_ultra_hd": prefer_ultra_hd,
            "prefer_youtube_merge": prefer_youtube_merge,
            "ultra_hd_available": ultra_hd_is_available(request),
            "youtube_merge_mode_available": youtube_merge_mode_is_available(request),
            "strategy_matrix": build_download_strategy_matrix(),
            "proxy_image_url": reverse("media_proxy_image"),
        },
    )


def placeholder_image_response(status=200, retry_after=0):
    svg = """
    <svg xmlns="http://www.w3.org/2000/svg" width="400" height="300">
        <rect width="100%" height="100%" fill="#f1f3f5"/>
        <text x="50%" y="45%" dominant-baseline="middle" text-anchor="middle"
              font-size="18" fill="#999">
            图片加载失败
        </text>
        <text x="50%" y="58%" dominant-baseline="middle" text-anchor="middle"
              font-size="14" fill="#bbb">
            请稍后刷新重试
        </text>
    </svg>
    """
    response = HttpResponse(svg, content_type="image/svg+xml", status=status)
    response["Cache-Control"] = "no-store"
    if retry_after:
        response["Retry-After"] = str(retry_after)
    return response


@require_GET
def proxy_image_view(request):
    try:
        identity, limit = check_preview_request_limits(request)
    except Exception:
        logger.exception("Preview rate-limit cache failed")
        return placeholder_image_response(status=503)
    if limit:
        return placeholder_image_response(
            status=429,
            retry_after=limit.retry_after,
        )

    encoded_url = request.GET.get("u")

    if not encoded_url:
        return placeholder_image_response()

    provider_item = None
    try:
        image_url = decode_url(encoded_url)
        provider_item = build_provider_preview_item(request, image_url)
        if provider_item is None:
            validate_media_url(image_url)
        response = fetch_media_response(
            image_url,
            timeout=15,
            provider_signed=provider_item,
        )

        with response:
            content_type = response.headers.get("Content-Type", "image/jpeg")
            if "image" not in content_type.lower():
                invalidate_external_provider_caches(provider_item)
                return placeholder_image_response()

            content = read_limited_response(response, max_bytes=PREVIEW_MAX_IMAGE_BYTES)

        try:
            within_limit = record_preview_bytes(identity, len(content))
        except Exception:
            logger.exception("Preview byte-limit cache failed")
            return placeholder_image_response(status=503)
        if not within_limit:
            return placeholder_image_response(status=429)

        result = HttpResponse(content, content_type=content_type)
        result["Cache-Control"] = "private, max-age=300"
        return result

    except Exception as error:
        if provider_item and is_recoverable_provider_download_error(error):
            invalidate_external_provider_caches(provider_item)
        logger.warning("XHS preview proxy failed: %s", error)
        return placeholder_image_response()


@require_POST
@csrf_protect
def download_images_api(request):
    started_at = time.monotonic()
    try:
        if request.content_type and request.content_type.startswith("application/json"):
            data = json.loads(request.body.decode("utf-8"))
        else:
            payload_json = request.POST.get("payload_json", "")
            if not payload_json:
                raise json.JSONDecodeError("missing payload_json", "", 0)
            data = json.loads(payload_json)
    except json.JSONDecodeError:
        log_download_request_event(
            request,
            download_type="zip",
            status=ObservabilityStatus.FAILED,
            started_at=started_at,
            error_code="INVALID_JSON",
            error=ValueError("请求数据不是有效 JSON。"),
        )
        return JsonResponse({"status": "error", "message": "请求数据不是有效 JSON。"}, status=400)

    media_items = data.get("media_items", [])
    route_only = bool(data.get("route_only"))
    image_urls = data.get("image_urls", [])
    if image_urls and not media_items:
        media_items = [{"type": "image", "url": url} for url in image_urls]
    download_mode = (data.get("download_mode") or "browser").lower().strip()
    if download_mode == "server" and not server_save_mode_is_enabled():
        download_mode = "auto"
    routed_download_modes = {"auto", "browser", "proxy", "server_single", "zip"}
    route_info = zip_download_route_info(
        request,
        media_items,
        reason="默认兼容旧 ZIP 下载流程",
        record_healing=download_mode not in routed_download_modes,
    )
    if download_items_have_invalid_provider_manifest(media_items):
        route_info = blocked_download_route_info(
            media_items,
            "Provider 签名资源未通过校验",
            "PROVIDER_MANIFEST_REJECTED",
        )
        log_download_request_event(
            request,
            download_type="zip",
            media_items=media_items,
            status=ObservabilityStatus.FAILED,
            started_at=started_at,
            error_code="PROVIDER_MANIFEST_REJECTED",
            error=ValueError("Provider 签名资源未通过校验"),
            route_info=route_info,
        )
        return JsonResponse(
            {"status": "error", "message": "Provider 签名资源未通过校验"},
            status=400,
        )

    runtime_setting = get_download_runtime_setting()
    if download_mode in routed_download_modes:
        def route_decision():
            decided = decide_download_route(
                request,
                media_items,
                requested_mode=download_mode,
                platform_key=data.get("platform_key", ""),
            )
            if download_mode == "auto":
                decided = apply_route_v3_metadata(
                    request,
                    media_items,
                    decided,
                    requested_mode="auto",
                    platform_key=data.get("platform_key", ""),
                )
            decided = apply_policy_decision_to_route(
                request,
                media_items,
                decided,
                requested_mode=download_mode,
                platform_key=data.get("platform_key", ""),
                provider_hint=decided.get("provider", ""),
            )
            return decided

        route_info = safe_routing_decision(
            request,
            media_items,
            route_decision,
            reason="下载路由异常，已降级为 ZIP 下载",
        )
        if route_info.get("recovery"):
            route_info = attach_self_healing_event(
                request,
                route_info,
                media_items,
                trigger="routing_exception",
                error=RuntimeError(route_info.get("route_reason", "")),
            )
    guard_result = check_download_cost_preflight(request, media_items, runtime_setting)
    if guard_result:
        error_code, message, status_code = guard_result
        route_info = blocked_download_route_info(media_items, message, error_code)
        route_info = attach_self_healing_event(
            request,
            route_info,
            media_items,
            trigger="cost_guard",
            error=ValueError(message),
            error_code=error_code,
        )
        log_download_request_event(
            request,
            download_type="zip",
            media_items=media_items,
            status=ObservabilityStatus.BLOCKED,
            started_at=started_at,
            error_code=error_code,
            error=ValueError(message),
            route_info=route_info,
        )
        return download_guard_error_response(
            message,
            status=status_code,
            error_code=error_code,
        )

    if download_mode in routed_download_modes and route_info.get("route_type") == ROUTE_BLOCKED:
        error_code = route_info.get("error_code") or "DOWNLOAD_BLOCKED"
        log_download_request_event(
            request,
            download_type="auto",
            media_items=media_items,
            status=ObservabilityStatus.BLOCKED,
            started_at=started_at,
            error_code=error_code,
            error=ValueError(route_info.get("route_reason", "下载被阻止")),
            route_info=route_info,
        )
        return download_guard_error_response(
            route_info.get("route_reason") or "下载被阻止",
            status=403,
            error_code=error_code,
        )

    if download_mode in routed_download_modes and route_info.get("route_type") == ROUTE_DIRECT:
        log_download_request_event(
            request,
            download_type=route_info.get("route_type", "auto"),
            media_items=media_items,
            status=ObservabilityStatus.SUCCESS,
            started_at=started_at,
            actual_bytes=0,
            route_info=route_info,
        )
        return JsonResponse(
            {
                "status": "routed",
                "route_type": route_info.get("route_type"),
                "route_reason": route_info.get("route_reason"),
                "download_strategy": route_info.get("download_strategy"),
                "items": route_info.get("items", []),
                "warnings": route_info.get("warnings", []),
                "estimated_bytes": route_info.get("estimated_bytes", 0),
                "server_egress_estimated_bytes": route_info.get("server_egress_estimated_bytes", 0),
                "browser_egress_estimated_bytes": route_info.get("browser_egress_estimated_bytes", 0),
                "strategy_version": route_info.get("strategy_version", "v3"),
                "confidence": route_info.get("confidence", 0),
                "routing_score": route_info.get("routing_score", 0),
            }
        )

    if route_only and download_mode in routed_download_modes:
        log_download_request_event(
            request,
            download_type=route_info.get("route_type", "auto"),
            media_items=media_items,
            status=ObservabilityStatus.SUCCESS,
            started_at=started_at,
            actual_bytes=0,
            route_info=route_info,
        )
        return JsonResponse(
            {
                "status": "routed",
                "route_type": route_info.get("route_type"),
                "route_reason": route_info.get("route_reason"),
                "download_strategy": route_info.get("download_strategy"),
                "items": route_info.get("items", []),
                "warnings": route_info.get("warnings", []),
                "estimated_bytes": route_info.get("estimated_bytes", 0),
                "server_egress_estimated_bytes": route_info.get("server_egress_estimated_bytes", 0),
                "browser_egress_estimated_bytes": route_info.get("browser_egress_estimated_bytes", 0),
                "strategy_version": route_info.get("strategy_version", "v3"),
                "confidence": route_info.get("confidence", 0),
                "routing_score": route_info.get("routing_score", 0),
                "native_browser_download": route_info.get("download_strategy") == "server_single_attachment",
            }
        )

    try:
        identity, limit_response = check_download_request_limits(request)
    except Exception:
        logger.exception("Download rate-limit cache failed")
        log_download_request_event(
            request,
            download_type="zip",
            media_items=media_items,
            status=ObservabilityStatus.FAILED,
            started_at=started_at,
            error_code="RATE_LIMIT_CACHE_ERROR",
            route_info=blocked_download_route_info(
                media_items,
                "下载保护服务暂时不可用",
                "RATE_LIMIT_CACHE_ERROR",
            ),
        )
        return JsonResponse(
            {"status": "error", "message": "下载保护服务暂时不可用，请稍后再试。"},
            status=503,
        )
    if limit_response:
        error_code = limit_response.get("X-Error-Code", "RATE_LIMITED")
        route_info = blocked_download_route_info(
            media_items,
            "下载请求被成本或频率限制阻止",
            error_code,
        )
        log_download_request_event(
            request,
            download_type="zip",
            media_items=media_items,
            status=ObservabilityStatus.RATE_LIMITED,
            started_at=started_at,
            error_code=error_code,
            route_info=route_info,
        )
        return limit_response

    try:
        concurrency = acquire_concurrency(
            "download-concurrent",
            identity,
            runtime_setting.max_concurrent_per_ip,
        )
    except Exception:
        logger.exception("Download concurrency cache failed")
        log_download_request_event(
            request,
            download_type="zip",
            media_items=media_items,
            status=ObservabilityStatus.FAILED,
            started_at=started_at,
            error_code="CONCURRENCY_CACHE_ERROR",
            route_info=blocked_download_route_info(
                media_items,
                "下载并发保护服务暂时不可用",
                "CONCURRENCY_CACHE_ERROR",
            ),
        )
        return JsonResponse(
            {"status": "error", "message": "下载保护服务暂时不可用，请稍后再试。"},
            status=503,
        )
    if not concurrency.allowed:
        route_info = blocked_download_route_info(
            media_items,
            "当前已有下载任务正在处理，请等待完成后再试",
            "DOWNLOAD_CONCURRENCY_LIMIT",
        )
        log_download_request_event(
            request,
            download_type="zip",
            media_items=media_items,
            status=ObservabilityStatus.RATE_LIMITED,
            started_at=started_at,
            error_code="DOWNLOAD_CONCURRENCY_LIMIT",
            route_info=route_info,
        )
        return limit_error_response(
            "当前已有下载任务正在处理，请等待完成后再试。",
            error_code="DOWNLOAD_CONCURRENCY_LIMIT",
        )

    try:
        if route_info.get("download_strategy") == "server_single_attachment":
            try:
                single_response, actual_bytes = build_single_attachment_response(
                    media_items[0],
                    data.get("base_filename", "xhs"),
                )
            except Exception as error:
                recovery_decision = proxy_failure_recovery(media_items, route_info, error)
                recovered_route = recovery_decision.route_info
                if recovered_route and recovered_route.get("route_type") == ROUTE_DIRECT:
                    recovered_route["recovery"] = recovery_decision.log_fields()
                    recovered_route = attach_self_healing_event(
                        request,
                        recovered_route,
                        media_items,
                        trigger="proxy_failure",
                        error=error,
                    )
                    log_download_request_event(
                        request,
                        download_type=ROUTE_DIRECT,
                        media_items=media_items,
                        status=ObservabilityStatus.SUCCESS,
                        started_at=started_at,
                        actual_bytes=0,
                        route_info=recovered_route,
                    )
                    return JsonResponse(
                        {
                            "status": "routed",
                            "route_type": recovered_route.get("route_type"),
                            "route_reason": recovered_route.get("route_reason"),
                            "download_strategy": recovered_route.get("download_strategy"),
                            "items": recovered_route.get("items", []),
                            "warnings": recovered_route.get("warnings", []),
                            "estimated_bytes": recovered_route.get("estimated_bytes", 0),
                            "server_egress_estimated_bytes": recovered_route.get("server_egress_estimated_bytes", 0),
                            "browser_egress_estimated_bytes": recovered_route.get("browser_egress_estimated_bytes", 0),
                            "strategy_version": recovered_route.get("strategy_version", "recovery"),
                            "confidence": recovered_route.get("confidence", 0),
                            "routing_score": recovered_route.get("routing_score", 0),
                        }
                    )

                blocked_route = blocked_download_route_info(
                    media_items,
                    "单文件代理失败，且没有安全的自动降级路径",
                    "PROXY_FAILED_RECOVERY_BLOCKED",
                )
                blocked_route["recovery"] = recovery_decision.log_fields()
                blocked_route = attach_self_healing_event(
                    request,
                    blocked_route,
                    media_items,
                    trigger="proxy_failure",
                    error=error,
                    error_code="PROXY_FAILED_RECOVERY_BLOCKED",
                    force_healed=False,
                )
                log_download_request_event(
                    request,
                    download_type="single_file",
                    media_items=media_items,
                    status=ObservabilityStatus.BLOCKED,
                    started_at=started_at,
                    actual_bytes=0,
                    error_code="PROXY_FAILED_RECOVERY_BLOCKED",
                    error=error,
                    error_message_short=public_download_error_message(error),
                    route_info=blocked_route,
                )
                return JsonResponse(
                    {"status": "error", "message": "单文件下载失败，请重新解析后再试。"},
                    status=400,
                )

            within_limit, bytes_error_code = record_download_archive_bytes(
                identity,
                None,
                actual_bytes,
            )
            if not within_limit:
                single_response.close()
                log_download_request_event(
                    request,
                    download_type="single_file",
                    media_items=media_items,
                    status=ObservabilityStatus.RATE_LIMITED,
                    started_at=started_at,
                    actual_bytes=actual_bytes,
                    error_code=bytes_error_code or "RATE_LIMITED",
                    route_info=blocked_download_route_info(
                        media_items,
                        "今日下载流量已达到限制",
                        bytes_error_code or "RATE_LIMITED",
                    ),
                )
                return limit_error_response(
                    "今日下载流量已达到限制，请稍后再试。",
                    error_code=bytes_error_code or "RATE_LIMITED",
                )
            log_download_request_event(
                request,
                download_type="single_file",
                media_items=media_items,
                status=ObservabilityStatus.SUCCESS,
                started_at=started_at,
                actual_bytes=actual_bytes,
                route_info=route_info,
            )
            return single_response

        if download_mode == "server":
            if not server_save_mode_is_enabled():
                route_info = blocked_download_route_info(
                    media_items,
                    "当前环境未开启服务端保存",
                    "SERVER_SAVE_DISABLED",
                )
                log_download_request_event(
                    request,
                    download_type="server",
                    media_items=media_items,
                    status=ObservabilityStatus.BLOCKED,
                    started_at=started_at,
                    error_code="SERVER_SAVE_DISABLED",
                    route_info=route_info,
                )
                return JsonResponse({"status": "error", "message": "当前环境未开启服务端保存。"}, status=403)

            result = save_media_items_to_server(
                media_items=media_items,
                base_filename=data.get("base_filename", "xhs"),
                save_dir=data.get("save_dir") or settings.XHS_DEFAULT_SERVER_SAVE_DIR,
                save_format=data.get("save_format", "original"),
                quality=data.get("quality", 95),
                video_save_mode=data.get("video_save_mode", "video"),
                allow_absolute=settings.XHS_ALLOW_ABSOLUTE_SAVE_PATH,
            )
            log_download_request_event(
                request,
                download_type="server",
                media_items=media_items,
                status=ObservabilityStatus.SUCCESS,
                started_at=started_at,
                actual_bytes=0,
                route_info=route_info,
            )
            return JsonResponse({
                "status": "success",
                "message": f"服务端保存完成：成功 {result['saved_count']} 个，失败 {result['failed_count']} 个",
                **result,
            })

        try:
            archive = build_download_archive_file(
                media_items=media_items,
                base_filename=data.get("base_filename", "xhs"),
                save_format=data.get("save_format", "original"),
                quality=data.get("quality", 95),
                video_save_mode=data.get("video_save_mode", "video"),
            )
        except Exception as error:
            recovery_decision = zip_failure_recovery(media_items, route_info, error)
            recovered_route = recovery_decision.route_info
            if recovered_route and recovered_route.get("route_type") == ROUTE_PROXY:
                recovered_route["recovery"] = recovery_decision.log_fields()
                recovered_route = attach_self_healing_event(
                    request,
                    recovered_route,
                    media_items,
                    trigger="zip_failure",
                    error=error,
                )
                try:
                    single_response, actual_bytes = build_single_attachment_response(
                        media_items[0],
                        data.get("base_filename", "xhs"),
                    )
                except Exception as proxy_error:
                    proxy_recovery = proxy_failure_recovery(media_items, recovered_route, proxy_error)
                    blocked_route = blocked_download_route_info(
                        media_items,
                        "ZIP 打包失败后单文件降级仍失败",
                        "ZIP_RECOVERY_PROXY_FAILED",
                    )
                    blocked_route["recovery"] = proxy_recovery.log_fields()
                    blocked_route = attach_self_healing_event(
                        request,
                        blocked_route,
                        media_items,
                        trigger="zip_recovery_proxy_failure",
                        error=proxy_error,
                        error_code="ZIP_RECOVERY_PROXY_FAILED",
                        depth=1,
                        force_healed=False,
                    )
                    log_download_request_event(
                        request,
                        download_type="zip",
                        media_items=media_items,
                        status=ObservabilityStatus.BLOCKED,
                        started_at=started_at,
                        actual_bytes=0,
                        error_code="ZIP_RECOVERY_PROXY_FAILED",
                        error=proxy_error,
                        error_message_short=public_download_error_message(proxy_error),
                        route_info=blocked_route,
                    )
                    return JsonResponse(
                        {"status": "error", "message": "资源下载失败，自动降级仍未成功，请重新解析后再试。"},
                        status=400,
                    )

                within_limit, bytes_error_code = record_download_archive_bytes(
                    identity,
                    None,
                    actual_bytes,
                )
                if not within_limit:
                    single_response.close()
                    blocked_route = blocked_download_route_info(
                        media_items,
                        "今日下载流量已达到限制",
                        bytes_error_code or "RATE_LIMITED",
                    )
                    blocked_route["recovery"] = recovery_decision.log_fields()
                    blocked_route = attach_self_healing_event(
                        request,
                        blocked_route,
                        media_items,
                        trigger="cost_guard",
                        error=ValueError("今日下载流量已达到限制"),
                        error_code=bytes_error_code or "RATE_LIMITED",
                        depth=1,
                    )
                    log_download_request_event(
                        request,
                        download_type="single_file",
                        media_items=media_items,
                        status=ObservabilityStatus.RATE_LIMITED,
                        started_at=started_at,
                        actual_bytes=actual_bytes,
                        error_code=bytes_error_code or "RATE_LIMITED",
                        route_info=blocked_route,
                    )
                    return limit_error_response(
                        "今日下载流量已达到限制，请稍后再试。",
                        error_code=bytes_error_code or "RATE_LIMITED",
                    )

                recovered_route["recovery"] = recovery_decision.log_fields()
                log_download_request_event(
                    request,
                    download_type="single_file",
                    media_items=media_items,
                    status=ObservabilityStatus.SUCCESS,
                    started_at=started_at,
                    actual_bytes=actual_bytes,
                    route_info=recovered_route,
                )
                return single_response

            blocked_route = blocked_download_route_info(
                media_items,
                "ZIP 打包失败，且没有可用降级路径",
                "ZIP_FAILED_RECOVERY_BLOCKED",
            )
            blocked_route["recovery"] = recovery_decision.log_fields()
            blocked_route = attach_self_healing_event(
                request,
                blocked_route,
                media_items,
                trigger="zip_failure",
                error=error,
                error_code="ZIP_FAILED_RECOVERY_BLOCKED",
                force_healed=False,
            )
            log_download_request_event(
                request,
                download_type="zip",
                media_items=media_items,
                status=ObservabilityStatus.BLOCKED,
                started_at=started_at,
                actual_bytes=0,
                error_code="ZIP_FAILED_RECOVERY_BLOCKED",
                error=error,
                error_message_short=public_download_error_message(error),
                route_info=blocked_route,
            )
            return JsonResponse(
                {"status": "error", "message": "ZIP 打包失败，请减少资源数量或重新解析后再试。"},
                status=400,
            )

        try:
            archive_size = file_size_for_observability(archive["file"])
            actual_size_guard = check_single_archive_actual_size(
                request,
                runtime_setting,
                archive_size,
            )
            if actual_size_guard:
                error_code, message, status_code = actual_size_guard
                archive["file"].close()
                log_download_request_event(
                    request,
                    download_type="zip",
                    media_items=media_items,
                    status=ObservabilityStatus.BLOCKED,
                    started_at=started_at,
                    actual_bytes=archive_size,
                    error_code=error_code,
                    error=ValueError(message),
                    route_info=blocked_download_route_info(
                        media_items,
                        message,
                        error_code,
                    ),
                )
                return download_guard_error_response(
                    message,
                    status=status_code,
                    error_code=error_code,
                )
            within_limit, bytes_error_code = record_download_archive_bytes(
                identity,
                archive["file"],
                archive_size,
            )
        except Exception:
            archive["file"].close()
            raise
        if not within_limit:
            archive["file"].close()
            log_download_request_event(
                request,
                download_type="zip",
                media_items=media_items,
                status=ObservabilityStatus.RATE_LIMITED,
                started_at=started_at,
                actual_bytes=archive_size,
                error_code=bytes_error_code or "RATE_LIMITED",
                route_info=blocked_download_route_info(
                    media_items,
                    "今日下载流量已达到限制",
                    bytes_error_code or "RATE_LIMITED",
                ),
            )
            return limit_error_response(
                "今日下载流量已达到限制，请稍后再试。",
                error_code=bytes_error_code or "RATE_LIMITED",
            )
    except XhsDownloadError as error:
        public_message = public_download_error_message(error)
        error_code = (
            "PROVIDER_MANIFEST_REJECTED"
            if "Provider 签名" in str(error) or "manifest" in str(error).lower()
            else "DOWNLOAD_ERROR"
        )
        route_info = blocked_download_route_info(media_items, "下载失败", error_code)
        route_info = attach_self_healing_event(
            request,
            route_info,
            media_items,
            trigger="download_exception",
            error=error,
            error_code=error_code,
        )
        log_download_request_event(
            request,
            download_type="zip",
            media_items=media_items,
            status=ObservabilityStatus.FAILED,
            started_at=started_at,
            error_code=error_code,
            error=error,
            error_message_short=public_message,
            route_info=route_info,
        )
        return JsonResponse(
            {"status": "error", "message": public_message},
            status=400,
        )
    except Exception as error:
        public_message = public_download_error_message(error)
        logger.warning(
            "xhs_download_failed",
            extra={
                "error_type": type(error).__name__,
                "error": sanitize_observability_text(public_message),
            },
        )
        route_info = blocked_download_route_info(media_items, "下载失败", "DOWNLOAD_ERROR")
        route_info = attach_self_healing_event(
            request,
            route_info,
            media_items,
            trigger="download_exception",
            error=error,
            error_code="DOWNLOAD_ERROR",
        )
        log_download_request_event(
            request,
            download_type="zip",
            media_items=media_items,
            status=ObservabilityStatus.FAILED,
            started_at=started_at,
            error_code="DOWNLOAD_ERROR",
            error=error,
            error_message_short=public_message,
            route_info=route_info,
        )
        return JsonResponse(
            {"status": "error", "message": public_message},
            status=500,
        )
    finally:
        release_concurrency("download-concurrent", identity)

    response = FileResponse(
        archive["file"],
        as_attachment=True,
        filename=archive["filename"],
        content_type="application/zip",
    )
    response["X-Saved-Count"] = str(archive["saved_count"])
    response["X-Failed-Count"] = str(archive["failed_count"])
    log_download_request_event(
        request,
        download_type="zip",
        media_items=media_items,
        status=ObservabilityStatus.SUCCESS,
        started_at=started_at,
        actual_bytes=archive_size,
        route_info=route_info,
    )
    return response


@require_POST
@csrf_protect
def create_download_task_api(request):
    started_at = time.monotonic()
    if not settings.DOWNLOAD_ASYNC_TASKS_ENABLED:
        log_download_request_event(
            request,
            download_type="task_create",
            status=ObservabilityStatus.BLOCKED,
            started_at=started_at,
            error_code="ASYNC_TASKS_DISABLED",
        )
        return JsonResponse(
            {
                "status": "error",
                "message": "当前环境未启用内存异步下载任务，请使用同步下载接口。",
            },
            status=503,
        )

    try:
        data = json.loads(request.body.decode("utf-8"))
    except json.JSONDecodeError:
        log_download_request_event(
            request,
            download_type="task_create",
            status=ObservabilityStatus.FAILED,
            started_at=started_at,
            error_code="INVALID_JSON",
            error=ValueError("请求数据不是有效 JSON。"),
        )
        return JsonResponse({"status": "error", "message": "请求数据不是有效 JSON。"}, status=400)

    media_items = data.get("media_items", [])
    if not media_items:
        log_download_request_event(
            request,
            download_type="task_create",
            media_items=media_items,
            status=ObservabilityStatus.FAILED,
            started_at=started_at,
            error_code="NO_MEDIA_SELECTED",
        )
        return JsonResponse({"status": "error", "message": "没有选择资源"}, status=400)
    if download_items_have_invalid_provider_manifest(media_items):
        log_download_request_event(
            request,
            download_type="task_create",
            media_items=media_items,
            status=ObservabilityStatus.FAILED,
            started_at=started_at,
            error_code="PROVIDER_MANIFEST_REJECTED",
            error=ValueError("Provider 签名资源未通过校验"),
        )
        return JsonResponse(
            {"status": "error", "message": "Provider 签名资源未通过校验"},
            status=400,
        )

    runtime_setting = get_download_runtime_setting()
    guard_result = check_download_cost_preflight(request, media_items, runtime_setting)
    if guard_result:
        error_code, message, status_code = guard_result
        log_download_request_event(
            request,
            download_type="task_create",
            media_items=media_items,
            status=ObservabilityStatus.BLOCKED,
            started_at=started_at,
            error_code=error_code,
            error=ValueError(message),
        )
        return download_guard_error_response(
            message,
            status=status_code,
            error_code=error_code,
        )

    try:
        identity, limit_response = check_download_request_limits(request)
    except Exception:
        logger.exception("Download rate-limit cache failed")
        log_download_request_event(
            request,
            download_type="task_create",
            media_items=media_items,
            status=ObservabilityStatus.FAILED,
            started_at=started_at,
            error_code="RATE_LIMIT_CACHE_ERROR",
        )
        return JsonResponse(
            {"status": "error", "message": "下载保护服务暂时不可用，请稍后再试。"},
            status=503,
        )
    if limit_response:
        error_code = limit_response.get("X-Error-Code", "RATE_LIMITED")
        log_download_request_event(
            request,
            download_type="task_create",
            media_items=media_items,
            status=ObservabilityStatus.RATE_LIMITED,
            started_at=started_at,
            error_code=error_code,
        )
        return limit_response

    try:
        concurrency = acquire_concurrency(
            "download-concurrent",
            identity,
            runtime_setting.max_concurrent_per_ip,
        )
    except Exception:
        logger.exception("Download concurrency cache failed")
        log_download_request_event(
            request,
            download_type="task_create",
            media_items=media_items,
            status=ObservabilityStatus.FAILED,
            started_at=started_at,
            error_code="CONCURRENCY_CACHE_ERROR",
        )
        return JsonResponse(
            {"status": "error", "message": "下载保护服务暂时不可用，请稍后再试。"},
            status=503,
        )
    if not concurrency.allowed:
        log_download_request_event(
            request,
            download_type="task_create",
            media_items=media_items,
            status=ObservabilityStatus.RATE_LIMITED,
            started_at=started_at,
            error_code="DOWNLOAD_CONCURRENCY_LIMIT",
        )
        return limit_error_response(
            "当前已有下载任务正在处理，请等待完成后再试。",
            error_code="DOWNLOAD_CONCURRENCY_LIMIT",
        )

    try:
        task = create_download_task({
            "media_items": media_items,
            "base_filename": data.get("base_filename", "xhs"),
            "save_format": data.get("save_format", "original"),
            "quality": data.get("quality", 95),
            "video_save_mode": data.get("video_save_mode", "video"),
            "download_runtime_limits": {
                "daily_bytes_limit_per_ip": runtime_setting.daily_bytes_limit_per_ip,
                "global_daily_bytes_limit": runtime_setting.global_daily_bytes_limit,
                "single_archive_bytes_limit": runtime_setting.single_archive_bytes_limit,
                "staff_large_download_exempt": runtime_setting.staff_large_download_exempt,
            },
        }, owner_identity=identity)
    except Exception:
        release_concurrency("download-concurrent", identity)
        log_download_request_event(
            request,
            download_type="task_create",
            media_items=media_items,
            status=ObservabilityStatus.FAILED,
            started_at=started_at,
            error_code="DOWNLOAD_TASK_CREATE_FAILED",
        )
        raise
    task_id = task["task_id"]
    log_download_request_event(
        request,
        download_type="task_create",
        media_items=media_items,
        status=ObservabilityStatus.SUCCESS,
        started_at=started_at,
    )
    return JsonResponse({
        "status": "success",
        "task": serialize_task(task),
        "status_url": reverse("media_download_task_status", args=[task_id]),
        "file_url": reverse("media_download_task_file", args=[task_id]),
    })


@require_GET
def download_task_status_api(request, task_id):
    task = get_download_task(task_id)
    if not task:
        return JsonResponse({"status": "error", "message": "下载任务不存在或已过期"}, status=404)

    payload = serialize_task(task)
    payload["status_url"] = reverse("media_download_task_status", args=[task_id])
    if task.get("status") == "completed":
        payload["file_url"] = reverse("media_download_task_file", args=[task_id])
    return JsonResponse(payload)


@require_GET
def download_task_file_api(request, task_id):
    started_at = time.monotonic()
    task = get_download_task(task_id)
    if not task:
        log_download_request_event(
            request,
            download_type="task_file",
            status=ObservabilityStatus.FAILED,
            started_at=started_at,
            error_code="TASK_NOT_FOUND",
        )
        return JsonResponse({"status": "error", "message": "下载任务不存在或已过期"}, status=404)
    if task.get("status") != "completed":
        log_download_request_event(
            request,
            download_type="task_file",
            status=ObservabilityStatus.BLOCKED,
            started_at=started_at,
            error_code="TASK_NOT_COMPLETED",
        )
        return JsonResponse({"status": "error", "message": "下载任务尚未完成"}, status=409)

    task = pop_download_task(task_id)
    if not task:
        log_download_request_event(
            request,
            download_type="task_file",
            status=ObservabilityStatus.FAILED,
            started_at=started_at,
            error_code="TASK_NOT_FOUND",
        )
        return JsonResponse({"status": "error", "message": "下载任务不存在或已过期"}, status=404)

    archive_file = task["file"]
    archive_file.seek(0)
    try:
        archive_size = Path(archive_file.name).stat().st_size
    except Exception:
        archive_size = 0
    response = FileResponse(
        archive_file,
        as_attachment=True,
        filename=task.get("filename") or "media.zip",
        content_type="application/zip",
    )
    response["X-Saved-Count"] = str(task.get("saved_count", 0))
    response["X-Failed-Count"] = str(task.get("failed_count", 0))
    try:
        response["Content-Length"] = str(archive_size)
    except Exception:
        pass
    log_download_request_event(
        request,
        download_type="task_file",
        status=ObservabilityStatus.SUCCESS,
        started_at=started_at,
        actual_bytes=archive_size,
    )
    return response
