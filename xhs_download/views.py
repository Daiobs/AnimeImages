import json
import logging

import requests
from django.conf import settings
from django.http import FileResponse, HttpResponse, JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST

from .services.common import (
    XhsDownloadError,
    XhsError,
    decode_url,
    encode_url,
    validate_media_url,
)
from .services.downloads import (
    build_download_archive_file,
    fetch_media_response,
    read_limited_response,
    save_media_items_to_server,
)
from .services.registry import parse_media_page


logger = logging.getLogger(__name__)
PREVIEW_MAX_IMAGE_BYTES = 8 * 1024 * 1024


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
        "xhs_allow_server_save": settings.XHS_ALLOW_SERVER_SAVE,
        "xhs_allow_absolute_save_path": settings.XHS_ALLOW_ABSOLUTE_SAVE_PATH,
        "xhs_default_server_save_dir": settings.XHS_DEFAULT_SERVER_SAVE_DIR,
    }


def search_results_view(request, platform_key="home"):
    query = request.GET.get("q", "").strip()

    context = {
        **get_download_settings_context(platform_key),
        "query": query,
        "platform": "",
        "platform_name": "",
        "platform_notice": "",
        "post_type": "",
        "image_items": [],
        "video_items": [],
        "error_message": None,
        "base_filename": "",
    }

    if not query:
        return render(request, "xhs_download.html", context)

    try:
        result = parse_media_page(query)
        context["platform"] = result.platform
        context["platform_name"] = PLATFORM_DISPLAY_NAMES.get(result.platform, result.platform or "未知平台")
        if context["page_platform_id"] and context["page_platform_id"] != result.platform:
            context["platform_notice"] = f"你粘贴的是{context['platform_name']}链接，已自动使用对应解析器。"
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


def placeholder_image_response():
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
    response = HttpResponse(svg, content_type="image/svg+xml")
    response["Cache-Control"] = "no-store"
    return response


@require_GET
def proxy_image_view(request):
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

        result = HttpResponse(content, content_type=content_type)
        result["Cache-Control"] = "private, max-age=300"
        return result

    except Exception as error:
        logger.warning("XHS preview proxy failed: %s", error)
        return placeholder_image_response()


@require_POST
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
    except XhsDownloadError as error:
        return JsonResponse({"status": "error", "message": str(error)}, status=400)
    except Exception as error:
        logger.exception("XHS download failed")
        return JsonResponse({"status": "error", "message": f"下载失败：{error}"}, status=500)

    response = FileResponse(
        archive["file"],
        as_attachment=True,
        filename=archive["filename"],
        content_type="application/zip",
    )
    response["X-Saved-Count"] = str(archive["saved_count"])
    response["X-Failed-Count"] = str(archive["failed_count"])
    return response
