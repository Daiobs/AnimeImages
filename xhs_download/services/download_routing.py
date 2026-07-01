from urllib.parse import urlparse

from download_console.models import DownloadRuntimeSetting

from .downloads import provider_manifest_allows_public_download


ROUTE_DIRECT = "direct"
ROUTE_PROXY = "proxy"
ROUTE_ZIP = "zip"
ROUTE_BLOCKED = "blocked"
ROUTE_FALLBACK = "fallback"
ROUTE_TYPES = (
    ROUTE_DIRECT,
    ROUTE_PROXY,
    ROUTE_ZIP,
    ROUTE_BLOCKED,
    ROUTE_FALLBACK,
)

SERVER_SIDE_PROVIDERS = {
    "kukutool",
    "kukutool_generic",
    "youtube_merge",
    "yt_dlp_merge",
}
MOBILE_USER_AGENT_MARKERS = (
    "android",
    "iphone",
    "ipad",
    "ipod",
    "mobile",
    "windows phone",
)
MOBILE_DOWNLOAD_WARNING = (
    "手机端建议单张/单个保存。ZIP 文件通常会保存到浏览器下载目录或系统“文件”App 中，不能保证直接进入相册。"
)


def safe_download_size(value):
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def request_is_mobile(request):
    user_agent = str(request.META.get("HTTP_USER_AGENT", "") or "").lower()
    return any(marker in user_agent for marker in MOBILE_USER_AGENT_MARKERS)


def item_type(item):
    return str(item.get("type") or item.get("media_type") or "image").lower()


def item_url(item):
    return str(item.get("url") or item.get("video_url") or "")


def item_provider(item):
    return str(item.get("provider") or "")


def item_is_image(item):
    return item_type(item) == "image"


def item_is_video(item):
    return item_type(item) == "video"


def is_http_url(url):
    parsed = urlparse(str(url or ""))
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def estimate_media_items_bytes(media_items):
    return sum(
        safe_download_size(item.get("size_bytes"))
        for item in media_items or ()
        if isinstance(item, dict)
    )


def request_is_staff(request):
    user = getattr(request, "user", None)
    return bool(getattr(user, "is_staff", False) or getattr(user, "is_superuser", False))


def staff_can_bypass_single_download_limits(request, runtime_setting):
    return bool(
        request_is_staff(request)
        and getattr(runtime_setting, "staff_large_download_exempt", False)
    )


def download_cost_guard_block(request, media_items, runtime_setting):
    has_video = any(item_is_video(item) for item in media_items or () if isinstance(item, dict))
    if has_video and not request_is_staff(request) and not runtime_setting.visitor_video_download_enabled:
        return (
            "VISITOR_VIDEO_DOWNLOAD_DISABLED",
            "当前环境暂不允许访客下载视频资源。",
        )

    if staff_can_bypass_single_download_limits(request, runtime_setting):
        return None

    single_video_limit = safe_download_size(runtime_setting.single_video_bytes_limit)
    if single_video_limit > 0:
        for item in media_items or ():
            if not isinstance(item, dict) or not item_is_video(item):
                continue
            size_bytes = safe_download_size(item.get("size_bytes"))
            if size_bytes and size_bytes > single_video_limit:
                return (
                    "SINGLE_VIDEO_SIZE_LIMIT",
                    "视频资源超过单视频下载限制。",
                )

    single_archive_limit = safe_download_size(runtime_setting.single_archive_bytes_limit)
    estimated_bytes = estimate_media_items_bytes(media_items)
    if single_archive_limit > 0 and estimated_bytes > single_archive_limit:
        return (
            "SINGLE_ARCHIVE_SIZE_LIMIT",
            "本次资源超过单次打包下载限制。",
        )

    return None


def provider_item_can_direct_download(item):
    provider = item_provider(item)
    if not provider:
        return True
    if provider in SERVER_SIDE_PROVIDERS:
        return False
    return provider_manifest_allows_public_download(item)


def provider_item_requires_server(item):
    provider = item_provider(item)
    if not provider:
        return False
    if provider in SERVER_SIDE_PROVIDERS:
        return True
    return not provider_manifest_allows_public_download(item)


def item_can_browser_direct_video(item):
    if not isinstance(item, dict) or not item_is_video(item):
        return False
    url = item_url(item)
    if not is_http_url(url):
        return False
    return not item_provider(item)


def single_video_can_attachment_download(item):
    return item_can_browser_direct_video(item)


def single_image_can_attachment_download(item):
    if not isinstance(item, dict) or not item_is_image(item):
        return False
    if not is_http_url(item_url(item)):
        return False
    return provider_item_can_direct_download(item)


def single_item_can_attachment_download(item):
    return single_video_can_attachment_download(item) or single_image_can_attachment_download(item)


def public_route_item(item):
    allowed_keys = (
        "type",
        "url",
        "cover_url",
        "asset_id",
        "provider",
        "provider_resource_id",
        "provider_cache_key",
        "size_bytes",
        "source_platform",
        "filename",
    )
    return {
        key: item.get(key)
        for key in allowed_keys
        if isinstance(item, dict) and item.get(key) not in (None, "")
    }


def route_payload(
    *,
    route_type,
    route_reason,
    download_strategy,
    media_items,
    estimated_bytes,
    error_code="",
    warnings=None,
):
    if route_type == ROUTE_DIRECT:
        server_egress = 0
        browser_egress = estimated_bytes
    elif route_type == ROUTE_BLOCKED:
        server_egress = 0
        browser_egress = 0
    else:
        server_egress = estimated_bytes
        browser_egress = 0

    return {
        "route_type": route_type,
        "route_reason": route_reason[:200],
        "download_strategy": download_strategy,
        "estimated_bytes": estimated_bytes,
        "server_egress_estimated_bytes": server_egress,
        "browser_egress_estimated_bytes": browser_egress,
        "items": [public_route_item(item) for item in media_items or () if isinstance(item, dict)],
        "warnings": list(warnings or ()),
        "error_code": error_code,
    }


def decide_download_route(request, media_items, *, requested_mode="", platform_key=""):
    del platform_key
    normalized_items = [item for item in media_items or () if isinstance(item, dict)]
    estimated_bytes = estimate_media_items_bytes(normalized_items)
    requested_mode = str(requested_mode or "").lower().strip()
    warnings = []
    if request_is_mobile(request):
        warnings.append(MOBILE_DOWNLOAD_WARNING)

    runtime_setting = DownloadRuntimeSetting.load()
    guard_block = download_cost_guard_block(request, normalized_items, runtime_setting)
    if guard_block:
        error_code, reason = guard_block
        return route_payload(
            route_type=ROUTE_BLOCKED,
            route_reason=reason,
            download_strategy="blocked_by_download_cost_guard",
            media_items=normalized_items,
            estimated_bytes=estimated_bytes,
            error_code=error_code,
            warnings=warnings,
        )

    if not normalized_items:
        return route_payload(
            route_type=ROUTE_BLOCKED,
            route_reason="没有选择资源",
            download_strategy="blocked_empty_selection",
            media_items=normalized_items,
            estimated_bytes=0,
            error_code="NO_MEDIA_SELECTED",
            warnings=warnings,
        )

    if requested_mode in {"zip", "browser"}:
        if (
            requested_mode == "browser"
            and len(normalized_items) == 1
            and single_item_can_attachment_download(normalized_items[0])
        ):
            return route_payload(
                route_type=ROUTE_PROXY,
                route_reason="单个资源使用单文件下载，不打包 ZIP",
                download_strategy="server_single_attachment",
                media_items=normalized_items,
                estimated_bytes=estimated_bytes,
                warnings=warnings,
            )
        return route_payload(
            route_type=ROUTE_ZIP,
            route_reason="用户选择打包下载，使用服务器 ZIP 流程",
            download_strategy="server_zip",
            media_items=normalized_items,
            estimated_bytes=estimated_bytes,
            warnings=warnings,
        )

    all_images = all(item_is_image(item) for item in normalized_items)
    all_videos = all(item_is_video(item) for item in normalized_items)
    has_server_provider = any(provider_item_requires_server(item) for item in normalized_items)
    has_non_http = any(not is_http_url(item_url(item)) for item in normalized_items)

    if (
        all_images
        and len(normalized_items) == 1
        and not has_server_provider
        and not has_non_http
    ):
        return route_payload(
            route_type=ROUTE_DIRECT,
            route_reason="图片资源优先浏览器直链下载，降低服务器流量",
            download_strategy="browser_direct",
            media_items=normalized_items,
            estimated_bytes=estimated_bytes,
            warnings=warnings,
        )

    if all_images:
        return route_payload(
            route_type=ROUTE_ZIP,
            route_reason="图片数量较多或资源需要服务端处理，使用 ZIP 打包下载",
            download_strategy="server_zip",
            media_items=normalized_items,
            estimated_bytes=estimated_bytes,
            warnings=warnings,
        )

    if all_videos and len(normalized_items) == 1:
        if single_video_can_attachment_download(normalized_items[0]):
            return route_payload(
                route_type=ROUTE_PROXY,
                route_reason="单个资源使用单文件下载，不打包 ZIP",
                download_strategy="server_single_attachment",
                media_items=normalized_items,
                estimated_bytes=estimated_bytes,
                warnings=warnings,
            )
        if has_server_provider:
            return route_payload(
                route_type=ROUTE_ZIP,
                route_reason="Provider 资源需要服务端校验、恢复或合并，使用 ZIP 流程",
                download_strategy="server_zip_provider",
                media_items=normalized_items,
                estimated_bytes=estimated_bytes,
                warnings=warnings,
            )
        return route_payload(
            route_type=ROUTE_PROXY,
            route_reason="单个普通视频使用服务器代理下载，保留成本守卫和内容校验",
            download_strategy="server_proxy_hint",
            media_items=normalized_items,
            estimated_bytes=estimated_bytes,
            warnings=warnings,
        )

    return route_payload(
        route_type=ROUTE_ZIP,
        route_reason="多资源或图视频混合需要统一打包下载",
        download_strategy="server_zip",
        media_items=normalized_items,
        estimated_bytes=estimated_bytes,
        warnings=warnings,
    )
