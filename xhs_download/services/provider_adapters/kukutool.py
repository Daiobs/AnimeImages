"""KuKuTool decrypted business JSON and unconverted sidecar envelope mapping.

The generic helper already returns raw JSON. Sidecar capture before the legacy
conversion is P12; this module never consumes that conversion or fetches it.
"""
from copy import deepcopy
import math
import re
import subprocess
from types import MappingProxyType
from urllib.parse import urldefrag, urlsplit

from ..pipeline import Failure, FailureKind, Fetchable, Item, Kind, Tracks, Variant, Work, height_from_label, validate_work
from ..pipeline.model import PLACEHOLDER_LABELS, PLACEHOLDER_TITLES, SEGMENTED_PATH_SUFFIXES
from ..work_caption import _TOPIC, work_caption_presentation
from .base import Adapter
from .failures import failure_for_code, failure_for_http
from .service_facts import (
    _browser_fetchable, _expiry, _first, _host_policy, _media_url, _number, _required_headers,
    _size, _source_id, _text, _tracks, _url,
)


NAME = "kukutool_generic"
SIDECAR = "douyin-source-resolver.v1"
# No recorded business code establishes not_found. Unknown codes stay unavailable.
_BUSINESS_FAILURES = {}
_EXTENSIONS = {"mp4", "webm", "mov", "mkv", "flv", "m4v", "m4a", "mp3", "aac", "ogg", "opus", "flac", "wav", "jpg", "jpeg", "png", "webp", "gif", "avif", "heic"}


def _is_raw(raw):
    return (type(raw) is dict and not any(str(key).startswith("_service_")
            or key in {"_sidecar", "_retained_video_fullinfo"} for key in raw))


def _failure(raw):
    if "contract_version" in raw:
        if raw["contract_version"] != SIDECAR:
            return Failure(FailureKind.INVALID_RESULT, "KUKUTOOL_SIDECAR_VERSION")
        if raw.get("status") != "ok":
            return failure_for_code(raw.get("reason") or raw.get("status"), _BUSINESS_FAILURES)
        return None
    for key in ("status", "code", "success"):
        if key not in raw:
            continue
        value = raw[key]
        if value is True or type(value) is not bool and str(value).lower() in {"0", "200", "ok", "success", "true"}:
            continue
        return failure_for_code(value, _BUSINESS_FAILURES)
    return None


def _identity(raw, source_url):
    ids = {str(raw[key]).strip() for key in ("aweme_id", "note_id", "bvid", "work_id", "id")
           if isinstance(raw.get(key), (str, int)) and not isinstance(raw.get(key), bool) and str(raw[key]).strip()}
    source_id = _source_id(source_url)
    if len(ids) > 1 or ids and source_id and source_id not in ids:
        raise ValueError("IDENTITY_MISMATCH")
    return (next(iter(ids)), NAME) if ids else (source_id, "request:work_id")


def _row(value):
    if isinstance(value, str):
        return {"url": value}
    if not isinstance(value, dict):
        raise ValueError("INVALID_MEDIA")
    return value


def _list(raw, keys):
    value = _first(raw, keys) or []
    if isinstance(value, (str, dict)):
        value = [value]
    if not isinstance(value, list):
        raise ValueError("INVALID_MEDIA_LIST")
    return [_row(row) for row in value]


def _array_resources(raw, keys):
    # Array positions are content items, even when their URLs happen to match.
    return [row for key in keys for row in _list(raw, (key,))]


def _resource_url(row):
    return _url(row.get("final_url")) or _media_url(row)


def _label(row):
    label = _text(_first(row, ("type", "quality_label", "qualityLabel", "quality", "resolution", "clarity")))
    return "" if label.casefold() in PLACEHOLDER_LABELS | {"image", "audio", "photo", "livephoto"} else label


def _container(row, url):
    explicit = _text(_first(row, ("container", "ext", "extension"))).lower().lstrip(".")
    if explicit in _EXTENSIONS:
        return explicit, NAME
    suffix = urlsplit(url).path.rpartition(".")[2].lower()
    if suffix in _EXTENSIONS:
        return suffix, "adapter:url_extension"
    return "", "contract:default"


def _variant(row, aliases, context, source_url, variant_id):
    url = _resource_url(row)
    if not url:
        raise ValueError("INVALID_MEDIA_URL")
    label = _label(row)
    size, exact = _size(row)
    if size is None:
        size = _number(row.get("verified_size"), integer=True)
        exact = size is not None
    container, container_source = _container(row, url)
    policy, policy_source = _host_policy(source_url, url)
    methods = {_text(a.get("request_method") or a.get("method")).upper() for a in aliases} - {"", "GET"}
    if len(methods) > 1:
        raise ValueError("CONFLICTING_METHODS")
    tracks = {_tracks(alias) for alias in aliases} - {Tracks.UNKNOWN}
    if len(tracks) > 1:
        raise ValueError("CONFLICTING_TRACKS")
    scoped = tuple(a for a in context if not _resource_url(a) or _resource_url(a) == url)
    variant = Variant(
        variant_id, url, container=container, tracks=next(iter(tracks), Tracks.UNKNOWN),
        height=_number(row.get("height"), integer=True) or height_from_label(label),
        width=_number(row.get("width"), integer=True), fps=_number(row.get("fps")),
        bitrate=_number(_first(row, ("bitrate", "bit_rate", "bitRate")), integer=True),
        quality_label=label, is_original=row.get("is_original") is True or label.casefold() in {"原画", "original", "source"},
        size_bytes=size, size_exact=exact,
        required_headers=_required_headers((*scoped, *aliases)),
        browser_fetchable=_browser_fetchable((*scoped, *aliases)),
        drm=any(a.get("drm") is True or a.get("has_drm") is True for a in aliases),
        segmented=any(a.get("segmented") is True or bool(a.get("fragments"))
                      or any(t in _text(a.get("protocol")).lower() for t in ("hls", "dash", "m3u8")) for a in aliases)
                  or urlsplit(url).path.lower().endswith(SEGMENTED_PATH_SUFFIXES),
        request_method=next(iter(methods), "GET"), expires_at=_expiry(row, url), host_policy=policy,
    )
    return variant, container_source, policy_source


def _video_rows(video):
    rows = _list(video, ("video_fullinfo",))
    main_url = _resource_url(video)
    if not rows:
        return [video]
    if main_url and not any(_resource_url(row) == main_url for row in rows):
        return [video, *rows]
    return [{**video, **{key: value for key, value in row.items() if value is not None}}
            if _resource_url(row) == main_url else row for row in rows]


def _map(raw, source_url):
    if not _is_raw(raw) or not _url(source_url):
        return Failure(FailureKind.INVALID_RESULT, "KUKUTOOL_INVALID_RAW")
    failure = _failure(raw)
    if failure:
        return failure
    data = raw.get("data") if isinstance(raw.get("data"), dict) else raw
    if not _is_raw(data):
        return Failure(FailureKind.INVALID_RESULT, "KUKUTOOL_INVALID_RAW")
    work_id, identity_source = _identity(data, source_url)
    if not work_id:
        return Failure(FailureKind.INVALID_RESULT, "KUKUTOOL_MISSING_WORK_ID")
    items, sources = [], {"source_url": "request", "work_id": identity_source, "items": NAME}
    context = (raw, data) if raw is not data else (data,)

    def add(kind, rows, parents=()):
        index = len(items) + 1
        item_id = f"{kind.value}:{index}"
        variants = []
        for ordinal, row in enumerate(rows, 1):
            aliases = tuple(a for a in (*parents, *rows) if _resource_url(a) == _resource_url(row))
            variant, container_source, policy_source = _variant(row, aliases, (*context, *parents), source_url, f"{item_id}:{ordinal}")
            variants.append(variant)
            for field in Variant.__dataclass_fields__:
                value = getattr(variant, field)
                origin = NAME if value not in (None, "", (), False) else "contract:default"
                if field == "variant_id":
                    origin = "adapter:resource_identity"
                elif field == "host_policy":
                    origin = policy_source
                elif field == "container":
                    origin = container_source
                elif (field == "browser_fetchable" and value is Fetchable.UNKNOWN
                      or field == "tracks" and value is Tracks.UNKNOWN):
                    origin = "contract:unknown"
                elif field == "request_method" and value == "GET" and not any(a.get("method") or a.get("request_method") for a in aliases):
                    origin = "contract:default"
                sources[f"items.{item_id}.variants.{variant.variant_id}.{field}"] = origin
        items.append(Item(item_id, kind, index, tuple(variants)))

    if data.get("contract_version") == SIDECAR:
        rows = _list(data, ("resources",))
        best = data.get("best")
        parents = ()
        if best is not None:
            best = _row(best)
            parents = (best,)
            matches = [i for i, row in enumerate(rows) if _resource_url(row) == _resource_url(best) and _label(row) == _label(best)]
            if matches:
                for i in matches:
                    rows[i] = {**best, **{key: value for key, value in rows[i].items() if value is not None}}
            else:
                rows.insert(0, best)
        add(Kind.VIDEO, rows, parents)
    else:
        videos = _list(data, ("videos",))
        if data.get("video_fullinfo"):
            videos = [data, *videos]
        images = _array_resources(data, ("pics", "images", "image_urls", "imageUrls"))
        audios = _array_resources(data, ("audios", "audio_url"))
        main_url = _resource_url(data)
        # The root is an alias only when the actual URL occurs in that video.
        # An unmatched URL has no evidence of being another video's variant.
        main_attached = False
        for video in videos:
            rows = _video_rows(video)
            parents = (video,)
            if main_url and any(_resource_url(row) == main_url for row in rows):
                parents = (*parents, data)
                rows = [{**data, **{key: value for key, value in row.items() if value is not None}}
                        if _resource_url(row) == main_url else row for row in rows]
                main_attached = True
            add(Kind.VIDEO, rows, parents)
        if main_url and not main_attached and not any(_resource_url(row) == main_url for row in (*images, *audios)):
            kind = {"image": Kind.IMAGE, "photo": Kind.IMAGE, "audio": Kind.AUDIO}.get(_text(data.get("type")).lower(), Kind.VIDEO)
            add(kind, [data])
        for kind, rows in ((Kind.IMAGE, images), (Kind.AUDIO, audios)):
            for row in rows:
                parents = ()
                if main_url and _resource_url(row) == main_url:
                    parents = (data,)
                    row = {**data, **{key: value for key, value in row.items() if value is not None}}
                add(kind, [row], parents)
    presentation = work_caption_presentation(_text(data.get("title")), _text(_first(data, ("description", "desc"))))
    title = presentation["title"]
    if title.casefold() in PLACEHOLDER_TITLES:
        title = ""
    author = _text(_first(data, ("author", "nickname")))
    cover = _url(_first(data, ("cover", "cover_url", "thumbnail")))
    duration = _number(_first(data, ("duration_seconds", "duration")))
    for field, value in (("title", title), ("description", presentation["description"]), ("topics", presentation["topics"]),
                         ("author", author), ("cover", cover), ("duration_seconds", duration),
                         ("quality", any(v.height or v.quality_label for i in items for v in i.variants)),
                         ("size", any(v.size_bytes for i in items for v in i.variants))):
        if value:
            sources[field] = NAME
    work = Work(source_url=urldefrag(source_url)[0], work_id=work_id, title=title,
                description=_TOPIC.sub("", presentation["description"]).strip(), topics=tuple(presentation["topics"]),
                author=author, cover_url=cover, duration_seconds=duration, items=tuple(items),
                field_sources=MappingProxyType(sources))
    errors = validate_work(work)
    return Failure(FailureKind.INVALID_RESULT, "KUKUTOOL_" + errors[0].upper()) if errors else work


def failure_for_exception(error):
    from ..common import XhsProviderTimeoutError
    if isinstance(error, (XhsProviderTimeoutError, TimeoutError, subprocess.TimeoutExpired)):
        return Failure(FailureKind.TIMEOUT, "KUKUTOOL_TIMEOUT")
    detail = getattr(error, "diagnostic_detail", "")
    match = re.fullmatch(r"SERVICE_HTTP_([1-5]\d{2})", detail)
    if match:
        return failure_for_http(int(match[1]))
    malformed = re.fullmatch(r"SERVICE_JSON_(?:AUTH|PARSE|UNKNOWN)_HTTP_([1-5]\d{2}|UNKNOWN)_TYPE_(?:JSON|HTML|TEXT|OTHER|MISSING)", detail)
    if malformed:
        http_failure = failure_for_http(int(malformed[1])) if malformed[1] != "UNKNOWN" else None
        return http_failure or Failure(FailureKind.INVALID_RESULT, "KUKUTOOL_INVALID_JSON")
    if detail in {"SERVICE_INVALID_JSON", "SERVICE_RESPONSE_TOO_LARGE"} or isinstance(error, (ValueError, TypeError)):
        return Failure(FailureKind.INVALID_RESULT, "KUKUTOOL_INVALID_RAW")
    return Failure(FailureKind.UNAVAILABLE, "KUKUTOOL_UNAVAILABLE")


class KukutoolAdapter(Adapter[dict]):
    name = NAME
    platforms = ("*",)

    def __init__(self, *, runner=None):
        self.runner = runner

    def fetch(self, source_url, budget):
        from ..common import XhsDownloadError, XhsProviderTimeoutError
        from ..platforms.kukutool_generic import KukutoolGenericProvider
        if not math.isfinite(budget) or budget <= 0:
            return Failure(FailureKind.TIMEOUT, "ADAPTER_BUDGET_EXHAUSTED")
        try:
            # This cache holds helper raw JSON, unlike TianTian's normalized cache.
            raw = KukutoolGenericProvider(runner=self.runner).parse(source_url, timeout_seconds=budget)
        except (XhsDownloadError, XhsProviderTimeoutError, OSError, RuntimeError, ValueError, TypeError, subprocess.TimeoutExpired) as error:
            return failure_for_exception(error)
        if not _is_raw(raw):
            return Failure(FailureKind.INVALID_RESULT, "KUKUTOOL_NO_RAW")
        return deepcopy(raw)

    def map(self, raw, source_url):
        try:
            return _map(raw, source_url)
        except (TypeError, ValueError, KeyError, OverflowError):
            return Failure(FailureKind.INVALID_RESULT, "KUKUTOOL_INVALID_RAW")
