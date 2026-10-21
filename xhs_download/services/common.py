import base64
import ipaddress
import math
import re
import socket
import time
from urllib.parse import urldefrag, urljoin, urlparse

import requests
from urllib3.exceptions import ConnectTimeoutError, NewConnectionError


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
    def __init__(self, message, *, http_status=None):
        super().__init__(message)
        self.http_status = http_status


class XhsDNSResolutionError(XhsDownloadError):
    def __init__(self, *, temporary=False):
        self.temporary = temporary
        super().__init__("DNS 暂时解析失败，请稍后重试" if temporary else "DNS 解析失败，无法确定目标地址")


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
    _validated_download_addresses(url, message)
    return url


def _validated_download_addresses(url, message):
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
        raise XhsDNSResolutionError(
            temporary=isinstance(error, socket.gaierror) and error.errno == socket.EAI_AGAIN,
        ) from error

    addresses = tuple(dict.fromkeys(item[4][0] for item in results if item and item[4]))
    if not addresses:
        raise XhsDNSResolutionError()
    if any(is_blocked_ip_address(address) for address in addresses):
        raise XhsSecurityError(message)
    return addresses


def validate_page_url(url):
    return validate_external_url(url, PAGE_HOST_SUFFIXES, "请输入正确的小红书链接")


def validate_media_url(url):
    return validate_external_url(url, MEDIA_HOST_SUFFIXES, "资源 URL 不在允许的下载域名内")


def _validate_request_target(url, message, allowed_host_suffixes=None):
    try:
        if re.search(r"[\x00-\x20\x7f]", url):
            raise XhsSecurityError(message)
        parsed = urlparse(url)
        if parsed.username is not None or parsed.password is not None:
            raise XhsSecurityError(message)
        if allowed_host_suffixes:
            validate_external_url(url, allowed_host_suffixes, message)
        addresses = _validated_download_addresses(url, message)
    except XhsDNSResolutionError:
        raise
    except (TypeError, ValueError) as error:
        raise XhsSecurityError(message) from error
    return parsed, addresses


def _request_origin(parsed):
    return parsed.scheme, parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)


class _PinnedAddressAdapter(requests.adapters.HTTPAdapter):
    """One validated hop owns its pools; proxies keep their own DNS boundary."""
    def __init__(self, url, addresses, deadline_monotonic=None):
        super().__init__()
        canonical = requests.Request("GET", url).prepare().url
        self.origin = _request_origin(urlparse(canonical))
        if isinstance(addresses, str):
            addresses = (addresses,)
        self.addresses = tuple(str(ipaddress.ip_address(address)) for address in addresses)
        if not self.addresses or any(is_blocked_ip_address(address) for address in self.addresses):
            raise XhsSecurityError("不允许请求该地址")
        self.deadline_monotonic = deadline_monotonic

    def _pin_pool_connections(self, pool):
        addresses, deadline = self.addresses, self.deadline_monotonic

        class PinnedConnection(pool.ConnectionCls):
            def _new_conn(self):
                original_host, original_timeout = self._dns_host, self.timeout
                connect_deadline = time.monotonic() + original_timeout
                if deadline is not None:
                    connect_deadline = min(connect_deadline, deadline)
                try:
                    for index, address in enumerate(addresses):
                        remaining = connect_deadline - time.monotonic()
                        if remaining <= 0:
                            raise ConnectTimeoutError(self, "已验证地址的连接超过时间限制")
                        self._dns_host = address
                        self.timeout = min(original_timeout, remaining)
                        try:
                            return super()._new_conn()
                        except (NewConnectionError, ConnectTimeoutError):
                            if index == len(addresses) - 1:
                                raise
                finally:
                    self._dns_host, self.timeout = original_host, original_timeout

        # This pool belongs to one admission. Only socket establishment may try
        # another admitted IP; TLS and HTTP are never replayed by this loop.
        pool.ConnectionCls = PinnedConnection
        return pool

    def get_connection_with_tls_context(self, request, verify, proxies=None, cert=None):
        if _request_origin(urlparse(request.url)) != self.origin or verify is False:
            raise XhsSecurityError("请求与已校验的目标不一致")
        parent = super()
        if requests.utils.select_proxy(request.url, proxies):
            # Environment proxy/NO_PROXY/CA behavior remains Requests-owned.
            # The proxy may resolve the origin itself: this path is NOT IP-pinned.
            connect = getattr(parent, "get_connection_with_tls_context", None) or parent._get_connection
            return connect(request, verify, proxies, cert)
        if hasattr(parent, "build_connection_pool_key_attributes"):
            host, options = parent.build_connection_pool_key_attributes(request, verify, cert)
        else:
            # Requests 2.32.0/2.32.1 expose the same TLS-aware operation privately.
            host, options = requests.adapters._urllib3_request_context(request, verify, cert)
        host["host"] = self.addresses[0]
        if host["scheme"] == "https":
            options["server_hostname"] = self.origin[1]
        pool = self.poolmanager.connection_from_host(**host, pool_kwargs=options)
        return self._pin_pool_connections(pool)

    def _get_connection(self, request, verify, proxies=None, cert=None):
        return self.get_connection_with_tls_context(request, verify, proxies, cert)

    def send(self, request, **kwargs):
        parsed = urlparse(request.url)
        host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
        request.headers["Host"] = f"{host}:{parsed.port}" if parsed.port else host
        return super().send(request, **kwargs)


class _MediaRequestObservation:
    """Safe transport facts only; never retain URLs, headers or response bodies."""

    def __init__(self):
        self.values = {
            "http_status": None,
            "final_host": "",
            "final_port": None,
            "redirect_count": 0,
            "referer_lost": False,
            "content_type": "",
            "range_supported": False,
            "request_count": 0,
        }
        self.port_denied = False
        self._previous_origin = None
        self._previous_referer = False
        self._range_end = None

    def target(self, url):
        try:
            parsed = urlparse(url or "")
            host = parsed.hostname or ""
            self.values["final_host"] = (
                host if re.fullmatch(r"[a-zA-Z0-9.:-]{1,253}", host) else ""
            )
            self.values["final_port"] = parsed.port if parsed.port is not None else (
                443 if parsed.scheme == "https" else 80 if parsed.scheme == "http" else None
            )
            self.port_denied = parsed.port is not None and parsed.port not in {80, 443}
        except (TypeError, ValueError):
            self.values["final_port"] = None

    def sent(self, request):
        self.target(request.url)
        origin = _request_origin(urlparse(request.url))
        has_referer = bool(request.headers.get("Referer"))
        if self._previous_origin is not None and origin != self._previous_origin:
            self.values["referer_lost"] |= self._previous_referer and not has_referer
        self._previous_origin = origin
        self._previous_referer = has_referer
        requested_range = re.fullmatch(r"bytes=0-([0-9]{1,20})", request.headers.get("Range", ""))
        self._range_end = int(requested_range[1]) if requested_range else None
        self.values["request_count"] += 1

    def received(self, response):
        self.values["http_status"] = response.status_code
        if response.url:
            self.target(response.url)
        content_type = str(response.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        self.values["content_type"] = (
            content_type if re.fullmatch(r"[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+", content_type)
            and len(content_type) <= 127 else ""
        )
        content_range = re.fullmatch(
            r"bytes 0-([0-9]{1,20})/([0-9]{1,20}|\*)",
            str(response.headers.get("Content-Range") or "").strip(),
        )
        self.values["range_supported"] = bool(
            response.status_code == 206 and content_range and self._range_end is not None
            and int(content_range[1]) <= self._range_end
            and (content_range[2] == "*" or int(content_range[1]) < int(content_range[2]))
        )
        if response.status_code in {301, 302, 303, 307, 308}:
            self.values["redirect_count"] += 1


class _ObservedSingleHopSession(requests.Session):
    def __init__(self, observation=None):
        super().__init__()
        self.observation = observation

    def send(self, request, **kwargs):
        if self.observation is not None:
            self.observation.sent(request)
        try:
            response = super().send(request, **kwargs)
        except requests.RequestException as error:
            if self.observation is not None and error.response is not None:
                self.observation.received(error.response)
            raise
        if self.observation is not None:
            self.observation.received(response)
        return response

    def resolve_redirects(self, *args, **kwargs):
        # Requests otherwise drains redirect bodies to prepare Response.next,
        # even with allow_redirects=False and stream=True.
        return iter(())


class _SingleHopSession(_ObservedSingleHopSession):
    def prepare_request(self, request):
        # Suppress only .netrc injection; normal environment CA/proxy/NO_PROXY
        # handling in Session.request and Session.send remains enabled.
        trust_env = self.trust_env
        self.trust_env = False
        try:
            return super().prepare_request(request)
        finally:
            self.trust_env = trust_env


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
    non_retry_statuses=(),
    return_redirect_response=False,
    accepted_statuses=(200,),
    observation=None,
):
    return _request_with_retry(
        url, headers=headers or HEADERS, timeout=timeout, retries=retries,
        sleep_seconds=sleep_seconds, stream=stream,
        allowed_final_host_suffixes=allowed_final_host_suffixes,
        final_url_message=final_url_message, deadline_monotonic=deadline_monotonic,
        max_redirects=max_redirects,
        non_retry_statuses=non_retry_statuses,
        return_redirect_response=return_redirect_response,
        accepted_statuses=accepted_statuses,
        observation=observation,
    )


class _RedirectBudget:
    """One hop allowance shared by the HEAD/Range pair, including retries."""
    def __init__(self, remaining):
        self.remaining = remaining

    def consume(self):
        if self.remaining <= 0:
            raise requests.TooManyRedirects("请求跳转次数过多")
        self.remaining -= 1


def _request_with_retry(
    url, headers=None, timeout=15, retries=3, sleep_seconds=0.8, stream=False,
    allowed_final_host_suffixes=None, final_url_message="请求跳转到了不允许的 URL",
    deadline_monotonic=None, max_redirects=5, *, method="GET", accepted_statuses=(200,),
    allowed_hop_host_suffixes=None, redirect_budget=None,
    non_retry_statuses=(),
    return_redirect_response=False,
    observation=None,
):
    """Validate each hop and pin direct connections to an admitted address.

    Callers retain their initial-host policy. Intermediates must be public, but
    the final-host allowlist is intentionally applied only to the final response.
    Configured proxies retain their existing, independently resolved egress.
    The deadline covers fetching the response, not consumption of a returned stream.
    Explicit redirect inspection returns the validated response without visiting Location.
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
            raise XhsSecurityError("不允许请求该 URL") from error
        current_headers = dict(HEADERS if headers is None else headers)
        visited = set()
        with _SingleHopSession(observation=observation) as session:
            for hop in range(max_redirects + 1):
                if observation is not None:
                    observation.target(current_url)
                remaining_budget()
                if current_url in visited:
                    raise requests.TooManyRedirects("请求出现循环跳转")
                visited.add(current_url)
                response = None
                try:
                    message = final_url_message if hop else "不允许请求该 URL"
                    current, addresses = _validate_request_target(current_url, message, allowed_hop_host_suffixes)
                    # DNS consumes the same budget and retry scope as HTTP.
                    phase_budget = remaining_budget() / 2
                    request_timeout = tuple(min(value, phase_budget) for value in phases)
                    if not isinstance(timeout, (tuple, list)):
                        request_timeout = request_timeout[0]
                    adapter = _PinnedAddressAdapter(current_url, addresses, deadline_monotonic)
                    prefix = current.scheme + "://"
                    session.adapters[prefix].close()
                    session.mount(prefix, adapter)
                    response = session.request(
                        method,
                        current_url,
                        headers=current_headers,
                        timeout=request_timeout,
                        allow_redirects=False,
                        stream=True,
                    )
                    remaining_budget()
                    is_redirect = response.status_code in {301, 302, 303, 307, 308}
                    if is_redirect and not return_redirect_response:
                        location = response.headers.get("Location")
                        if not location or not location.strip():
                            raise XhsDownloadError("请求跳转缺少 Location")
                        if hop == max_redirects:
                            raise requests.TooManyRedirects("请求跳转次数过多")
                        if redirect_budget is not None:
                            redirect_budget.consume()
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

                    if (accepted_statuses is None or response.status_code in accepted_statuses
                            or (is_redirect and return_redirect_response)):
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

                    last_error = XhsDownloadError(
                        f"HTTP {response.status_code}", http_status=response.status_code
                    )
                except XhsDNSResolutionError as error:
                    if not error.temporary:
                        raise
                    last_error = error
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

        if getattr(last_error, "http_status", None) in non_retry_statuses:
            raise last_error
        if attempt < retries - 1:
            retry_delay = sleep_seconds * (attempt + 1)
            if retry_delay >= remaining_budget():
                raise requests.Timeout("请求超过时间限制") from last_error
            time.sleep(retry_delay)

    raise last_error
