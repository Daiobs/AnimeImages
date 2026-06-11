from dataclasses import dataclass

import requests

from .common import MEDIA_HOST_SUFFIXES, validate_external_url, validate_media_url


@dataclass(frozen=True)
class QualityProbeResult:
    url: str
    final_url: str = ""
    reachable: bool = False
    size_bytes: int = 0
    content_type: str = ""
    method: str = ""
    error: str = ""


def parse_content_length(response):
    try:
        return int(response.headers.get("Content-Length") or 0)
    except (AttributeError, TypeError, ValueError):
        return 0


def parse_content_range_total(content_range):
    if not content_range:
        return 0
    try:
        return int(str(content_range).split("/", 1)[1])
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
        validate_media_url(url)
        request_headers = headers or {}
        probes = (
            (self._probe_range, self._probe_head)
            if strategy == "range_first"
            else (self._probe_head, self._probe_range)
        )

        primary_result = probes[0](url, request_headers, timeout, expected_type)
        if primary_result.reachable and primary_result.size_bytes:
            return primary_result
        if not allow_fallback:
            return primary_result

        fallback_result = probes[1](url, request_headers, timeout, expected_type)
        if fallback_result.reachable:
            return fallback_result
        if primary_result.reachable:
            return primary_result
        return QualityProbeResult(
            url=url,
            error=fallback_result.error or primary_result.error,
        )

    def _validate_final_url(self, url):
        validate_external_url(
            url,
            self.allowed_final_host_suffixes,
            "资源 URL 跳转到了不允许的下载域名",
        )

    def _probe_head(self, url, headers, timeout, expected_type=""):
        try:
            response = requests.head(
                url,
                headers=headers,
                timeout=timeout,
                allow_redirects=True,
            )
            try:
                if response.status_code >= 400:
                    return QualityProbeResult(url=url, final_url=response.url, method="HEAD", error=f"HTTP {response.status_code}")

                self._validate_final_url(response.url)
                content_type = response.headers.get("Content-Type", "")
                if not content_type_matches_expected(content_type, expected_type):
                    return QualityProbeResult(
                        url=url,
                        final_url=response.url,
                        method="HEAD",
                        content_type=content_type,
                        error=f"Unexpected content type {content_type}",
                    )
                return QualityProbeResult(
                    url=url,
                    final_url=response.url,
                    reachable=True,
                    size_bytes=parse_content_length(response),
                    content_type=content_type,
                    method="HEAD",
                )
            finally:
                response.close()
        except Exception as error:
            return QualityProbeResult(url=url, method="HEAD", error=str(error))

    def _probe_range(self, url, headers, timeout, expected_type=""):
        try:
            response = requests.get(
                url,
                headers={**headers, "Range": "bytes=0-0"},
                timeout=timeout,
                allow_redirects=True,
                stream=True,
            )
            try:
                if response.status_code >= 400:
                    return QualityProbeResult(url=url, final_url=response.url, method="RANGE", error=f"HTTP {response.status_code}")

                self._validate_final_url(response.url)
                content_type = response.headers.get("Content-Type", "")
                if not content_type_matches_expected(content_type, expected_type):
                    return QualityProbeResult(
                        url=url,
                        final_url=response.url,
                        method="RANGE",
                        content_type=content_type,
                        error=f"Unexpected content type {content_type}",
                    )
                size_bytes = (
                    parse_content_range_total(response.headers.get("Content-Range"))
                    if response.status_code == 206
                    else parse_content_length(response)
                )
                return QualityProbeResult(
                    url=url,
                    final_url=response.url,
                    reachable=True,
                    size_bytes=size_bytes,
                    content_type=content_type,
                    method="RANGE",
                )
            finally:
                response.close()
        except Exception as error:
            return QualityProbeResult(url=url, method="RANGE", error=str(error))
