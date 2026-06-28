import json
import logging
import time
from pathlib import Path
from urllib.parse import urlencode, urlparse

import requests
from django.conf import settings
from django.contrib import messages
from django.db.models import Avg, Count, Q, Sum
from django.db.models.functions import TruncHour
from django.http import FileResponse, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.template.loader import render_to_string
from django.utils import timezone
from django.urls import reverse
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_GET, require_POST

from .models import (
    DownloadRequestLog,
    ObservabilityStatus,
    ParseRequestLog,
    ProviderAccessMode,
    ProviderAdminAuditLog,
    ProviderRuntimeSetting,
)
from .services.common import (
    XhsDownloadError,
    XhsError,
    XhsProviderTimeoutError,
    decode_url,
    encode_url,
    validate_media_url,
)
from .services.download_tasks import (
    create_download_task,
    get_download_task,
    pop_download_task,
    serialize_task,
)
from .services.downloads import (
    build_download_archive_file,
    fetch_media_response,
    invalidate_external_provider_caches,
    is_recoverable_provider_download_error,
    provider_manifest_allows_public_download,
    probe_media_size,
    read_limited_response,
    save_media_items_to_server,
)
from .services.platforms.douyin_kukutool import (
    get_kukutool_provider_health,
    set_kukutool_admin_disabled,
)
from .services.platforms.gallerydl_generic import GalleryDlGenericProvider
from .services.platforms.kukutool_generic import KukutoolGenericProvider
from .services.platforms.third_party import build_third_party_rollout_status
from .services.platforms.third_party import THIRD_PARTY_PLATFORM_RULES
from .services.platforms.ytdlp_generic import YtDlpGenericProvider
from .services.provider_access import (
    PROVIDER_DOUYIN_ULTRA,
    PROVIDER_THIRD_PARTY,
    PROVIDER_YOUTUBE_MERGE,
    provider_access_summary,
    provider_is_allowed_for_request,
    third_party_public_access_summary,
)
from .services.registry import get_adapter_for_url, parse_media_page
from .services.provider_manifest import build_provider_manifest_token
from .services.observability import (
    provider_runtime_snapshot,
    record_download_request_log,
    record_parse_request_log,
    record_provider_admin_audit_log,
)
from .services.youtube_merge import get_youtube_merge_health
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
PREVIEW_MAX_IMAGE_BYTES = 8 * 1024 * 1024
SIGNED_THIRD_PARTY_VIDEO_PROVIDERS = {
    "kukutool_generic",
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


def build_third_party_provider_summary(kukutool_health, gallerydl_health, ytdlp_health):
    kukutool_enabled = bool(kukutool_health.get("provider_enabled"))
    kukutool_circuit_open = bool((kukutool_health.get("circuit") or {}).get("open"))
    provider_states = {
        "kukutool_generic": {
            "enabled": kukutool_enabled,
            "ready": bool(kukutool_enabled and not kukutool_circuit_open),
            "issue": "circuit_open" if kukutool_circuit_open else "",
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


def build_provider_dashboard_rows(runtime_setting, request):
    kukutool_health = get_kukutool_provider_health()
    third_party_health = KukutoolGenericProvider().health()
    gallerydl_health = GalleryDlGenericProvider().health()
    ytdlp_health = YtDlpGenericProvider().health()
    youtube_health = get_youtube_merge_health()
    third_party_summary = build_third_party_provider_summary(
        third_party_health,
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
            "description": "使用 KuKuTool 上游获取抖音超高清 / 4K 候选资源。",
            "mode_field": "douyin_ultra_mode",
            "rollout_field": "douyin_ultra_rollout_percent",
            "disabled_reason_field": "douyin_ultra_disabled_reason",
            "current_mode": runtime_setting.douyin_ultra_mode,
            "current_rollout_percent": runtime_setting.douyin_ultra_rollout_percent,
            "disabled_reason": runtime_setting.douyin_ultra_disabled_reason,
            "health_ready": bool(kukutool_health.get("available")),
            "health_label": "可用" if kukutool_health.get("available") else "不可用",
            "issue": (
                "熔断中"
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
        "youtube_merge": youtube_health,
        "third_party": third_party_summary,
    }


@csrf_protect
def provider_settings_view(request):
    if not provider_settings_admin_required(request):
        return JsonResponse({"detail": "Not found."}, status=404)

    runtime_setting = ProviderRuntimeSetting.load()
    valid_modes = {choice[0] for choice in ProviderAccessMode.choices}
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
    gallerydl_health = GalleryDlGenericProvider().health()
    ytdlp_health = YtDlpGenericProvider().health()
    health["third_party"] = third_party_health
    health["third_party_gallerydl"] = gallerydl_health
    health["third_party_ytdlp"] = ytdlp_health
    health["youtube_merge"] = get_youtube_merge_health()
    health["third_party_summary"] = build_third_party_provider_summary(
        third_party_health,
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
    gallerydl_health = GalleryDlGenericProvider().health()
    ytdlp_health = YtDlpGenericProvider().health()
    health["third_party"] = third_party_health
    health["third_party_gallerydl"] = gallerydl_health
    health["third_party_ytdlp"] = ytdlp_health
    health["youtube_merge"] = get_youtube_merge_health()
    health["third_party_summary"] = build_third_party_provider_summary(
        third_party_health,
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
    return provider_settings_admin_required(request)


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
    today_start = timezone.localtime(now).replace(hour=0, minute=0, second=0, microsecond=0)
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
        recent_parse.annotate(hour=TruncHour("created_at"))
        .values("hour")
        .annotate(count=Count("id"), avg_duration_ms=Avg("duration_ms"))
        .order_by("hour")
    )

    context = {
        "title": "运营看板",
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
        "recent_audit_logs": ProviderAdminAuditLog.objects.all()[:10],
    }
    return render(request, "admin/ops_dashboard.html", context)


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


@require_GET
def parse_logs_view(request):
    if not admin_ops_required(request):
        return JsonResponse({"detail": "Not found."}, status=404)
    filter_fields = ("status", "source_platform", "selected_provider", "is_staff", "error_type")
    queryset = filtered_log_queryset(ParseRequestLog, request, filter_fields)[:200]
    return render(
        request,
        "admin/observability_log_list.html",
        {
            "title": "解析日志",
            "nav_title": "解析日志",
            "search_placeholder": "搜索 request_id / host / error_type",
            "filter_fields": filter_fields,
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
                    "created_at": item.created_at,
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
                for item in queryset
            ],
        },
    )


@require_GET
def download_logs_view(request):
    if not admin_ops_required(request):
        return JsonResponse({"detail": "Not found."}, status=404)
    filter_fields = ("status", "download_type", "provider", "source_platform", "error_type")
    queryset = filtered_log_queryset(DownloadRequestLog, request, filter_fields)[:200]
    return render(
        request,
        "admin/observability_log_list.html",
        {
            "title": "下载日志",
            "nav_title": "下载日志",
            "search_placeholder": "搜索 request_id / provider / error_type",
            "filter_fields": filter_fields,
            "headers": (
                "created_at",
                "status",
                "download_type",
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
                    "created_at": item.created_at,
                    "status": item.status,
                    "download_type": item.download_type,
                    "item_count": item.item_count,
                    "actual_bytes": format_file_size(item.actual_bytes),
                    "provider": item.provider,
                    "source_platform": item.source_platform,
                    "duration_ms": item.duration_ms,
                    "error_type": item.error_type,
                    "request_id": item.request_id,
                }
                for item in queryset
            ],
        },
    )


@require_GET
def provider_audit_logs_view(request):
    if not admin_ops_required(request):
        return JsonResponse({"detail": "Not found."}, status=404)
    filter_fields = ("action", "actor_username")
    queryset = filtered_log_queryset(ProviderAdminAuditLog, request, filter_fields)[:200]
    return render(
        request,
        "admin/observability_log_list.html",
        {
            "title": "Provider 审计",
            "nav_title": "Provider 审计",
            "search_placeholder": "搜索 request_id / actor / action",
            "filter_fields": filter_fields,
            "headers": ("created_at", "actor_username", "action", "changed_fields", "request_id"),
            "rows": [
                {
                    "created_at": item.created_at,
                    "actor_username": item.actor_username,
                    "action": item.action,
                    "changed_fields": ", ".join(item.changed_fields or ()),
                    "request_id": item.request_id,
                }
                for item in queryset
            ],
        },
    )


def limit_error_response(message, retry_after=0):
    response = JsonResponse({"status": "error", "message": message}, status=429)
    if retry_after:
        response["Retry-After"] = str(retry_after)
    return response


def check_download_request_limits(request):
    identity = get_client_identity(request)
    checks = (
        lambda: consume_fixed_window(
            "download-minute-ip",
            identity,
            settings.DOWNLOAD_RATE_LIMIT_PER_MINUTE,
            60,
        ),
        lambda: consume_daily(
            "download-requests-ip",
            identity,
            settings.DOWNLOAD_DAILY_REQUEST_LIMIT_PER_IP,
        ),
        lambda: consume_daily(
            "download-requests-global",
            "all",
            settings.DOWNLOAD_GLOBAL_DAILY_REQUEST_LIMIT,
        ),
        lambda: has_daily_capacity(
            "download-bytes-ip",
            identity,
            settings.DOWNLOAD_DAILY_BYTES_LIMIT_PER_IP,
        ),
        lambda: has_daily_capacity(
            "download-bytes-global",
            "all",
            settings.DOWNLOAD_GLOBAL_DAILY_BYTES_LIMIT,
        ),
    )
    for check in checks:
        result = check()
        if not result.allowed:
            return identity, limit_error_response(
                "下载请求过于频繁或今日流量已达到限制，请稍后再试。",
                result.retry_after,
            )
    return identity, None


def record_download_archive_bytes(identity, archive_file):
    try:
        archive_size = Path(archive_file.name).stat().st_size
    except (AttributeError, OSError):
        archive_size = 0
    if archive_size <= 0:
        return True

    per_ip = consume_daily(
        "download-bytes-ip",
        identity,
        settings.DOWNLOAD_DAILY_BYTES_LIMIT_PER_IP,
        amount=archive_size,
    )
    if not per_ip.allowed:
        return False
    global_result = consume_daily(
        "download-bytes-global",
        "all",
        settings.DOWNLOAD_GLOBAL_DAILY_BYTES_LIMIT,
        amount=archive_size,
    )
    return global_result.allowed


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
):
    duration_ms = (time.monotonic() - started_at) * 1000 if started_at else 0
    record_download_request_log(
        request,
        download_type=download_type,
        media_items=media_items or (),
        status=status,
        actual_bytes=actual_bytes,
        duration_ms=duration_ms,
        error_code=error_code,
        error_type=type(error).__name__ if error is not None else "",
        error_message_short=str(error) if error is not None else "",
    )


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
        extra["error"] = str(error)[:300]
    elif error_message_short:
        extra["error_type"] = error_code
        extra["error"] = error_message_short[:300]
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
        error_message_short=error_message_short or (str(error) if error is not None else ""),
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
        context["platform"] = result.platform
        target_platform_key = get_platform_key_for_result(result.platform, result_metadata)
        if (
            platform_key != "home"
            and target_platform_key
            and target_platform_key != platform_key
        ):
            context["target_page"] = get_page_payload(target_platform_key)
        context["result_theme"] = get_result_theme(
            platform_key,
            result.platform,
            result_metadata,
        )
        context["platform_name"] = (
            result_metadata.get("source_platform_name")
            or PLATFORM_DISPLAY_NAMES.get(result.platform, result.platform or "未知平台")
        )
        if context["page_platform_id"] and context["page_platform_id"] != result.platform:
            context["platform_notice"] = f"你粘贴的是{context['platform_name']}链接，已自动使用对应解析器。"
        if result.platform == "third_party":
            provider_label = result_metadata.get("provider_label") or "第三方 Provider"
            context["provider_notice"] = f"本次由 {provider_label} 第三方解析，资源可用性取决于上游平台。"
            if result_metadata.get("selected_provider") == "youtube_merge":
                context["provider_notice"] = "本次使用 YouTube 最高画质任务；下载时将在服务端合并音频和视频，文件可能更大。"
            elif result_metadata.get("selected_provider") == "yt_dlp_merge":
                context["provider_notice"] = "本次使用 yt-dlp 高清合并；默认优先 1080p，下载时将在服务端合并音频和视频。"
        if prefer_ultra_hd:
            if result.platform == "douyin":
                context["provider_notice"] = "本次优先使用 KuKuTool 超高清上游；若不可用会自动回退本地解析。"
            else:
                context["provider_notice"] = "超高清 Provider 目前仅用于抖音视频，本次已按对应平台本地解析。"
        context["post_type"] = result.post_type
        context["base_filename"] = result.base_filename
        context["image_items"] = [
            {
                **asset.to_download_item(),
                "type": "image",
                "url": asset.url,
                "encoded_url": encode_url(asset.url),
                "encoded_preview_url": encode_url(asset.preview_url or asset.url),
                "preview_provider_query": build_provider_preview_query(
                    asset,
                    asset.preview_url or asset.url,
                ),
            }
            for asset in result.image_assets
        ]
        context["video_items"] = [
            {
                **asset.to_download_item(),
                "type": "video",
                "video_url": asset.url,
                "cover_url": asset.cover_url,
                "size_bytes": (size_bytes := get_video_size_bytes(asset)),
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
            for asset in result.video_assets
        ]

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
        data = json.loads(request.body.decode("utf-8"))
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
    image_urls = data.get("image_urls", [])
    if image_urls and not media_items:
        media_items = [{"type": "image", "url": url} for url in image_urls]
    if download_items_have_invalid_provider_manifest(media_items):
        log_download_request_event(
            request,
            download_type="zip",
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
        )
        return JsonResponse(
            {"status": "error", "message": "下载保护服务暂时不可用，请稍后再试。"},
            status=503,
        )
    if limit_response:
        log_download_request_event(
            request,
            download_type="zip",
            media_items=media_items,
            status=ObservabilityStatus.RATE_LIMITED,
            started_at=started_at,
            error_code="RATE_LIMITED",
        )
        return limit_response

    try:
        concurrency = acquire_concurrency(
            "download-concurrent",
            identity,
            settings.DOWNLOAD_MAX_CONCURRENT_PER_IP,
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
        )
        return JsonResponse(
            {"status": "error", "message": "下载保护服务暂时不可用，请稍后再试。"},
            status=503,
        )
    if not concurrency.allowed:
        log_download_request_event(
            request,
            download_type="zip",
            media_items=media_items,
            status=ObservabilityStatus.RATE_LIMITED,
            started_at=started_at,
            error_code="RATE_LIMITED",
        )
        return limit_error_response("当前已有下载任务正在处理，请等待完成后再试。")

    try:
        download_mode = (data.get("download_mode") or "browser").lower().strip()
        if download_mode == "server":
            if not settings.XHS_ALLOW_SERVER_SAVE:
                log_download_request_event(
                    request,
                    download_type="server",
                    media_items=media_items,
                    status=ObservabilityStatus.BLOCKED,
                    started_at=started_at,
                    error_code="SERVER_SAVE_DISABLED",
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
            )
            return JsonResponse({
                "status": "success",
                "message": f"服务端保存完成：成功 {result['saved_count']} 个，失败 {result['failed_count']} 个",
                **result,
            })

        archive = build_download_archive_file(
            media_items=media_items,
            base_filename=data.get("base_filename", "xhs"),
            save_format=data.get("save_format", "original"),
            quality=data.get("quality", 95),
            video_save_mode=data.get("video_save_mode", "video"),
        )
        try:
            archive_size = file_size_for_observability(archive["file"])
            within_limit = record_download_archive_bytes(identity, archive["file"])
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
                error_code="RATE_LIMITED",
            )
            return limit_error_response("今日下载流量已达到限制，请稍后再试。")
    except XhsDownloadError as error:
        error_code = (
            "PROVIDER_MANIFEST_REJECTED"
            if "Provider 签名" in str(error) or "manifest" in str(error).lower()
            else "DOWNLOAD_ERROR"
        )
        log_download_request_event(
            request,
            download_type="zip",
            media_items=media_items,
            status=ObservabilityStatus.FAILED,
            started_at=started_at,
            error_code=error_code,
            error=error,
        )
        return JsonResponse({"status": "error", "message": str(error)}, status=400)
    except Exception as error:
        logger.exception("XHS download failed")
        log_download_request_event(
            request,
            download_type="zip",
            media_items=media_items,
            status=ObservabilityStatus.FAILED,
            started_at=started_at,
            error_code="DOWNLOAD_ERROR",
            error=error,
        )
        return JsonResponse({"status": "error", "message": f"下载失败：{error}"}, status=500)
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
        log_download_request_event(
            request,
            download_type="task_create",
            media_items=media_items,
            status=ObservabilityStatus.RATE_LIMITED,
            started_at=started_at,
            error_code="RATE_LIMITED",
        )
        return limit_response

    try:
        concurrency = acquire_concurrency(
            "download-concurrent",
            identity,
            settings.DOWNLOAD_MAX_CONCURRENT_PER_IP,
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
            error_code="RATE_LIMITED",
        )
        return limit_error_response("当前已有下载任务正在处理，请等待完成后再试。")

    try:
        task = create_download_task({
            "media_items": media_items,
            "base_filename": data.get("base_filename", "xhs"),
            "save_format": data.get("save_format", "original"),
            "quality": data.get("quality", 95),
            "video_save_mode": data.get("video_save_mode", "video"),
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
