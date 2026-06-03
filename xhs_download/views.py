import json
import logging

import requests
from django.conf import settings
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.utils.http import content_disposition_header
from django.views.decorators.http import require_GET, require_POST

from .services.common import (
    HEADERS,
    XhsDownloadError,
    XhsError,
    decode_url,
    encode_url,
    request_get_with_retry,
    validate_media_url,
)
from .services.downloads import build_download_archive, save_media_items_to_server
from .services.parser import parse_xhs_page


logger = logging.getLogger(__name__)


def search_view(request):
    return render(request, "xhs_download.html", get_download_settings_context())


def get_download_settings_context():
    return {
        "xhs_allow_server_save": settings.XHS_ALLOW_SERVER_SAVE,
        "xhs_allow_absolute_save_path": settings.XHS_ALLOW_ABSOLUTE_SAVE_PATH,
        "xhs_default_server_save_dir": settings.XHS_DEFAULT_SERVER_SAVE_DIR,
    }


def search_results_view(request):
    query = request.GET.get("q", "").strip()

    context = {
        **get_download_settings_context(),
        "query": query,
        "post_type": "",
        "image_items": [],
        "video_items": [],
        "error_message": None,
        "base_filename": "",
    }

    if not query:
        return render(request, "xhs_download.html", context)

    try:
        result = parse_xhs_page(query)
        context["post_type"] = result["post_type"]
        context["base_filename"] = result["base_filename"]
        context["image_items"] = [
            {
                "type": "image",
                "url": url,
                "encoded_url": encode_url(url),
            }
            for url in result["image_urls"]
        ]
        context["video_items"] = [
            {
                "type": "video",
                "video_url": item["video_url"],
                "cover_url": item["cover_url"],
                "encoded_cover_url": encode_url(item["cover_url"]) if item["cover_url"] else "",
            }
            for item in result["video_items"]
        ]

        if not context["image_items"] and not context["video_items"]:
            context["error_message"] = "没有解析到图片或视频。"

    except XhsError as error:
        context["error_message"] = f"解析失败：{error}"
    except requests.RequestException as error:
        context["error_message"] = f"请求小红书页面失败：{error}"

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
        response = request_get_with_retry(
            image_url,
            headers=HEADERS,
            timeout=15,
            retries=3,
        )

        content_type = response.headers.get("Content-Type", "image/jpeg")
        if "image" not in content_type.lower():
            return placeholder_image_response()

        result = HttpResponse(response.content, content_type=content_type)
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
                save_format=data.get("save_format", "jpg"),
                quality=data.get("quality", 95),
                video_save_mode=data.get("video_save_mode", "video"),
                allow_absolute=settings.XHS_ALLOW_ABSOLUTE_SAVE_PATH,
            )
            return JsonResponse({
                "status": "success",
                "message": f"服务端保存完成：成功 {result['saved_count']} 个，失败 {result['failed_count']} 个",
                **result,
            })

        archive = build_download_archive(
            media_items=media_items,
            base_filename=data.get("base_filename", "xhs"),
            save_format=data.get("save_format", "jpg"),
            quality=data.get("quality", 95),
            video_save_mode=data.get("video_save_mode", "video"),
        )
    except XhsDownloadError as error:
        return JsonResponse({"status": "error", "message": str(error)}, status=400)
    except Exception as error:
        logger.exception("XHS download failed")
        return JsonResponse({"status": "error", "message": f"下载失败：{error}"}, status=500)

    response = HttpResponse(archive["content"], content_type="application/zip")
    response["Content-Disposition"] = content_disposition_header(True, archive["filename"])
    response["X-Saved-Count"] = str(archive["saved_count"])
    response["X-Failed-Count"] = str(archive["failed_count"])
    return response
