DOWNLOAD_STRATEGY_BROWSER_BLOB = "browser_blob"
DOWNLOAD_STRATEGY_BROWSER_DIRECT = "browser_direct"
DOWNLOAD_STRATEGY_SERVER_ATTACHMENT = "server_attachment"
DOWNLOAD_STRATEGY_SERVER_MERGE = "server_merge"
DOWNLOAD_STRATEGY_SERVER_ZIP = "server_zip"

FALLBACK_SERVER_ATTACHMENT = "server_attachment"
FALLBACK_SERVER_ZIP = "server_zip"
FALLBACK_NONE = "none"

BROWSER_BLOB_VIDEO_PROVIDERS = {
    "kukutool",
    "tiantianjiexi",
    "kukutool_generic",
    "tiantianjiexi_generic",
}
SERVER_MERGE_PROVIDERS = {"youtube_merge", "yt_dlp_merge"}
SERVER_ATTACHMENT_VIDEO_PROVIDERS = {"yt_dlp_generic"}


def normalize_provider(value):
    return str(value or "").strip().lower()


def normalize_media_type(value):
    return str(value or "").strip().lower()


def build_download_strategy(asset, *, result_platform="", result_metadata=None):
    media_type = normalize_media_type(getattr(asset, "media_type", ""))
    provider = normalize_provider(getattr(asset, "provider", ""))
    source_provider = normalize_provider((result_metadata or {}).get("source_provider"))
    selected_provider = normalize_provider((result_metadata or {}).get("selected_provider"))
    effective_provider = provider or selected_provider or source_provider

    if media_type == "image":
        return {
            "download_strategy": DOWNLOAD_STRATEGY_BROWSER_DIRECT,
            "fallback_strategy": FALLBACK_SERVER_ZIP,
            "cors_expected": False,
            "requires_merge": False,
            "requires_cookie": False,
            "strategy_label": "图片直链优先",
        }

    if effective_provider in SERVER_MERGE_PROVIDERS:
        return {
            "download_strategy": DOWNLOAD_STRATEGY_SERVER_MERGE,
            "fallback_strategy": FALLBACK_NONE,
            "cors_expected": False,
            "requires_merge": True,
            "requires_cookie": False,
            "strategy_label": "服务端合并下载",
        }

    if effective_provider in BROWSER_BLOB_VIDEO_PROVIDERS:
        return {
            "download_strategy": DOWNLOAD_STRATEGY_BROWSER_BLOB,
            "fallback_strategy": FALLBACK_SERVER_ATTACHMENT,
            "cors_expected": True,
            "requires_merge": False,
            "requires_cookie": False,
            "strategy_label": "浏览器 Blob 优先",
        }

    if effective_provider in SERVER_ATTACHMENT_VIDEO_PROVIDERS:
        return {
            "download_strategy": DOWNLOAD_STRATEGY_SERVER_ATTACHMENT,
            "fallback_strategy": FALLBACK_NONE,
            "cors_expected": False,
            "requires_merge": False,
            "requires_cookie": False,
            "strategy_label": "服务端单文件下载",
        }

    if media_type == "video":
        return {
            "download_strategy": DOWNLOAD_STRATEGY_BROWSER_DIRECT,
            "fallback_strategy": FALLBACK_SERVER_ATTACHMENT,
            "cors_expected": False,
            "requires_merge": False,
            "requires_cookie": False,
            "strategy_label": "视频直链优先",
        }

    return {
        "download_strategy": DOWNLOAD_STRATEGY_SERVER_ZIP,
        "fallback_strategy": FALLBACK_NONE,
        "cors_expected": False,
        "requires_merge": False,
        "requires_cookie": False,
        "strategy_label": "服务端打包",
    }


def build_download_strategy_matrix():
    return (
        {
            "source": "抖音天天解析",
            "strategy": DOWNLOAD_STRATEGY_BROWSER_BLOB,
            "fallback": FALLBACK_SERVER_ATTACHMENT,
            "note": "原画视频优先由浏览器拉取 CDN，失败自动切换服务端单文件。",
        },
        {
            "source": "抖音 KuKuTool",
            "strategy": DOWNLOAD_STRATEGY_BROWSER_BLOB,
            "fallback": FALLBACK_SERVER_ATTACHMENT,
            "note": "与天天解析一致，适合支持 CORS 的字节 CDN 链接。",
        },
        {
            "source": "小红书图片",
            "strategy": DOWNLOAD_STRATEGY_BROWSER_DIRECT,
            "fallback": FALLBACK_SERVER_ZIP,
            "note": "单图直链优先，多图仍建议 ZIP。",
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
