import re
import time
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

import requests
from requests.utils import requote_uri

from ..assets import MediaAsset, ParseResult
from ..candidates import MediaCandidate, dedupe_candidates
from ..common import (
    XhsParseError,
    XhsProviderTimeoutError,
    clean_filename,
    normalize_text,
    request_get_with_retry,
    validate_external_url,
)
from .base import BasePlatformAdapter, PlatformCapabilities, ProviderPolicy


BILIBILI_PAGE_HOST_SUFFIXES = ("bilibili.com", "b23.tv")
BILIBILI_MEDIA_HOST_SUFFIXES = ("bilivideo.com", "bilivideo.cn", "hdslb.com")
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


def bilibili_request_timeout(deadline_monotonic, fallback):
    if not deadline_monotonic:
        return float(fallback)
    remaining = float(deadline_monotonic) - time.monotonic()
    if remaining <= 0:
        raise XhsProviderTimeoutError("哔哩哔哩解析超过时间限制")
    return min(float(fallback), max(0.001, remaining))


def api_get_json(
    url,
    params,
    referer="https://www.bilibili.com/",
    *,
    deadline_monotonic=None,
):
    query = urlencode({key: value for key, value in params.items() if value is not None})
    request_url = f"{url}?{query}" if query else url
    try:
        response = request_get_with_retry(
            request_url,
            headers={**BILIBILI_HEADERS, "Referer": requote_uri(referer)},
            timeout=bilibili_request_timeout(deadline_monotonic, 15),
            retries=2,
            allowed_final_host_suffixes=("bilibili.com",),
            final_url_message="哔哩哔哩接口跳转到了不允许的域名",
            deadline_monotonic=deadline_monotonic,
        )
    except (requests.Timeout, requests.ConnectionError) as error:
        raise XhsProviderTimeoutError("哔哩哔哩接口请求超时，请稍后重试") from error
    try:
        data = response.json()
    finally:
        response.close()

    if data.get("code") != 0:
        message = data.get("message") or "接口返回异常"
        raise XhsParseError(f"哔哩哔哩解析失败：{message}")
    return data.get("data") or {}


def resolve_video_page(url, *, deadline_monotonic=None):
    current_url = normalize_bilibili_url(url)
    max_redirects = 5
    for _ in range(max_redirects + 1):
        validate_external_url(
            current_url,
            BILIBILI_PAGE_HOST_SUFFIXES,
            "哔哩哔哩页面跳转到了不允许的域名",
        )
        response = requests.get(
            current_url,
            headers=BILIBILI_HEADERS,
            timeout=bilibili_request_timeout(deadline_monotonic, 5),
            allow_redirects=False,
            stream=True,
        )
        try:
            if response.status_code not in {301, 302, 303, 307, 308}:
                return response.url or current_url
            location = response.headers.get("Location")
            if not location:
                return response.url or current_url
            redirect_url = normalize_bilibili_url(urljoin(current_url, location))
            validate_external_url(
                redirect_url,
                BILIBILI_PAGE_HOST_SUFFIXES,
                "哔哩哔哩页面跳转到了不允许的域名",
            )
            if extract_bvid(redirect_url) or extract_aid(redirect_url):
                return redirect_url
            current_url = redirect_url
        finally:
            response.close()

    raise XhsParseError("哔哩哔哩短链接跳转次数过多")


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


def build_dash_play_params(view_data, page, preferred_qn=80):
    params = build_play_params(view_data, page, preferred_qn=preferred_qn)
    params["fnval"] = 16
    return params


def normalize_dash_urls(item):
    urls = []
    for key in ("baseUrl", "base_url", "url"):
        url = normalize_bilibili_url(item.get(key) or "")
        if url:
            urls.append(url)
    for key in ("backupUrl", "backup_url", "backup_url_list"):
        value = item.get(key)
        if isinstance(value, str):
            value = [value]
        for url in value or ():
            normalized = normalize_bilibili_url(url or "")
            if normalized:
                urls.append(normalized)
    return urls


def get_dash_play_items(play_data):
    videos = ((play_data.get("dash") or {}).get("video")) or []
    candidates = []
    for index, item in enumerate(videos, start=1):
        if not isinstance(item, dict):
            continue
        size = int(item.get("size") or 0)
        width = int(item.get("width") or 0)
        height = int(item.get("height") or 0)
        bandwidth = int(item.get("bandwidth") or 0)
        quality_id = item.get("id") or item.get("quality")
        quality_label = f"{quality_id}P DASH" if quality_id else "DASH 视频"
        for url in normalize_dash_urls(item):
            candidates.append(
                MediaCandidate(
                    url=url,
                    size_bytes=size,
                    width=width,
                    height=height,
                    bit_rate=bandwidth,
                    priority=max(0, len(videos) - index),
                    quality_label=quality_label,
                    source="dash.video",
                )
            )
    return [
        {
            "url": candidate.url,
            "size_bytes": candidate.size_bytes,
            "quality_label": candidate.quality_label,
        }
        for candidate in dedupe_candidates(
            sorted(candidates, key=lambda item: item.score, reverse=True)
        )
    ]


def get_play_items(play_data):
    durls = play_data.get("durl") or []
    items = []
    for item in durls:
        url = normalize_bilibili_url(item.get("url") or "")
        if not url:
            continue
        order = int(item.get("order") or len(items) + 1)
        size = int(item.get("size") or 0)
        items.append((order, url, size))

    items.sort(key=lambda item: item[0])
    candidates = []
    for _, url, size in items:
        candidates.append(MediaCandidate(url=url, size_bytes=size, source="durl"))
    if candidates:
        return [
            {
                "url": candidate.url,
                "size_bytes": candidate.size_bytes,
                "quality_label": candidate.quality_label,
            }
            for candidate in dedupe_candidates(candidates)
        ]
    return get_dash_play_items(play_data)


class BilibiliAdapter(BasePlatformAdapter):
    platform_id = "bilibili"
    display_name = "哔哩哔哩"
    page_host_suffixes = BILIBILI_PAGE_HOST_SUFFIXES
    media_host_suffixes = BILIBILI_MEDIA_HOST_SUFFIXES
    headers = BILIBILI_HEADERS
    capabilities = PlatformCapabilities(
        media_types=("video",),
        batch_download=True,
        cover=True,
    )
    provider_policy = ProviderPolicy(primary_provider="native")
    parse_option_names = ("timeout_seconds", "deadline_monotonic")

    def parse_url(self, url, timeout_seconds=None, deadline_monotonic=None):
        self.require_supported_url(url)
        deadline = float(deadline_monotonic or 0)
        if not deadline and timeout_seconds is not None:
            deadline = time.monotonic() + max(0.001, float(timeout_seconds or 0))
        source_url = normalize_bilibili_url(url)
        page_url = source_url
        if not extract_bvid(source_url) and not extract_aid(source_url):
            page_url = resolve_video_page(
                source_url,
                deadline_monotonic=deadline or None,
            )

        bvid = extract_bvid(page_url)
        aid = extract_aid(page_url)
        page_number = extract_page_number(page_url)
        view_data = api_get_json(
            "https://api.bilibili.com/x/web-interface/view",
            build_view_params(bvid, aid),
            referer=page_url,
            deadline_monotonic=deadline or None,
        )
        page = choose_page(view_data, page_number)
        if not page.get("cid"):
            raise XhsParseError("没有解析到哔哩哔哩视频 CID")

        play_data = api_get_json(
            "https://api.bilibili.com/x/player/playurl",
            build_play_params(view_data, page),
            referer=page_url,
            deadline_monotonic=deadline or None,
        )
        video_items = get_play_items(play_data)
        if not video_items:
            play_data = api_get_json(
                "https://api.bilibili.com/x/player/playurl",
                build_dash_play_params(view_data, page),
                referer=page_url,
                deadline_monotonic=deadline or None,
            )
            video_items = get_play_items(play_data)
        if not video_items:
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
                    url=video_item["url"],
                    platform=self.platform_id,
                    cover_url=cover_url,
                    cover_preview_url=cover_url,
                    headers=self.headers,
                    media_host_suffixes=self.media_host_suffixes,
                    quality_label=(
                        video_item.get("quality_label")
                        or (f"{play_data.get('quality')}P" if play_data.get("quality") else "原始文件")
                    ),
                    size_bytes=video_item["size_bytes"],
                )
                for video_item in video_items
            ),
        )
