import json
import re
from html import unescape
from urllib.parse import unquote

from bs4 import BeautifulSoup

from ..assets import MediaAsset, ParseResult
from ..common import (
    clean_filename,
    normalize_text,
    request_get_with_retry,
)
from .base import BasePlatformAdapter


DOUYIN_PAGE_HOST_SUFFIXES = ("douyin.com", "iesdouyin.com")
DOUYIN_MEDIA_HOST_SUFFIXES = (
    "douyin.com",
    "douyinpic.com",
    "douyinvod.com",
    "douyincdn.com",
    "douyinstatic.com",
    "byteimg.com",
    "bytetos.com",
)
DOUYIN_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.douyin.com/",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
}


def normalize_douyin_url(url):
    url = normalize_text(url or "").strip()
    if url.startswith("//"):
        return f"https:{url}"
    if url.startswith("http://"):
        return "https://" + url[len("http://"):]
    return url


def first_url(value):
    if not value:
        return ""
    if isinstance(value, str):
        return normalize_douyin_url(value)
    if isinstance(value, list):
        for item in value:
            url = first_url(item)
            if url:
                return url
    if isinstance(value, dict):
        for key in ("url_list", "url", "uri"):
            url = first_url(value.get(key))
            if url:
                return url
    return ""


def iter_dicts(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from iter_dicts(child)
    elif isinstance(value, list):
        for item in value:
            yield from iter_dicts(item)


def extract_balanced_object(text, start_index):
    brace_index = text.find("{", start_index)
    if brace_index == -1:
        return ""

    depth = 0
    in_string = False
    escape = False

    for index in range(brace_index, len(text)):
        char = text[index]

        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[brace_index:index + 1]

    return ""


def load_json_candidates(html):
    soup = BeautifulSoup(html, "html.parser")
    candidates = []

    for script in soup.find_all("script"):
        script_id = script.get("id", "")
        content = script.string or script.get_text() or ""
        content = unescape(content).strip()
        if not content:
            continue

        if script_id in {"RENDER_DATA", "__UNIVERSAL_DATA_FOR_REHYDRATION__"}:
            candidates.append(unquote(content))
            continue

        if content.startswith("{") and content.endswith("}"):
            candidates.append(content)

        for marker in (
            "window._ROUTER_DATA",
            "window.__INIT_PROPS__",
            "window.__UNIVERSAL_DATA_FOR_REHYDRATION__",
        ):
            marker_index = content.find(marker)
            if marker_index == -1:
                continue
            json_text = extract_balanced_object(content, marker_index)
            if json_text:
                candidates.append(json_text)

    parsed = []
    for candidate in candidates:
        try:
            parsed.append(json.loads(candidate))
        except json.JSONDecodeError:
            continue

    return parsed


def find_aweme_detail(data):
    for item in iter_dicts(data):
        aweme_detail = item.get("aweme_detail")
        if isinstance(aweme_detail, dict):
            return aweme_detail

        aweme = item.get("aweme")
        if isinstance(aweme, dict) and ("video" in aweme or "images" in aweme):
            return aweme

        if item.get("aweme_id") and ("video" in item or "images" in item):
            return item

    return {}


def get_author_name(aweme):
    author = aweme.get("author") or {}
    return author.get("nickname") or author.get("unique_id") or ""


def get_base_filename(aweme):
    author = get_author_name(aweme)
    title = aweme.get("desc") or aweme.get("item_title") or aweme.get("share_info", {}).get("share_title") or ""
    return clean_filename(f"{author}_{title}") if author or title else "douyin"


def get_image_url(image_item):
    for key in (
        "download_url_list",
        "url_list",
        "origin_url",
        "large_image",
        "display_image",
        "image",
        "image_url",
    ):
        url = first_url(image_item.get(key) if isinstance(image_item, dict) else None)
        if url:
            return url
    return first_url(image_item)


def get_image_items(aweme):
    image_items = []
    for key in ("images", "image_infos", "image_list"):
        value = aweme.get(key)
        if isinstance(value, list):
            image_items.extend(value)
    return image_items


def get_video_cover_url(video):
    for key in ("origin_cover", "cover", "dynamic_cover"):
        url = first_url(video.get(key))
        if url:
            return url
    return ""


def get_video_candidates(video):
    candidates = []

    for item in video.get("bit_rate") or []:
        if not isinstance(item, dict):
            continue
        play_addr = item.get("play_addr") or item.get("play_addr_265")
        url = first_url(play_addr)
        if not url:
            continue
        score = int(item.get("bit_rate") or item.get("quality_type") or 0)
        candidates.append((score, url))

    for key in ("play_addr", "download_addr", "play_addr_265"):
        url = first_url(video.get(key))
        if url:
            candidates.append((0, url))

    candidates.sort(key=lambda item: item[0], reverse=True)
    return [url for _, url in candidates]


class DouyinAdapter(BasePlatformAdapter):
    platform_id = "douyin"
    display_name = "抖音"
    page_host_suffixes = DOUYIN_PAGE_HOST_SUFFIXES
    media_host_suffixes = DOUYIN_MEDIA_HOST_SUFFIXES
    headers = DOUYIN_HEADERS

    def parse_html(self, html, source_url=None):
        for data in load_json_candidates(html):
            aweme = find_aweme_detail(data)
            if aweme:
                return self.parse_aweme(aweme)

        return ParseResult(
            platform=self.platform_id,
            post_type="unknown",
            base_filename="douyin",
            assets=(),
        )

    def parse_aweme(self, aweme):
        base_filename = get_base_filename(aweme)
        image_assets = self.build_image_assets(aweme)
        if image_assets:
            return ParseResult(
                platform=self.platform_id,
                post_type="image",
                base_filename=base_filename,
                assets=tuple(image_assets),
            )

        video = aweme.get("video") or {}
        video_urls = get_video_candidates(video)
        if video_urls:
            cover_url = get_video_cover_url(video)
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
                        cover_preview_url=cover_url,
                        headers=self.headers,
                        media_host_suffixes=self.media_host_suffixes,
                    ),
                ),
            )

        return ParseResult(
            platform=self.platform_id,
            post_type="unknown",
            base_filename=base_filename,
            assets=(),
        )

    def build_image_assets(self, aweme):
        assets = []
        seen = set()

        for image_item in get_image_items(aweme):
            image_url = get_image_url(image_item)
            if not image_url or image_url in seen:
                continue
            seen.add(image_url)
            assets.append(
                MediaAsset(
                    media_type="image",
                    url=image_url,
                    platform=self.platform_id,
                    preview_url=image_url,
                    headers=self.headers,
                    media_host_suffixes=self.media_host_suffixes,
                )
            )

        return assets

    def parse_url(self, url):
        self.require_supported_url(url)
        response = request_get_with_retry(
            url,
            headers=self.headers,
            timeout=15,
            retries=3,
            allowed_final_host_suffixes=self.page_host_suffixes,
            final_url_message="抖音页面跳转到了不允许的域名",
        )
        try:
            return self.parse_html(response.text, source_url=response.url)
        finally:
            response.close()
