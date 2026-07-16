import re
import time
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from ..assets import MediaAsset, ParseResult
from ..common import (
    HEADERS,
    clean_filename,
    host_matches,
    normalize_text,
    request_get_with_retry,
)
from .base import BasePlatformAdapter, PlatformCapabilities, ProviderPolicy


XHS_PAGE_HOST_SUFFIXES = ("xiaohongshu.com", "xhslink.com")
XHS_MEDIA_HOST_SUFFIXES = ("xiaohongshu.com", "xhscdn.com")
XHS_EMPTY_RESOURCE_VALUES = {"", ".", "..", "null", "none", "undefined"}


def _parse_xhs_image_candidate(url):
    value = normalize_text(url).strip()
    if value.lower() in XHS_EMPTY_RESOURCE_VALUES:
        return None
    if value.startswith("//"):
        value = f"https:{value}"

    parsed = urlparse(value)
    hostname = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or not hostname:
        return None
    if not host_matches(hostname, XHS_MEDIA_HOST_SUFFIXES):
        return None

    path_parts = tuple(part.strip() for part in parsed.path.split("/") if part.strip())
    if not path_parts:
        return None
    resource_key = path_parts[-1]
    if resource_key.lower() in XHS_EMPTY_RESOURCE_VALUES | {
        "notes_pre_post",
        "spectrum",
    }:
        return None

    has_known_resource_path = any(
        part in {"notes_pre_post", "spectrum"}
        and index + 1 < len(path_parts)
        for index, part in enumerate(path_parts)
    )
    is_ci_host = hostname == "ci.xiaohongshu.com"
    is_sns_image_host = hostname.startswith(("sns-webpic", "sns-img"))
    if not (
        has_known_resource_path
        or is_ci_host
        or is_sns_image_host
    ):
        return None
    return value, parsed


def normalize_xhs_image_url(url):
    candidate = _parse_xhs_image_candidate(url)
    if candidate is None:
        return None

    url, _parsed = candidate
    url = url.split("!")[0]
    url = url.split("?")[0]

    match = re.search(r"(notes_pre_post/[^/?!]+)", url)
    if match:
        return f"https://ci.xiaohongshu.com/{match.group(1)}?imageView2/format/jpg"

    match = re.search(r"(spectrum/[^/?!]+)", url)
    if match:
        return f"https://ci.xiaohongshu.com/{match.group(1)}?imageView2/format/jpg"

    match = re.search(r"/([a-zA-Z0-9]{20,})$", url)
    if match:
        return f"https://ci.xiaohongshu.com/{match.group(1)}?imageView2/format/jpg"

    return url


def is_xhs_note_image_candidate(url):
    return _parse_xhs_image_candidate(url) is not None


def normalize_xhs_preview_image_url(url, width=640):
    image_url = normalize_xhs_image_url(url)
    if not image_url:
        return None

    base_url = image_url.split("!")[0].split("?")[0]
    return f"{base_url}?imageView2/2/w/{int(width)}/format/webp"


def normalize_video_url(value):
    if not value:
        return None

    value = normalize_text(value).strip()
    if value.startswith(("http://", "https://")):
        return value

    value = value.lstrip("/")
    return f"https://sns-video-bd.xhscdn.com/{value}"


def extract_video_urls(html):
    html = normalize_text(html)
    video_urls = []

    key_patterns = [
        r'"originVideoKey"\s*:\s*"([^"]+)"',
        r'"origin_video_key"\s*:\s*"([^"]+)"',
        r'"videoKey"\s*:\s*"([^"]+)"',
        r'"video_key"\s*:\s*"([^"]+)"',
    ]

    for pattern in key_patterns:
        for match in re.finditer(pattern, html):
            raw_value = normalize_text(match.group(1)).strip()
            if not raw_value or raw_value.lower() in {"null", "undefined", "none"} or len(raw_value) < 10:
                continue

            video_url = normalize_video_url(raw_value)
            if video_url and video_url not in video_urls:
                video_urls.append(video_url)

    url_patterns = [
        r'"masterUrl"\s*:\s*"([^"]+)"',
        r'"backupUrl"\s*:\s*"([^"]+)"',
        r'"videoUrl"\s*:\s*"([^"]+)"',
        r'"video_url"\s*:\s*"([^"]+)"',
    ]

    for pattern in url_patterns:
        for match in re.finditer(pattern, html):
            raw_url = normalize_text(match.group(1)).strip()
            if raw_url and any(key in raw_url for key in ["sns-video", ".mp4", ".mov", ".m3u8"]):
                if raw_url not in video_urls:
                    video_urls.append(raw_url)

    return video_urls


class XiaohongshuAdapter(BasePlatformAdapter):
    platform_id = "xiaohongshu"
    display_name = "小红书"
    page_host_suffixes = XHS_PAGE_HOST_SUFFIXES
    media_host_suffixes = XHS_MEDIA_HOST_SUFFIXES
    headers = HEADERS
    capabilities = PlatformCapabilities(
        media_types=("image", "video"),
        batch_download=True,
        cover=True,
    )
    provider_policy = ProviderPolicy(primary_provider="native")
    parse_option_names = ("timeout_seconds", "deadline_monotonic")

    def parse_html(self, html, source_url=None):
        soup = BeautifulSoup(html, "html.parser")

        username_tag = soup.find("span", class_="username")
        title_tag = soup.find("div", {"id": "detail-title"})
        username = username_tag.get_text(strip=True) if username_tag else ""
        title = title_tag.get_text(strip=True) if title_tag else ""
        base_filename = clean_filename(f"{username}_{title}") if username or title else "xhs"

        video_urls = extract_video_urls(html)
        meta_image_urls = []

        for tag in soup.find_all("meta"):
            content = tag.get("content", "")
            name = tag.get("name", "")
            property_name = tag.get("property", "")

            if ("og:image" in name or "og:image" in property_name) and content:
                if not is_xhs_note_image_candidate(content):
                    continue
                image_url = normalize_xhs_image_url(content)
                if image_url and image_url not in meta_image_urls:
                    meta_image_urls.append(image_url)

        if video_urls:
            cover_url = meta_image_urls[0] if meta_image_urls else ""
            return ParseResult(
                platform=self.platform_id,
                post_type="video",
                base_filename=base_filename,
                assets=(
                    MediaAsset(
                        media_type="video",
                        url=video_urls[0],
                        platform=self.platform_id,
                        cover_url=cover_url,
                        cover_preview_url=normalize_xhs_preview_image_url(cover_url) or cover_url,
                    ),
                ),
            )

        if not meta_image_urls:
            meta_image_urls = self.extract_fallback_image_urls(html)

        return ParseResult(
            platform=self.platform_id,
            post_type="image",
            base_filename=base_filename,
            assets=tuple(
                MediaAsset(
                    media_type="image",
                    url=image_url,
                    platform=self.platform_id,
                    preview_url=normalize_xhs_preview_image_url(image_url) or image_url,
                )
                for image_url in meta_image_urls
            ),
        )

    def extract_fallback_image_urls(self, html):
        fallback_image_urls = []
        html_normalized = normalize_text(html)
        candidates = re.findall(r'https?://[^\s"\'<>]+', html_normalized)

        for item in candidates:
            if is_xhs_note_image_candidate(item):
                image_url = normalize_xhs_image_url(item)
                if image_url and image_url not in fallback_image_urls:
                    fallback_image_urls.append(image_url)

        return fallback_image_urls

    def parse_url(self, url, timeout_seconds=None, deadline_monotonic=None):
        self.require_supported_url(url)
        deadline = float(deadline_monotonic or 0)
        if not deadline and timeout_seconds is not None:
            deadline = time.monotonic() + max(0.001, float(timeout_seconds or 0))
        request_timeout = 15.0
        if deadline:
            request_timeout = min(
                request_timeout,
                max(0.001, deadline - time.monotonic()),
            )
        response = request_get_with_retry(
            url,
            headers=self.headers,
            timeout=request_timeout,
            retries=3,
            allowed_final_host_suffixes=self.page_host_suffixes,
            final_url_message="小红书页面跳转到了不允许的域名",
            deadline_monotonic=deadline or None,
        )
        try:
            return self.parse_html(response.text, source_url=response.url)
        finally:
            response.close()
