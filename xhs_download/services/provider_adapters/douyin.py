"""Native Douyin fetching and pure aweme JSON mapping, without URL upgrades."""
from dataclasses import dataclass, replace
import json
import math
import re
import time
from types import MappingProxyType
from urllib.parse import parse_qs, urldefrag, urlsplit

from ..pipeline import Failure, FailureKind, Fetchable, Item, Kind, Tracks, Variant, Work, height_from_label, validate_work
from ..pipeline.model import PLACEHOLDER_LABELS, PLACEHOLDER_TITLES, SEGMENTED_PATH_SUFFIXES
from ..work_caption import _TOPIC, work_caption_presentation
from .failures import failure_for_code, failure_for_http
from .base import Adapter
from .service_facts import _browser_fetchable, _expiry, _host_matches, _host_policy, _number, _required_headers, _text, _tracks, _url


NAME = "douyin_native"
_ADDR_KEYS = ("play_addr", "download_addr", "play_addr_h264", "play_addr_265", "play_addr_bytevc1")
_AUDIO_EXTENSIONS = {"mp3", "m4a", "aac", "wav", "ogg", "opus", "flac"}
_EXTENSIONS = _AUDIO_EXTENSIONS | {"mp4", "webm", "mov", "jpg", "jpeg", "png", "webp", "heic", "avif", "gif"}
_MIMES = {"video/mp4": "mp4", "video_mp4": "mp4", "audio/mp4": "m4a", "audio_mp4": "m4a",
          "audio/mpeg": "mp3", "image/jpeg": "jpg", "image/webp": "webp", "image/png": "png"}


def _identity(value):
    if type(value) not in (str, int) or not re.fullmatch(r"[1-9][0-9]*", str(value)):
        raise ValueError("INVALID_IDENTITY")
    return str(value)


def _source_identity(source):
    if not _url(source):
        raise ValueError("INVALID_SOURCE")
    parsed = urlsplit(source)
    if not any(_host_matches(parsed.hostname, host) for host in ("douyin.com", "iesdouyin.com", "amemv.com")):
        raise ValueError("UNSUPPORTED_SOURCE")
    match = re.fullmatch(r"/(?:share/)?(?:video|note)/([1-9][0-9]*)/?", parsed.path)
    claims = [match[1]] if match else []
    for value in parse_qs(parsed.query, keep_blank_values=True).get("modal_id", []):
        claims.append(_identity(value))
    if not claims:
        raise ValueError("MISSING_SOURCE_IDENTITY")
    if len(set(claims)) != 1:
        raise ValueError("IDENTITY_MISMATCH")
    return claims[0]


def _aweme(raw, identity):
    if type(raw) is not dict or any(str(key).startswith(("_service_", "_sidecar", "_retained")) for key in raw):
        raise ValueError("NOT_RAW_AWEME")
    if "status_code" in raw:
        if type(raw["status_code"]) is not int:
            raise ValueError("INVALID_STATUS")
        if raw["status_code"]:
            return failure_for_code(raw["status_code"], {})
    forms = [key for key in ("aweme_id", "aweme_detail", "aweme_list", "item_list") if key in raw]
    if len(forms) != 1:
        raise ValueError("INVALID_ENVELOPE")
    if forms[0] == "aweme_id":
        candidates = [raw]
    elif forms[0] == "aweme_detail":
        candidates = [raw["aweme_detail"]]
    else:
        candidates = raw[forms[0]]
    if type(candidates) is not list or any(type(item) is not dict for item in candidates):
        raise ValueError("INVALID_AWEME_LIST")
    selected = [item for item in candidates if _identity(item.get("aweme_id")) == identity]
    if len(selected) != 1:
        raise ValueError("IDENTITY_MISMATCH")
    if any(str(key).startswith(("_service_", "_sidecar", "_retained")) for key in selected[0]):
        raise ValueError("NOT_RAW_AWEME")
    return selected[0]


def _media_url(value):
    value = _text(value)
    result = _url("https:" + value if value.startswith("//") else value)
    if not result or any(char.isspace() for char in result) or "\\" in result:
        raise ValueError("INVALID_MEDIA_URL")
    return result


def _urls(address):
    if type(address) is not dict or type(address.get("url_list")) is not list:
        raise ValueError("INVALID_ADDRESS")
    return tuple(dict.fromkeys(_media_url(url) for url in address["url_list"]))


def _audio_url(url):
    parsed = urlsplit(url)
    mime = parse_qs(parsed.query).get("mime_type", [""])[0].lower()
    path = parsed.path.lower()
    return (mime == "audio" or mime.startswith(("audio/", "audio_"))
            or "/play/music/" in path or "/obj/ies-music" in path
            or path.rpartition(".")[2] in _AUDIO_EXTENSIONS)


@dataclass(frozen=True)
class _Resource:
    url: str
    kind: Kind
    origin: str
    metadata: tuple
    context: tuple
    audio_origin: str = ""


def _mapping(parent, key):
    value = parent.get(key)
    if value is None:
        return {}
    if type(value) is not dict:
        raise ValueError("INVALID_MEDIA_OBJECT")
    return value


def _image_address(value, path):
    if isinstance(value, str):
        return (_media_url(value),), (), path
    if type(value) is list:
        return tuple(dict.fromkeys(_media_url(url) for url in value)), (), path
    if type(value) is dict:
        return _urls(value), ((value, path),), path + ".url_list"
    raise ValueError("INVALID_IMAGE_ADDRESS")


def _resources(aweme):
    base = ((aweme, NAME + ":aweme"),)
    video, music = _mapping(aweme, "video"), _mapping(aweme, "music")
    videos, audios, items = [], [], []
    for list_key in ("images", "image_infos", "image_list", "imageList"):
        images = aweme.get(list_key, [])
        if images is None:
            continue
        if type(images) is not list:
            raise ValueError("INVALID_IMAGES")
        for index, row in enumerate(images):
            origin = NAME + f":aweme.{list_key}[{index}]"
            if isinstance(row, str):
                addresses, parent = [(row, origin)], ()
            elif type(row) is dict:
                addresses = [(row[key], origin + "." + key) for key in (
                    "url_list", "origin_url", "large_image", "display_image", "image", "image_url", "download_url_list")
                    if row.get(key) is not None]
                parent = ((row, origin),)
            else:
                raise ValueError("INVALID_IMAGE")
            resources = []
            for value, path in addresses:
                urls, address_facts, url_origin = _image_address(value, path)
                metadata = (*address_facts, *parent) if path == origin + ".url_list" else address_facts
                resources.extend(_Resource(url, Kind.IMAGE, url_origin, metadata,
                                           (*base, *parent, *address_facts)) for url in urls)
            if not resources:
                raise ValueError("IMAGE_WITHOUT_URL")
            items.append(resources)
    rates = video.get("bit_rate", [])
    if rates is None:
        rates = []
    if type(rates) is not list or any(type(rate) is not dict for rate in rates):
        raise ValueError("INVALID_BIT_RATE")
    video_origin = NAME + ":aweme.video"
    rows = [(rate, video_origin + f".bit_rate[{index}]") for index, rate in enumerate(rates)]
    rows.append((video, video_origin))
    for row, path in rows:
        for key in _ADDR_KEYS:
            if row.get(key) is None:
                continue
            address = _mapping(row, key)
            addr_path = path + "." + key
            # Root dimensions describe play_addr, not every encoded rendition.
            metadata = ((address, addr_path), (row, path)) if row is not video or key == "play_addr" else ((address, addr_path),)
            context = (*base, (video, video_origin), (row, path), (address, addr_path))
            for url in _urls(address):
                scoped = _context_for_url(context, url)
                explicit_audio = any(_tracks(fact) is Tracks.AUDIO_ONLY for fact, _ in scoped)
                audio_origin = next((origin + ":tracks" for fact, origin in scoped if _tracks(fact) is Tracks.AUDIO_ONLY), "")
                if _audio_url(url):
                    audio_origin = "adapter:native_audio_url"
                kind = Kind.AUDIO if explicit_audio or audio_origin else Kind.VIDEO
                resource = _Resource(url, kind, addr_path + ".url_list", metadata, context, audio_origin)
                (audios if kind is Kind.AUDIO else videos).append(resource)
    if videos:
        items.append(videos)
    if audios:
        items.append(audios)
    if music.get("play_url") is not None:
        address = _mapping(music, "play_url")
        path = NAME + ":aweme.music.play_url"
        resources = [_Resource(url, Kind.AUDIO, path + ".url_list", ((address, path),),
                               (*base, (music, NAME + ":aweme.music"), (address, path)), path)
                     for url in _urls(address)]
        if resources:
            items.append(resources)
    if not items:
        raise ValueError("NO_MEDIA")
    return items, video, music


def _field(metadata, keys, convert):
    for row, origin in metadata:
        for key in keys:
            value = convert(row.get(key))
            if value is not None and value != "":
                return value, origin + "." + key
    return None, "contract:default"


def _context_for_url(context, url):
    return tuple((row, path) for row, path in context
                 if (not row.get("url") or _media_url(row["url"]) == url)
                 and ("url_list" not in row or url in _urls(row)))


def _variant(resource, aliases, source, variant_id):
    # Same URL can be advertised as several formats. Only access restrictions
    # cross alias boundaries; scalar format facts retain their own observation.
    metadata = _context_for_url(resource.metadata, resource.url)
    observations = [_context_for_url(alias.context, resource.url) for alias in aliases]
    facts = tuple(fact for observation in observations for fact in observation)
    rows = tuple(row for row, _ in facts)
    tracks = {_tracks(row) for row in rows} - {Tracks.UNKNOWN} if resource.kind is not Kind.IMAGE else set()
    if resource.kind is not Kind.IMAGE and any(alias.audio_origin for alias in aliases):
        tracks.add(Tracks.AUDIO_ONLY)
    if len(tracks) > 1:
        raise ValueError("CONFLICTING_TRACKS")
    methods = {_text(row.get("request_method") or row.get("method")).upper() for row in rows} - {"", "GET"}
    if len(methods) > 1:
        raise ValueError("CONFLICTING_METHODS")
    values, sources = {}, {}
    for field, keys in (("height", ("height",)), ("width", ("width",)), ("fps", ("FPS", "fps")),
                        ("bitrate", ("bit_rate", "bitrate"))):
        values[field], sources[field] = _field(metadata, keys, lambda value: _number(value, integer=field != "fps"))
        if resource.kind is Kind.IMAGE and field in {"height", "width"} and values[field] is None:
            observations_for_dimension = [_field(alias.metadata, keys, lambda value: _number(value, integer=True)) for alias in aliases]
            known = [(value, path) for value, path in observations_for_dimension if value is not None]
            if len({value for value, _ in known}) == 1:
                values[field], sources[field] = known[0]
    if resource.kind is Kind.AUDIO:
        for field in ("height", "width", "fps"):
            values[field], sources[field] = None, "contract:default"
    label, label_origin = _field(metadata, ("quality_label", "ratio"), _text)
    if not label:
        label = parse_qs(urlsplit(resource.url).query).get("ratio", [""])[0]
        label_origin = "adapter:url_ratio" if label else "contract:default"
    if not label or label.casefold() in PLACEHOLDER_LABELS:
        label, label_origin = "", "contract:default"
    if resource.kind is not Kind.AUDIO and values["height"] is None and height_from_label(label):
        values["height"], sources["height"] = height_from_label(label), "adapter:height_from_label"
    container, container_origin = _field(metadata, ("ext", "container"),
        lambda value: _text(value).lower().lstrip(".") if _text(value).lower().lstrip(".") in _EXTENSIONS else None)
    if not container:
        container, container_origin = _field(metadata, ("mime_type",), lambda value: _MIMES.get(_text(value).lower()))
    if not container:
        mime = parse_qs(urlsplit(resource.url).query).get("mime_type", [""])[0].lower()
        ext = urlsplit(resource.url).path.rpartition(".")[2].lower()
        container = _MIMES.get(mime) or (ext if ext in _EXTENSIONS else "")
        container_origin = ("adapter:url_mime_type" if mime in _MIMES else "adapter:url_extension") if container else "contract:default"
    size, size_origin = _field(metadata, ("data_size", "size"), lambda value: _number(value, integer=True))
    exact = size is not None
    exact_origin = size_origin if exact else "contract:default"
    for row, path in metadata:
        if type(row.get("size_exact")) is bool and size is not None:
            exact, exact_origin = row["size_exact"], path + ".size_exact"
            break
    policy, policy_origin = _host_policy(source, resource.url)
    expiry = [(value, path + ".expires_at") for row, path in facts if (value := _expiry(row, resource.url)) is not None]
    expires_at, expires_origin = min(expiry, key=lambda pair: pair[0]) if expiry else (None, "contract:default")
    browser = Fetchable.FALSE if any(_browser_fetchable(tuple(row for row, _ in observation)) is Fetchable.FALSE
                                     for observation in observations) else Fetchable.UNKNOWN
    variant = Variant(variant_id, resource.url, container=container, tracks=next(iter(tracks), Tracks.UNKNOWN),
        **values, quality_label=label, size_bytes=size, size_exact=exact,
        is_original=any(row.get("is_original") is True for row, _ in metadata),
        required_headers=_required_headers(rows), browser_fetchable=browser,
        drm=any(row.get(key) is True for row in rows for key in ("drm", "has_drm")),
        segmented=urlsplit(resource.url).path.lower().endswith(SEGMENTED_PATH_SUFFIXES) or any(
            row.get("segmented") is True or bool(row.get("fragments"))
            or any(token in _text(row.get("protocol")).lower() for token in ("m3u8", "hls", "dash")) for row in rows),
        request_method=next(iter(methods), "GET"), expires_at=expires_at, host_policy=policy)
    sources.update(variant_id="adapter:resource_identity", url=resource.origin, container=container_origin,
        tracks="contract:unknown", quality_label=label_origin, size_bytes=size_origin, size_exact=exact_origin,
        merge_with="contract:default", host_policy=policy_origin, expires_at=expires_origin,
        browser_fetchable="contract:unknown", required_headers="contract:default", request_method="contract:default")
    if tracks:
        sources["tracks"] = next((path + ":tracks" for row, path in facts if _tracks(row) is not Tracks.UNKNOWN),
                                 next((alias.audio_origin for alias in aliases if alias.audio_origin), "contract:unknown"))
    # Provenance names are fixed schema paths, never upstream text or header values.
    for field, keys in (("is_original", ("is_original",)), ("drm", ("drm", "has_drm")), ("segmented", ("segmented",))):
        observed = metadata if field == "is_original" else facts
        positive = [path for row, path in observed if any(row.get(key) is True for key in keys)]
        if field == "segmented":
            positive.extend(path for row, path in facts if bool(row.get("fragments"))
                            or any(token in _text(row.get("protocol")).lower() for token in ("m3u8", "hls", "dash")))
        negative = [path for row, path in observed if any(row.get(key) is False for key in keys)]
        sources[field] = (positive[0] if positive else "adapter:url_segmented_path"
                          if field == "segmented" and variant.segmented else negative[0] if negative else "contract:default")
    if variant.required_headers:
        sources["required_headers"] = ";".join(dict.fromkeys(path for row, path in facts if _required_headers((row,))))
    if browser is Fetchable.FALSE:
        sources["browser_fetchable"] = ";".join(dict.fromkeys(path for row, path in facts if _browser_fetchable((row,)) is Fetchable.FALSE))
    methods_sources = [(row, path) for row, path in facts if row.get("request_method") or row.get("method")]
    if methods_sources:
        sources["request_method"] = next((path for row, path in methods_sources
            if _text(row.get("request_method") or row.get("method")).upper() == variant.request_method), "contract:default")
    return variant, sources


def _map(raw, source):
    if type(raw) is dict and "payload" in raw and "page_url" in raw:
        identity = _source_identity(raw["page_url"])
        try:
            requested = _source_identity(source)
        except ValueError as error:
            if str(error) != "MISSING_SOURCE_IDENTITY" or urlsplit(source).hostname != "v.douyin.com":
                raise
        else:
            if requested != identity:
                raise ValueError("IDENTITY_MISMATCH")
        raw = raw["payload"]
    else:
        identity = _source_identity(source)
    aweme = _aweme(raw, identity)
    if isinstance(aweme, Failure):
        return aweme
    resources, video, music = _resources(aweme)
    aliases = {}
    for group in resources:
        for resource in group:
            aliases.setdefault(resource.url, []).append(resource)
    items, sources = [], {"source_url": "request", "work_id": NAME + ":aweme.aweme_id", "items": NAME + ":aweme"}
    for index, group in enumerate(resources, 1):
        kind, variants = group[0].kind, []
        item_id = f"{kind.value}:{index}"
        for resource in group:
            variant, observed = _variant(resource, aliases[resource.url], source, f"{item_id}:{len(variants) + 1}")
            variants.append(variant)
            sources.update({f"items.{item_id}.variants.{variant.variant_id}.{field}": origin for field, origin in observed.items()})
        items.append(Item(item_id, kind, index, tuple(variants)))
        for field in ("item_id", "index", "kind", "variants", "default_variant"):
            sources[f"items.{item_id}.{field}"] = "contract:default_selection" if field == "default_variant" else "adapter:raw_content_order"
    caption = _text(aweme.get("desc"))
    title = _text(aweme.get("item_title"))
    title_origin = NAME + ":aweme.item_title" if title else NAME + ":aweme.desc"
    if not title and not caption:
        title = _text(_mapping(aweme, "share_info").get("share_title"))
        title_origin = NAME + ":aweme.share_info.share_title"
    text = work_caption_presentation(title, caption) if title else work_caption_presentation("", caption)
    title = text["title"]
    if title.casefold() in PLACEHOLDER_TITLES | {"douyin", "抖音"}:
        title = ""
    author = _mapping(aweme, "author")
    cover, cover_origin = "", "contract:default"
    for key in ("cover", "origin_cover", "dynamic_cover", "animated_cover"):
        if video.get(key):
            urls, _, origin = _image_address(video[key], NAME + ":aweme.video." + key)
            if urls:
                cover, cover_origin = urls[0], origin
                break
    if not cover:
        thumbs = video.get("big_thumbs") or []
        if type(thumbs) is not list or any(type(thumb) is not dict for thumb in thumbs):
            raise ValueError("INVALID_THUMBNAILS")
        for index, thumb in enumerate(thumbs):
            key = "img_urls" if thumb.get("img_urls") else "img_url"
            if not thumb.get(key):
                continue
            urls, _, origin = _image_address(thumb[key], NAME + f":aweme.video.big_thumbs[{index}]." + key)
            if urls:
                cover, cover_origin = urls[0], origin
                break
    duration, duration_origin = _field(((video, NAME + ":aweme.video"),
        (_mapping(video, "download_addr"), NAME + ":aweme.video.download_addr")), ("duration",), _number)
    if duration is not None:
        duration /= 1000
        duration_origin += ":milliseconds"
    elif len(items) == 1 and items[0].kind is Kind.AUDIO and all(
            resource.origin == NAME + ":aweme.music.play_url.url_list" for resource in resources[0]):
        duration, duration_origin = _field(((music, NAME + ":aweme.music"),), ("duration",), _number)
    work = Work(urldefrag(source)[0], identity, tuple(items), title=title,
        description=_TOPIC.sub("", text["description"]).strip(), topics=tuple(text["topics"]),
        author=_text(author.get("nickname")), cover_url=cover, duration_seconds=duration)
    for field, origin in (("title", title_origin), ("description", NAME + ":aweme.desc" if caption else title_origin),
                          ("topics", NAME + ":aweme.desc" if caption else title_origin), ("author", NAME + ":aweme.author.nickname")):
        sources[field] = origin if getattr(work, field) else "contract:default"
    sources.update(cover_url=cover_origin, duration_seconds=duration_origin, groups="contract:default",
                   platform="router", provider="router", completeness="contract:default")
    if cover:
        sources["cover"] = cover_origin
    if any(v.height or v.quality_label for item in items for v in item.variants):
        sources["quality"] = NAME + ":aweme"
    if any(v.size_bytes for item in items for v in item.variants):
        sources["size"] = NAME + ":aweme"
    work = replace(work, field_sources=MappingProxyType(sources))
    violations = validate_work(work)
    return Failure(FailureKind.INVALID_RESULT, "DOUYIN_" + violations[0].upper()) if violations else work


class DouyinMapper:
    name = NAME
    platforms = ("douyin",)

    def map(self, raw, source_url):
        try:
            return _map(raw, source_url)
        except (TypeError, ValueError, KeyError, AttributeError, OverflowError, RecursionError) as error:
            code = str(error) if type(error) is ValueError and re.fullmatch(r"[A-Z_]+", str(error)) else "INVALID_RAW"
            kind = FailureKind.UNSUPPORTED if code == "UNSUPPORTED_SOURCE" else FailureKind.INVALID_RESULT
            return Failure(kind, "DOUYIN_" + code)


class DouyinAdapter(DouyinMapper, Adapter[dict]):
    def fetch(self, source_url, budget):
        from ..pipeline_shadow_capture import collect_raw
        from ..platforms.douyin import DouyinAdapter

        if not math.isfinite(budget) or budget <= 0:
            return Failure(FailureKind.TIMEOUT, "ADAPTER_BUDGET_EXHAUSTED")
        failure = None
        with collect_raw() as session:
            try:
                DouyinAdapter().parse_url(source_url, prefer_ultra_hd=False, timeout_seconds=budget,
                                         deadline_monotonic=time.monotonic() + budget,
                                         probe_media_metadata=False)
            except Exception as error:
                failure = failure_for_exception(error)
        records = [record for record in session.records if record.provider == self.name]
        if records:
            record = records[-1]
            return {"payload": record.raw, "page_url": record.source_url}
        return failure or Failure(FailureKind.INVALID_RESULT, "DOUYIN_NO_RAW")

def failure_for_exception(error):
    if isinstance(error, TimeoutError) or type(error).__name__ in {"Timeout", "ReadTimeout", "ConnectTimeout", "XhsProviderTimeoutError"}:
        return Failure(FailureKind.TIMEOUT, "DOUYIN_TIMEOUT")
    status = getattr(error, "http_status", None) or getattr(getattr(error, "response", None), "status_code", None)
    if type(status) is int:
        try:
            return failure_for_http(status) or Failure(FailureKind.INVALID_RESULT, "DOUYIN_INVALID_RESPONSE")
        except ValueError:
            return Failure(FailureKind.INVALID_RESULT, "DOUYIN_INVALID_HTTP_STATUS")
    if isinstance(error, (json.JSONDecodeError, ValueError, TypeError)):
        return Failure(FailureKind.INVALID_RESULT, "DOUYIN_INVALID_RAW")
    return Failure(FailureKind.UNAVAILABLE, "DOUYIN_UNAVAILABLE")
