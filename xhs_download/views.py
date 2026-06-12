import json
import logging
from pathlib import Path

import requests
from django.conf import settings
from django.http import FileResponse, HttpResponse, JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_GET, require_POST

from .services.common import (
    XhsDownloadError,
    XhsError,
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
    probe_media_size,
    read_limited_response,
    save_media_items_to_server,
)
from .services.platforms.douyin_kukutool import find_node_runtime
from .services.registry import parse_media_page
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
    return probe_media_size(asset.url)


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
        "results_route": "bilibili_results",
    },
}

PLATFORM_DISPLAY_NAMES = {
    "xiaohongshu": "小红书",
    "douyin": "抖音",
    "bilibili": "哔哩哔哩",
}


def request_flag(value):
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def ultra_hd_is_available():
    return (
        settings.ULTRA_HD_PROVIDER_ENABLED
        and settings.ULTRA_HD_PUBLIC_ACCESS_ENABLED
        and settings.KUKUTOOL_PROVIDER_ENABLED
        and bool(find_node_runtime(settings.KUKUTOOL_NODE_BINARY))
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


def search_view(request, platform_key="home"):
    return render(request, "xhs_download.html", get_download_settings_context(platform_key))


def get_download_settings_context(platform_key="home"):
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
    }


def search_results_view(request, platform_key="home"):
    query = request.GET.get("q", "").strip()
    requested_ultra_hd = request_flag(request.GET.get("ultra_hd"))
    prefer_ultra_hd = requested_ultra_hd and ultra_hd_is_available()

    context = {
        **get_download_settings_context(platform_key),
        "query": query,
        "prefer_ultra_hd": prefer_ultra_hd,
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
        return render(request, "xhs_download.html", context)

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
            response = render(request, "xhs_download.html", context, status=429)
            response["Retry-After"] = str(limit.retry_after)
            return response
    elif requested_ultra_hd:
        context["provider_notice"] = "当前环境未向访客开放超高清 Provider，本次使用本地解析。"

    try:
        client_country_code = (
            get_request_country_code(request)
            if prefer_ultra_hd
            else ""
        )
        parse_options = {"prefer_ultra_hd": prefer_ultra_hd}
        if client_country_code:
            parse_options["client_country_code"] = client_country_code
        result = parse_media_page(query, **parse_options)
        context["platform"] = result.platform
        context["platform_name"] = PLATFORM_DISPLAY_NAMES.get(result.platform, result.platform or "未知平台")
        if context["page_platform_id"] and context["page_platform_id"] != result.platform:
            context["platform_notice"] = f"你粘贴的是{context['platform_name']}链接，已自动使用对应解析器。"
        if prefer_ultra_hd:
            if result.platform == "douyin":
                context["provider_notice"] = "本次优先使用 KuKuTool 超高清上游；若不可用会自动回退本地解析。"
            else:
                context["provider_notice"] = "超高清 Provider 目前仅用于抖音视频，本次已按对应平台本地解析。"
        context["post_type"] = result.post_type
        context["base_filename"] = result.base_filename
        context["image_items"] = [
            {
                "type": "image",
                "url": asset.url,
                "encoded_url": encode_url(asset.url),
                "encoded_preview_url": encode_url(asset.preview_url or asset.url),
            }
            for asset in result.image_assets
        ]
        context["video_items"] = [
            {
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
            }
            for asset in result.video_assets
        ]

        if not context["image_items"] and not context["video_items"]:
            context["error_message"] = "没有解析到图片或视频。"

    except XhsError as error:
        context["error_message"] = f"解析失败：{error}"
    except requests.RequestException as error:
        context["error_message"] = f"请求页面失败：{error}"

    return render(request, "xhs_download.html", context)


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

    try:
        image_url = decode_url(encoded_url)
        validate_media_url(image_url)
        response = fetch_media_response(
            image_url,
            timeout=15,
        )

        with response:
            content_type = response.headers.get("Content-Type", "image/jpeg")
            if "image" not in content_type.lower():
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
