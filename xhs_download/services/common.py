import base64
import ipaddress
import re
import time
from urllib.parse import urlparse

import requests


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.xiaohongshu.com/",
    "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
}

PAGE_HOST_SUFFIXES = ("xiaohongshu.com", "xhslink.com", "douyin.com", "iesdouyin.com")
MEDIA_HOST_SUFFIXES = (
    "xiaohongshu.com",
    "xhscdn.com",
    "douyin.com",
    "douyinpic.com",
    "douyinvod.com",
    "douyincdn.com",
    "douyinstatic.com",
    "byteimg.com",
    "bytetos.com",
)


class XhsError(ValueError):
    pass


class XhsSecurityError(XhsError):
    pass


class XhsParseError(XhsError):
    pass


class XhsDownloadError(XhsError):
    pass


def clean_filename(filename):
    filename = re.sub(r'[\\/*?:"<>|]', "", filename or "")
    filename = filename.strip()
    return filename or "xhs_image"


def encode_url(url):
    return base64.urlsafe_b64encode(url.encode("utf-8")).decode("utf-8")


def decode_url(encoded):
    return base64.urlsafe_b64decode(encoded.encode("utf-8")).decode("utf-8")


def normalize_text(text):
    if not text:
        return ""

    return (
        text
        .replace("\\u002F", "/")
        .replace("\\/", "/")
        .replace("&amp;", "&")
    )


def host_matches(hostname, allowed_suffixes):
    hostname = (hostname or "").lower()
    return any(hostname == suffix or hostname.endswith(f".{suffix}") for suffix in allowed_suffixes)


def is_blocked_hostname(hostname):
    hostname = (hostname or "").strip().lower()
    if not hostname:
        return True
    if hostname in {"localhost", "127.0.0.1", "::1"} or hostname.endswith(".localhost"):
        return True

    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return False

    return (
        address.is_loopback
        or address.is_private
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


def validate_external_url(url, allowed_suffixes, message="不允许请求该 URL"):
    parsed = urlparse(url or "")
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise XhsSecurityError(message)
    if is_blocked_hostname(parsed.hostname):
        raise XhsSecurityError(message)
    if not host_matches(parsed.hostname, allowed_suffixes):
        raise XhsSecurityError(message)
    return url


def validate_page_url(url):
    return validate_external_url(url, PAGE_HOST_SUFFIXES, "请输入正确的小红书链接")


def validate_media_url(url):
    return validate_external_url(url, MEDIA_HOST_SUFFIXES, "资源 URL 不在允许的下载域名内")


def request_get_with_retry(
    url,
    headers=None,
    timeout=15,
    retries=3,
    sleep_seconds=0.8,
    stream=False,
    allowed_final_host_suffixes=None,
    final_url_message="请求跳转到了不允许的 URL",
):
    last_error = None

    for attempt in range(retries):
        try:
            response = requests.get(
                url,
                headers=headers or HEADERS,
                timeout=timeout,
                allow_redirects=True,
                stream=stream,
            )

            if response.status_code == 200:
                if allowed_final_host_suffixes:
                    try:
                        validate_external_url(
                            response.url,
                            allowed_final_host_suffixes,
                            final_url_message,
                        )
                    except Exception:
                        response.close()
                        raise
                return response

            last_error = XhsDownloadError(f"HTTP {response.status_code}")
            response.close()

        except Exception as error:
            last_error = error

        time.sleep(sleep_seconds * (attempt + 1))

    raise last_error
