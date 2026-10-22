from dataclasses import dataclass
from hashlib import sha256
import math
import time

import requests
from django.core.cache import cache

from .common import (
    MEDIA_HOST_SUFFIXES, XhsDownloadError, XhsSecurityError,
    _RedirectBudget, _request_with_retry,
)


def native_attachment_cache_key(url):
    return 'media-attachment:' + sha256(str(url).encode()).hexdigest()


def native_attachment_expiry(url):
    """A short-lived response fact, never a substitute for resource authorization."""
    try:
        value = cache.get(native_attachment_cache_key(url))
    except Exception:
        return 0
    return value if isinstance(value, int) and value > time.time() * 1000 else 0


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
            return primary_result
        if not allow_fallback or primary_result.terminal:
            return primary_result

        fallback_result = self._probe(url, request_headers, timeout, expected_type, methods[1], deadline, redirects)
        if fallback_result.security_rejected:
            return fallback_result
        if fallback_result.reachable:
            return fallback_result
        if primary_result.reachable:
            return primary_result
        return QualityProbeResult(
            url=url,
            error=fallback_result.error or primary_result.error,
            terminal=fallback_result.terminal,
        )

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
                # Reuse this existing metadata request. Do not probe again at
                # download time or infer attachment behavior from a URL suffix.
                if (response.url == url and normalize_content_type(content_type) in {'video/mp4', 'video/webm', 'video/quicktime'}
                        and str(response.headers.get('Content-Disposition', '')).split(';', 1)[0].strip().lower() == 'attachment'
                        and not any(str(key).lower() in {'cookie', 'authorization', 'proxy-authorization'}
                                    and value for key, value in headers.items())):
                    try:
                        cache.set(native_attachment_cache_key(url), int(time.time() * 1000) + 60000, timeout=60)
                    except Exception:
                        pass
                return QualityProbeResult(
                    url=url,
                    final_url=response.url,
                    reachable=True,
                    size_bytes=size_bytes,
                    content_type=content_type,
                    method=method,
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
