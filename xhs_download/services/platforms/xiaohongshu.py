import re
import time
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from ..assets import MediaAsset, ParseResult
from ..common import (
    HEADERS,
    XhsParseError,
    clean_filename,
    host_matches,
    normalize_text,
    request_get_with_retry,
)
from .base import BasePlatformAdapter, PlatformCapabilities, ProviderPolicy
from .xhs_fallback_policy import resolve_xhs_fallback_policy
from .xhs_page_state import (
    XHS_MAX_AUTHOR_LENGTH,
    XHS_MAX_TITLE_LENGTH,
    extract_xhs_page_state,
    normalize_xhs_state_text,
)


XHS_PAGE_HOST_SUFFIXES = ("xiaohongshu.com", "xhslink.com")
XHS_MEDIA_HOST_SUFFIXES = ("xiaohongshu.com", "xhscdn.com")
XHS_EMPTY_RESOURCE_VALUES = {"", ".", "..", "null", "none", "undefined"}
XHS_NON_NOTE_IMAGE_HINTS = (
    "avatar",
    "favicon",
    "logo",
    "icon",
    "sprite",
    "emoji",
    "qrcode",
)
XHS_NON_NOTE_IMAGE_PATH_RE = re.compile(
    r"(?:^|[/_.-])(?:avatar|favicon|logo|icon|sprite|emoji|qrcode)(?:[/_.-]|$)",
    re.IGNORECASE,
)
XHS_GENERIC_TITLES = {
    "小红书",
    "xiaohongshu",
    "登录后查看更多",
    "登录后查看",
}
XHS_GENERIC_AUTHORS = XHS_GENERIC_TITLES | {"小红书用户", "作者"}
XHS_PAGE_STATE_MESSAGES = {
    "login_required": "当前页面需要登录后访问，无法读取公开素材。",
    "private": "当前笔记不可公开访问。",
    "deleted": "当前笔记已不可用或未返回公开素材。",
    "unavailable": "当前笔记已不可用或未返回公开素材。",
}


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
    path_lower = parsed.path.lower()
    if XHS_NON_NOTE_IMAGE_PATH_RE.search(path_lower):
        return None
    if path_lower.endswith((".js", ".css", ".zip")):
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
    if value.startswith("//"):
        value = f"https:{value}"
    if value.startswith(("http://", "https://")):
        parsed = urlparse(value)
        hostname = (parsed.hostname or "").lower()
        if (
            parsed.scheme not in {"http", "https"}
            or not host_matches(hostname, XHS_MEDIA_HOST_SUFFIXES)
            or not parsed.path.strip("/")
            or parsed.path.lower().endswith(".m3u8")
        ):
            return None
        return value

    value = value.lstrip("/")
    if (
        len(value) < 10
        or value.lower() in XHS_EMPTY_RESOURCE_VALUES
        or value.startswith((".", "\\"))
        or "://" in value
        or "\x00" in value
        or value.lower().endswith(".m3u8")
    ):
        return None
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
            if raw_url and any(key in raw_url for key in ["sns-video", ".mp4", ".mov"]):
                video_url = normalize_video_url(raw_url)
                if video_url and video_url not in video_urls:
                    video_urls.append(video_url)

    return video_urls


def normalize_xhs_title(value):
    value = normalize_xhs_state_text(value, max_length=XHS_MAX_TITLE_LENGTH)
    for suffix in (" - 小红书", "_小红书", " | 小红书"):
        if value.endswith(suffix):
            value = value[:-len(suffix)].strip()
            break
    if not value or value.lower() in XHS_GENERIC_TITLES or value.startswith("小红书 - "):
        return ""
    return value


def normalize_xhs_author(value):
    value = normalize_xhs_state_text(value, max_length=XHS_MAX_AUTHOR_LENGTH)
    return "" if not value or value.lower() in XHS_GENERIC_AUTHORS else value


def _meta_content(soup, names, normalizer):
    normalized_names = {name.lower() for name in names}
    for tag in soup.find_all("meta"):
        key = str(tag.get("property") or tag.get("name") or "").strip().lower()
        if key not in normalized_names:
            continue
        value = normalizer(tag.get("content", ""))
        if value:
            return value
    return ""


def _dom_text(soup, selectors, normalizer):
    for selector in selectors:
        tag = soup.select_one(selector)
        if tag is None:
            continue
        value = normalizer(tag.get_text(" ", strip=True))
        if value:
            return value
    return ""


def _normalize_image_urls(values):
    image_urls = []
    for value in values:
        image_url = normalize_xhs_image_url(value)
        if image_url and image_url not in image_urls:
            image_urls.append(image_url)
    return image_urls


def _meta_image_urls(soup):
    values = []
    allowed_names = {"og:image", "twitter:image", "twitter:image:src"}
    for tag in soup.find_all("meta"):
        key = str(tag.get("property") or tag.get("name") or "").strip().lower()
        if key in allowed_names:
            values.append(tag.get("content", ""))
    return _normalize_image_urls(values)


def _dom_image_urls(soup):
    values = []
    selectors = (
        "#noteContainer img",
        ".note-detail img",
        ".note-content img",
        ".carousel-container img",
        ".swiper-slide img",
    )
    for selector in selectors:
        for tag in soup.select(selector):
            marker = " ".join(
                (
                    str(tag.get("class") or ""),
                    str(tag.get("alt") or ""),
                )
            ).lower()
            if any(hint in marker for hint in XHS_NON_NOTE_IMAGE_HINTS):
                continue
            for attribute in ("src", "data-src", "data-original"):
                if tag.get(attribute):
                    values.append(tag.get(attribute))
                    break
    return _normalize_image_urls(values)


def _detect_dom_page_state(soup):
    for tag in soup.select("[data-page-state]"):
        state = str(tag.get("data-page-state") or "").strip().lower()
        if state in XHS_PAGE_STATE_MESSAGES:
            return state

    state_selectors = (
        ".login-container",
        ".login-wrapper",
        ".error-page",
        ".not-found",
        ".access-denied",
        ".empty-page",
    )
    state_phrases = (
        ("login_required", ("当前页面需要登录后访问", "请登录后查看", "登录后查看更多")),
        ("private", ("当前笔记不可见", "笔记已设为私密", "笔记不可公开访问")),
        ("deleted", ("当前笔记已删除", "笔记已删除", "内容不存在")),
        ("unavailable", ("当前内容不可用", "笔记暂不可用")),
    )
    for selector in state_selectors:
        for tag in soup.select(selector):
            text = normalize_xhs_state_text(tag.get_text(" ", strip=True), max_length=500)
            for state, phrases in state_phrases:
                if any(phrase in text for phrase in phrases):
                    return state
    return "unknown"


def _build_base_filename(author_name, title, note_id):
    if author_name and title:
        value = f"{author_name}_{title}"
    elif title:
        value = title
    elif author_name and note_id:
        value = f"{author_name}_{note_id}"
    elif note_id:
        value = note_id
    else:
        value = "xhs"
    return clean_filename(value)[:180] or "xhs"


def _build_xhs_metadata(
    *,
    title,
    author_name,
    page_state,
    title_source,
    author_source,
    image_source,
    video_source,
    cover_source,
    structured_state,
    fallback_policy,
):
    metadata = {
        "page_state": page_state,
        "title_source": title_source,
        "author_source": author_source,
        "image_source": image_source,
        "video_source": video_source,
        "cover_source": cover_source,
        "structured_state_found": bool(structured_state.structured_state_found),
        "structured_payload_found": bool(structured_state.structured_payload_found),
        "current_note_found": bool(structured_state.current_note_found),
        "note_candidate_found": bool(structured_state.note_candidate_found),
        "fallback_policy": fallback_policy.name,
        "structured_image_authoritative": bool(
            fallback_policy.structured_image_authoritative
        ),
        "terminal_page_state": bool(fallback_policy.terminal_marker_detected),
    }
    if title:
        metadata["title"] = title
    if author_name:
        metadata["author_name"] = author_name
    if structured_state.note_id:
        metadata["note_id"] = structured_state.note_id
    return metadata


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
        structured_state = extract_xhs_page_state(soup, source_url=source_url)

        structured_image_urls = _normalize_image_urls(structured_state.image_urls)
        structured_video_urls = []
        structured_video_size_bytes = 0
        for index, raw_url in enumerate(structured_state.video_urls):
            video_url = normalize_video_url(raw_url)
            if not video_url or video_url in structured_video_urls:
                continue
            structured_video_urls.append(video_url)
            if index == 0:
                structured_video_size_bytes = structured_state.video_size_bytes

        fallback_policy = resolve_xhs_fallback_policy(
            structured_state,
            structured_image_urls=structured_image_urls,
            structured_video_urls=structured_video_urls,
            dom_page_state=_detect_dom_page_state(soup),
        )
        if fallback_policy.terminal_state:
            raise XhsParseError(
                XHS_PAGE_STATE_MESSAGES[fallback_policy.terminal_state]
            )

        structured_title = normalize_xhs_title(structured_state.title)
        meta_title = _meta_content(
            soup,
            ("og:title", "twitter:title"),
            normalize_xhs_title,
        )
        dom_title = _dom_text(
            soup,
            ("#detail-title", ".note-detail .title", ".note-content .title"),
            normalize_xhs_title,
        )
        title = structured_title or meta_title or dom_title
        title_source = (
            "structured" if structured_title else "meta" if meta_title else "dom" if dom_title else ""
        )

        structured_author = normalize_xhs_author(structured_state.author_name)
        meta_author = _meta_content(
            soup,
            ("author", "article:author", "og:article:author", "twitter:creator"),
            normalize_xhs_author,
        )
        dom_author = _dom_text(
            soup,
            ("span.username", ".note-detail .username", ".author-container .name"),
            normalize_xhs_author,
        )
        author_name = structured_author or meta_author or dom_author
        author_source = (
            "structured"
            if structured_author
            else "meta"
            if meta_author
            else "dom"
            if dom_author
            else ""
        )

        meta_image_urls = (
            _meta_image_urls(soup) if fallback_policy.allow_meta_images else []
        )
        dom_image_urls = (
            _dom_image_urls(soup) if fallback_policy.allow_dom_images else []
        )
        if structured_image_urls:
            image_urls = structured_image_urls
            image_source = "structured"
        elif meta_image_urls:
            image_urls = meta_image_urls
            image_source = "meta"
        elif dom_image_urls:
            image_urls = dom_image_urls
            image_source = "dom"
        elif fallback_policy.allow_broad_image_scan:
            image_urls = self.extract_fallback_image_urls(html)
            image_source = "fallback" if image_urls else ""
        else:
            image_urls = []
            image_source = ""

        if structured_video_urls:
            video_urls = structured_video_urls
            video_source = "structured"
        elif fallback_policy.allow_broad_video_scan:
            video_urls = extract_video_urls(html)
            video_source = "fallback" if video_urls else ""
        else:
            video_urls = []
            video_source = ""

        page_state = fallback_policy.page_state

        base_filename = _build_base_filename(
            author_name,
            title,
            structured_state.note_id,
        )

        is_video_note = structured_state.post_type == "video" or bool(video_urls)
        if is_video_note and video_urls:
            structured_cover_urls = _normalize_image_urls(structured_state.cover_urls)
            if structured_cover_urls:
                cover_url = structured_cover_urls[0]
                cover_source = "structured"
            elif structured_image_urls:
                cover_url = structured_image_urls[0]
                cover_source = "structured"
            elif meta_image_urls:
                cover_url = meta_image_urls[0]
                cover_source = "meta"
            elif dom_image_urls:
                cover_url = dom_image_urls[0]
                cover_source = "dom"
            else:
                cover_url = ""
                cover_source = ""
            metadata = _build_xhs_metadata(
                title=title,
                author_name=author_name,
                page_state=page_state,
                title_source=title_source,
                author_source=author_source,
                image_source=image_source,
                video_source=video_source,
                cover_source=cover_source,
                structured_state=structured_state,
                fallback_policy=fallback_policy,
            )
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
                        size_bytes=structured_video_size_bytes,
                        size_is_estimate=False,
                    ),
                ),
                metadata=metadata,
            )

        if not is_video_note and image_urls:
            metadata = _build_xhs_metadata(
                title=title,
                author_name=author_name,
                page_state=page_state,
                title_source=title_source,
                author_source=author_source,
                image_source=image_source,
                video_source=video_source,
                cover_source="",
                structured_state=structured_state,
                fallback_policy=fallback_policy,
            )
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
                    for image_url in image_urls
                ),
                metadata=metadata,
            )

        return ParseResult(
            platform=self.platform_id,
            post_type="video" if is_video_note else "image",
            base_filename=base_filename,
            assets=(),
            metadata=_build_xhs_metadata(
                title=title,
                author_name=author_name,
                page_state=page_state,
                title_source=title_source,
                author_source=author_source,
                image_source=image_source,
                video_source=video_source,
                cover_source="",
                structured_state=structured_state,
                fallback_policy=fallback_policy,
            ),
        )

    def extract_fallback_image_urls(self, html):
        fallback_image_urls = []
        html_normalized = normalize_text(html)
        candidates = re.findall(r'(?:https?:)?//[^\s"\'<>]+', html_normalized)

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
