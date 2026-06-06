import re
from urllib.parse import parse_qs, urlencode, urlparse

from ..assets import MediaAsset, ParseResult
from ..common import (
    XhsParseError,
    clean_filename,
    normalize_text,
    request_get_with_retry,
)
from .base import BasePlatformAdapter


BILIBILI_PAGE_HOST_SUFFIXES = ("bilibili.com", "b23.tv")
BILIBILI_MEDIA_HOST_SUFFIXES = ("bilivideo.com", "hdslb.com")
BILIBILI_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.bilibili.com/",
    "Accept": "application/json,text/plain,*/*",
}


BVID_PATTERN = re.compile(r"(BV[0-9A-Za-z]{10})")
AV_PATH_PATTERN = re.compile(r"/video/av(\d+)", re.IGNORECASE)


def normalize_bilibili_url(url):
    url = normalize_text(url or "").strip()
    if url.startswith("//"):
        return f"https:{url}"
    if url.startswith("http://"):
        return "https://" + url[len("http://"):]
    return url


def extract_bvid(value):
    match = BVID_PATTERN.search(value or "")
    return match.group(1) if match else ""


def extract_aid(value):
    value = value or ""
    match = AV_PATH_PATTERN.search(value)
    if match:
        return match.group(1)
    parsed = urlparse(value)
    query = parse_qs(parsed.query)
    for key in ("aid", "avid"):
        if query.get(key):
            return query[key][0]
    return ""


def extract_page_number(value):
    parsed = urlparse(value or "")
    query = parse_qs(parsed.query)
    try:
        return max(1, int((query.get("p") or ["1"])[0]))
    except (TypeError, ValueError):
        return 1


def api_get_json(url, params, referer="https://www.bilibili.com/"):
    query = urlencode({key: value for key, value in params.items() if value is not None})
    request_url = f"{url}?{query}" if query else url
    response = request_get_with_retry(
        request_url,
        headers={**BILIBILI_HEADERS, "Referer": referer},
        timeout=15,
        retries=2,
        allowed_final_host_suffixes=("bilibili.com",),
        final_url_message="哔哩哔哩接口跳转到了不允许的域名",
    )
    try:
        data = response.json()
    finally:
        response.close()

    if data.get("code") != 0:
        message = data.get("message") or "接口返回异常"
        raise XhsParseError(f"哔哩哔哩解析失败：{message}")
    return data.get("data") or {}


def resolve_video_page(url):
    response = request_get_with_retry(
        url,
        headers=BILIBILI_HEADERS,
        timeout=15,
        retries=2,
        allowed_final_host_suffixes=BILIBILI_PAGE_HOST_SUFFIXES,
        final_url_message="哔哩哔哩页面跳转到了不允许的域名",
    )
    try:
        return response.url
    finally:
        response.close()


def choose_page(view_data, page_number):
    pages = view_data.get("pages") or []
    if not pages:
        cid = view_data.get("cid")
        return {"cid": cid, "page": 1, "part": view_data.get("title") or ""}

    for page in pages:
        if int(page.get("page") or 0) == page_number:
            return page
    return pages[0]


def build_view_params(bvid, aid):
    if bvid:
        return {"bvid": bvid}
    if aid:
        return {"aid": aid}
    raise XhsParseError("没有识别到哔哩哔哩 BV/AV 号")


def build_play_params(view_data, page, preferred_qn=80):
    params = {
        "cid": page.get("cid"),
        "qn": preferred_qn,
        "fnval": 0,
        "fnver": 0,
        "fourk": 1,
        "otype": "json",
        "platform": "html5",
    }
    if view_data.get("bvid"):
        params["bvid"] = view_data["bvid"]
    else:
        params["avid"] = view_data.get("aid")
    return params


def get_play_urls(play_data):
    durls = play_data.get("durl") or []
    urls = []
    for item in durls:
        url = normalize_bilibili_url(item.get("url") or "")
        if not url:
            continue
        order = int(item.get("order") or len(urls) + 1)
        urls.append((order, url))

    urls.sort(key=lambda item: item[0])
    seen = set()
    ordered_urls = []
    for _, url in urls:
        if url in seen:
            continue
        seen.add(url)
        ordered_urls.append(url)
    return ordered_urls


class BilibiliAdapter(BasePlatformAdapter):
    platform_id = "bilibili"
    display_name = "哔哩哔哩"
    page_host_suffixes = BILIBILI_PAGE_HOST_SUFFIXES
    media_host_suffixes = BILIBILI_MEDIA_HOST_SUFFIXES
    headers = BILIBILI_HEADERS

    def parse_url(self, url):
        self.require_supported_url(url)
        source_url = normalize_bilibili_url(url)
        page_url = source_url
        if not extract_bvid(source_url) and not extract_aid(source_url):
            page_url = resolve_video_page(source_url)

        bvid = extract_bvid(page_url)
        aid = extract_aid(page_url)
        page_number = extract_page_number(page_url)
        view_data = api_get_json(
            "https://api.bilibili.com/x/web-interface/view",
            build_view_params(bvid, aid),
            referer=page_url,
        )
        page = choose_page(view_data, page_number)
        if not page.get("cid"):
            raise XhsParseError("没有解析到哔哩哔哩视频 CID")

        play_data = api_get_json(
            "https://api.bilibili.com/x/player/playurl",
            build_play_params(view_data, page),
            referer=page_url,
        )
        video_urls = get_play_urls(play_data)
        if not video_urls:
            raise XhsParseError("没有解析到可直接下载的哔哩哔哩视频源")

        owner = view_data.get("owner") or {}
        title_parts = [owner.get("name") or "", view_data.get("title") or ""]
        if page.get("part") and len(view_data.get("pages") or []) > 1:
            title_parts.append(page.get("part"))
        base_filename = clean_filename("_".join(part for part in title_parts if part))
        cover_url = normalize_bilibili_url(view_data.get("pic") or "")

        return ParseResult(
            platform=self.platform_id,
            post_type="video",
            base_filename=base_filename or "bilibili",
            assets=tuple(
                MediaAsset(
                    media_type="video",
                    url=video_url,
                    platform=self.platform_id,
                    cover_url=cover_url,
                    cover_preview_url=cover_url,
                    headers=self.headers,
                    media_host_suffixes=self.media_host_suffixes,
                    quality_label=f"{play_data.get('quality')}P" if play_data.get("quality") else "原始文件",
                )
                for video_url in video_urls
            ),
        )
