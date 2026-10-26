"""TianTian raw JSON adapter; never consumes legacy normalized media rows."""
from copy import deepcopy
from datetime import datetime, timezone
import math
import re
import time
from types import MappingProxyType
from urllib.parse import parse_qs, urldefrag, urlsplit

from ..pipeline import (
    Failure, FailureKind, Fetchable, Item, Kind, Tracks, Variant, Work,
    height_from_label, validate_work,
)
from ..pipeline.model import PLACEHOLDER_LABELS, PLACEHOLDER_TITLES, SEGMENTED_PATH_SUFFIXES
from ..platforms.douyin import DOUYIN_MEDIA_HOST_SUFFIXES
from ..platforms.bilibili import BILIBILI_MEDIA_HOST_SUFFIXES
from ..platforms.third_party import THIRD_PARTY_PLATFORM_RULES
from ..platforms.xiaohongshu import XHS_MEDIA_HOST_SUFFIXES
from ..request_context import _DEFAULT_MEDIA_HEADERS, inherit_request_context
from ..work_caption import _TOPIC, work_caption_presentation
from .base import Adapter
from .failures import failure_for_code, failure_for_http


NAME = "tiantianjiexi_generic"
_BUSINESS_FAILURES = {
    "1001": Failure(FailureKind.UNAVAILABLE, "TIANTIAN_SERVICE_ERROR"),
}
_URL_KEYS = ("url", "video_url", "videoUrl", "download_url", "downloadUrl", "play_url", "playUrl", "src")
_MEDIA_KEYS = (*_URL_KEYS, "videos", "video", "images", "image_urls", "pics", "photos", "audioUrl", "audio_url", "audio", "music_url")
_EXTENSIONS = {"mp4", "webm", "mov", "mkv", "flv", "m4v", "m4a", "mp3", "aac", "ogg", "opus", "flac", "wav", "jpg", "jpeg", "png", "webp", "gif", "avif", "heic"}


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


def _identity(raw, payload, source_url):
    identities = []
    for row in (raw, payload):
        for key in ("work_id", "aweme_id", "note_id", "bvid", "id"):
            value = row.get(key)
            if isinstance(value, (str, int)) and not isinstance(value, bool) and str(value).strip():
                identities.append(str(value).strip())
    identity = _source_id(source_url)
    if identity and identities and identity not in identities:
        raise ValueError("IDENTITY_MISMATCH")
    if len(set(identities)) > 1:
        raise ValueError("IDENTITY_MISMATCH")
    return (identities[0], NAME) if identities else (identity, "request:work_id")


def _unwrap(raw):
    if not isinstance(raw, dict) or any(key.startswith("_service_") for key in raw):
        raise ValueError("NOT_RAW")
    for key in ("data", "result", "detail", "info"):
        value = raw.get(key)
        if isinstance(value, dict) and any(name in value for name in _MEDIA_KEYS):
            if any(name.startswith("_service_") for name in value):
                raise ValueError("NOT_RAW")
            return value
    return raw


def _business_failure(raw):
    for key in ("code", "status", "success"):
        if key not in raw:
            continue
        value = raw[key]
        if value is True or type(value) is not bool and str(value).lower() in {"0", "1", "200", "true", "success", "ok"}:
            continue
        # Only this code + message has recorded unsupported semantics. 451
        # alone (and every unrecognized business code) remains unavailable.
        message = _text(raw.get("msg") or raw.get("message"))
        if str(value) == "451" and "暂不支持该链接" in message:
            return failure_for_code(value, {"451": Failure(FailureKind.UNSUPPORTED, "TIANTIAN_UNSUPPORTED_URL")})
        return failure_for_code(value, _BUSINESS_FAILURES)
    return None


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


def _container(row, url):
    explicit = _text(_first(row, ("ext", "container", "extension"))).lower()
    if explicit in _EXTENSIONS:
        return explicit, NAME
    path = urlsplit(url)
    suffix = path.path.rpartition(".")[2].lower()
    if suffix in _EXTENSIONS:
        return suffix, "adapter:url_extension"
    # A MIME declaration in the original URL is a format fact, unlike br/qs.
    mime = parse_qs(path.query).get("mime_type", [""])[0].lower()
    mime_formats = {"video_mp4": "mp4", "video/mp4": "mp4", "video_webm": "webm", "video/webm": "webm"}
    return (mime_formats[mime], "adapter:url_mime_type") if mime in mime_formats else ("", "contract:default")


def _variant(row, variant_id, source_url, context, aliases):
    url = _media_url(row)
    if not url:
        raise ValueError("INVALID_MEDIA_URL")
    label = _text(_first(row, ("qualityLabel", "quality_label", "quality", "resolution", "clarity", "type", "name")))
    if label.casefold() in PLACEHOLDER_LABELS:
        label = ""
    container, _ = _container(row, url)
    size, exact = _size(row)
    policy, provenance = _host_policy(source_url, url)
    methods = {_text(source.get("request_method") or source.get("method")).upper()
               for source in aliases} - {"", "GET"}
    if len(methods) > 1:
        raise ValueError("CONFLICTING_METHODS")
    method = next(iter(methods), "GET")
    scoped_context = tuple(source for source in context
                           if not _media_url(source) or _media_url(source) == url)
    return Variant(
        variant_id, url, container=container, tracks=_tracks(row),
        height=_number(row.get("height"), integer=True) or height_from_label(label),
        width=_number(row.get("width"), integer=True), fps=_number(row.get("fps")),
        bitrate=_number(row.get("bitrate"), integer=True), quality_label=label,
        is_original=row.get("is_original") is True or label.casefold() in {"原画", "original", "source"},
        size_bytes=size, size_exact=exact, required_headers=_required_headers((*scoped_context, *aliases)),
        browser_fetchable=Fetchable.UNKNOWN,
        drm=any(source.get("drm") is True or source.get("has_drm") is True for source in aliases),
        segmented=(any(source.get("segmented") is True or bool(source.get("fragments"))
                   or any(token in _text(source.get("protocol")).lower() for token in ("m3u8", "dash", "hls")) for source in aliases)
                   or urlsplit(url).path.lower().endswith(SEGMENTED_PATH_SUFFIXES)),
        request_method=method.upper(), expires_at=_expiry(row, url), host_policy=policy,
    ), provenance


def _video_rows(video):
    if isinstance(video, str):
        return [{"url": video}]
    if not isinstance(video, dict):
        raise ValueError("INVALID_VIDEO")
    streams = video.get("streams") or []
    if not isinstance(streams, list) or any(not isinstance(row, dict) for row in streams):
        raise ValueError("INVALID_STREAMS")
    rows = list(streams)
    root_url = _media_url(video)
    # A bare main-link alias is not another format. Distinct labels in streams
    # stay distinct, even when they point to the same URL.
    if root_url and not any(_media_url(row) == root_url for row in rows):
        rows.insert(0, video)
    elif root_url:
        rows = [{**video, **{key: value for key, value in row.items() if value is not None}}
                if _media_url(row) == root_url else row for row in rows]
    return rows


def _map(raw, source_url):
    if not isinstance(raw, dict) or not _url(source_url):
        return Failure(FailureKind.INVALID_RESULT, "TIANTIAN_INVALID_RAW")
    failed = _business_failure(raw)
    if failed:
        return failed
    data = _unwrap(raw)
    work_id, identity_source = _identity(raw, data, source_url)
    if not work_id:
        return Failure(FailureKind.INVALID_RESULT, "TIANTIAN_MISSING_WORK_ID")
    context = (raw, data) if data is not raw else (data,)
    items, observations = [], {}

    def add(kind, rows, parents=()):
        index = len(items) + 1
        item_id = f"{kind.value}:{index}"
        variants = []
        rows = [{"url": row} if isinstance(row, str) else row for row in rows]
        for ordinal, row in enumerate(rows, 1):
            if not isinstance(row, dict):
                raise ValueError("INVALID_MEDIA")
            # Different labels can alias one resource. None/False on an alias
            # must not erase another alias's explicit access restrictions.
            aliases = tuple(source for source in (*parents, *rows)
                            if isinstance(source, dict) and _media_url(source) == _media_url(row))
            variant, host_source = _variant(row, f"{item_id}:{ordinal}", source_url, (*context, *parents), aliases)
            variants.append(variant)
            observations[variant.variant_id] = (row, host_source)
        items.append(Item(item_id, kind, index, tuple(variants)))

    videos = data.get("videos") or data.get("video") or []
    if isinstance(videos, (dict, str)):
        videos = [videos]
    if not isinstance(videos, list):
        raise ValueError("INVALID_VIDEOS")
    for video in videos:
        add(Kind.VIDEO, _video_rows(video), (video,) if isinstance(video, dict) else ())
    if not videos and _media_url(data):
        add(Kind.VIDEO, [data])
    images = _first(data, ("images", "image_urls", "pics", "photos")) or []
    if isinstance(images, str):
        images = [images]
    if not isinstance(images, list):
        raise ValueError("INVALID_IMAGES")
    # Human confirms TianTian represents Live Photos as a cover + short video,
    # but these arrays carry no per-item association. Preserve every resource;
    # equal counts do not prove pairs (notes can mix live and ordinary images).
    # Native's exact imageList-item evidence belongs to P10, not this raw map.
    for image in images:
        add(Kind.IMAGE, [image])
    audio = _first(data, ("audioUrl", "audio_url", "audio", "music_url"))
    if audio:
        add(Kind.AUDIO, [audio])
    cover = _url(_first(data, ("cover", "cover_url", "thumbnail", "pic")))
    if not cover and images:
        cover = _media_url(images[0])
    title = _text(_first(data, ("title", "name")) or raw.get("title"))
    description = _text(_first(data, ("desc", "description")) or _first(raw, ("desc", "description")))
    presentation = work_caption_presentation(title, description)
    title = presentation["title"]
    if title.casefold() in PLACEHOLDER_TITLES:
        title = ""
    author = _text(_first(data, ("nickname", "author", "user")))
    sources = {"source_url": "request", "work_id": identity_source, "items": NAME}
    for name, value in (("title", title), ("description", presentation["description"]),
                        ("topics", presentation["topics"]), ("author", author), ("cover", cover),
                        ("duration_seconds", data.get("duration")),
                        ("quality", any(v.height or v.quality_label for i in items for v in i.variants)),
                        ("size", any(v.size_bytes for i in items for v in i.variants))):
        if value:
            sources[name] = NAME
    for item in items:
        for variant in item.variants:
            row, host_source = observations[variant.variant_id]
            for field in Variant.__dataclass_fields__:
                value = getattr(variant, field)
                origin = NAME if value not in (None, "", (), False) else "contract:default"
                if field == "variant_id":
                    origin = "adapter:resource_identity"
                elif field == "host_policy":
                    origin = host_source
                elif field == "browser_fetchable" or field == "tracks" and value is Tracks.UNKNOWN:
                    origin = "contract:unknown"
                elif field == "request_method" and value == "GET" and not row.get("request_method") and not row.get("method"):
                    origin = "contract:default"
                elif field == "container":
                    origin = _container(row, variant.url)[1]
                sources[f"items.{item.item_id}.variants.{variant.variant_id}.{field}"] = origin
    work = Work(source_url=urldefrag(source_url)[0], work_id=work_id, items=tuple(items),
                title=title, description=_TOPIC.sub("", presentation["description"]).strip(),
                topics=tuple(presentation["topics"]), author=author, cover_url=cover,
                duration_seconds=_number(data.get("duration")), field_sources=MappingProxyType(sources))
    violations = validate_work(work)
    return Failure(FailureKind.INVALID_RESULT, "TIANTIAN_" + violations[0].upper()) if violations else work


def failure_for_exception(error):
    from ..common import XhsProviderTimeoutError
    from ..platforms.tiantianjiexi_generic import TianTianJieXiUnsupportedError
    from ..provider_reliability import ProviderResultInvalidError
    if isinstance(error, ProviderResultInvalidError):
        return Failure(FailureKind.INVALID_RESULT, "TIANTIAN_INVALID_RESULT")
    if isinstance(error, TianTianJieXiUnsupportedError):
        return Failure(FailureKind.UNSUPPORTED, "TIANTIAN_UNSUPPORTED_URL")
    if isinstance(error, XhsProviderTimeoutError):
        return Failure(FailureKind.TIMEOUT, "TIANTIAN_TIMEOUT")
    diagnostic = getattr(error, "diagnostic_detail", "")
    status = re.fullmatch(r"SERVICE_HTTP_([1-5]\d{2})", diagnostic)
    if status:
        return failure_for_http(int(status[1]))
    if diagnostic == "SERVICE_INVALID_JSON":
        return Failure(FailureKind.INVALID_RESULT, "TIANTIAN_INVALID_JSON")
    code = re.fullmatch(r"SERVICE_BUSINESS_CODE_(-?\d+)", diagnostic)
    if code:
        return failure_for_code(code[1], _BUSINESS_FAILURES)
    return Failure(FailureKind.UNAVAILABLE, "TIANTIAN_UNAVAILABLE")


class TianTianAdapter(Adapter[dict]):
    name = NAME
    platforms = ("*",)

    def __init__(self, *, session=None):
        self.session = session

    def fetch(self, source_url, budget):
        from ..common import XhsDownloadError, XhsProviderTimeoutError
        from ..provider_reliability import ProviderResultInvalidError
        from ..platforms.tiantianjiexi_generic import (
            TianTianJieXiApiClient, TianTianJieXiGenericProvider,
        )
        if not math.isfinite(budget) or budget <= 0:
            return Failure(FailureKind.TIMEOUT, "ADAPTER_BUDGET_EXHAUSTED")
        observed = []

        def capture(payload):
            observed[:] = [deepcopy(payload)]

        client = TianTianJieXiApiClient(session=self.session, observe=capture)
        provider = TianTianJieXiGenericProvider(client=client)
        try:
            # Preserve existing serial admission, cache, rate limit and budget.
            # The return value is intentionally discarded: it is normalized.
            provider.parse(source_url, timeout_seconds=budget,
                           deadline_monotonic=time.monotonic() + budget)
        except (XhsDownloadError, XhsProviderTimeoutError, ProviderResultInvalidError, RuntimeError, OSError) as error:
            if (getattr(error, "diagnostic_detail", "") == "SERVICE_RESPONSE_REJECTED"
                    and observed and not isinstance(observed[-1], dict)):
                return Failure(FailureKind.INVALID_RESULT, "TIANTIAN_INVALID_RAW")
            return failure_for_exception(error)
        return observed[0] if observed else Failure(FailureKind.INVALID_RESULT, "TIANTIAN_NO_RAW")

    def map(self, raw, source_url):
        try:
            return _map(raw, source_url)
        except (TypeError, ValueError, KeyError, OverflowError):
            return Failure(FailureKind.INVALID_RESULT, "TIANTIAN_INVALID_RAW")
