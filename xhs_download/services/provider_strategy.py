from urllib.parse import urlsplit


DOWNLOAD_STRATEGY_BROWSER_BLOB = "browser_blob"
DOWNLOAD_STRATEGY_BROWSER_STREAMING = "browser_streaming"
DOWNLOAD_STRATEGY_BROWSER_DIRECT = "browser_direct"
DOWNLOAD_STRATEGY_SERVER_ATTACHMENT = "server_attachment"
DOWNLOAD_STRATEGY_SERVER_MERGE = "server_merge"
DOWNLOAD_STRATEGY_SERVER_ZIP = "server_zip"

FALLBACK_SERVER_ATTACHMENT = "server_attachment"
FALLBACK_SERVER_ZIP = "server_zip"
FALLBACK_BROWSER_BLOB = "browser_blob"
FALLBACK_NONE = "none"

BROWSER_BLOB_VIDEO_PROVIDERS = {
    "kukutool",
    "tiantianjiexi",
    "kukutool_generic",
    "tiantianjiexi_generic",
}
BROWSER_DIRECT_FETCH_SAFE_PROVIDERS = frozenset({"kukutool", "tiantianjiexi"})
SERVER_MERGE_PROVIDERS = {"youtube_merge", "yt_dlp_merge"}
SERVER_ATTACHMENT_VIDEO_PROVIDERS = {"yt_dlp_generic"}


def normalize_provider(value):
    return str(value or "").strip().lower()


def normalize_media_type(value):
    return str(value or "").strip().lower()


def browser_streaming_resource_url_is_allowed(value):
    """The reviewed initial SW hosts, without DNS or provider-specific priority."""
    from .platforms.douyin_media_policy import is_allowed_douyin_media_hostname
    from .platforms.xiaohongshu import xhs_native_video_url_is_browser_streaming_allowed
    from .platforms.xhs_tiantian_media_policy import xhs_tiantian_media_url_is_allowed
    from .common import host_matches

    url = str(value or "")
    try:
        parsed = urlsplit(url)
        if (not url or len(url) > 8192 or any(ord(c) <= 32 or ord(c) == 127 for c in url)
                or "\\" in url or parsed.scheme != "https" or parsed.fragment
                or parsed.username is not None or parsed.password is not None
                or parsed.port not in (None, 443) or not parsed.path.strip("/")
                or parsed.path.lower().endswith((".m3u8", ".mpd", ".m4s"))):
            return False
    except ValueError:
        return False
    return bool(
        is_allowed_douyin_media_hostname(parsed.hostname, existing_suffixes=("365yg.com", "douyinvod.com"))
        or xhs_native_video_url_is_browser_streaming_allowed(url)
        or xhs_tiantian_media_url_is_allowed(url)
        or host_matches(parsed.hostname, ("bilivideo.com", "bilivideo.cn"))
    )


def browser_transport_hint(item):
    """Presentation only: callers must establish browser authority first."""
    if (item.get("type") != "video" or item.get("browser_direct_fetch_safe") is not True
            or item.get("requires_merge") is not False or item.get("requires_cookie") is not False
            or any(item.get(key) for key in ("has_drm", "requires_context", "download_context_required"))
            or item.get("video_tracks") == "absent"):
        return {}
    streaming = browser_streaming_resource_url_is_allowed(item.get("url"))
    return {
        "download_strategy": DOWNLOAD_STRATEGY_BROWSER_STREAMING if streaming else DOWNLOAD_STRATEGY_BROWSER_BLOB,
        "fallback_strategy": FALLBACK_BROWSER_BLOB if streaming else FALLBACK_NONE,
    }


def build_download_strategy(asset, *, result_platform="", result_metadata=None):
    media_type = normalize_media_type(getattr(asset, "media_type", ""))
    provider = normalize_provider(getattr(asset, "provider", ""))
    source_provider = normalize_provider((result_metadata or {}).get("source_provider"))
    selected_provider = normalize_provider((result_metadata or {}).get("selected_provider"))
    effective_provider = provider or selected_provider or source_provider
    if str(getattr(asset, "asset_id", "") or "").startswith("upstream:"):
        # Pre-authorization intent only; the authenticated route chooses transport.
        return {
            "download_strategy": DOWNLOAD_STRATEGY_BROWSER_DIRECT,
            "fallback_strategy": FALLBACK_NONE,
            "cors_expected": True,
            "browser_direct_fetch_safe": False,
            "requires_merge": False,
            "requires_cookie": False,
            "strategy_label": "下载",
        }
    is_xhs_tiantian_video = bool(
        result_platform == "xiaohongshu"
        and effective_provider == "tiantianjiexi_generic"
        and (result_metadata or {}).get("xhs_tiantian_hybrid_status") == "success"
    )
    is_xhs_native_video = bool(
        result_platform == "xiaohongshu"
        and media_type == "video"
        and effective_provider in {"", "native"}
    )

    if media_type == "image":
        is_parsehub_xhs_image = effective_provider == "parsehub_xhs_images"
        is_xhs_image = result_platform == "xiaohongshu"
        return {
            "download_strategy": DOWNLOAD_STRATEGY_BROWSER_DIRECT,
            "fallback_strategy": (
                FALLBACK_NONE
                if is_xhs_image or is_parsehub_xhs_image
                else FALLBACK_SERVER_ZIP
            ),
            "cors_expected": is_xhs_image or is_parsehub_xhs_image,
            "browser_direct_fetch_safe": (
                is_xhs_image or is_parsehub_xhs_image or not bool(effective_provider)
            ),
            "requires_merge": False,
            "requires_cookie": False,
            "strategy_label": "图片直链优先",
        }

    if effective_provider in SERVER_MERGE_PROVIDERS:
        return {
            "download_strategy": DOWNLOAD_STRATEGY_SERVER_MERGE,
            "fallback_strategy": FALLBACK_NONE,
            "cors_expected": False,
            "browser_direct_fetch_safe": False,
            "requires_merge": True,
            "requires_cookie": False,
            "strategy_label": "服务端合并下载",
        }

    # Transport is a property of the file's access conditions, not its parser.
    headers = getattr(asset, "headers", {}) or {}
    if (media_type == "video" and browser_streaming_resource_url_is_allowed(asset.url)
            and not any(str(key).lower() in {"cookie", "authorization", "proxy-authorization"}
                        and value for key, value in headers.items())):
        return {
            "download_strategy": DOWNLOAD_STRATEGY_BROWSER_STREAMING,
            "fallback_strategy": FALLBACK_BROWSER_BLOB,
            "cors_expected": True,
            "browser_direct_fetch_safe": True,
            "requires_merge": False,
            "requires_cookie": False,
            "strategy_label": "浏览器下载",
        }

    if media_type == "video" and (is_xhs_tiantian_video or is_xhs_native_video):
        return {
            "download_strategy": DOWNLOAD_STRATEGY_BROWSER_STREAMING,
            "fallback_strategy": FALLBACK_BROWSER_BLOB,
            "cors_expected": True,
            "browser_direct_fetch_safe": True,
            "requires_merge": False,
            "requires_cookie": False,
            "strategy_label": "浏览器下载",
        }

    if effective_provider in BROWSER_BLOB_VIDEO_PROVIDERS:
        browser_direct_fetch_safe = (
            effective_provider in BROWSER_DIRECT_FETCH_SAFE_PROVIDERS
        )
        return {
            "download_strategy": (
                DOWNLOAD_STRATEGY_BROWSER_STREAMING
                if browser_direct_fetch_safe
                else DOWNLOAD_STRATEGY_SERVER_ATTACHMENT
            ),
            "fallback_strategy": (
                FALLBACK_BROWSER_BLOB
                if browser_direct_fetch_safe
                else FALLBACK_SERVER_ATTACHMENT
            ),
            "cors_expected": browser_direct_fetch_safe,
            "browser_direct_fetch_safe": browser_direct_fetch_safe,
            "requires_merge": False,
            "requires_cookie": False,
            "strategy_label": (
                "浏览器下载"
                if browser_direct_fetch_safe
                else "服务端可观测下载"
            ),
        }

    if effective_provider in SERVER_ATTACHMENT_VIDEO_PROVIDERS:
        return {
            "download_strategy": DOWNLOAD_STRATEGY_SERVER_ATTACHMENT,
            "fallback_strategy": FALLBACK_NONE,
            "cors_expected": False,
            "browser_direct_fetch_safe": False,
            "requires_merge": False,
            "requires_cookie": False,
            "strategy_label": "服务端单文件下载",
        }

    if media_type == "video":
        return {
            "download_strategy": DOWNLOAD_STRATEGY_BROWSER_DIRECT,
            "fallback_strategy": FALLBACK_SERVER_ATTACHMENT,
            "cors_expected": False,
            "browser_direct_fetch_safe": not bool(effective_provider),
            "requires_merge": False,
            "requires_cookie": False,
            "strategy_label": "视频直链优先",
        }

    return {
        "download_strategy": DOWNLOAD_STRATEGY_SERVER_ZIP,
        "fallback_strategy": FALLBACK_NONE,
        "cors_expected": False,
        "browser_direct_fetch_safe": False,
        "requires_merge": False,
        "requires_cookie": False,
        "strategy_label": "服务端打包",
    }


def build_download_strategy_matrix():
    return (
        {
            "source": "抖音天天解析",
            "strategy": DOWNLOAD_STRATEGY_BROWSER_STREAMING,
            "fallback": FALLBACK_BROWSER_BLOB,
            "note": "浏览器流式保存源文件；仅在任务启动前能力不可用时回退浏览器内存保存，不转服务器中转。",
        },
        {
            "source": "抖音 KuKuTool",
            "strategy": DOWNLOAD_STRATEGY_BROWSER_STREAMING,
            "fallback": FALLBACK_BROWSER_BLOB,
            "note": "普通视频默认由浏览器流式保存；服务端不传输媒体正文。",
        },
        {
            "source": "小红书图片",
            "strategy": DOWNLOAD_STRATEGY_BROWSER_DIRECT,
            "fallback": FALLBACK_NONE,
            "note": "单图由浏览器保存，多图由浏览器打包；服务器不传输图片正文。",
        },
        {
            "source": "YouTube 高清合并",
            "strategy": DOWNLOAD_STRATEGY_SERVER_MERGE,
            "fallback": FALLBACK_NONE,
            "note": "1080p+ 经常音画分离，必须服务端合并。",
        },
        {
            "source": "yt-dlp 长尾视频",
            "strategy": DOWNLOAD_STRATEGY_SERVER_ATTACHMENT,
            "fallback": FALLBACK_NONE,
            "note": "平台 headers/签名差异较大，默认服务端处理更稳。",
        },
        {
            "source": "gallery-dl 图集",
            "strategy": DOWNLOAD_STRATEGY_SERVER_ZIP,
            "fallback": FALLBACK_NONE,
            "note": "多图资源优先统一打包，避免浏览器连续弹窗。",
        },
    )
