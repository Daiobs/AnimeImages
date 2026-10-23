from urllib.parse import urlsplit
from time import time
import ipaddress
import re


DOWNLOAD_STRATEGY_BROWSER_BLOB = "browser_blob"
DOWNLOAD_STRATEGY_BROWSER_STREAMING = "browser_streaming"
DOWNLOAD_STRATEGY_BROWSER_DIRECT = "browser_direct"
DOWNLOAD_STRATEGY_BROWSER_NATIVE = "browser_native"
DOWNLOAD_STRATEGY_SERVER_ATTACHMENT = "server_attachment"
DOWNLOAD_STRATEGY_SERVER_MERGE = "server_merge"
DOWNLOAD_STRATEGY_SERVER_ZIP = "server_zip"

FALLBACK_SERVER_ATTACHMENT = "server_attachment"
FALLBACK_SERVER_ZIP = "server_zip"
FALLBACK_BROWSER_BLOB = "browser_blob"
FALLBACK_NONE = "none"

SERVER_MERGE_PROVIDERS = {"youtube_merge", "yt_dlp_merge"}


def normalize_provider(value):
    return str(value or "").strip().lower()


def normalize_media_type(value):
    return str(value or "").strip().lower()


def browser_streaming_resource_url_is_allowed(value):
    """Transport syntax only, after source admission and resource authorization.

    Server requests additionally validate and pin DNS at every hop. A browser
    response still has to pass CORS, MIME and size checks; this is no CORS grant.
    """
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
    host = (parsed.hostname or "").lower()
    if (not host or len(host) > 253 or host.endswith('.')
            or re.search(r'(?:^|\.)(?:localhost|local|internal|test|invalid|home|lan)$', host)):
        return False
    try:
        ipaddress.ip_address(host)
        return False
    except ValueError:
        return bool('.' in host and all(re.fullmatch(
            r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label) for label in host.split('.')))


def browser_video_access_is_compatible(item):
    """File and access facts, independent of parser labels and authorization."""
    from .request_context import request_context_compatible
    return bool(
        item.get('type') == 'video'
        and browser_streaming_resource_url_is_allowed(item.get('url'))
        and not any(item.get(key) for key in (
            'requires_merge', 'requires_cookie', 'has_drm', 'requires_context',
            'download_context_required', 'ip_bound'))
        and item.get('video_tracks') != 'absent'
        and item.get('delivery_state') not in {'streaming_media', 'separate_media_tracks', 'silent_video'}
        and not any(marker in str(item.get('protocol') or '').lower() for marker in ('m3u8', 'dash'))
        and request_context_compatible(item.get('http_headers') or item.get('headers'),
                                       **media_request_context(item))
    )


def media_request_context(item):
    return {key: item[key] for key in (
        'required_headers', 'requires_cookie', 'requires_context', 'ip_bound', 'headers_present',
    ) if key in item}


def browser_transport_hint(item):
    """Presentation only: callers must establish browser authority first."""
    if item.get("browser_direct_fetch_safe") is not True or not browser_video_access_is_compatible(item):
        return {}
    from .quality import browser_response_evidence, native_attachment_expiry
    expires_at = native_attachment_expiry(
        item.get('url'), item.get('http_headers') or item.get('headers'), **media_request_context(item))
    if expires_at:
        selection_expiry = item.get('selection_expires_at')
        if selection_expiry is not None:
            try:
                expires_at = min(expires_at, int(selection_expiry))
            except (TypeError, ValueError, OverflowError):
                expires_at = 0
        if expires_at > time() * 1000:
            return {'download_strategy': DOWNLOAD_STRATEGY_BROWSER_NATIVE,
                    'fallback_strategy': FALLBACK_NONE, 'native_download_expires_at': expires_at}
    hint = {
        "download_strategy": DOWNLOAD_STRATEGY_BROWSER_STREAMING,
        "fallback_strategy": FALLBACK_BROWSER_BLOB,
    }
    evidence = browser_response_evidence(
        item.get('url'), item.get('http_headers') or item.get('headers'), **media_request_context(item))
    if evidence and browser_streaming_resource_url_is_allowed(evidence[0]):
        final_url, response_expiry = evidence
        try:
            response_expiry = min(response_expiry, int(item.get('selection_expires_at') or response_expiry))
        except (TypeError, ValueError, OverflowError):
            response_expiry = 0
        if response_expiry > time() * 1000:
            hint['browser_response_hosts'] = list(dict.fromkeys((
                urlsplit(item['url']).hostname, urlsplit(final_url).hostname)))
            hint['browser_response_hosts_expires_at'] = response_expiry
    return hint


def prepare_browser_transport(item, *, request_context=None):
    """Called only after action authorization/limits, for this selected file."""
    from .quality import ensure_native_attachment_evidence
    context = request_context or {}
    facts = {**item, **media_request_context(context),
             'headers': context.get('http_headers') or context.get('headers') or {}}
    if item.get('browser_direct_fetch_safe') is True and browser_video_access_is_compatible(facts):
        ensure_native_attachment_evidence(
            item['url'], facts['headers'], authorized_resource=True,
            **media_request_context(facts))
    return {**item, **browser_transport_hint(facts)}


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
            "requires_cookie": (getattr(asset, 'request_context', {}) or {}).get('requires_cookie'),
            "strategy_label": "下载",
        }

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
    file_facts = {'type': media_type, 'url': asset.url,
                  'headers': getattr(asset, 'headers', {}) or {},
                  **(getattr(asset, 'request_context', {}) or {})}
    if browser_video_access_is_compatible(file_facts):
        return {
            "download_strategy": DOWNLOAD_STRATEGY_BROWSER_STREAMING,
            "fallback_strategy": FALLBACK_BROWSER_BLOB,
            "cors_expected": True,
            "browser_direct_fetch_safe": True,
            "requires_merge": False,
            "requires_cookie": file_facts.get('requires_cookie'),
            "request_context_status": "no_known_requirement",
            "strategy_label": "浏览器下载",
        }

    if media_type == "video" and urlsplit(asset.url).scheme == 'http':
        return {
            'download_strategy': DOWNLOAD_STRATEGY_SERVER_ATTACHMENT,
            'fallback_strategy': FALLBACK_NONE, 'cors_expected': False,
            'browser_direct_fetch_safe': False, 'requires_merge': False,
            'requires_cookie': file_facts.get('requires_cookie'),
            'strategy_label': '已有 HTTP 附件路径',
        }
    if media_type == "video":
        return {
            "download_strategy": DOWNLOAD_STRATEGY_BROWSER_DIRECT,
            "fallback_strategy": FALLBACK_NONE,
            "cors_expected": False,
            "browser_direct_fetch_safe": False,
            "requires_merge": file_facts.get('requires_merge'),
            "requires_cookie": file_facts.get('requires_cookie'),
            "strategy_label": "此资源的访问条件暂不支持浏览器保存",
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
            "note": "完整视频按资源条件优先原生附件，否则使用已有浏览器流式交付；不按来源强制传输方式。",
        },
        {
            "source": "抖音 KuKuTool",
            "strategy": DOWNLOAD_STRATEGY_BROWSER_STREAMING,
            "fallback": FALLBACK_BROWSER_BLOB,
            "note": "与其他已授权完整视频共用原生附件／浏览器流式判断；服务端不传输媒体正文。",
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
            "strategy": DOWNLOAD_STRATEGY_BROWSER_STREAMING,
            "fallback": FALLBACK_BROWSER_BLOB,
            "note": "完整文件按最终附件证据和实际访问条件选择原生或浏览器流式；分离轨与必要上下文另行判断。",
        },
        {
            "source": "gallery-dl 图集",
            "strategy": DOWNLOAD_STRATEGY_SERVER_ZIP,
            "fallback": FALLBACK_NONE,
            "note": "多图资源优先统一打包，避免浏览器连续弹窗。",
        },
    )
