import logging
import os
import re
import shutil
import tempfile
from io import BytesIO
from pathlib import Path
from urllib.parse import urljoin, urlparse
from zipfile import ZIP_DEFLATED, ZipFile

import requests
from django.conf import settings
from django.core.cache import cache
from PIL import Image

from .common import (
    HEADERS,
    MEDIA_HOST_SUFFIXES,
    XhsDownloadError,
    clean_filename,
    host_matches,
    request_get_with_retry,
    validate_external_url,
    validate_media_url,
    validate_public_download_url,
    validate_public_url,
)
from .platforms.bilibili import BILIBILI_HEADERS, BILIBILI_MEDIA_HOST_SUFFIXES
from .platforms.douyin import DOUYIN_HEADERS, DOUYIN_MEDIA_HOST_SUFFIXES
from .platforms.external_tools import (
    clear_cached_external_failure,
    clear_cached_external_payload,
)
from .platforms.kukutool_generic import (
    build_failure_cache_key as build_generic_kukutool_failure_cache_key,
    build_payload_cache_key as build_generic_kukutool_payload_cache_key,
)
from .platforms.douyin_kukutool import is_byte_cdn_url
from .provider_recovery import (
    get_kukutool_manifest_identity,
    get_tiantianjiexi_manifest_identity,
    iter_kukutool_download_recovery_urls,
    refresh_tiantianjiexi_download_item,
    TianTianJieXiRecoveryError,
)
from .provider_runtime_config import runtime_float
from .provider_manifest import build_provider_manifest_token, provider_manifest_is_valid
from .platforms.third_party import third_party_manifest_item_is_allowed
from .platforms.ytdlp_merge import (
    PROVIDER_ID as YTDLP_MERGE_PROVIDER_ID,
    download_to_temp_path as download_ytdlp_merge_to_temp_path,
    manifest_item_is_allowed as ytdlp_merge_manifest_item_is_allowed,
    provider_manifest_allows_download as provider_manifest_allows_ytdlp_merge,
)
from .youtube_merge import (
    PROVIDER_ID as YOUTUBE_MERGE_PROVIDER_ID,
    download_youtube_merge_to_temp_path,
    provider_manifest_allows_youtube_merge,
    youtube_merge_manifest_identity,
    youtube_merge_manifest_item_is_allowed,
)
from .quality import (
    QualityProbe,
    content_type_matches_expected,
    parse_content_length,
)


logger = logging.getLogger(__name__)
MAX_DOWNLOAD_ITEMS = 30
MAX_IMAGE_BYTES = 50 * 1024 * 1024
MAX_VIDEO_BYTES = 1024 * 1024 * 1024
MAX_ARCHIVE_BYTES = 1024 * 1024 * 1024
CHUNK_SIZE = 1024 * 256
ORIGINAL_FORMAT = "original"
EXTERNAL_PAYLOAD_CACHE_PROVIDERS = {
    "gallery_dl_generic",
    "yt_dlp_generic",
}
GENERIC_KUKUTOOL_CACHE_PROVIDER = "kukutool_generic"
YOUTUBE_MEDIA_HOST_SUFFIXES = ("googlevideo.com", "ytimg.com")
YOUTUBE_HEADERS = {
    **HEADERS,
    "Referer": "https://www.youtube.com/",
    "Origin": "https://www.youtube.com",
}
YOUTUBE_COVER_FALLBACK_NAMES = (
    "sddefault.jpg",
    "hqdefault.jpg",
    "mqdefault.jpg",
    "default.jpg",
)
DOWNLOAD_ERROR_URL_PATTERN = re.compile(r"https?://[^\s<>'\"\]\)]+", re.IGNORECASE)
NETWORK_DOWNLOAD_ERROR_MARKERS = (
    "connectionpool",
    "max retries exceeded",
    "ssl",
    "ssleoferror",
    "unexpected_eof",
    "eof occurred",
    "connection aborted",
    "connection reset",
    "read timed out",
    "connect timeout",
    "timeout",
    "timed out",
    "temporary failure",
    "name or service not known",
)


def get_media_headers(url, platform=None):
    if platform == "bilibili":
        return BILIBILI_HEADERS
    if platform == "douyin":
        return DOUYIN_HEADERS

    hostname = (urlparse(url or "").hostname or "").lower()
    if host_matches(hostname, BILIBILI_MEDIA_HOST_SUFFIXES):
        return BILIBILI_HEADERS
    if host_matches(hostname, DOUYIN_MEDIA_HOST_SUFFIXES):
        return DOUYIN_HEADERS
    if host_matches(hostname, YOUTUBE_MEDIA_HOST_SUFFIXES):
        return YOUTUBE_HEADERS

    return HEADERS


def normalize_save_format(save_format):
    save_format = (save_format or ORIGINAL_FORMAT).lower().strip()
    if save_format == "jpeg":
        save_format = "jpg"
    if save_format not in [ORIGINAL_FORMAT, "jpg", "png", "webp"]:
        save_format = ORIGINAL_FORMAT
    return save_format


def normalize_quality(quality):
    try:
        quality = int(quality)
    except Exception:
        quality = 95
    return max(1, min(quality, 100))


def normalize_video_save_mode(mode):
    mode = (mode or "video").lower().strip()
    if mode not in ["video", "cover", "both"]:
        mode = "video"
    return mode


def public_download_error_message(error, limit=120):
    text = str(error or "").strip()
    if not text:
        return "下载失败，请稍后再试。"
    lower_text = text.lower()
    if any(marker in lower_text for marker in NETWORK_DOWNLOAD_ERROR_MARKERS):
        return "资源下载失败：上游 CDN 连接异常，请重新解析后再试。"
    if text.startswith("没有资源保存成功") and DOWNLOAD_ERROR_URL_PATTERN.search(text):
        return "没有资源保存成功：上游资源下载失败，请重新解析后再试。"
    text = DOWNLOAD_ERROR_URL_PATTERN.sub("[url-redacted]", text)
    text = re.sub(
        r"(?i)(provider_manifest_token=)[^&\s]+",
        r"\1[redacted]",
        text,
    )
    return text[: max(0, int(limit or 0))]


def observable_download_error(error, *, provider=""):
    if isinstance(error, TianTianJieXiRecoveryError):
        return {
            "error_code": error.error_code,
            "message": public_download_error_message(error),
            "status_code": 504
            if error.error_code == "TIANTIANJIEXI_REFRESH_TIMEOUT"
            else 400,
        }
    if isinstance(error, requests.exceptions.SSLError):
        return {
            "error_code": "UPSTREAM_SSL_ERROR",
            "message": "上游资源连接异常，请稍后重试。",
            "status_code": 502,
        }
    if isinstance(
        error,
        (
            requests.exceptions.ConnectionError,
            requests.exceptions.ConnectTimeout,
            requests.exceptions.ReadTimeout,
        ),
    ):
        return {
            "error_code": "UPSTREAM_CONNECTION_ERROR",
            "message": "上游资源暂时无法连接，请稍后重试。",
            "status_code": 502,
        }

    message = str(error or "")
    if provider == "tiantianjiexi" and (
        message.startswith("HTTP 403")
        or message.startswith("HTTP 404")
    ):
        return {
            "error_code": "TIANTIANJIEXI_RESOURCE_EXPIRED",
            "message": "资源已失效，请重新解析后再试。",
            "status_code": 409,
        }
    return {
        "error_code": "PROXY_FAILED_RECOVERY_BLOCKED",
        "message": "资源连接失败，自动恢复未成功，请重新解析后再试。",
        "status_code": 400,
    }


def safe_download_size(value):
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def guess_video_extension(url):
    url = (url or "").lower()
    if ".m3u8" in url:
        return "m3u8"
    if ".mov" in url:
        return "mov"
    return "mp4"


IMAGE_CONTENT_TYPE_EXTENSIONS = {
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/gif": "gif",
    "image/avif": "avif",
}

VIDEO_CONTENT_TYPE_EXTENSIONS = {
    "video/mp4": "mp4",
    "video/quicktime": "mov",
    "application/vnd.apple.mpegurl": "m3u8",
    "application/x-mpegurl": "m3u8",
}


def get_extension_from_url(url, allowed_extensions):
    suffix = Path(urlparse(url or "").path).suffix.lower().lstrip(".")
    if suffix == "jpeg":
        suffix = "jpg"
    return suffix if suffix in allowed_extensions else None


def get_extension_from_content_type(content_type, mapping):
    content_type = (content_type or "").split(";", 1)[0].strip().lower()
    return mapping.get(content_type)


def guess_image_extension(url, content_type=None):
    return (
        get_extension_from_content_type(content_type, IMAGE_CONTENT_TYPE_EXTENSIONS)
        or get_extension_from_url(url, set(IMAGE_CONTENT_TYPE_EXTENSIONS.values()))
        or "jpg"
    )


def guess_video_extension_from_response(url, content_type=None):
    return (
        get_extension_from_content_type(content_type, VIDEO_CONTENT_TYPE_EXTENSIONS)
        or guess_video_extension(url)
    )


def save_image_to_buffer(image, save_format, quality):
    save_format = normalize_save_format(save_format)
    quality = normalize_quality(quality)
    buffer = BytesIO()

    if save_format == "png":
        image.save(buffer, "PNG", optimize=True)
    elif save_format == "webp":
        if image.mode not in ("RGB", "RGBA"):
            image = image.convert("RGB")
        image.save(buffer, "WEBP", quality=quality, method=6)
    else:
        if image.mode != "RGB":
            image = image.convert("RGB")
        image.save(buffer, "JPEG", quality=quality, optimize=True, progressive=True)

    return buffer.getvalue()


def validate_content_length(response, max_bytes):
    content_length = parse_content_length(response)
    if content_length and content_length > max_bytes:
        raise XhsDownloadError("资源超过允许的大小限制")


def response_chunk_looks_like_html(chunk):
    sample = bytes(chunk[:512]).lstrip().lower()
    return sample.startswith(
        (
            b"<!doctype html",
            b"<html",
            b"<head",
            b"<body",
        )
    )


def write_limited_response(
    response,
    target,
    max_bytes,
    progress_callback=None,
    expected_type="",
):
    total = 0
    content_length = parse_content_length(response)
    first_chunk = True

    validate_content_length(response, max_bytes)

    for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
        if not chunk:
            continue
        if (
            first_chunk
            and expected_type in {"video", "image"}
            and response_chunk_looks_like_html(chunk)
        ):
            raise XhsDownloadError("资源类型异常：HTML response body")
        first_chunk = False
        total += len(chunk)
        if total > max_bytes:
            raise XhsDownloadError("资源超过允许的大小限制")
        target.write(chunk)
        if progress_callback:
            progress_callback(total, content_length)

    return total


def read_limited_response(response, max_bytes, expected_type=""):
    buffer = BytesIO()
    write_limited_response(
        response,
        buffer,
        max_bytes,
        expected_type=expected_type,
    )
    return buffer.getvalue()


def validate_response_content_type(content_type, expected_type):
    if not expected_type:
        return
    if not content_type or not content_type_matches_expected(
        content_type,
        expected_type,
    ):
        raise XhsDownloadError(
            f"资源类型异常：{content_type or 'missing Content-Type'}"
        )


def is_recoverable_provider_download_error(error):
    if isinstance(error, requests.RequestException):
        return True
    if not isinstance(error, XhsDownloadError):
        return False
    message = str(error)
    return message.startswith("HTTP ") or message.startswith("资源类型异常：")


def tiantianjiexi_douyin_manifest_is_allowed(item):
    if not isinstance(item, dict) or item.get("provider") != "tiantianjiexi":
        return False
    if not provider_manifest_is_valid(item):
        return False
    aweme_id = str(item.get("provider_resource_id") or "")
    if not aweme_id.isdigit():
        return False
    expected_cache_key = f"douyin:tiantianjiexi:validated:{aweme_id}"
    if str(item.get("provider_cache_key") or "") != expected_cache_key:
        return False
    expected_asset_id = f"douyin:{aweme_id}:video:tiantianjiexi"
    if str(item.get("asset_id") or "") != expected_asset_id:
        return False
    try:
        validate_external_url(
            item.get("url"),
            DOUYIN_MEDIA_HOST_SUFFIXES,
            "天天解析抖音 Provider URL 不允许请求",
        )
    except (TypeError, ValueError):
        return False
    return True


def provider_manifest_allows_public_download(item):
    if not isinstance(item, dict):
        return False
    if item.get("provider") not in {
        "kukutool",
        "tiantianjiexi",
        "kukutool_generic",
        "tiantianjiexi_generic",
        "gallery_dl_generic",
        "yt_dlp_generic",
        YOUTUBE_MERGE_PROVIDER_ID,
        YTDLP_MERGE_PROVIDER_ID,
    }:
        return False
    if item.get("provider") == "kukutool":
        return bool(get_kukutool_manifest_identity(item))
    if item.get("provider") == "tiantianjiexi":
        return tiantianjiexi_douyin_manifest_is_allowed(item)
    if item.get("provider") == YOUTUBE_MERGE_PROVIDER_ID:
        return provider_manifest_allows_youtube_merge(item)
    if item.get("provider") == YTDLP_MERGE_PROVIDER_ID:
        return provider_manifest_allows_ytdlp_merge(item)
    if not provider_manifest_is_valid(item):
        return False
    if third_party_manifest_item_is_allowed(item):
        return True
    logger.warning(
        "third_party_provider_manifest_rejected asset_id=%s provider_resource_id=%s",
        item.get("asset_id", ""),
        item.get("provider_resource_id", ""),
    )
    return False


def get_external_payload_manifest_identity(item):
    if not isinstance(item, dict):
        return None
    provider = str(item.get("provider") or "")
    if provider not in EXTERNAL_PAYLOAD_CACHE_PROVIDERS:
        return None
    if not provider_manifest_allows_public_download(item):
        return None
    source_key = str(item.get("provider_resource_id") or "")
    expected_cache_key = f"third_party:{provider}:{source_key}"
    if item.get("provider_cache_key") != expected_cache_key:
        return None
    return provider, source_key


def get_generic_kukutool_manifest_identity(item):
    if not isinstance(item, dict):
        return None
    if str(item.get("provider") or "") != GENERIC_KUKUTOOL_CACHE_PROVIDER:
        return None
    if not provider_manifest_allows_public_download(item):
        return None
    source_key = str(item.get("provider_resource_id") or "")
    expected_cache_key = f"third_party:{GENERIC_KUKUTOOL_CACHE_PROVIDER}:{source_key}"
    if item.get("provider_cache_key") != expected_cache_key:
        return None
    return source_key


def invalidate_external_provider_caches(item):
    identity = get_external_payload_manifest_identity(item)
    if identity:
        provider, source_key = identity
        clear_cached_external_payload(provider, source_key)
        clear_cached_external_failure(provider, source_key)
        logger.info(
            "external_provider_cache_invalidated",
            extra={"provider": provider, "source_key": source_key},
        )
        return

    source_key = get_generic_kukutool_manifest_identity(item)
    if not source_key:
        return
    for cache_key in (
        build_generic_kukutool_payload_cache_key(source_key),
        build_generic_kukutool_failure_cache_key(source_key),
    ):
        try:
            cache.delete(cache_key)
        except Exception:
            logger.exception(
                "external_provider_cache_delete_failed",
                extra={
                    "provider": GENERIC_KUKUTOOL_CACHE_PROVIDER,
                    "source_key": source_key,
                    "cache_key": cache_key,
                },
            )
    logger.info(
        "external_provider_cache_invalidated",
        extra={"provider": GENERIC_KUKUTOOL_CACHE_PROVIDER, "source_key": source_key},
    )


def validate_provider_signed_download_url(provider_item, url, message):
    if provider_item is None:
        validate_public_download_url(url, message)
        return url
    validate_public_url(url, message)
    parsed = urlparse(url or "")
    try:
        port = parsed.port
    except ValueError as error:
        raise XhsDownloadError(message) from error
    if port is not None and port not in {80, 443}:
        raise XhsDownloadError(message)
    provider = str(provider_item.get("provider") or "")
    signed_url_item = {
        **provider_item,
        "url": url,
    }
    if provider == YOUTUBE_MERGE_PROVIDER_ID:
        allowed = youtube_merge_manifest_item_is_allowed(signed_url_item)
    elif provider == YTDLP_MERGE_PROVIDER_ID:
        allowed = ytdlp_merge_manifest_item_is_allowed(signed_url_item)
    elif provider == "kukutool":
        allowed = bool(get_kukutool_manifest_identity(provider_item)) and is_byte_cdn_url(url)
    elif provider == "tiantianjiexi":
        allowed = tiantianjiexi_douyin_manifest_is_allowed(signed_url_item)
    else:
        allowed = third_party_manifest_item_is_allowed(signed_url_item)
    if not allowed:
        raise XhsDownloadError(message)
    return url


def build_provider_signed_cover_item(item, cover_url):
    if not cover_url or not provider_manifest_allows_public_download(item):
        return {"type": "image", "url": cover_url}

    provider = str(item.get("provider") or "")
    cover_item = {
        "type": "image",
        "url": cover_url,
        "asset_id": f"{item.get('asset_id', '')}:cover",
        "provider": item.get("provider", ""),
        "provider_resource_id": item.get("provider_resource_id", ""),
        "provider_cache_key": item.get("provider_cache_key", ""),
    }
    if provider == YOUTUBE_MERGE_PROVIDER_ID:
        allowed = youtube_merge_manifest_item_is_allowed(cover_item)
    elif provider == YTDLP_MERGE_PROVIDER_ID:
        allowed = ytdlp_merge_manifest_item_is_allowed(cover_item)
    else:
        allowed = third_party_manifest_item_is_allowed(cover_item)
    if not allowed:
        return {"type": "image", "url": cover_url}

    cover_item["provider_manifest_token"] = build_provider_manifest_token(
        asset_id=cover_item["asset_id"],
        provider=cover_item["provider"],
        provider_resource_id=cover_item["provider_resource_id"],
        provider_cache_key=cover_item["provider_cache_key"],
        url=cover_url,
        media_type="image",
    )
    return cover_item


def extract_youtube_cover_video_id(url):
    parsed = urlparse(str(url or ""))
    if not host_matches(parsed.hostname or "", ("ytimg.com",)):
        return ""
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 3 or parts[0] not in {"vi", "vi_webp"}:
        return ""
    return parts[1]


def iter_youtube_cover_fallback_urls(url):
    video_id = extract_youtube_cover_video_id(url)
    if not video_id:
        return ()
    return tuple(
        f"https://i.ytimg.com/vi/{video_id}/{name}"
        for name in YOUTUBE_COVER_FALLBACK_NAMES
    )


def build_provider_signed_variant_item(provider_item, url):
    item = {**provider_item, "url": url}
    item["provider_manifest_token"] = build_provider_manifest_token(
        asset_id=item.get("asset_id", ""),
        provider=item.get("provider", ""),
        provider_resource_id=item.get("provider_resource_id", ""),
        provider_cache_key=item.get("provider_cache_key", ""),
        url=url,
        media_type=item.get("type", ""),
    )
    return item


def iter_provider_signed_request_candidates(url, provider_item):
    yield url, provider_item
    if not isinstance(provider_item, dict):
        return
    item_type = str(provider_item.get("type") or "").lower()
    asset_id = str(provider_item.get("asset_id") or "")
    if item_type != "image" or not asset_id.endswith(":cover"):
        return

    original_video_id = extract_youtube_cover_video_id(url)
    if not original_video_id:
        return

    seen = {url}
    for fallback_url in iter_youtube_cover_fallback_urls(url):
        if fallback_url in seen:
            continue
        seen.add(fallback_url)
        fallback_item = build_provider_signed_variant_item(provider_item, fallback_url)
        if provider_manifest_allows_public_download(fallback_item):
            yield fallback_url, fallback_item


def request_public_provider_get_with_retry(
    url,
    headers=None,
    timeout=30,
    retries=3,
    stream=True,
    max_redirects=5,
    provider_item=None,
):
    if provider_item is not None and not provider_manifest_allows_public_download(provider_item):
        raise XhsDownloadError("Provider 签名资源未通过校验")

    last_error = None
    for candidate_url, candidate_item in iter_provider_signed_request_candidates(
        url,
        provider_item,
    ):
        for _ in range(max(1, int(retries or 1))):
            current_url = candidate_url
            try:
                for _ in range(max_redirects + 1):
                    validate_provider_signed_download_url(
                        candidate_item,
                        current_url,
                        "Provider URL 不允许请求",
                    )
                    response = requests.get(
                        current_url,
                        headers=headers or get_media_headers(current_url),
                        timeout=timeout,
                        allow_redirects=False,
                        stream=stream,
                    )
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("Location")
                        response.close()
                        if not location:
                            raise XhsDownloadError(f"HTTP {response.status_code}")
                        next_url = urljoin(current_url, location)
                        validate_provider_signed_download_url(
                            candidate_item,
                            next_url,
                            "Provider URL 跳转到了不允许的媒体域名",
                        )
                        current_url = next_url
                        continue
                    if response.status_code == 200:
                        try:
                            validate_provider_signed_download_url(
                                candidate_item,
                                response.url,
                                "Provider URL 跳转到了不允许的媒体域名",
                            )
                        except Exception:
                            response.close()
                            raise
                        return response
                    last_error = XhsDownloadError(f"HTTP {response.status_code}")
                    response.close()
                    break
                else:
                    last_error = XhsDownloadError("Provider URL 重定向次数过多")
            except Exception as error:
                last_error = error

    raise last_error


def fetch_media_response(url, headers=None, timeout=30, retries=3, provider_signed=False):
    if provider_signed:
        if not isinstance(provider_signed, dict):
            raise XhsDownloadError("Provider signed request is missing manifest context")
        return request_public_provider_get_with_retry(
            url,
            headers=headers or get_media_headers(url),
            timeout=timeout,
            retries=retries,
            stream=True,
            provider_item=provider_signed,
        )
    validate_media_url(url)
    return request_get_with_retry(
        url,
        headers=headers or get_media_headers(url),
        timeout=timeout,
        retries=max(1, int(retries or 1)),
        stream=True,
        allowed_final_host_suffixes=MEDIA_HOST_SUFFIXES,
        final_url_message="资源 URL 跳转到了不允许的下载域名",
    )


def probe_media_size(url, headers=None, timeout=6):
    result = QualityProbe().probe(url, headers=headers or get_media_headers(url), timeout=timeout)
    return result.size_bytes if result.reachable else 0


def fetch_to_temp_path(
    url,
    headers=None,
    timeout=30,
    max_bytes=MAX_IMAGE_BYTES,
    progress_callback=None,
    expected_type="",
    retries=3,
    provider_signed=False,
):
    response = fetch_media_response(
        url,
        headers=headers,
        timeout=timeout,
        retries=retries,
        provider_signed=provider_signed,
    )
    fd, temp_path = tempfile.mkstemp(prefix="xhs_media_")

    try:
        with os.fdopen(fd, "wb") as temp_file:
            fd = None
            with response:
                content_type = response.headers.get("Content-Type", "")
                validate_response_content_type(content_type, expected_type)
                final_url = response.url
                size = write_limited_response(
                    response,
                    temp_file,
                    max_bytes=max_bytes,
                    progress_callback=progress_callback,
                    expected_type=expected_type,
                )
        return {
            "path": temp_path,
            "content_type": content_type,
            "final_url": final_url,
            "size": size,
        }
    except Exception:
        response.close()
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        Path(temp_path).unlink(missing_ok=True)
        raise


def fetch_item_to_temp_path(
    item,
    url=None,
    headers=None,
    timeout=30,
    max_bytes=MAX_IMAGE_BYTES,
    progress_callback=None,
    expected_type="",
):
    if (
        str(item.get("provider") or "") == YOUTUBE_MERGE_PROVIDER_ID
        and str(item.get("type") or "") == "video"
    ):
        return download_youtube_merge_to_temp_path(item, progress_callback=progress_callback)
    if (
        str(item.get("provider") or "") == YTDLP_MERGE_PROVIDER_ID
        and str(item.get("type") or "") == "video"
    ):
        return download_ytdlp_merge_to_temp_path(item, progress_callback=progress_callback)

    initial_url = url or item.get("url")
    kukutool_recovery_enabled = bool(get_kukutool_manifest_identity(item))
    tiantianjiexi_recovery_enabled = bool(
        get_tiantianjiexi_manifest_identity(item)
    )
    recovery_enabled = kukutool_recovery_enabled or tiantianjiexi_recovery_enabled
    provider_item = {
        **item,
        "url": initial_url,
    }
    provider_signed = (
        provider_item
        if provider_manifest_allows_public_download(provider_item)
        else None
    )
    attempted_urls = set()
    candidates = [(initial_url, provider_item)]
    recovery_urls = None
    recovery_attempted = False
    last_error = None
    provider_download_timeout = timeout
    if kukutool_recovery_enabled:
        try:
            configured_timeout = runtime_float(
                "kukutool_download_timeout_seconds",
                getattr(settings, "KUKUTOOL_DOWNLOAD_TIMEOUT_SECONDS", 8.0)
                or 8.0,
                minimum=1.0,
                maximum=180.0,
            )
        except (TypeError, ValueError):
            configured_timeout = 8.0
        provider_download_timeout = max(1.0, min(float(timeout or configured_timeout), configured_timeout))

    while candidates or recovery_urls is not None:
        if not candidates:
            try:
                candidates.append((next(recovery_urls), provider_item))
            except StopIteration:
                recovery_urls = None
                continue
        candidate_url, candidate_provider_item = candidates.pop(0)
        if not candidate_url or candidate_url in attempted_urls:
            continue
        attempted_urls.add(candidate_url)
        candidate_provider_signed = (
            candidate_provider_item
            if provider_manifest_allows_public_download(candidate_provider_item)
            else None
        )
        if kukutool_recovery_enabled and not is_byte_cdn_url(candidate_url):
            candidate_provider_signed = None
        try:
            return fetch_to_temp_path(
                candidate_url,
                headers=headers or get_media_headers(candidate_url),
                timeout=provider_download_timeout if candidate_provider_signed else timeout,
                max_bytes=max_bytes,
                progress_callback=progress_callback,
                expected_type=expected_type,
                retries=1 if recovery_enabled else 3,
                provider_signed=candidate_provider_signed,
            )
        except Exception as error:
            last_error = error
            if (
                provider_signed
                and is_recoverable_provider_download_error(error)
            ):
                invalidate_external_provider_caches(provider_item)
            if (
                recovery_enabled
                and not recovery_attempted
                and len(attempted_urls) == 1
                and is_recoverable_provider_download_error(error)
            ):
                recovery_attempted = True
                if tiantianjiexi_recovery_enabled:
                    try:
                        refreshed_item = refresh_tiantianjiexi_download_item(item)
                    except Exception as refresh_error:
                        last_error = refresh_error
                    else:
                        candidates.append(
                            (refreshed_item.get("url"), refreshed_item)
                        )
                else:
                    recovery_urls = iter(
                        iter_kukutool_download_recovery_urls(item) or ()
                    )

    raise last_error or XhsDownloadError("资源下载失败")


def fetch_cover_to_temp_path(
    item,
    cover_url,
    timeout=30,
    max_bytes=MAX_IMAGE_BYTES,
    progress_callback=None,
):
    if youtube_merge_manifest_identity(item):
        cover_item = {
            "type": "image",
            "url": cover_url,
            "asset_id": f"{item.get('asset_id', '')}:cover",
            "provider": item.get("provider", ""),
            "provider_resource_id": item.get("provider_resource_id", ""),
            "provider_cache_key": item.get("provider_cache_key", ""),
        }
        cover_item["provider_manifest_token"] = build_provider_manifest_token(
            asset_id=cover_item["asset_id"],
            provider=cover_item["provider"],
            provider_resource_id=cover_item["provider_resource_id"],
            provider_cache_key=cover_item["provider_cache_key"],
            url=cover_url,
            media_type="image",
        )
        return fetch_item_to_temp_path(
            cover_item,
            timeout=timeout,
            max_bytes=max_bytes,
            progress_callback=progress_callback,
            expected_type="image",
        )
    return fetch_item_to_temp_path(
        build_provider_signed_cover_item(item, cover_url),
        timeout=timeout,
        max_bytes=max_bytes,
        progress_callback=progress_callback,
        expected_type="image",
    )


def fetch_binary(
    url,
    headers=None,
    timeout=30,
    max_bytes=MAX_IMAGE_BYTES,
    expected_type="",
    retries=3,
):
    response = fetch_media_response(
        url,
        headers=headers,
        timeout=timeout,
        retries=retries,
    )
    with response:
        content_type = response.headers.get("Content-Type", "")
        validate_response_content_type(content_type, expected_type)
        return read_limited_response(
            response,
            max_bytes=max_bytes,
            expected_type=expected_type,
        )


def fetch_item_binary(
    item,
    url=None,
    headers=None,
    timeout=30,
    max_bytes=MAX_IMAGE_BYTES,
    expected_type="",
):
    temp_result = fetch_item_to_temp_path(
        item,
        url=url,
        headers=headers,
        timeout=timeout,
        max_bytes=max_bytes,
        expected_type=expected_type,
    )
    try:
        return Path(temp_result["path"]).read_bytes()
    finally:
        Path(temp_result["path"]).unlink(missing_ok=True)


def fetch_cover_binary(item, cover_url, timeout=30, max_bytes=MAX_IMAGE_BYTES):
    return fetch_item_binary(
        build_provider_signed_cover_item(item, cover_url),
        timeout=timeout,
        max_bytes=max_bytes,
        expected_type="image",
    )


def iter_download_files(media_items, base_filename, save_format=ORIGINAL_FORMAT, quality=95, video_save_mode="video"):
    base_filename = clean_filename(base_filename or "xhs")
    save_format = normalize_save_format(save_format)
    quality = normalize_quality(quality)
    video_save_mode = normalize_video_save_mode(video_save_mode)
    failures = []

    for index, item in enumerate(media_items, start=1):
        media_type = item.get("type", "image")
        url = item.get("url")

        try:
            if not url:
                raise XhsDownloadError("资源 URL 为空")

            if media_type == "image":
                if save_format == ORIGINAL_FORMAT:
                    temp_result = fetch_item_to_temp_path(
                        item,
                        timeout=20,
                        max_bytes=MAX_IMAGE_BYTES,
                        expected_type="image",
                    )
                    try:
                        ext = guess_image_extension(temp_result["final_url"], temp_result["content_type"])
                        filename = clean_filename(f"{base_filename}_{index}.{ext}")
                        yield filename, Path(temp_result["path"]).read_bytes()
                    finally:
                        Path(temp_result["path"]).unlink(missing_ok=True)
                else:
                    content = fetch_item_binary(
                        item,
                        timeout=20,
                        max_bytes=MAX_IMAGE_BYTES,
                        expected_type="image",
                    )
                    with Image.open(BytesIO(content)) as image:
                        filename = clean_filename(f"{base_filename}_{index}.{save_format}")
                        yield filename, save_image_to_buffer(image, save_format, quality)

            elif media_type == "video":
                if video_save_mode in ["video", "both"]:
                    ext = guess_video_extension(url)
                    filename = clean_filename(f"{base_filename}_{index}.{ext}")
                    yield filename, fetch_item_binary(
                        item,
                        timeout=60,
                        max_bytes=MAX_VIDEO_BYTES,
                        expected_type="video",
                    )

                if video_save_mode in ["cover", "both"]:
                    cover_url = item.get("cover_url")
                    if not cover_url:
                        raise XhsDownloadError("当前视频没有解析到封面")
                    if save_format == ORIGINAL_FORMAT:
                        temp_result = fetch_cover_to_temp_path(
                            item,
                            cover_url,
                            timeout=20,
                            max_bytes=MAX_IMAGE_BYTES,
                        )
                        try:
                            ext = guess_image_extension(temp_result["final_url"], temp_result["content_type"])
                            filename = clean_filename(f"{base_filename}_{index}_cover.{ext}")
                            yield filename, Path(temp_result["path"]).read_bytes()
                        finally:
                            Path(temp_result["path"]).unlink(missing_ok=True)
                    else:
                        content = fetch_cover_binary(
                            item,
                            cover_url,
                            timeout=20,
                            max_bytes=MAX_IMAGE_BYTES,
                        )
                        with Image.open(BytesIO(content)) as image:
                            filename = clean_filename(f"{base_filename}_{index}_cover.{save_format}")
                            yield filename, save_image_to_buffer(image, save_format, quality)

            else:
                raise XhsDownloadError(f"暂不支持的资源类型：{media_type}")

        except Exception as error:
            failures.append(f"{media_type} #{index}: {public_download_error_message(error)}")

    if failures:
        yield "failed.txt", "\n".join(failures).encode("utf-8")


def assert_download_request(media_items):
    if not media_items:
        raise XhsDownloadError("没有选择资源")
    if len(media_items) > MAX_DOWNLOAD_ITEMS:
        raise XhsDownloadError(f"一次最多下载 {MAX_DOWNLOAD_ITEMS} 个资源")


def assert_archive_total(current_total, next_size):
    if current_total + next_size > MAX_ARCHIVE_BYTES:
        raise XhsDownloadError("本次下载资源总大小超过允许限制")


def write_archive_entry_from_temp(archive, temp_result, filename, current_total):
    assert_archive_total(current_total, temp_result["size"])
    archive.write(temp_result["path"], filename)
    return temp_result["size"]


def write_archive_entry_from_bytes(archive, filename, content, current_total):
    assert_archive_total(current_total, len(content))
    archive.writestr(filename, content)
    return len(content)


def build_download_archive_file(
    media_items,
    base_filename,
    save_format=ORIGINAL_FORMAT,
    quality=95,
    video_save_mode="video",
    progress_callback=None,
):
    assert_download_request(media_items)

    base_filename = clean_filename(base_filename or "xhs")
    save_format = normalize_save_format(save_format)
    quality = normalize_quality(quality)
    video_save_mode = normalize_video_save_mode(video_save_mode)
    archive_file = tempfile.NamedTemporaryFile(prefix="xhs_archive_", suffix=".zip")
    saved_count = 0
    total_size = 0
    failures = []
    processed_count = 0
    total_items = len(media_items)

    def report_item_progress(item_index, bytes_read, content_length, fallback_size=0):
        if not progress_callback:
            return
        total_bytes = content_length or safe_download_size(fallback_size)
        if not total_bytes:
            return
        item_fraction = min(bytes_read / total_bytes, 0.98)
        progress_callback((item_index - 1) + item_fraction, total_items)

    try:
        with ZipFile(archive_file, "w", ZIP_DEFLATED, allowZip64=True) as archive:
            for index, item in enumerate(media_items, start=1):
                media_type = item.get("type", "image")
                url = item.get("url")

                try:
                    if not url:
                        raise XhsDownloadError("资源 URL 为空")

                    if media_type == "image":
                        if save_format == ORIGINAL_FORMAT:
                            temp_result = fetch_item_to_temp_path(
                                item,
                                timeout=20,
                                max_bytes=MAX_IMAGE_BYTES,
                                expected_type="image",
                                progress_callback=lambda done, total, item_index=index: report_item_progress(
                                    item_index,
                                    done,
                                    total,
                                    item.get("size_bytes"),
                                ),
                            )
                            try:
                                ext = guess_image_extension(temp_result["final_url"], temp_result["content_type"])
                                filename = clean_filename(f"{base_filename}_{index}.{ext}")
                                total_size += write_archive_entry_from_temp(
                                    archive,
                                    temp_result,
                                    filename,
                                    total_size,
                                )
                                saved_count += 1
                            finally:
                                Path(temp_result["path"]).unlink(missing_ok=True)
                        else:
                            content = fetch_item_binary(
                                item,
                                timeout=20,
                                max_bytes=MAX_IMAGE_BYTES,
                                expected_type="image",
                            )
                            with Image.open(BytesIO(content)) as image:
                                filename = clean_filename(f"{base_filename}_{index}.{save_format}")
                                converted = save_image_to_buffer(image, save_format, quality)
                            total_size += write_archive_entry_from_bytes(archive, filename, converted, total_size)
                            saved_count += 1

                    elif media_type == "video":
                        if video_save_mode in ["video", "both"]:
                            temp_result = fetch_item_to_temp_path(
                                item,
                                timeout=60,
                                max_bytes=MAX_VIDEO_BYTES,
                                expected_type="video",
                                progress_callback=lambda done, total, item_index=index: report_item_progress(
                                    item_index,
                                    done,
                                    total,
                                    item.get("size_bytes"),
                                ),
                            )
                            try:
                                ext = guess_video_extension_from_response(
                                    temp_result["final_url"],
                                    temp_result["content_type"],
                                )
                                filename = clean_filename(f"{base_filename}_{index}.{ext}")
                                total_size += write_archive_entry_from_temp(
                                    archive,
                                    temp_result,
                                    filename,
                                    total_size,
                                )
                                saved_count += 1
                            finally:
                                Path(temp_result["path"]).unlink(missing_ok=True)

                        if video_save_mode in ["cover", "both"]:
                            cover_url = item.get("cover_url")
                            if not cover_url:
                                raise XhsDownloadError("当前视频没有解析到封面")

                            if save_format == ORIGINAL_FORMAT:
                                temp_result = fetch_cover_to_temp_path(
                                    item,
                                    cover_url,
                                    timeout=20,
                                    max_bytes=MAX_IMAGE_BYTES,
                                    progress_callback=lambda done, total, item_index=index: report_item_progress(
                                        item_index,
                                        done,
                                        total,
                                        item.get("cover_size_bytes") or item.get("size_bytes"),
                                    ),
                                )
                                try:
                                    ext = guess_image_extension(temp_result["final_url"], temp_result["content_type"])
                                    filename = clean_filename(f"{base_filename}_{index}_cover.{ext}")
                                    total_size += write_archive_entry_from_temp(
                                        archive,
                                        temp_result,
                                        filename,
                                        total_size,
                                    )
                                    saved_count += 1
                                finally:
                                    Path(temp_result["path"]).unlink(missing_ok=True)
                            else:
                                content = fetch_cover_binary(
                                    item,
                                    cover_url,
                                    timeout=20,
                                    max_bytes=MAX_IMAGE_BYTES,
                                )
                                with Image.open(BytesIO(content)) as image:
                                    filename = clean_filename(f"{base_filename}_{index}_cover.{save_format}")
                                    converted = save_image_to_buffer(image, save_format, quality)
                                total_size += write_archive_entry_from_bytes(
                                    archive,
                                    filename,
                                    converted,
                                    total_size,
                                )
                                saved_count += 1

                    else:
                        raise XhsDownloadError(f"暂不支持的资源类型：{media_type}")

                except Exception as error:
                    failures.append(f"{media_type} #{index}: {public_download_error_message(error)}")
                finally:
                    processed_count += 1
                    if progress_callback:
                        progress_callback(processed_count, total_items)

            if failures:
                archive.writestr("failed.txt", "\n".join(failures).encode("utf-8"))

        if saved_count == 0:
            detail = f"：{failures[0]}" if failures else ""
            raise XhsDownloadError(public_download_error_message(f"没有资源保存成功{detail}"))

        archive_file.seek(0)
        return {
            "filename": clean_filename(f"{base_filename}.zip"),
            "file": archive_file,
            "saved_count": saved_count,
            "failed_count": len(failures),
        }

    except Exception:
        archive_file.close()
        raise


def build_download_archive(media_items, base_filename, save_format=ORIGINAL_FORMAT, quality=95, video_save_mode="video"):
    archive_file = build_download_archive_file(media_items, base_filename, save_format, quality, video_save_mode)

    try:
        content = archive_file["file"].read()
    finally:
        archive_file["file"].close()

    return {
        "filename": archive_file["filename"],
        "content": content,
        "saved_count": archive_file["saved_count"],
        "failed_count": archive_file["failed_count"],
    }


def resolve_server_save_folder(save_dir, allow_absolute=False):
    save_dir = (save_dir or settings.XHS_DEFAULT_SERVER_SAVE_DIR or "xhs").strip()
    if not save_dir:
        save_dir = "xhs"

    candidate = Path(save_dir).expanduser()
    if candidate.is_absolute():
        if not allow_absolute:
            raise XhsDownloadError("当前环境不允许保存到绝对路径")
        return candidate

    media_root = Path(settings.MEDIA_ROOT).resolve()
    target = (media_root / candidate).resolve()
    try:
        target.relative_to(media_root)
    except ValueError as error:
        raise XhsDownloadError("保存路径不能跳出 MEDIA_ROOT") from error
    return target


def save_media_items_to_server(
    media_items,
    base_filename,
    save_dir=None,
    save_format=ORIGINAL_FORMAT,
    quality=95,
    video_save_mode="video",
    allow_absolute=False,
):
    assert_download_request(media_items)

    base_filename = clean_filename(base_filename or "xhs")
    save_format = normalize_save_format(save_format)
    quality = normalize_quality(quality)
    video_save_mode = normalize_video_save_mode(video_save_mode)
    save_folder = resolve_server_save_folder(save_dir, allow_absolute=allow_absolute)
    save_folder.mkdir(parents=True, exist_ok=True)

    saved_files = []
    failures = []

    for index, item in enumerate(media_items, start=1):
        media_type = item.get("type", "image")
        url = item.get("url")

        try:
            if not url:
                raise XhsDownloadError("资源 URL 为空")

            if media_type == "image":
                if save_format == ORIGINAL_FORMAT:
                    temp_result = fetch_item_to_temp_path(
                        item,
                        timeout=20,
                        max_bytes=MAX_IMAGE_BYTES,
                        expected_type="image",
                    )
                    try:
                        ext = guess_image_extension(temp_result["final_url"], temp_result["content_type"])
                        filename = clean_filename(f"{base_filename}_{index}.{ext}")
                        target = save_folder / filename
                        shutil.move(temp_result["path"], target)
                        saved_files.append(str(target))
                    finally:
                        Path(temp_result["path"]).unlink(missing_ok=True)
                else:
                    content = fetch_item_binary(
                        item,
                        timeout=20,
                        max_bytes=MAX_IMAGE_BYTES,
                        expected_type="image",
                    )
                    with Image.open(BytesIO(content)) as image:
                        filename = clean_filename(f"{base_filename}_{index}.{save_format}")
                        converted = save_image_to_buffer(image, save_format, quality)
                    target = save_folder / filename
                    target.write_bytes(converted)
                    saved_files.append(str(target))

            elif media_type == "video":
                if video_save_mode in ["video", "both"]:
                    temp_result = fetch_item_to_temp_path(
                        item,
                        timeout=60,
                        max_bytes=MAX_VIDEO_BYTES,
                        expected_type="video",
                    )
                    try:
                        ext = guess_video_extension_from_response(
                            temp_result["final_url"],
                            temp_result["content_type"],
                        )
                        filename = clean_filename(f"{base_filename}_{index}.{ext}")
                        target = save_folder / filename
                        shutil.move(temp_result["path"], target)
                        saved_files.append(str(target))
                    finally:
                        Path(temp_result["path"]).unlink(missing_ok=True)

                if video_save_mode in ["cover", "both"]:
                    cover_url = item.get("cover_url")
                    if not cover_url:
                        raise XhsDownloadError("当前视频没有解析到封面")

                    if save_format == ORIGINAL_FORMAT:
                        temp_result = fetch_cover_to_temp_path(
                            item,
                            cover_url,
                            timeout=20,
                            max_bytes=MAX_IMAGE_BYTES,
                        )
                        try:
                            ext = guess_image_extension(temp_result["final_url"], temp_result["content_type"])
                            filename = clean_filename(f"{base_filename}_{index}_cover.{ext}")
                            target = save_folder / filename
                            shutil.move(temp_result["path"], target)
                            saved_files.append(str(target))
                        finally:
                            Path(temp_result["path"]).unlink(missing_ok=True)
                    else:
                        content = fetch_cover_binary(
                            item,
                            cover_url,
                            timeout=20,
                            max_bytes=MAX_IMAGE_BYTES,
                        )
                        with Image.open(BytesIO(content)) as image:
                            filename = clean_filename(f"{base_filename}_{index}_cover.{save_format}")
                            converted = save_image_to_buffer(image, save_format, quality)
                        target = save_folder / filename
                        target.write_bytes(converted)
                        saved_files.append(str(target))

            else:
                raise XhsDownloadError(f"暂不支持的资源类型：{media_type}")

        except Exception as error:
            failures.append(f"{media_type} #{index}: {public_download_error_message(error)}")

    if failures:
        (save_folder / "failed.txt").write_text("\n".join(failures), encoding="utf-8")

    if not saved_files:
        detail = f"：{failures[0]}" if failures else ""
        raise XhsDownloadError(public_download_error_message(f"没有资源保存成功{detail}"))

    return {
        "save_folder": str(save_folder),
        "saved_count": len(saved_files),
        "failed_count": len(failures),
        "saved_files": saved_files,
    }
