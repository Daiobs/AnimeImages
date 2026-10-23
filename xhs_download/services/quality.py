from dataclasses import dataclass
from hashlib import sha256
import math
import re
import time
import unicodedata
from urllib.parse import unquote_to_bytes

import requests
from django.core.cache import cache

from .common import (
    MEDIA_HOST_SUFFIXES, XhsDownloadError, XhsSecurityError,
    _RedirectBudget, _request_with_retry,
)


def native_attachment_cache_key(url):
    return 'media-attachment:v2:' + sha256(str(url).encode()).hexdigest()


def native_request_context_is_compatible(headers):
    # Navigation cannot replay application-supplied request headers. In
    # particular, a successful provider Referer is not browser evidence.
    return not headers or (isinstance(headers, dict) and not any(headers.values()))


def native_attachment_expiry(url, headers=None):
    """A short-lived response fact, never a substitute for resource authorization."""
    if not native_request_context_is_compatible(headers):
        return 0
    try:
        value = cache.get(native_attachment_cache_key(url))
    except Exception:
        return 0
    return value if isinstance(value, int) and value > time.time() * 1000 else 0


_HTTP_TOKEN = r"[!#$%&'*+.^_`|~0-9A-Za-z-]+"
_DISPOSITION_PARAMETER = re.compile(
    rf';[ \t]*({_HTTP_TOKEN})[ \t]*=[ \t]*({_HTTP_TOKEN}|"(?:[^"\\\r\n]|\\[^\r\n])*")[ \t]*'
)
_EXT_VALUE = re.compile(r"([A-Za-z0-9-]+)'([A-Za-z0-9-]*)'((?:[!#$&+.^_`|~0-9A-Za-z-]|%[0-9A-Fa-f]{2})*)")
_NATIVE_VIDEO_EXTENSIONS = {
    'video/mp4': {'mp4'},
    'video/webm': {'webm'},
    'video/quicktime': {'mov'},
}


def attachment_filename(disposition):
    """Decode RFC 6266/8187, without MIME continuations or ambiguous duplicates."""
    if (not isinstance(disposition, str) or len(disposition) > 8192
            or any(ord(char) < 32 and char != '\t' or ord(char) == 127 for char in disposition)):
        return ''
    kind, separator, rest = disposition.partition(';')
    if kind.strip().lower() != 'attachment':
        return ''
    parameters = {}
    tail = separator + rest
    # General MIME parsers collapse filename/filename* or tolerate broken
    # escapes; preserve both HTTP parameters until strict decoding succeeds.
    while tail:
        match = _DISPOSITION_PARAMETER.match(tail)
        if not match:
            return ''
        name, value = match.groups()
        name = name.lower()
        if name in parameters or name.startswith('filename*') and name != 'filename*':
            return ''
        parameters[name] = value
        tail = tail[match.end():]
    extended = _EXT_VALUE.fullmatch(parameters.get('filename*', ''))
    if extended:
        charset, _, encoded = extended.groups()
        if charset.lower() in {'utf-8', 'iso-8859-1'}:
            try:
                return unquote_to_bytes(encoded).decode(charset, errors='strict')
            except UnicodeError:
                pass
    filename = parameters.get('filename', '')
    if filename.startswith('"'):
        filename = re.sub(r'\\(.)', r'\1', filename[1:-1])
    return filename


def native_attachment_name_is_eligible(disposition, content_type):
    extensions = _NATIVE_VIDEO_EXTENSIONS.get(normalize_content_type(content_type), set())
    if not extensions:
        return False
    filename = attachment_filename(disposition)
    if (not filename or filename != filename.strip()
            or any(char in '/\\<>:"|?*' or unicodedata.category(char).startswith('C') for char in filename)
            or len(filename.encode('utf-8')) > 255):
        return False
    stem, dot, extension = filename.rpartition('.')
    return bool(dot and stem.strip(' .') and extension.lower() in extensions)


@dataclass(frozen=True)
class QualityProbeResult:
    url: str
    final_url: str = ""
    reachable: bool = False
    size_bytes: int = 0
    content_type: str = ""
    method: str = ""
    error: str = ""
    terminal: bool = False
    security_rejected: bool = False
    native_attachment_expires_at: int = 0


def parse_content_length(response):
    try:
        return max(0, int(response.headers.get("Content-Length") or 0))
    except (AttributeError, TypeError, ValueError):
        return 0


def parse_content_range_total(content_range):
    if not content_range:
        return 0
    try:
        return max(0, int(str(content_range).split("/", 1)[1]))
    except (IndexError, TypeError, ValueError):
        return 0


def normalize_content_type(content_type):
    return (content_type or "").split(";", 1)[0].strip().lower()


def content_type_matches_expected(content_type, expected_type=""):
    media_type = normalize_content_type(content_type)
    expected_type = (expected_type or "").strip().lower()
    if not expected_type or not media_type:
        return True

    if expected_type == "video":
        return media_type.startswith("video/") or media_type == "application/octet-stream"
    if expected_type == "image":
        return media_type.startswith("image/") or media_type == "application/octet-stream"

    return media_type.startswith(f"{expected_type}/") or media_type == expected_type


class QualityProbe:
    def __init__(self, allowed_final_host_suffixes=MEDIA_HOST_SUFFIXES):
        self.allowed_final_host_suffixes = allowed_final_host_suffixes or MEDIA_HOST_SUFFIXES

    def probe(
        self,
        url,
        headers=None,
        timeout=6,
        expected_type="",
        strategy="head_first",
        allow_fallback=True,
    ):
        timeout = float(timeout)
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be positive and finite")
        deadline = time.monotonic() + timeout
        try:
            cache.delete(native_attachment_cache_key(url))
        except Exception:
            pass
        redirects = _RedirectBudget(5)
        request_headers = headers or {}
        methods = ("RANGE", "HEAD") if strategy == "range_first" else ("HEAD", "RANGE")

        primary_result = self._probe(url, request_headers, timeout, expected_type, methods[0], deadline, redirects)
        if primary_result.reachable and primary_result.size_bytes:
            result = primary_result
        elif not allow_fallback or primary_result.terminal:
            result = primary_result
        else:
            fallback_result = self._probe(url, request_headers, timeout, expected_type, methods[1], deadline, redirects)
            if fallback_result.terminal or fallback_result.final_url or fallback_result.reachable:
                # A received response or terminal refusal supersedes HEAD.
                result = fallback_result
            elif primary_result.reachable:
                # Ordinary transport failure supplies no contrary response.
                result = primary_result
            else:
                result = QualityProbeResult(url=url, error=fallback_result.error or primary_result.error)
        try:
            remaining = result.native_attachment_expires_at / 1000 - time.time()
            if result.reachable and remaining > 0:
                cache.set(native_attachment_cache_key(url), result.native_attachment_expires_at,
                          timeout=math.ceil(remaining))
            else:
                cache.delete(native_attachment_cache_key(url))
        except Exception:
            pass
        return result

    def _probe(self, url, headers, timeout, expected_type, method, deadline, redirects):
        try:
            response = _request_with_retry(
                url, method="HEAD" if method == "HEAD" else "GET",
                headers=headers if method == "HEAD" else {**headers, "Range": "bytes=0-0"},
                timeout=timeout, retries=2, stream=True,
                accepted_statuses=None, deadline_monotonic=deadline,
                redirect_budget=redirects, allowed_hop_host_suffixes=MEDIA_HOST_SUFFIXES,
                allowed_final_host_suffixes=self.allowed_final_host_suffixes,
                final_url_message="资源 URL 跳转到了不允许的下载域名",
            )
            try:
                if response.status_code not in {200, 206}:
                    return QualityProbeResult(url=url, final_url=response.url, method=method, error=f"HTTP {response.status_code}")
                content_type = response.headers.get("Content-Type", "")
                if not content_type_matches_expected(content_type, expected_type):
                    return QualityProbeResult(
                        url=url,
                        final_url=response.url,
                        method=method,
                        content_type=content_type,
                        error=f"Unexpected content type {content_type}",
                    )
                size_bytes = (
                    parse_content_range_total(response.headers.get("Content-Range"))
                    if method == "RANGE" and response.status_code == 206
                    else parse_content_length(response)
                )
                native_expiry = 0
                if (response.url == url and native_request_context_is_compatible(headers)
                        and native_attachment_name_is_eligible(
                            response.headers.get('Content-Disposition', ''), content_type)):
                    native_expiry = int(time.time() * 1000) + 60000
                return QualityProbeResult(
                    url=url,
                    final_url=response.url,
                    reachable=True,
                    size_bytes=size_bytes,
                    content_type=content_type,
                    method=method,
                    native_attachment_expires_at=native_expiry,
                )
            finally:
                response.close()
        except XhsSecurityError as error:
            return QualityProbeResult(url=url, method=method, error=str(error), terminal=True, security_rejected=True)
        except (XhsDownloadError, requests.TooManyRedirects) as error:
            return QualityProbeResult(url=url, method=method, error=str(error), terminal=True)
        except requests.RequestException as error:
            return QualityProbeResult(url=url, method=method, error=str(error))
        except OSError as error:
            return QualityProbeResult(url=url, method=method, error=str(error), terminal=True)
