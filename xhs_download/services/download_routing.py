from urllib.parse import urlparse

from download_console.models import DownloadRuntimeSetting
from ..models import KukuToolDownloadDeliveryMode, ProviderRuntimeSetting

from .downloads import provider_manifest_allows_public_download
from .provider_strategy import BROWSER_DIRECT_FETCH_SAFE_PROVIDERS
from .platforms.xhs_tiantian_media_policy import (
    xhs_tiantian_manifest_item_is_allowed,
)
from .platforms.xiaohongshu import (
    xhs_native_video_url_is_browser_streaming_allowed,
)


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
    "kukutool_generic",
    "youtube_merge",
    "yt_dlp_merge",
}
BROWSER_DIRECT_FETCH_SAFE_VIDEO_PROVIDERS = BROWSER_DIRECT_FETCH_SAFE_PROVIDERS
BROWSER_DIRECT_VIDEO_PROVIDERS = BROWSER_DIRECT_FETCH_SAFE_VIDEO_PROVIDERS
BROWSER_FIRST_VIDEO_PROVIDERS = {
    "kukutool",
    "tiantianjiexi",
}
SERVER_PROXY_VIDEO_PROVIDERS = {
    "kukutool",
    "tiantianjiexi",
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
    "手机端建议单张图片或单个视频保存。ZIP 文件通常会进入浏览器下载目录或系统“文件”App，不能保证直接进入相册。"
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


def item_is_kukutool_video(item):
    return item_provider(item) == "kukutool" and item_is_video(item)


def item_is_browser_first_provider_video(item):
    return item_provider(item) in BROWSER_FIRST_VIDEO_PROVIDERS and item_is_video(item)


def item_is_image(item):
    return item_type(item) == "image"


def item_is_video(item):
    return item_type(item) == "video"


def item_is_upstream(item):
    return isinstance(item, dict) and str(item.get("asset_id") or "").startswith("upstream:")


def item_is_xhs_zero_relay_image(item):
    return bool(
        isinstance(item, dict)
        and item_is_image(item)
        and (
            str(item.get("source_platform") or "").strip().lower()
            == "xiaohongshu"
            or item_provider(item) == "parsehub_xhs_images"
        )
    )


def selection_has_xhs_zero_relay_image(items):
    normalized = [item for item in items or () if isinstance(item, dict)]
    return any(item_is_xhs_zero_relay_image(item) for item in normalized)


def item_is_xhs_native_browser_video(item):
    return bool(
        isinstance(item, dict)
        and item_is_video(item)
        and not item_provider(item)
        and str(item.get("source_platform") or "").strip().lower()
        == "xiaohongshu"
        and xhs_native_video_url_is_browser_streaming_allowed(item_url(item))
    )


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
    if item_is_upstream(item):
        return False
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
    provider = item_provider(item)
    if item_is_upstream(item):
        return provider_manifest_allows_public_download(item)
    if item_is_xhs_native_browser_video(item):
        return True
    if (
        provider == "tiantianjiexi_generic"
        and xhs_tiantian_manifest_item_is_allowed(item)
    ):
        return True
    return (
        provider in BROWSER_DIRECT_VIDEO_PROVIDERS
        and provider in BROWSER_DIRECT_FETCH_SAFE_VIDEO_PROVIDERS
        and provider_manifest_allows_public_download(item)
    )


def single_video_can_server_proxy_download(item):
    if item_is_upstream(item):
        return False
    if not isinstance(item, dict) or not item_is_video(item):
        return False
    if not is_http_url(item_url(item)):
        return False
    if item_can_browser_direct_video(item):
        return False
    provider = item_provider(item)
    if not provider:
        return True
    return (
        provider in SERVER_PROXY_VIDEO_PROVIDERS
        and provider_manifest_allows_public_download(item)
    )


def single_video_can_attachment_download(item):
    if item_is_upstream(item):
        return False
    if not isinstance(item, dict) or not item_is_video(item):
        return False
    if not is_http_url(item_url(item)):
        return False
    return not item_provider(item) and not item_can_browser_direct_video(item)


def single_video_can_browser_direct_download(item):
    return item_can_browser_direct_video(item)


def kukutool_browser_first_enabled(runtime_setting):
    mode = str(
        getattr(
            runtime_setting,
            "kukutool_download_delivery_mode",
            KukuToolDownloadDeliveryMode.SERVER_STREAM,
        )
        or KukuToolDownloadDeliveryMode.SERVER_STREAM
    )
    return mode == KukuToolDownloadDeliveryMode.BROWSER_FIRST


def single_video_can_browser_direct_attempt(item):
    if not isinstance(item, dict) or not item_is_video(item):
        return False
    if not is_http_url(item_url(item)):
        return False
    return single_video_can_browser_direct_download(item)


def single_video_prefers_browser_direct(item, runtime_setting=None):
    if single_video_can_browser_direct_attempt(item):
        return True
    if not isinstance(item, dict) or not item_is_browser_first_provider_video(item):
        return False
    if not is_http_url(item_url(item)):
        return False
    runtime_setting = runtime_setting or ProviderRuntimeSetting.load()
    return (
        item_provider(item) in BROWSER_DIRECT_FETCH_SAFE_VIDEO_PROVIDERS
        and kukutool_browser_first_enabled(runtime_setting)
        and provider_manifest_allows_public_download(item)
    )


def single_image_can_attachment_download(item):
    if item_is_upstream(item):
        return False
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
        "size_is_estimate",
        "source_platform",
        "filename",
        "browser_direct_fetch_safe",
        "requires_merge",
        "requires_cookie",
    )
    public_item = {
        key: item.get(key)
        for key in allowed_keys
        if isinstance(item, dict) and item.get(key) not in (None, "")
    }
    if isinstance(item, dict):
        if item_is_upstream(item):
            public_item["browser_upstream_bound"] = provider_manifest_allows_public_download(item)
        browser_direct_fetch_safe = item_can_browser_direct_video(item)
        public_item["browser_direct_fetch_safe"] = browser_direct_fetch_safe
        if browser_direct_fetch_safe:
            public_item["requires_merge"] = False
            public_item["requires_cookie"] = False
    return public_item


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
    if any(item_is_upstream(item) for item in media_items or ()) and route_type not in {ROUTE_DIRECT, ROUTE_BLOCKED}:
        route_type = ROUTE_BLOCKED
        route_reason = "扩展资源仅支持浏览器下载，不支持服务器中转。"
        download_strategy = "browser_download_required"
        error_code = "UPSTREAM_BROWSER_DOWNLOAD_REQUIRED"
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

    if any(item_is_upstream(item) for item in normalized_items):
        valid = all(
            item_is_upstream(item) and provider_manifest_allows_public_download(item)
            for item in normalized_items
        )
        images_only = all(item_is_image(item) for item in normalized_items)
        browser_selection = images_only or (
            len(normalized_items) == 1 and item_is_video(normalized_items[0])
        )
        allowed = valid and browser_selection and requested_mode in {"", "auto", "browser"}
        return route_payload(
            route_type=ROUTE_DIRECT if allowed else ROUTE_BLOCKED,
            route_reason=("资源由浏览器获取并保存" if allowed else "扩展资源仅支持浏览器下载；请单独选择图片或一个视频，失效时重新解析。"),
            download_strategy=(
                "browser_direct_images" if allowed and images_only
                else "browser_direct_provider_video" if allowed
                else "browser_download_required"
            ),
            media_items=normalized_items,
            estimated_bytes=estimated_bytes,
            error_code="" if allowed else "UPSTREAM_BROWSER_DOWNLOAD_REQUIRED",
            warnings=warnings,
        )

    if requested_mode in {"proxy", "server_single"}:
        if selection_has_xhs_zero_relay_image(normalized_items):
            return route_payload(
                route_type=ROUTE_BLOCKED,
                route_reason="小红书图片必须由浏览器直接保存；资源失效时请重新解析。",
                download_strategy="browser_image_download_required",
                media_items=normalized_items,
                estimated_bytes=estimated_bytes,
                error_code="XHS_IMAGE_BROWSER_DOWNLOAD_REQUIRED",
                warnings=warnings,
            )
        if (
            len(normalized_items) == 1
            and item_can_browser_direct_video(normalized_items[0])
        ):
            return route_payload(
                route_type=ROUTE_BLOCKED,
                route_reason="普通视频必须由浏览器直接获取；请重试下载或重新解析。",
                download_strategy="browser_download_required",
                media_items=normalized_items,
                estimated_bytes=estimated_bytes,
                error_code="ORDINARY_BROWSER_DOWNLOAD_REQUIRED",
                warnings=warnings,
            )
        if len(normalized_items) == 1:
            item = normalized_items[0]
            if single_image_can_attachment_download(item) or single_video_can_server_proxy_download(item):
                return route_payload(
                    route_type=ROUTE_PROXY,
                    route_reason="单个资源使用服务器代理下载，不打包 ZIP",
                    download_strategy="server_single_attachment",
                    media_items=normalized_items,
                    estimated_bytes=estimated_bytes,
                    warnings=warnings,
                )
        return route_payload(
            route_type=ROUTE_ZIP,
            route_reason="当前选择无法单文件代理下载，使用服务器 ZIP 流程",
            download_strategy="server_zip",
            media_items=normalized_items,
            estimated_bytes=estimated_bytes,
            warnings=warnings,
        )

    if requested_mode in {"zip", "browser"}:
        if selection_has_xhs_zero_relay_image(normalized_items):
            if requested_mode == "zip":
                return route_payload(
                    route_type=ROUTE_BLOCKED,
                    route_reason="小红书图片不使用服务器 ZIP；请使用浏览器下载。",
                    download_strategy="browser_image_download_required",
                    media_items=normalized_items,
                    estimated_bytes=estimated_bytes,
                    error_code="XHS_IMAGE_BROWSER_DOWNLOAD_REQUIRED",
                    warnings=warnings,
                )
            if not all(item_is_image(item) for item in normalized_items):
                return route_payload(
                    route_type=ROUTE_BLOCKED,
                    route_reason="小红书图片不能与需服务端处理的资源混合下载。",
                    download_strategy="browser_image_download_required",
                    media_items=normalized_items,
                    estimated_bytes=estimated_bytes,
                    error_code="XHS_IMAGE_BROWSER_DOWNLOAD_REQUIRED",
                    warnings=warnings,
                )
            return route_payload(
                route_type=ROUTE_DIRECT,
                route_reason="小红书图片由浏览器获取并保存",
                download_strategy="browser_direct_images",
                media_items=normalized_items,
                estimated_bytes=estimated_bytes,
                warnings=warnings,
            )
        if (
            len(normalized_items) == 1
            and single_video_prefers_browser_direct(normalized_items[0])
        ):
            return route_payload(
                route_type=ROUTE_DIRECT,
                route_reason="浏览器将直接获取并保存视频文件",
                download_strategy="browser_direct_provider_video",
                media_items=normalized_items,
                estimated_bytes=estimated_bytes,
                warnings=warnings,
            )
        if (
            requested_mode == "browser"
            and len(normalized_items) == 1
            and (
                single_item_can_attachment_download(normalized_items[0])
                or single_video_can_server_proxy_download(normalized_items[0])
            )
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

    if selection_has_xhs_zero_relay_image(normalized_items):
        if not all_images:
            return route_payload(
                route_type=ROUTE_BLOCKED,
                route_reason="小红书图片不能与需服务端处理的资源混合下载。",
                download_strategy="browser_image_download_required",
                media_items=normalized_items,
                estimated_bytes=estimated_bytes,
                error_code="XHS_IMAGE_BROWSER_DOWNLOAD_REQUIRED",
                warnings=warnings,
            )
        return route_payload(
            route_type=ROUTE_DIRECT,
            route_reason="小红书图片由浏览器获取并保存",
            download_strategy="browser_direct_images",
            media_items=normalized_items,
            estimated_bytes=estimated_bytes,
            warnings=warnings,
        )

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
        if single_video_prefers_browser_direct(normalized_items[0]):
            return route_payload(
                route_type=ROUTE_DIRECT,
                route_reason="浏览器将直接获取并保存视频文件",
                download_strategy="browser_direct_provider_video",
                media_items=normalized_items,
                estimated_bytes=estimated_bytes,
                warnings=warnings,
            )
        if single_video_can_attachment_download(normalized_items[0]):
            return route_payload(
                route_type=ROUTE_PROXY,
                route_reason="单个资源使用单文件下载，不打包 ZIP",
                download_strategy="server_single_attachment",
                media_items=normalized_items,
                estimated_bytes=estimated_bytes,
                warnings=warnings,
            )
        if single_video_can_server_proxy_download(normalized_items[0]):
            return route_payload(
                route_type=ROUTE_PROXY,
                route_reason="Provider 视频使用服务器单文件下载，避免浏览器直链拦截或签名 URL 失效",
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
