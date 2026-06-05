import re

from bs4 import BeautifulSoup

from .common import HEADERS, PAGE_HOST_SUFFIXES, clean_filename, normalize_text, request_get_with_retry, validate_page_url


def normalize_xhs_image_url(url):
    if not url:
        return None

    url = normalize_text(url)
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


def parse_xhs_html(html):
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
            image_url = normalize_xhs_image_url(content)
            if image_url and image_url not in meta_image_urls:
                meta_image_urls.append(image_url)

    if video_urls:
        return {
            "post_type": "video",
            "base_filename": base_filename,
            "image_urls": [],
            "video_items": [{"video_url": video_urls[0], "cover_url": meta_image_urls[0] if meta_image_urls else None}],
        }

    if meta_image_urls:
        return {
            "post_type": "image",
            "base_filename": base_filename,
            "image_urls": meta_image_urls,
            "video_items": [],
        }

    fallback_image_urls = []
    html_normalized = normalize_text(html)
    candidates = re.findall(r'https?://[^\s"\'<>]+', html_normalized)

    for item in candidates:
        if any(key in item for key in ["sns-webpic", "ci.xiaohongshu.com", "notes_pre_post"]):
            image_url = normalize_xhs_image_url(item)
            if image_url and image_url not in fallback_image_urls:
                fallback_image_urls.append(image_url)

    return {
        "post_type": "image",
        "base_filename": base_filename,
        "image_urls": fallback_image_urls,
        "video_items": [],
    }


def parse_xhs_page(url):
    validate_page_url(url)
    response = request_get_with_retry(
        url,
        headers=HEADERS,
        timeout=15,
        retries=3,
        allowed_final_host_suffixes=PAGE_HOST_SUFFIXES,
        final_url_message="小红书页面跳转到了不允许的域名",
    )
    try:
        return parse_xhs_html(response.text)
    finally:
        response.close()
