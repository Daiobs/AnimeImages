import hashlib
from contextlib import contextmanager
import logging
import re
import threading
import time
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

import requests
from requests.utils import requote_uri

from ..assets import MediaAsset, ParseResult
from ..candidates import MediaCandidate, dedupe_candidates
from ..common import (
    XhsDownloadError,
    XhsParseError,
    XhsProviderTimeoutError,
    XhsSecurityError,
    clean_filename,
    normalize_text,
    request_get_with_retry,
    validate_external_url,
)
from .base import BasePlatformAdapter, PlatformCapabilities, ProviderPolicy


BILIBILI_PAGE_HOST_SUFFIXES = ("bilibili.com", "b23.tv")
BILIBILI_MEDIA_HOST_SUFFIXES = ("bilivideo.com", "bilivideo.cn", "hdslb.com")
ANONYMOUS_COOKIE_NAMES = frozenset({"buvid3", "buvid4", "b_nut"})
ANONYMOUS_COOKIE_TTL = 600
WBI_KEY_TTL = 3600
_context_lock = threading.RLock()
_context_generation = threading.Event()
_cookie_cache = None
_cookie_expires = 0
_wbi_cache = None
_wbi_expires = 0
WBI_MIXIN_ORDER = (
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30,
    4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
)
logger = logging.getLogger(__name__)
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


class BilibiliIncompleteMediaError(XhsParseError):
    def __init__(self, reason, formats):
        self.delivery_reason = reason
        self.formats = tuple(formats)
        message = (
            "已获取哔哩哔哩分离轨媒体，但当前路径未接通合流，暂不能保存为完整视频。"
            if reason == "separate_tracks"
            else "已获取哔哩哔哩分段视频，但当前路径未接通拼接，暂不能保存为完整视频。"
        )
        super().__init__(message)


def play_quality_label(play_data, quality_id=None, *, height=0):
    quality_id = play_data.get("quality") if quality_id is None else quality_id
    for item in play_data.get("support_formats") or ():
        if str(item.get("quality")) == str(quality_id):
            label = item.get("new_description") or item.get("display_desc")
            if label:
                return str(label)
    for value, label in zip(
        play_data.get("accept_quality") or (), play_data.get("accept_description") or ()
    ):
        if str(value) == str(quality_id) and label:
            return str(label)
    if height:
        return f"{height}p"
    return f"清晰度编号 {quality_id}" if quality_id else "原始文件"


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


def _context_clock():
    return time.monotonic()


@contextmanager
def _locked_context(deadline_monotonic=None):
    acquired = _context_lock.acquire(blocking=False)
    try:
        yield acquired
    finally:
        if acquired:
            _context_lock.release()


def clear_anonymous_context_cache():
    """Drop process-local anonymous material; never touch shared caches or media."""
    global _context_generation, _cookie_cache, _cookie_expires, _wbi_cache, _wbi_expires
    with _context_lock:
        _context_generation.set()
        _context_generation = threading.Event()
        _cookie_cache, _cookie_expires = None, 0
        _wbi_cache, _wbi_expires = None, 0


def _reject_anonymous_context(error, cookies, *, optional=False):
    error.bilibili_context_reused = bool(getattr(cookies, "_bilibili_reused", False))
    if cookies is None:
        return
    cookies._bilibili_rejected = True
    generation = getattr(cookies, "_bilibili_generation", None)
    if optional and not error.bilibili_context_reused:
        # A fresh optional nav failure still permits the existing legacy view path,
        # but cannot seed the cache for future parses.
        if generation is not None:
            generation.set()
            cookies._bilibili_generation = threading.Event()
            cookies._bilibili_snapshot = None
        return
    # Mark rejection immediately, without waiting for cache publication.
    # A late failure only revokes its own generation, never a newer context.
    if generation is not None:
        generation.set()


def _require_live_context(cookies):
    generation = getattr(cookies, "_bilibili_generation", None)
    if generation is not None and generation.is_set():
        error = XhsParseError("哔哩哔哩匿名上下文已失效")
        error.bilibili_context_reused = True
        raise error


def _publish_anonymous_cookies(cookies, deadline_monotonic):
    global _cookie_cache
    if getattr(cookies, "_bilibili_rejected", False):
        return
    with _locked_context(deadline_monotonic) as acquired:
        if not acquired:
            # Keep response changes local when publishing would wait. Upstream
            # rejection still revokes the generation independently of this lock.
            return
        if (not _context_generation.is_set()
                and getattr(cookies, "_bilibili_generation", None) is _context_generation
                and getattr(cookies, "_bilibili_expires", None) == _cookie_expires
                and _context_clock() < _cookie_expires):
            snapshot = getattr(cookies, "_bilibili_snapshot", None)
            if snapshot is not _cookie_cache:
                # Stale additions never overwrite newer values, but a same-value
                # revocation must not disappear merely because another response won.
                current = {(c.domain, c.path, c.name): c.value for c in (_cookie_cache or ())}
                updated = {(c.domain, c.path, c.name) for c in cookies}
                if any((c.domain, c.path, c.name) not in updated
                       and current.get((c.domain, c.path, c.name)) == c.value
                       for c in (snapshot or ())):
                    _context_generation.set()
                return
            _cookie_cache = cookies.copy() if cookies else None
            cookies._bilibili_snapshot = _cookie_cache


def retain_anonymous_cookies(response, cookies):
    updated = cookies.copy()
    # Apply Set-Cookie deletion semantics, which response.cookies omits.
    requests.cookies.extract_cookies_to_jar(updated, response.request, response.raw)
    updated.update(response.cookies)
    cookies.clear()
    for cookie in updated:
        if (cookie.name in ANONYMOUS_COOKIE_NAMES
                and cookie.domain.lstrip(".").lower() in {
                    "bilibili.com", "www.bilibili.com", "api.bilibili.com"}
                and len(cookie.value or "") <= 4096):
            cookies.set_cookie(cookie)
    cookies.clear_expired_cookies()


def _new_anonymous_cookie_context(*, deadline_monotonic=None):
    cookies = requests.cookies.RequestsCookieJar()
    try:
        response = request_get_with_retry(
            "https://www.bilibili.com/",
            headers={**BILIBILI_HEADERS, "Accept": "text/html"},
            timeout=bilibili_request_timeout(deadline_monotonic, 3),
            retries=1,
            stream=True,
            max_redirects=0,
            return_redirect_response=True,
            non_retry_statuses=(412, 429),
            allowed_final_host_suffixes=("bilibili.com",),
            final_url_message="哔哩哔哩匿名初始化跳转到了不允许的域名",
            deadline_monotonic=deadline_monotonic,
        )
    except requests.exceptions.SSLError:
        raise
    except (XhsDownloadError, requests.Timeout, requests.ConnectionError,
            requests.TooManyRedirects) as error:
        # Optional initialization must not remove the existing cookie-free path.
        logger.info("bilibili_anonymous_context_unavailable error_type=%s http_status=%s",
                    type(error).__name__, getattr(error, "http_status", None))
        return cookies
    try:
        retain_anonymous_cookies(response, cookies)
    finally:
        response.close()
    spi_url = "https://api.bilibili.com/x/frontend/finger/spi"
    cookie_header = requests.cookies.get_cookie_header(
        cookies, requests.Request("GET", spi_url).prepare())
    if cookie_header and not re.search(r"(?:^|;\s*)buvid4=", cookie_header):
        try:
            data = api_get_json(
                spi_url, {}, cookies=cookies, deadline_monotonic=deadline_monotonic,
                retries=1, timeout_seconds=3,
            )
            buvid4 = data.get("b_4") if isinstance(data, dict) else None
            if (isinstance(buvid4, str) and 0 < len(buvid4) <= 4096
                    and not re.search(r"[\x00-\x20\x7f;]", buvid4)):
                cookies.set("buvid4", buvid4, domain=".bilibili.com", path="/", secure=True)
        except (XhsSecurityError, requests.exceptions.SSLError):
            raise
        except (XhsDownloadError, XhsParseError, requests.Timeout, requests.ConnectionError,
                requests.TooManyRedirects, ValueError) as error:
            logger.info("bilibili_anonymous_buvid4_unavailable error_type=%s", type(error).__name__)
    logger.info("bilibili_anonymous_context_ready cookie_count=%s", len(cookies))
    return cookies


def _attach_anonymous_context(cookies, generation, expires, *, reused=False, snapshot=None):
    cookies._bilibili_generation = generation
    cookies._bilibili_expires = expires
    cookies._bilibili_reused = reused
    cookies._bilibili_snapshot = snapshot
    return cookies


def _cached_anonymous_cookies():
    # Caller holds the cache lock; only copy live material, never perform HTTP here.
    if _context_generation.is_set() or _context_clock() >= _cookie_expires:
        return None
    cookies = _cookie_cache.copy() if _cookie_cache is not None else None
    if cookies is not None:
        cookies.clear_expired_cookies()
    if cookies:
        return _attach_anonymous_context(
            cookies, _context_generation, _cookie_expires,
            reused=True, snapshot=_cookie_cache,
        )
    return None


def anonymous_cookie_context(*, deadline_monotonic=None):
    global _context_generation, _cookie_cache, _cookie_expires
    observed_generation = None
    with _locked_context(deadline_monotonic) as acquired:
        if acquired:
            if _context_generation.is_set():
                clear_anonymous_context_cache()
            cached = _cached_anonymous_cookies()
            if cached is not None:
                return cached
            observed_generation = _context_generation

    cookies = _new_anonymous_cookie_context(deadline_monotonic=deadline_monotonic)
    _attach_anonymous_context(
        cookies, threading.Event(), _context_clock() + ANONYMOUS_COOKIE_TTL,
    )
    with _locked_context(deadline_monotonic) as acquired:
        if acquired and observed_generation is not None:
            cached = _cached_anonymous_cookies()
            if cached is not None:
                return cached
            if (observed_generation is _context_generation
                    and not observed_generation.is_set() and cookies
                    and not getattr(cookies, "_bilibili_rejected", False)):
                # First completion wins. Expiry changes the cookie generation only;
                # still-valid WBI keys survive. Contenders keep a private context.
                _context_generation = cookies._bilibili_generation
                _cookie_expires = cookies._bilibili_expires
                _cookie_cache = cookies.copy()
                cookies._bilibili_snapshot = _cookie_cache
    return cookies


def api_get_json(
    url,
    params,
    referer="https://www.bilibili.com/",
    *,
    deadline_monotonic=None,
    cookies=None,
    accepted_codes=(0,),
    retries=2,
    timeout_seconds=15,
):
    query = urlencode({key: value for key, value in params.items() if value is not None})
    _require_live_context(cookies)
    request_url = f"{url}?{query}" if query else url
    headers = {**BILIBILI_HEADERS, "Referer": requote_uri(referer)}
    if cookies is not None:
        cookie_header = requests.cookies.get_cookie_header(
            cookies, requests.Request("GET", request_url).prepare())
        if cookie_header:
            headers["Cookie"] = cookie_header
    try:
        response = request_get_with_retry(
            request_url,
            headers=headers,
            timeout=bilibili_request_timeout(deadline_monotonic, timeout_seconds),
            retries=retries,
            # An explicit Cookie header is scoped to this exact API request.
            max_redirects=0 if "Cookie" in headers else 5,
            non_retry_statuses=(412, 429),
            allowed_final_host_suffixes=("bilibili.com",),
            final_url_message="哔哩哔哩接口跳转到了不允许的域名",
            deadline_monotonic=deadline_monotonic,
        )
    except requests.exceptions.SSLError:
        raise
    except XhsDownloadError as error:
        stage = "view" if url.endswith("/view") else "playurl" if url.endswith("/playurl") else "api"
        logger.info("bilibili_native_http_failure stage=%s http_status=%s",
                    stage, getattr(error, "http_status", None))
        if getattr(error, "http_status", None) == 412:
            _reject_anonymous_context(error, cookies, optional=url.endswith(("/nav", "/spi")))
        raise
    except (requests.Timeout, requests.ConnectionError) as error:
        raise XhsProviderTimeoutError("哔哩哔哩接口请求超时，请稍后重试") from error
    try:
        if cookies is not None:
            retain_anonymous_cookies(response, cookies)
            _publish_anonymous_cookies(cookies, deadline_monotonic)
        data = response.json()
    finally:
        response.close()

    if not isinstance(data, dict):
        raise XhsParseError("哔哩哔哩接口返回格式错误")
    if data.get("code") not in accepted_codes:
        message = data.get("message") or "接口返回异常"
        error = XhsParseError(f"哔哩哔哩解析失败：{message}")
        if data.get("code") == -412 or (data.get("code") == -403 and "/wbi/" in url):
            _reject_anonymous_context(error, cookies, optional=url.endswith(("/nav", "/spi")))
        raise error
    return data.get("data") or {}


def _load_wbi_mixin(cookies, *, deadline_monotonic=None):
    nav_url = "https://api.bilibili.com/x/web-interface/nav"
    if not requests.cookies.get_cookie_header(cookies, requests.Request("GET", nav_url).prepare()):
        return None
    try:
        # Anonymous nav returns -101 but still supplies the public signing material.
        data = api_get_json(
            nav_url, {}, cookies=cookies, accepted_codes=(0, -101),
            deadline_monotonic=deadline_monotonic, retries=1, timeout_seconds=3,
        )
        view_request = requests.Request("GET", "https://api.bilibili.com/x/web-interface/wbi/view").prepare()
        if not requests.cookies.get_cookie_header(cookies, view_request):
            return None
        wbi = data.get("wbi_img") if isinstance(data, dict) else None
        if not isinstance(wbi, dict):
            return None
        keys = []
        for name in ("img_url", "sub_url"):
            value = wbi.get(name)
            key = urlparse(value).path.rsplit("/", 1)[-1].split(".", 1)[0] if isinstance(value, str) else ""
            if not re.fullmatch(r"[0-9a-fA-F]{32}", key):
                return None
            keys.append(key)
    except (XhsSecurityError, requests.exceptions.SSLError):
        raise
    except (XhsDownloadError, XhsParseError, requests.Timeout, requests.ConnectionError,
            requests.TooManyRedirects, ValueError) as error:
        logger.info("bilibili_wbi_context_unavailable error_type=%s", type(error).__name__)
        return None
    lookup = "".join(keys)
    return "".join(lookup[index] for index in WBI_MIXIN_ORDER)[:32]


def signed_view_params(params, cookies, *, deadline_monotonic=None):
    global _wbi_cache, _wbi_expires
    view_request = requests.Request("GET", "https://api.bilibili.com/x/web-interface/wbi/view").prepare()
    if not requests.cookies.get_cookie_header(cookies, view_request):
        return None
    _require_live_context(cookies)
    generation = getattr(cookies, "_bilibili_generation", None)
    mixin = None
    with _locked_context(deadline_monotonic) as acquired:
        if (acquired and generation is _context_generation and not generation.is_set()
                and _wbi_cache is not None and _context_clock() < _wbi_expires):
            mixin = _wbi_cache
            cookies._bilibili_reused = True
    if mixin is None:
        mixin = _load_wbi_mixin(cookies, deadline_monotonic=deadline_monotonic)
        _require_live_context(cookies)
        with _locked_context(deadline_monotonic) as acquired:
            if (acquired and mixin and generation is _context_generation
                    and not generation.is_set()
                    and not getattr(cookies, "_bilibili_rejected", False)):
                if _wbi_cache is not None and _context_clock() < _wbi_expires:
                    mixin = _wbi_cache
                    cookies._bilibili_reused = True
                else:
                    _wbi_cache = mixin
                    _wbi_expires = _context_clock() + WBI_KEY_TTL
    if not mixin:
        return None
    signed = {
        key: "".join(char for char in str(value) if char not in "!'()*")
        for key, value in sorted({**params, "wts": round(time.time())}.items())
        if value is not None
    }
    signed["w_rid"] = hashlib.md5((urlencode(signed) + mixin).encode()).hexdigest()
    return signed


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


def describe_dash_formats(play_data):
    """Retain track facts without issuing standalone video-download assets."""
    videos = ((play_data.get("dash") or {}).get("video")) or []
    formats = []
    for item in videos:
        if not isinstance(item, dict):
            continue
        quality_id = item.get("id") or item.get("quality")
        formats.append({
            "quality_id": quality_id,
            "quality_label": play_quality_label(
                play_data, quality_id, height=int(item.get("height") or 0)),
            "width": int(item.get("width") or 0),
            "height": int(item.get("height") or 0),
            "size_bytes": int(item.get("size") or 0),
            "codecs": str(item.get("codecs") or ""),
            "protocol": "dash",
            "requires_merge": True,
            "separate_audio_count": len((play_data.get("dash") or {}).get("audio") or []),
        })
    return formats


def get_play_items(play_data):
    durls = play_data.get("durl") or []
    if len(durls) > 1:
        raise BilibiliIncompleteMediaError("segmented_file", [{
            "quality_label": play_quality_label(play_data),
            "segment_count": len(durls),
            "requires_merge": True,
        }])
    items = []
    for item in durls:
        url = normalize_bilibili_url(item.get("url") or "")
        if not url:
            continue
        if urlparse(url).path.lower().endswith((".m4s", ".mpd", ".m3u8")):
            raise BilibiliIncompleteMediaError("segmented_file", [{
                "quality_label": play_quality_label(play_data),
                "requires_merge": True,
            }])
        order = int(item.get("order") or len(items) + 1)
        size = int(item.get("size") or 0)
        items.append((order, url, size))

    items.sort(key=lambda item: item[0])
    candidates = []
    for _, url, size in items:
        candidates.append(MediaCandidate(
            url=url, size_bytes=size, source="durl",
            quality_label=play_quality_label(play_data)))
    if candidates:
        return [
            {
                "url": candidate.url,
                "size_bytes": candidate.size_bytes,
                "quality_label": candidate.quality_label,
            }
            for candidate in dedupe_candidates(candidates)
        ]
    formats = describe_dash_formats(play_data)
    if formats:
        raise BilibiliIncompleteMediaError("separate_tracks", formats)
    return []


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
        deadline = float(deadline_monotonic or 0)
        if not deadline and timeout_seconds is not None:
            deadline = time.monotonic() + max(0.001, float(timeout_seconds or 0))
        try:
            return self._parse_url(url, deadline)
        except (XhsDownloadError, XhsParseError) as error:
            if not getattr(error, "bilibili_context_reused", False):
                raise
            # Only a rejected cached context gets one fresh attempt, within the same budget.
            bilibili_request_timeout(deadline or None, 3)
            try:
                return self._parse_url(url, deadline)
            except (XhsParseError, XhsDownloadError) as rebuild_error:
                # A refresh cannot remove the original HTTP 412 fallback eligibility.
                # Keep the last diagnostic as the cause; TLS/security errors are not caught.
                if getattr(error, "http_status", None) == 412:
                    raise error from rebuild_error
                raise

    def _parse_url(self, url, deadline):
        self.require_supported_url(url)
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
        # Every parse owns its jar; only anonymous material is reused inside this process.
        cookies = anonymous_cookie_context(deadline_monotonic=deadline or None)
        view_params = build_view_params(bvid, aid)
        signed_params = signed_view_params(view_params, cookies, deadline_monotonic=deadline or None)
        view_data = api_get_json(
            "https://api.bilibili.com/x/web-interface/wbi/view" if signed_params is not None
            else "https://api.bilibili.com/x/web-interface/view",
            signed_params if signed_params is not None else view_params,
            referer=page_url,
            deadline_monotonic=deadline or None,
            cookies=cookies,
        )
        page = choose_page(view_data, page_number)
        if not page.get("cid"):
            raise XhsParseError("没有解析到哔哩哔哩视频 CID")

        play_data = api_get_json(
            "https://api.bilibili.com/x/player/playurl",
            build_play_params(view_data, page),
            referer=page_url,
            deadline_monotonic=deadline or None,
            cookies=cookies,
        )
        video_items = get_play_items(play_data)
        if not video_items:
            play_data = api_get_json(
                "https://api.bilibili.com/x/player/playurl",
                build_dash_play_params(view_data, page),
                referer=page_url,
                deadline_monotonic=deadline or None,
                cookies=cookies,
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
                        or play_quality_label(play_data)
                    ),
                    size_bytes=video_item["size_bytes"],
                )
                for video_item in video_items
            ),
        )
