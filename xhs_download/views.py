import json
import logging
import time
from pathlib import Path
from urllib.parse import urlencode, urlparse

import requests
from django.conf import settings
from django.http import FileResponse, HttpResponse, JsonResponse
from django.shortcuts import render
from django.template.loader import render_to_string
from django.urls import reverse
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_GET, require_POST

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
from .services.platforms.ytdlp_generic import YtDlpGenericProvider
from .services.registry import get_adapter_for_url, parse_media_page
from .services.provider_manifest import build_provider_manifest_token
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


def ultra_hd_is_available():
    if not (
        settings.ULTRA_HD_PUBLIC_ACCESS_ENABLED
        and settings.ULTRA_HD_PROVIDER_ENABLED
        and settings.KUKUTOOL_PROVIDER_ENABLED
    ):
        return False
    health = get_kukutool_provider_health()
    return health["available"]


def youtube_merge_mode_is_available(request):
    health = get_youtube_merge_health()
    if not health.get("implemented") or not health.get("ready"):
        return False
    if not health.get("public_access_enabled"):
        return False
    if health.get("staff_only"):
        user = getattr(request, "user", None)
        return bool(
            getattr(user, "is_active", False)
            and getattr(user, "is_staff", False)
        )
    return True


def request_user_is_staff(request):
    user = getattr(request, "user", None)
    return bool(
        getattr(user, "is_active", False)
        and getattr(user, "is_staff", False)
    )


def third_party_provider_is_available(request):
    if not (
        settings.THIRD_PARTY_PROVIDER_ENABLED
        and settings.THIRD_PARTY_PUBLIC_ACCESS_ENABLED
    ):
        return False
    if getattr(settings, "THIRD_PARTY_STAFF_ONLY", False):
        return request_user_is_staff(request)
    return True


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
    public_access_enabled = bool(
        getattr(settings, "THIRD_PARTY_PUBLIC_ACCESS_ENABLED", False)
    )
    staff_only = bool(getattr(settings, "THIRD_PARTY_STAFF_ONLY", False))
    issue = ""
    if global_enabled and public_access_enabled and not enabled_provider_ids:
        issue = "no_provider_enabled"
    elif global_enabled and public_access_enabled and not ready_provider_ids:
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
            and public_access_enabled
            and not staff_only
            and ready_provider_ids
        ),
        "staff_ready": bool(
            global_enabled and public_access_enabled and ready_provider_ids
        ),
        "enabled_provider_ids": enabled_provider_ids,
        "ready_provider_ids": ready_provider_ids,
        "provider_states": provider_states,
        "issue": issue,
        "rollout": rollout,
    }


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
    response = JsonResponse(health)
    response["Cache-Control"] = "no-store"
    return response


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
):
    parsed = urlparse(str(query or ""))
    metadata = getattr(result, "metadata", None) or {}
    image_count = len(context.get("image_items", ())) if context else 0
    video_count = len(context.get("video_items", ())) if context else 0
    extra = {
        "page_platform": platform_key,
        "query_host": parsed.hostname or "",
        "requested_ultra_hd": bool(requested_ultra_hd),
        "prefer_ultra_hd": bool(prefer_ultra_hd),
        "requested_youtube_merge": bool(requested_youtube_merge),
        "prefer_youtube_merge": bool(prefer_youtube_merge),
        "duration_ms": int(duration_ms),
        "platform": getattr(result, "platform", "") if result else "",
        "post_type": getattr(result, "post_type", "") if result else "",
        "source_platform": metadata.get("source_platform", ""),
        "selected_provider": metadata.get("selected_provider", ""),
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
        "ultra_hd_available": ultra_hd_is_available(),
        "youtube_merge_mode_available": (
            youtube_merge_mode_is_available(request) if request is not None else False
        ),
    }


def build_search_results_context(request, platform_key="home"):
    query = request.GET.get("q", "").strip()
    requested_ultra_hd = request_flag(request.GET.get("ultra_hd"))
    prefer_ultra_hd = requested_ultra_hd and ultra_hd_is_available()
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

    if query_uses_third_party_provider(query):
        if not third_party_provider_is_available(request):
            if (
                settings.THIRD_PARTY_PROVIDER_ENABLED
                and settings.THIRD_PARTY_PUBLIC_ACCESS_ENABLED
                and getattr(settings, "THIRD_PARTY_STAFF_ONLY", False)
            ):
                context["error_message"] = "第三方通用解析暂未向该用户开放。"
                return context, 403
            context["error_message"] = "第三方通用解析暂未开放。"
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
            return context, 503
        if not limit.allowed:
            context["error_message"] = "第三方解析请求过于频繁，请稍后再试。"
            context["retry_after"] = limit.retry_after
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
            return context, 503
        if not global_limit.allowed:
            context["error_message"] = "第三方解析当前排队请求较多，请稍后再试。"
            context["retry_after"] = global_limit.retry_after
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
            return context, 429
    elif requested_ultra_hd:
        context["provider_notice"] = "当前环境未向访客开放超高清 Provider，本次使用本地解析。"

    if requested_youtube_merge and not prefer_youtube_merge:
        context["provider_notice"] = "当前环境未向该用户开放 YouTube 最高画质，本次默认优先 1080p。"

    parse_started_at = time.monotonic()
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
            "media_parse_failed",
            query=query,
            platform_key=platform_key,
            requested_ultra_hd=requested_ultra_hd,
            prefer_ultra_hd=prefer_ultra_hd,
            requested_youtube_merge=requested_youtube_merge,
            prefer_youtube_merge=prefer_youtube_merge,
            duration_ms=(time.monotonic() - parse_started_at) * 1000,
            error=error,
        )
    except XhsError as error:
        context["error_message"] = f"解析失败：{error}"
        log_parse_request_event(
            "media_parse_failed",
            query=query,
            platform_key=platform_key,
            requested_ultra_hd=requested_ultra_hd,
            prefer_ultra_hd=prefer_ultra_hd,
            requested_youtube_merge=requested_youtube_merge,
            prefer_youtube_merge=prefer_youtube_merge,
            duration_ms=(time.monotonic() - parse_started_at) * 1000,
            error=error,
        )
    except requests.RequestException as error:
        context["error_message"] = f"请求页面失败：{error}"
        log_parse_request_event(
            "media_parse_failed",
            query=query,
            platform_key=platform_key,
            requested_ultra_hd=requested_ultra_hd,
            prefer_ultra_hd=prefer_ultra_hd,
            requested_youtube_merge=requested_youtube_merge,
            prefer_youtube_merge=prefer_youtube_merge,
            duration_ms=(time.monotonic() - parse_started_at) * 1000,
            error=error,
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
    try:
        data = json.loads(request.body.decode("utf-8"))
    except json.JSONDecodeError:
        return JsonResponse({"status": "error", "message": "请求数据不是有效 JSON。"}, status=400)

    media_items = data.get("media_items", [])
    image_urls = data.get("image_urls", [])
    if image_urls and not media_items:
        media_items = [{"type": "image", "url": url} for url in image_urls]

    try:
        identity, limit_response = check_download_request_limits(request)
    except Exception:
        logger.exception("Download rate-limit cache failed")
        return JsonResponse(
            {"status": "error", "message": "下载保护服务暂时不可用，请稍后再试。"},
            status=503,
        )
    if limit_response:
        return limit_response

    try:
        concurrency = acquire_concurrency(
            "download-concurrent",
            identity,
            settings.DOWNLOAD_MAX_CONCURRENT_PER_IP,
        )
    except Exception:
        logger.exception("Download concurrency cache failed")
        return JsonResponse(
            {"status": "error", "message": "下载保护服务暂时不可用，请稍后再试。"},
            status=503,
        )
    if not concurrency.allowed:
        return limit_error_response("当前已有下载任务正在处理，请等待完成后再试。")

    try:
        download_mode = (data.get("download_mode") or "browser").lower().strip()
        if download_mode == "server":
            if not settings.XHS_ALLOW_SERVER_SAVE:
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
            within_limit = record_download_archive_bytes(identity, archive["file"])
        except Exception:
            archive["file"].close()
            raise
        if not within_limit:
            archive["file"].close()
            return limit_error_response("今日下载流量已达到限制，请稍后再试。")
    except XhsDownloadError as error:
        return JsonResponse({"status": "error", "message": str(error)}, status=400)
    except Exception as error:
        logger.exception("XHS download failed")
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
    return response


@require_POST
@csrf_protect
def create_download_task_api(request):
    if not settings.DOWNLOAD_ASYNC_TASKS_ENABLED:
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
        return JsonResponse({"status": "error", "message": "请求数据不是有效 JSON。"}, status=400)

    media_items = data.get("media_items", [])
    if not media_items:
        return JsonResponse({"status": "error", "message": "没有选择资源"}, status=400)

    try:
        identity, limit_response = check_download_request_limits(request)
    except Exception:
        logger.exception("Download rate-limit cache failed")
        return JsonResponse(
            {"status": "error", "message": "下载保护服务暂时不可用，请稍后再试。"},
            status=503,
        )
    if limit_response:
        return limit_response

    try:
        concurrency = acquire_concurrency(
            "download-concurrent",
            identity,
            settings.DOWNLOAD_MAX_CONCURRENT_PER_IP,
        )
    except Exception:
        logger.exception("Download concurrency cache failed")
        return JsonResponse(
            {"status": "error", "message": "下载保护服务暂时不可用，请稍后再试。"},
            status=503,
        )
    if not concurrency.allowed:
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
        raise
    task_id = task["task_id"]
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
    task = get_download_task(task_id)
    if not task:
        return JsonResponse({"status": "error", "message": "下载任务不存在或已过期"}, status=404)
    if task.get("status") != "completed":
        return JsonResponse({"status": "error", "message": "下载任务尚未完成"}, status=409)

    task = pop_download_task(task_id)
    if not task:
        return JsonResponse({"status": "error", "message": "下载任务不存在或已过期"}, status=404)

    archive_file = task["file"]
    archive_file.seek(0)
    response = FileResponse(
        archive_file,
        as_attachment=True,
        filename=task.get("filename") or "media.zip",
        content_type="application/zip",
    )
    response["X-Saved-Count"] = str(task.get("saved_count", 0))
    response["X-Failed-Count"] = str(task.get("failed_count", 0))
    try:
        response["Content-Length"] = str(Path(archive_file.name).stat().st_size)
    except Exception:
        pass
    return response
