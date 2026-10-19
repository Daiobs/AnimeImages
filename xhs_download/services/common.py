import base64
import ipaddress
import math
import re
import socket
import time
from urllib.parse import urldefrag, urljoin, urlparse

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

PAGE_HOST_SUFFIXES = (
    "xiaohongshu.com",
    "xhslink.com",
    "douyin.com",
    "iesdouyin.com",
    "bilibili.com",
    "b23.tv",
)
MEDIA_HOST_SUFFIXES = (
    "xiaohongshu.com",
    "xhscdn.com",
    "amemv.com",
    "snssdk.com",
    "douyin.com",
    "douyinpic.com",
    "douyinvod.com",
    "douyincdn.com",
    "douyinstatic.com",
    "byteimg.com",
    "bytetos.com",
    "365yg.com",
    "zjcdn.com",
    "bilivideo.com",
    "bilivideo.cn",
    "hdslb.com",
)
URL_PATTERN = re.compile(r"https?://[^\s\"'<>]+")
TRAILING_URL_CHARS = "，,。；;！!？?）)]】》"


class XhsError(ValueError):
    pass


class XhsSecurityError(XhsError):
    pass


class XhsParseError(XhsError):
    pass


class XhsProviderTimeoutError(XhsParseError):
    pass


class XhsDownloadError(XhsError):
    pass


def clean_filename(filename):
    filename = re.sub(r"[\x00-\x1f\x7f]", "", str(filename or ""))
    filename = re.sub(r'[\\/*?:"<>|]', "", filename)
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


def extract_url_from_text(value):
    value = (value or "").strip()
    match = URL_PATTERN.search(value)
    candidate = match.group(0).rstrip(TRAILING_URL_CHARS) if match else value
    parsed = urlparse(candidate)
    if parsed.scheme in {"http", "https"} and parsed.hostname:
        return candidate
    return value


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


def is_blocked_ip_address(address):
    """Public-download DNS answers must be globally routable, including no CGNAT."""
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return True

    return (
        not parsed.is_global
        or parsed.is_loopback
        or parsed.is_private
        or parsed.is_link_local
        or parsed.is_multicast
        or parsed.is_reserved
        or parsed.is_unspecified
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


def validate_public_url(url, message="不允许请求该 URL"):
    parsed = urlparse(url or "")
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise XhsSecurityError(message)
    if is_blocked_hostname(parsed.hostname):
        raise XhsSecurityError(message)
    return url


def validate_public_download_url(url, message="不允许请求该 URL"):
    parsed = urlparse(url or "")
    validate_public_url(url, message)
    try:
        port = parsed.port
    except ValueError as error:
        raise XhsSecurityError(message) from error
    if port is not None and port not in {80, 443}:
        raise XhsSecurityError(message)

    try:
        results = socket.getaddrinfo(
            parsed.hostname,
            port or (443 if parsed.scheme == "https" else 80),
            type=socket.SOCK_STREAM,
        )
    except OSError as error:
        raise XhsSecurityError(message) from error

    addresses = {item[4][0] for item in results if item and item[4]}
    if not addresses or any(is_blocked_ip_address(address) for address in addresses):
        raise XhsSecurityError(message)
    return url


def validate_page_url(url):
    return validate_external_url(url, PAGE_HOST_SUFFIXES, "请输入正确的小红书链接")


def validate_media_url(url):
    return validate_external_url(url, MEDIA_HOST_SUFFIXES, "资源 URL 不在允许的下载域名内")


def _validate_request_target(url, message):
    try:
        if re.search(r"[\x00-\x20\x7f]", url):
            raise XhsSecurityError(message)
        parsed = urlparse(url)
        if parsed.username is not None or parsed.password is not None:
            raise XhsSecurityError(message)
        validate_public_download_url(url, message)
    except (TypeError, ValueError) as error:
        raise XhsSecurityError(message) from error
    return parsed


def _request_origin(parsed):
    return parsed.scheme, parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)


class _SingleHopSession(requests.Session):
    def prepare_request(self, request):
        # Suppress only .netrc injection; normal environment CA/proxy/NO_PROXY
        # handling in Session.request and Session.send remains enabled.
        trust_env = self.trust_env
        self.trust_env = False
        try:
            return super().prepare_request(request)
        finally:
            self.trust_env = trust_env

    def resolve_redirects(self, *args, **kwargs):
        # Requests otherwise drains redirect bodies to prepare Response.next,
        # even with allow_redirects=False and stream=True.
        return iter(())


def request_get_with_retry(
    url,
    headers=None,
    timeout=15,
    retries=3,
    sleep_seconds=0.8,
    stream=False,
    allowed_final_host_suffixes=None,
    final_url_message="请求跳转到了不允许的 URL",
    deadline_monotonic=None,
    max_redirects=5,
):
    """Validate each hop before sending; DNS checks do not pin the connection IP.

    Callers retain their initial-host policy. Intermediates must be public, but
    the final-host allowlist is intentionally applied only to the final response.
    The deadline covers fetching the response, not consumption of a returned stream.
    """
    retries = max(1, int(retries))
    max_redirects = max(0, int(max_redirects))
    phases = tuple(timeout) if isinstance(timeout, (tuple, list)) else (timeout, timeout)
    phases = tuple(float(value) for value in phases)
    if len(phases) != 2 or any(not math.isfinite(value) or value <= 0 for value in phases):
        raise ValueError("timeout must contain positive finite connect/read budgets")
    sleep_seconds = max(0.0, float(sleep_seconds))
    if not math.isfinite(sleep_seconds):
        raise ValueError("sleep_seconds must be finite")
    if deadline_monotonic is None:
        deadline_monotonic = (
            time.monotonic() + sum(phases) * retries
            + sleep_seconds * retries * (retries - 1) / 2
        )
    deadline_monotonic = float(deadline_monotonic)
    if not math.isfinite(deadline_monotonic):
        raise ValueError("deadline_monotonic must be finite")
    last_error = None

    def remaining_budget():
        remaining = deadline_monotonic - time.monotonic()
        if remaining <= 0:
            raise requests.Timeout("请求超过时间限制") from last_error
        return remaining

    for attempt in range(retries):
        try:
            current_url = urldefrag(url)[0]
        except (TypeError, ValueError) as error:
            raise XhsSecurityError(final_url_message) from error
        current_headers = dict(headers or HEADERS)
        visited = set()
        with _SingleHopSession() as session:
            for hop in range(max_redirects + 1):
                remaining_budget()
                if current_url in visited:
                    raise requests.TooManyRedirects("请求出现循环跳转")
                visited.add(current_url)
                current = _validate_request_target(current_url, final_url_message)
                # DNS resolution consumes the same budget as all hops and retries.
                phase_budget = remaining_budget() / 2
                request_timeout = tuple(min(value, phase_budget) for value in phases)
                if not isinstance(timeout, (tuple, list)):
                    request_timeout = request_timeout[0]
                response = None
                try:
                    response = session.get(
                        current_url,
                        headers=current_headers,
                        timeout=request_timeout,
                        allow_redirects=False,
                        stream=True,
                    )
                    remaining_budget()
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("Location")
                        if not location or not location.strip():
                            raise XhsDownloadError("请求跳转缺少 Location")
                        if hop == max_redirects:
                            raise requests.TooManyRedirects("请求跳转次数过多")
                        try:
                            next_url = urldefrag(urljoin(current_url, location))[0]
                            next_target = urlparse(next_url)
                            changed_origin = _request_origin(current) != _request_origin(next_target)
                        except ValueError as error:
                            raise XhsSecurityError(final_url_message) from error
                        if changed_origin:
                            # Drop custom tokens too, not only standard auth headers.
                            current_headers = {
                                key: value for key, value in current_headers.items()
                                if key.lower() in {
                                    "accept", "accept-encoding", "accept-language",
                                    "user-agent", "range", "if-range",
                                }
                            }
                            session.cookies.clear()
                        current_url = next_url
                        continue

                    if response.status_code == 200:
                        final_url = response.url or current_url
                        if final_url != current_url:
                            _validate_request_target(final_url, final_url_message)
                        if allowed_final_host_suffixes:
                            validate_external_url(
                                final_url,
                                allowed_final_host_suffixes,
                                final_url_message,
                            )
                        if not stream:
                            chunks = []
                            for chunk in response.iter_content(chunk_size=64 * 1024):
                                remaining_budget()
                                chunks.append(chunk)
                            remaining_budget()
                            response._content = b"".join(chunks)
                            response._content_consumed = True
                        result, response = response, None
                        return result

                    last_error = XhsDownloadError(f"HTTP {response.status_code}")
                except requests.RequestException as error:
                    if error.response is not None and error.response is not response:
                        error.response.close()
                    if isinstance(error, requests.TooManyRedirects):
                        raise
                    last_error = error
                finally:
                    if response is not None:
                        response.close()
                break

        if attempt < retries - 1:
            retry_delay = sleep_seconds * (attempt + 1)
            if retry_delay >= remaining_budget():
                raise requests.Timeout("请求超过时间限制") from last_error
            time.sleep(retry_delay)

    raise last_error
