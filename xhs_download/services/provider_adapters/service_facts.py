"""Pure raw-service facts shared by service-family adapters, without decisions."""
from datetime import datetime, timezone
import math
import re
from urllib.parse import parse_qs, urlsplit

from ..pipeline import Tracks
from ..platforms.douyin import DOUYIN_MEDIA_HOST_SUFFIXES
from ..platforms.bilibili import BILIBILI_MEDIA_HOST_SUFFIXES
from ..platforms.third_party import THIRD_PARTY_PLATFORM_RULES
from ..platforms.xiaohongshu import XHS_MEDIA_HOST_SUFFIXES
from ..request_context import _DEFAULT_MEDIA_HEADERS, inherit_request_context

_URL_KEYS = ("url", "video_url", "videoUrl", "download_url", "downloadUrl", "play_url", "playUrl", "src")


def _text(value):
    return value.strip() if isinstance(value, str) else ""


def _first(row, keys):
    return next((row[key] for key in keys if row.get(key) not in (None, "", [], {})), None)


def _number(value, *, integer=False):
    if isinstance(value, str) and re.fullmatch(r"\d+(?:\.\d+)?", value):
        value = float(value)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        return None
    return int(value) or None if integer else value


def _url(value):
    value = _text(value)
    try:
        parsed = urlsplit(value)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            return ""
        parsed.port
    except ValueError:
        return ""
    return value


def _media_url(row):
    return _url(row if isinstance(row, str) else _first(row, _URL_KEYS) if isinstance(row, dict) else None)


def _host_matches(host, suffix):
    return host == suffix or host.endswith("." + suffix)


def _host_policy(source_url, media_url):
    host = urlsplit(source_url).hostname or ""
    for sources, suffixes, name in (
        (("douyin.com", "iesdouyin.com", "amemv.com"), DOUYIN_MEDIA_HOST_SUFFIXES, "DOUYIN_MEDIA_HOST_SUFFIXES"),
        (("bilibili.com", "b23.tv"), BILIBILI_MEDIA_HOST_SUFFIXES, "BILIBILI_MEDIA_HOST_SUFFIXES"),
        (("xiaohongshu.com", "xhslink.com"), XHS_MEDIA_HOST_SUFFIXES, "XHS_MEDIA_HOST_SUFFIXES"),
    ):
        if any(_host_matches(host, suffix) for suffix in sources):
            return tuple(suffixes), "existing:" + name
    for rule in THIRD_PARTY_PLATFORM_RULES:
        if rule.media_host_suffixes and any(_host_matches(host, suffix) for suffix in rule.host_suffixes):
            return tuple(rule.media_host_suffixes), "existing:THIRD_PARTY_PLATFORM_RULES." + rule.platform_id
    return (urlsplit(media_url).hostname,), "adapter:exact_url_host"


def _source_id(source_url):
    parsed = urlsplit(source_url)
    host, path = parsed.hostname or "", parsed.path
    patterns = (
        (("douyin.com", "iesdouyin.com"), r"/(?:video|note)/(\d+)/?"),
        (("bilibili.com",), r"/video/(BV[0-9A-Za-z]{10}|av[1-9][0-9]*)/?"),
        (("xiaohongshu.com",), r"/(?:explore|discovery/item)/([a-fA-F0-9]{24})/?"),
        (("acfun.cn",), r"/v/(ac\d+)/?"),
        (("weibo.com", "weibo.cn"), r"/(?:\d+|status|detail)/([A-Za-z0-9]+)/?"),
        (("tiktok.com",), r"/@[^/]+/video/(\d+)/?"),
        (("instagram.com",), r"/(?:p|reel|tv)/([^/]+)/?"),
        (("kuaishou.com",), r"/short-video/([^/]+)/?"),
    )
    for hosts, pattern in patterns:
        if any(_host_matches(host, suffix) for suffix in hosts):
            match = re.fullmatch(pattern, path)
            if match:
                return match[1]
    query = parse_qs(parsed.query)
    if _host_matches(host, "douyin.com"):
        value = query.get("modal_id", [""])[0]
        return value if value.isdigit() else ""
    if host == "youtu.be":
        return path.strip("/")
    if _host_matches(host, "youtube.com"):
        return query.get("v", [""])[0]
    return ""


def _size(row):
    for key in ("sizeInBytes", "size_bytes", "file_size", "fileSize", "filesize", "size"):
        value = row.get(key)
        number = _number(value, integer=True)
        if number:
            return number, row.get("size_exact") is not False
        match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(KiB|MiB|GiB|KB|MB|GB)\s*", str(value or ""), re.I)
        if match:
            unit = match[2].lower()
            power = {"k": 1, "m": 2, "g": 3}[unit[0]]
            size = int(float(match[1]) * (1024 if "i" in unit else 1000) ** power)
            return (size or None), False
    return None, False


def _tracks(row):
    value = row.get("tracks")
    if value in {item.value for item in Tracks}:
        return Tracks(value)
    video, audio = row.get("vcodec"), row.get("acodec")
    if video not in (None, "", "unknown", "none") and audio not in (None, "", "unknown", "none"):
        return Tracks.AV
    if video not in (None, "", "unknown", "none") and audio == "none":
        return Tracks.VIDEO_ONLY
    if video == "none" and audio not in (None, "", "unknown", "none"):
        return Tracks.AUDIO_ONLY
    return Tracks.UNKNOWN


def _required_headers(sources):
    context = inherit_request_context(*sources)
    names = context.get("required_headers") or ()
    if not isinstance(names, (list, tuple)) or any(not isinstance(name, str) for name in names):
        raise ValueError("INVALID_HEADERS")
    required = {name.lower() for name in names if name.lower() not in _DEFAULT_MEDIA_HEADERS}
    for source in sources:
        required.update(name.lower() for name, value in inherit_request_context(source)["headers"].items()
                        if value not in (None, "") and name.lower() not in _DEFAULT_MEDIA_HEADERS)
    if any(source.get("requires_cookie") is True or source.get("cookies") for source in sources):
        required.add("cookie")
    return tuple(sorted(required))


def _expiry(row, media_url):
    value = _number(row.get("expires_at"))
    if not value and any(_host_matches(urlsplit(media_url).hostname or "", suffix) for suffix in BILIBILI_MEDIA_HOST_SUFFIXES):
        value = _number(parse_qs(urlsplit(media_url).query).get("deadline", [None])[0])
    if value:
        try:
            return datetime.fromtimestamp(value, timezone.utc)
        except (ValueError, OverflowError, OSError):
            pass
    return None
