import json
import logging
import re
from html import unescape
from time import monotonic, time
from urllib.parse import parse_qsl, urlencode, unquote, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup
from django.conf import settings

from ..assets import MediaAsset, ParseResult
from ..candidates import MediaCandidate, rank_media_candidates
from ..common import (
    XhsProviderTimeoutError,
    clean_filename,
    normalize_text,
    request_get_with_retry,
    validate_external_url,
)
from ..diagnostics import select_best_media_candidate
from .base import BasePlatformAdapter, PlatformCapabilities, ProviderPolicy
from .douyin_providers import DouyinParseContext, build_douyin_result_providers


DOUYIN_PAGE_HOST_SUFFIXES = ("douyin.com", "iesdouyin.com")
DOUYIN_MEDIA_HOST_SUFFIXES = (
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
DOUYIN_MOBILE_HEADERS = {
    **DOUYIN_HEADERS,
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 "
        "aweme_260000 JsSdk/2.0 NetType/WIFI Channel/App Store ByteLocale/zh-CN"
    ),
    "Referer": "https://www.iesdouyin.com/",
}
DOUYIN_APP_HEADERS = {
    **DOUYIN_HEADERS,
    "User-Agent": (
        "com.ss.android.ugc.aweme/260000 "
        "(Linux; U; Android 12; zh_CN; Pixel 6; Build/SP1A.210812.016)"
    ),
    "Accept": "application/json",
    "Referer": "https://www.douyin.com/",
}
logger = logging.getLogger(__name__)


AWEME_ID_PATTERNS = (
    re.compile(r"/(?:share/)?(?:video|note)/(\d{15,25})(?:[/?#]|$)"),
    re.compile(r"[?&](?:aweme_id|item_id|modal_id)=(\d{15,25})(?:[&#]|$)"),
    re.compile(r'"(?:aweme_id|itemId|item_id)"\s*:\s*"?(\d{15,25})"?'),
)
PREFERRED_VIDEO_RATIOS = ("1080p_60fps", "1080p", "2k", "4k", "720p")
VIDEO_RATIO_SCORES = {
    "4k": 5000,
    "2k": 4000,
    "1080p_60fps": 3500,
    "1080p": 3000,
    "720p": 2000,
    "540p": 1000,
}
BROWSER_IMAGE_EXTENSIONS = {
    "jpg": 60,
    "jpeg": 60,
    "webp": 55,
    "png": 50,
    "avif": 45,
    "gif": 40,
    "image": 20,
}
UNSUPPORTED_PREVIEW_IMAGE_EXTENSIONS = {"heic", "heif"}


def normalize_douyin_url(url):
    url = normalize_text(url or "").strip()
    if url.startswith("//"):
        return f"https:{url}"
    if url.startswith("http://"):
        return "https://" + url[len("http://"):]
    return url


def normalize_douyin_video_url(url):
    url = normalize_douyin_url(url)
    return url.replace("/playwm/", "/play/")


def replace_query_param(url, key, value):
    parsed = urlparse(url)
    query_items = [
        (item_key, item_value)
        for item_key, item_value in parse_qsl(parsed.query, keep_blank_values=True)
        if item_key != key
    ]
    query_items.append((key, value))
    return urlunparse(parsed._replace(query=urlencode(query_items)))


def get_url_ratio(url):
    query = dict(parse_qsl(urlparse(url).query, keep_blank_values=True))
    return query.get("ratio", "")


def get_ratio_score(ratio):
    return VIDEO_RATIO_SCORES.get(ratio, 0)


def expand_video_quality_urls(url):
    url = normalize_douyin_video_url(url)
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    if (
        "video_id" not in query
        or "/aweme/v1/play" not in parsed.path
        or query.get("sign")
        or query.get("biz_sign")
        or query.get("is_play_url")
    ):
        return [(get_ratio_score(get_url_ratio(url)), url)]

    expanded = []
    seen = set()
    for ratio in PREFERRED_VIDEO_RATIOS:
        candidate = replace_query_param(url, "ratio", ratio)
        if candidate in seen:
            continue
        seen.add(candidate)
        expanded.append((get_ratio_score(ratio), candidate))

    if url not in seen:
        expanded.append((get_ratio_score(get_url_ratio(url)), url))

    return expanded


def extract_aweme_id(value):
    value = normalize_text(unquote(unescape(value or "")))
    for pattern in AWEME_ID_PATTERNS:
        match = pattern.search(value)
        if match:
            return match.group(1)
    return ""


def is_douyin_short_url(url):
    return (urlparse(url or "").hostname or "").lower() == "v.douyin.com"


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
        for key in ("url_list", "urlList", "url", "uri", "download_url", "downloadUrl"):
            url = first_url(value.get(key))
            if url:
                return url
    return ""


def get_url_extension(url):
    path = urlparse(url or "").path.lower()
    if "." not in path:
        return ""
    return path.rsplit(".", 1)[1]


def is_http_url(url):
    parsed = urlparse(url or "")
    return parsed.scheme in {"http", "https"} and bool(parsed.hostname)


def iter_urls(value):
    if not value:
        return
    if isinstance(value, str):
        url = normalize_douyin_url(value)
        if url:
            yield url
        return
    if isinstance(value, list):
        for item in value:
            yield from iter_urls(item)
        return
    if isinstance(value, dict):
        for key in ("url_list", "urlList", "url", "uri", "download_url", "downloadUrl"):
            yield from iter_urls(value.get(key))


def iter_http_urls(value):
    for url in iter_urls(value) or ():
        if is_http_url(url):
            yield url


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
    html = html or ""
    raw_text = unquote(unescape(html.strip()))
    soup = BeautifulSoup(html, "html.parser")
    candidates = []

    if raw_text.startswith("{") and raw_text.endswith("}"):
        candidates.append(raw_text)

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
            "window._SSR_DATA",
            "window.__INIT_PROPS__",
            "window.__UNIVERSAL_DATA_FOR_REHYDRATION__",
        ):
            marker_index = content.find(marker)
            if marker_index == -1:
                continue
            json_text = extract_balanced_object(content, marker_index)
            if json_text:
                candidates.append(json_text)

        for match in re.finditer(r"JSON\.parse\((?P<quote>['\"])(?P<value>.*?)(?P=quote)\)", content):
            try:
                candidates.append(json.loads(f'"{match.group("value")}"'))
            except json.JSONDecodeError:
                continue

    parsed = []
    seen = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
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

        item_list = item.get("item_list")
        if isinstance(item_list, list):
            for candidate in item_list:
                if isinstance(candidate, dict) and candidate.get("aweme_id"):
                    return candidate

        aweme = item.get("aweme")
        if isinstance(aweme, dict) and ("video" in aweme or "images" in aweme):
            return aweme

        if item.get("aweme_id") and ("video" in item or "images" in item):
            return item

    return {}


def find_aweme_in_html(html):
    for data in load_json_candidates(html):
        aweme = find_aweme_detail(data)
        if aweme:
            return aweme
    return {}


def get_author_name(aweme):
    author = aweme.get("author") or {}
    return author.get("nickname") or author.get("unique_id") or ""


def get_base_filename(aweme):
    author = get_author_name(aweme)
    title = aweme.get("desc") or aweme.get("item_title") or aweme.get("share_info", {}).get("share_title") or ""
    return clean_filename(f"{author}_{title}") if author or title else "douyin"


def get_aweme_metadata(aweme):
    return {
        "aweme_id": str(aweme.get("aweme_id") or ""),
        "author": get_author_name(aweme),
        "title": str(
            aweme.get("desc")
            or aweme.get("item_title")
            or aweme.get("share_info", {}).get("share_title")
            or ""
        ),
    }


def get_image_url(image_item):
    for key in (
        "url_list",
        "origin_url",
        "large_image",
        "display_image",
        "image",
        "image_url",
        "download_url_list",
    ):
        url = first_url(image_item.get(key) if isinstance(image_item, dict) else None)
        if url:
            return url
    return first_url(image_item)


def get_image_items(aweme):
    image_items = []
    for key in ("images", "image_infos", "image_list", "imageList"):
        value = aweme.get(key)
        if isinstance(value, list):
            image_items.extend(value)
    return image_items


def get_browser_image_score(url):
    extension = get_url_extension(url)
    if extension in UNSUPPORTED_PREVIEW_IMAGE_EXTENSIONS:
        return 0
    return BROWSER_IMAGE_EXTENSIONS.get(extension, 10)


def choose_browser_preview_image_url(value):
    candidates = [
        (get_browser_image_score(url), index, url)
        for index, url in enumerate(iter_http_urls(value))
        if get_browser_image_score(url) > 0
    ]
    if not candidates:
        return ""
    candidates.sort(key=lambda item: (item[0], -item[1]), reverse=True)
    return candidates[0][2]


def get_video_cover_urls(video):
    cover_url = ""
    preview_url = ""

    for key in ("cover", "origin_cover", "dynamic_cover", "animated_cover"):
        value = video.get(key)
        if not cover_url:
            cover_url = choose_browser_preview_image_url(value) or next(iter_http_urls(value), "")
        if not preview_url:
            preview_url = choose_browser_preview_image_url(value)
        if cover_url and preview_url:
            return cover_url, preview_url

    for thumb in video.get("big_thumbs") or []:
        if not isinstance(thumb, dict):
            continue
        value = thumb.get("img_urls") or thumb.get("img_url")
        if not cover_url:
            cover_url = choose_browser_preview_image_url(value) or next(iter_http_urls(value), "")
        if not preview_url:
            preview_url = choose_browser_preview_image_url(value)
        if cover_url and preview_url:
            return cover_url, preview_url

    return cover_url, preview_url or cover_url


def get_video_cover_url(video):
    cover_url, _ = get_video_cover_urls(video)
    return cover_url


def parse_int(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def get_addr_size(addr):
    if not isinstance(addr, dict):
        return 0
    return parse_int(addr.get("data_size") or addr.get("size"))


def get_addr_dimension(addr, key):
    if not isinstance(addr, dict):
        return 0
    return parse_int(addr.get(key))


def get_video_media_candidates(video):
    candidates = []

    def is_audio_only_candidate(url):
        url = str(url or "").lower()
        return (
            "mime_type=audio" in url
            or "/play/music/" in url
            or "/obj/ies-music" in url
            or get_url_extension(url) in {"mp3", "m4a", "aac", "wav", "ogg"}
        )

    for item in video.get("bit_rate") or []:
        if not isinstance(item, dict):
            continue
        bit_rate = parse_int(item.get("bit_rate"))
        quality_type = parse_int(item.get("quality_type"))
        for key in ("play_addr", "play_addr_h264", "play_addr_265"):
            play_addr = item.get(key)
            size_bytes = get_addr_size(play_addr)
            width = get_addr_dimension(play_addr, "width")
            height = get_addr_dimension(play_addr, "height")
            for url in iter_urls(play_addr) or ():
                if is_audio_only_candidate(url):
                    continue
                for ratio_score, candidate_url in expand_video_quality_urls(url):
                    candidates.append(
                        MediaCandidate(
                            url=candidate_url,
                            size_bytes=size_bytes,
                            width=width,
                            height=height,
                            bit_rate=bit_rate,
                            ratio_score=ratio_score,
                            quality_type=quality_type,
                            source=f"bit_rate.{key}",
                            quality_label=get_url_ratio(candidate_url) or video.get("ratio") or "原始文件",
                        )
                    )

    for priority, key in enumerate(("download_addr", "play_addr_h264", "play_addr", "play_addr_265"), start=1):
        addr = video.get(key)
        size_bytes = get_addr_size(addr)
        width = get_addr_dimension(addr, "width")
        height = get_addr_dimension(addr, "height")
        for url in iter_urls(addr) or ():
            if is_audio_only_candidate(url):
                continue
            for ratio_score, candidate_url in expand_video_quality_urls(url):
                candidates.append(
                    MediaCandidate(
                        url=candidate_url,
                        size_bytes=size_bytes,
                        width=width,
                        height=height,
                        ratio_score=ratio_score,
                        priority=priority,
                        source=key,
                        quality_label=get_url_ratio(candidate_url) or video.get("ratio") or "原始文件",
                    )
                )

    return rank_media_candidates(candidates)


def get_video_candidate_items(video):
    return [
        {
            "url": candidate.url,
            "size_bytes": candidate.size_bytes,
            "quality_label": candidate.quality_label,
            "score": candidate.score,
            "source": candidate.source,
        }
        for candidate in get_video_media_candidates(video)
    ]


def get_video_candidates(video):
    return [item["url"] for item in get_video_candidate_items(video)]


def build_mobile_share_urls(aweme_id):
    if not aweme_id:
        return ()
    return (
        f"https://www.iesdouyin.com/share/video/{aweme_id}/?region=CN",
        f"https://www.iesdouyin.com/share/note/{aweme_id}/?region=CN",
    )


def build_mobile_feed_url(aweme_id):
    if not aweme_id:
        return ""
    params = {
        "aid": "1128",
        "app_name": "aweme",
        "version_name": "26.0.0",
        "version_code": "260000",
        "device_platform": "android",
        "os": "android",
        "ssmix": "a",
        "device_type": "Pixel 6",
        "device_brand": "google",
        "language": "zh",
        "os_api": "31",
        "os_version": "12",
        "openudid": "1234567890",
        "manifest_version_code": "260000",
        "resolution": "1080*1920",
        "dpi": "420",
        "update_version_code": "26009900",
        "aweme_id": aweme_id,
    }
    return f"https://aweme.snssdk.com/aweme/v1/feed/?{urlencode(params)}"


def find_aweme_by_id(data, aweme_id):
    if not aweme_id:
        return {}
    for item in iter_dicts(data):
        if item.get("aweme_id") == aweme_id and ("video" in item or "images" in item):
            return item
    return {}


class DouyinAdapter(BasePlatformAdapter):
    platform_id = "douyin"
    display_name = "抖音"
    page_host_suffixes = DOUYIN_PAGE_HOST_SUFFIXES
    media_host_suffixes = DOUYIN_MEDIA_HOST_SUFFIXES
    headers = DOUYIN_HEADERS
    capabilities = PlatformCapabilities(
        media_types=("image", "video"),
        batch_download=True,
        cover=True,
        ultra_hd=True,
    )
    provider_policy = ProviderPolicy(
        primary_provider="native",
        fallback_providers=("tiantianjiexi", "kukutool", "tikhub"),
        enhanced_provider_enabled_by="ULTRA_HD_PROVIDER_ENABLED",
    )
    parse_option_names = (
        "prefer_ultra_hd",
        "client_country_code",
        "douyin_provider_order",
        "timeout_seconds",
        "deadline_monotonic",
    )

    def parse_html(self, html, source_url=None, probe_video_candidates=False):
        aweme = find_aweme_in_html(html)
        if aweme:
            return self.parse_aweme(aweme, probe_video_candidates=probe_video_candidates)

        return ParseResult(
            platform=self.platform_id,
            post_type="unknown",
            base_filename="douyin",
            assets=(),
        )

    def parse_aweme(self, aweme, probe_video_candidates=False):
        base_filename = get_base_filename(aweme)
        metadata = get_aweme_metadata(aweme)
        image_assets = self.build_image_assets(aweme)
        if image_assets:
            return ParseResult(
                platform=self.platform_id,
                post_type="image",
                base_filename=base_filename,
                assets=tuple(image_assets),
                metadata=metadata,
            )

        video = aweme.get("video") or {}
        video_candidates = get_video_media_candidates(video)
        if video_candidates:
            video_candidate = video_candidates[0]
            if probe_video_candidates:
                selection = select_best_media_candidate(
                    video_candidates,
                    headers=self.headers,
                    allowed_final_host_suffixes=self.media_host_suffixes,
                    expected_type="video",
                )
                video_candidate = selection.candidate or video_candidate

            cover_url, cover_preview_url = get_video_cover_urls(video)
            return ParseResult(
                platform=self.platform_id,
                post_type="video",
                base_filename=base_filename,
                assets=(
                    MediaAsset(
                        media_type="video",
                        url=video_candidate.url,
                        platform=self.platform_id,
                        cover_url=cover_url,
                        cover_preview_url=cover_preview_url,
                        quality_label=video_candidate.quality_label,
                        size_bytes=video_candidate.size_bytes,
                        headers=self.headers,
                        media_host_suffixes=self.media_host_suffixes,
                    ),
                ),
                metadata=metadata,
            )

        return ParseResult(
            platform=self.platform_id,
            post_type="unknown",
            base_filename=base_filename,
            assets=(),
            metadata=metadata,
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

    def fetch_mobile_feed_aweme(self, aweme_id, request_timeout=15, request_retries=2):
        feed_url = build_mobile_feed_url(aweme_id)
        if not feed_url:
            return {}

        response = request_get_with_retry(
            feed_url,
            headers=DOUYIN_APP_HEADERS,
            timeout=request_timeout,
            retries=request_retries,
            allowed_final_host_suffixes=("snssdk.com",),
            final_url_message="抖音移动接口跳转到了不允许的域名",
        )
        try:
            data = response.json()
        finally:
            response.close()

        return find_aweme_by_id(data, aweme_id)

    def fetch_mobile_feed_result(
        self,
        aweme_id,
        probe_video_candidates=False,
        request_timeout=15,
        request_retries=2,
    ):
        aweme = self.fetch_mobile_feed_aweme(
            aweme_id,
            request_timeout=request_timeout,
            request_retries=request_retries,
        )
        if not aweme:
            return None
        result = self.parse_aweme(aweme, probe_video_candidates=probe_video_candidates)
        return result if result.assets else None

    def fetch_mobile_share_aweme(
        self,
        aweme_id,
        request_timeout=15,
        request_retries=2,
    ):
        for share_url in build_mobile_share_urls(aweme_id):
            try:
                response = request_get_with_retry(
                    share_url,
                    headers=DOUYIN_MOBILE_HEADERS,
                    timeout=request_timeout,
                    retries=request_retries,
                    allowed_final_host_suffixes=self.page_host_suffixes,
                    final_url_message="抖音移动分享页跳转到了不允许的域名",
                )
                try:
                    aweme = find_aweme_in_html(response.text)
                    if aweme:
                        return aweme
                finally:
                    response.close()
            except Exception:
                continue
        return {}

    def fetch_mobile_share_result(
        self,
        aweme_id,
        probe_video_candidates=False,
        request_timeout=15,
        request_retries=2,
    ):
        aweme = self.fetch_mobile_share_aweme(
            aweme_id,
            request_timeout=request_timeout,
            request_retries=request_retries,
        )
        if aweme:
            result = self.parse_aweme(aweme, probe_video_candidates=probe_video_candidates)
            if result.assets:
                return result
        return None

    def parse_mobile_feed(self, aweme_id):
        return self.fetch_mobile_feed_result(aweme_id)

    def parse_mobile_share(self, aweme_id):
        return self.fetch_mobile_share_result(aweme_id)

    def resolve_short_url(self, url, request_deadline=0):
        current_url = url
        short_link_deadline = monotonic() + max(
            0.1,
            float(getattr(settings, "DOUYIN_SHORT_LINK_TIMEOUT_SECONDS", 2.0) or 0),
        )
        deadline = (
            min(short_link_deadline, request_deadline)
            if request_deadline
            else short_link_deadline
        )
        max_redirects = max(
            1,
            int(getattr(settings, "DOUYIN_SHORT_LINK_MAX_REDIRECTS", 4) or 1),
        )

        for _ in range(max_redirects + 1):
            validate_external_url(
                current_url,
                self.page_host_suffixes,
                "抖音短链接跳转到了不允许的域名",
            )
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise requests.Timeout("抖音短链接解析超时")

            response = requests.get(
                current_url,
                headers=self.headers,
                timeout=max(0.1, remaining),
                allow_redirects=False,
                stream=True,
            )
            try:
                if response.status_code not in {301, 302, 303, 307, 308}:
                    return response.url or current_url
                location = response.headers.get("Location")
                if not location:
                    return response.url or current_url
                redirect_url = urljoin(current_url, location)
                validate_external_url(
                    redirect_url,
                    self.page_host_suffixes,
                    "抖音短链接跳转到了不允许的域名",
                )
                if extract_aweme_id(redirect_url):
                    return redirect_url
                current_url = redirect_url
            finally:
                response.close()

        raise requests.TooManyRedirects("抖音短链接跳转次数过多")

    def build_parse_context(
        self,
        url,
        fetch_direct_page=True,
        fast_short_link=False,
        request_deadline=0,
        prefer_ultra_hd=False,
    ):
        context = DouyinParseContext(
            url=url,
            deadline_monotonic=request_deadline,
            prefer_ultra_hd=prefer_ultra_hd,
        )
        context.aweme_id = extract_aweme_id(context.url)
        short_url = is_douyin_short_url(url)

        if short_url and not context.aweme_id:
            try:
                context.final_url = self.resolve_short_url(
                    url,
                    request_deadline=request_deadline,
                )
                context.aweme_id = extract_aweme_id(context.final_url)
            except Exception as error:
                context.direct_error = error

        should_fetch_direct_page = (fetch_direct_page or not context.aweme_id) and not context.aweme_id
        if should_fetch_direct_page:
            try:
                remaining_timeout = 15
                if request_deadline:
                    remaining_timeout = min(
                        remaining_timeout,
                        max(0.1, request_deadline - monotonic()),
                    )
                response = request_get_with_retry(
                    url,
                    headers=self.headers,
                    timeout=remaining_timeout,
                    retries=1 if fast_short_link else 3,
                    allowed_final_host_suffixes=self.page_host_suffixes,
                    final_url_message="抖音页面跳转到了不允许的域名",
                )
                try:
                    context.final_url = response.url
                    context.html = response.text
                    context.direct_result = self.parse_html(
                        context.html,
                        source_url=context.final_url,
                        probe_video_candidates=prefer_ultra_hd,
                    )
                finally:
                    response.close()
            except Exception as error:
                context.direct_error = error

        context.aweme_id = (
            context.aweme_id
            or extract_aweme_id(context.final_url)
            or extract_aweme_id(context.html)
        )
        return context

    def get_result_providers(self, prefer_ultra_hd=False, provider_order=None):
        return build_douyin_result_providers(
            prefer_ultra_hd=prefer_ultra_hd,
            provider_order=provider_order,
        )

    def parse_url(
        self,
        url,
        prefer_ultra_hd=False,
        client_country_code="",
        douyin_provider_order=None,
        timeout_seconds=None,
        deadline_monotonic=None,
    ):
        parse_started_at = time()
        self.require_supported_url(url)
        request_deadline = float(deadline_monotonic or 0)
        if not request_deadline and timeout_seconds is not None:
            request_deadline = monotonic() + max(0.1, float(timeout_seconds or 0))
        if prefer_ultra_hd and not request_deadline:
            request_deadline = monotonic() + max(
                0.1,
                float(getattr(settings, "DOUYIN_ULTRA_TOTAL_TIMEOUT_SECONDS", 5.0) or 0),
            )
        context = self.build_parse_context(
            url,
            fetch_direct_page=not bool(extract_aweme_id(url)),
            fast_short_link=prefer_ultra_hd,
            request_deadline=request_deadline,
            prefer_ultra_hd=prefer_ultra_hd,
        )
        context.client_country_code = client_country_code
        context.prefer_ultra_hd = prefer_ultra_hd

        last_timeout_error = None
        for provider in self.get_result_providers(
            prefer_ultra_hd=prefer_ultra_hd,
            provider_order=douyin_provider_order,
        ):
            provider_started_at = time()
            try:
                result = provider.parse(self, context)
                if result and result.assets:
                    logger.info(
                        "douyin_parse_selected",
                        extra={
                            "provider": getattr(provider, "name", type(provider).__name__),
                            "selected_provider": (result.metadata or {}).get("selected_provider", getattr(provider, "name", "")),
                            "fallback_reason": (result.metadata or {}).get("fallback_reason", ""),
                            "aweme_id": context.aweme_id,
                            "asset_count": len(result.assets),
                            "duration_ms": int((time() - provider_started_at) * 1000),
                            "total_duration_ms": int((time() - parse_started_at) * 1000),
                            "prefer_ultra_hd": prefer_ultra_hd,
                        },
                    )
                    return result
            except XhsProviderTimeoutError as error:
                last_timeout_error = error
                logger.info(
                    "douyin_provider_failed",
                    extra={
                        "provider": getattr(provider, "name", type(provider).__name__),
                        "aweme_id": context.aweme_id,
                        "error_type": type(error).__name__,
                        "error": str(error),
                        "duration_ms": int((time() - provider_started_at) * 1000),
                        "prefer_ultra_hd": prefer_ultra_hd,
                    },
                )
                continue
            except Exception as error:
                logger.info(
                    "douyin_provider_failed",
                    extra={
                        "provider": getattr(provider, "name", type(provider).__name__),
                        "aweme_id": context.aweme_id,
                        "error_type": type(error).__name__,
                        "error": str(error),
                        "duration_ms": int((time() - provider_started_at) * 1000),
                        "prefer_ultra_hd": prefer_ultra_hd,
                    },
                )
                continue

        if context.direct_result is not None and context.direct_result.assets:
            return context.direct_result

        if context.direct_result is not None:
            return context.direct_result
        if context.direct_error:
            raise context.direct_error
        if last_timeout_error:
            raise last_timeout_error

        return ParseResult(
            platform=self.platform_id,
            post_type="unknown",
            base_filename="douyin",
            assets=(),
        )
