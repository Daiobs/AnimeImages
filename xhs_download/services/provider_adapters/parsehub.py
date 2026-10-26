"""Pure ParseHub mapping and fetch through the existing bounded bridges."""
from dataclasses import replace
import ipaddress
from pathlib import PurePosixPath
import re
from types import MappingProxyType
from urllib.parse import parse_qs, urldefrag, urljoin, urlsplit

from bs4 import BeautifulSoup
from markdown import markdown

from ..pipeline import Failure, FailureKind, Fetchable, Group, Item, Kind, Tracks, Variant, Work, height_from_label, validate_work
from ..pipeline.model import PLACEHOLDER_LABELS, PLACEHOLDER_TITLES, SEGMENTED_PATH_SUFFIXES
from ..work_caption import _TOPIC, work_caption_presentation
from .failures import failure_for_http
from .base import Adapter
from .worker_raw import captured_fetch, decode_raw
from .service_facts import _browser_fetchable, _expiry, _first, _host_policy, _number, _required_headers, _size, _source_id, _text, _tracks, _url
from .ytdlp import YtDlpAdapter


NAME = "parsehub_generic"
XHS_NAME = "parsehub_xhs_images"
MAX_BYTES = 2 * 1024 * 1024  # tools.upstream_parsehub_worker.MAX_BYTES
_IMAGES = {"jpg", "jpeg", "png", "gif", "webp", "avif", "bmp", "tif", "tiff"}
_VIDEOS = {"mp4", "webm", "mov", "m4v", "mkv"}
_RICHTEXT_RESULTS = {"RichTextParseResult", "CoolapkRichTextParseResult", "DoubanRichTextParseResult"}
_RESULTS = _RICHTEXT_RESULTS | {"ParseResult", "VideoParseResult", "ImageParseResult", "MultimediaParseResult",
            "YtVideoParseResult", "YtbVideoParseResult", "BiliYtVideoParseResult",
            "BiliVideoParseResult", "DouyinVideoParseResult", "DouyinImageParseResult",
            "CoolapkImageParseResult", "CoolapkMultimediaParseResult", "TikTokVideoParseResult"}


def _object(value):
    # Recorded object snapshots retain class and all available raw fields. Never
    # call to_dict/download or import an optional upstream runtime into Django.
    if isinstance(value, dict) and isinstance(value.get("_class"), str) and isinstance(value.get("fields"), dict):
        return value["_class"], value["fields"]
    if type(value).__module__.startswith("parsehub.") and hasattr(value, "__dict__"):
        return type(value).__name__, vars(value)
    raise ValueError("NOT_RAW_OBJECT")


def _identity(source):
    identity = _source_id(source)
    if identity:
        return identity
    parsed = urlsplit(source)
    host = parsed.hostname or ""
    patterns = (
        (("mp.weixin.qq.com",), r"/s/([^/]+)/?"),
        (("coolapk.com",), r"/(?:feed|picture)/(\d+)/?"),
        (("douban.com",), r"/(?:group/)?topic/(\d+)/?"),
        (("x.com", "twitter.com"), r"/[^/]+/status/(\d+)/?"),
        (("tieba.baidu.com",), r"/p/(\d+)/?"),
        (("threads.net", "threads.com"), r"/@[^/]+/post/([^/]+)/?"),
        (("facebook.com",), r"/(?:reel|watch)/(\d+)/?"),
        (("zhihu.com",), r"/question/\d+/answer/(\d+)/?"),
        (("pipix.com",), r"/ppx/item/(\d+)/?"),
        (("snapchat.com",), r"/@[^/]+/spotlight/([^/]+)/?"),
    )
    for hosts, pattern in patterns:
        if any(host == suffix or host.endswith("." + suffix) for suffix in hosts):
            match = re.fullmatch(pattern, parsed.path)
            if match:
                return match[1]
    query = parse_qs(parsed.query)
    if host == "mp.weixin.qq.com" and parsed.path == "/s":
        if all(query.get(key) for key in ("__biz", "mid", "idx")):
            return ":".join(query[key][0] for key in ("__biz", "mid", "idx"))
    return ""


def _container(row, url):
    if row.get("ext") == "" and not row.get("container"):
        return ""
    ext = _text(row.get("ext") or row.get("container")).lower().lstrip(".")
    if ext:
        return ext if re.fullmatch(r"[a-z0-9]{1,12}", ext) else ""
    ext = urlsplit(url).path.rsplit(".", 1)[-1].lower()
    return ext if ext in _IMAGES | _VIDEOS | {"mp3", "m4a", "aac", "flac", "opus", "ogg"} else ""


def _resolved_identity(fields, source, info=None):
    supplied = []
    for record in (fields, info or {}):
        for key in ("id", "work_id"):
            value = record.get(key)
            if isinstance(value, (str, int)) and not isinstance(value, bool) and str(value).strip():
                supplied.append(str(value).strip())
    canonical = _url(fields.get("raw_url"))
    if fields.get("raw_url") and not canonical:
        raise ValueError("INVALID_SOURCE")
    source_id, canonical_id = _identity(source), _identity(canonical) if canonical else ""
    identities = set(supplied) | {value for value in (source_id, canonical_id) if value}
    if len(identities) > 1:
        raise ValueError("IDENTITY_MISMATCH")
    if supplied:
        return supplied[0], NAME
    if canonical_id:
        return canonical_id, NAME + ":raw_url"
    return source_id, "request:work_id"


def _body_media_url(url):
    # Same lexical/public-address boundary as the Worker; no DNS or fetching.
    if (not _url(url) or len(url) > 8192 or "\\" in url
            or any(ord(char) <= 32 or ord(char) == 127 for char in url)):
        raise ValueError("INVALID_BODY_MEDIA")
    parsed = urlsplit(url)
    host = parsed.hostname.lower().rstrip(".")
    if (parsed.scheme not in {"http", "https"} or parsed.port not in {None, 80, 443}
            or parsed.username is not None or parsed.password is not None
            or host == "localhost" or host.endswith((".localhost", ".local", ".internal"))):
        raise ValueError("INVALID_BODY_MEDIA")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return url
    if (not address.is_global or address.is_multicast or getattr(address, "ipv4_mapped", None)
            or getattr(address, "sixtofour", None) or getattr(address, "teredo", None)):
        raise ValueError("INVALID_BODY_MEDIA")
    return url


def _richtext_media(fields, media, canonical):
    body = fields.get("markdown_content") or ""
    if not isinstance(body, str):
        raise ValueError("INVALID_MARKDOWN")
    if len(body.encode()) > MAX_BYTES:
        raise ValueError("OUTPUT_LIMIT")
    soup = BeautifulSoup(markdown(body), "html.parser")
    for node in soup(["script", "style", "template"]):
        node.decompose()
    typed = {_object(item)[1].get("url", ""): item for item in media}
    ordered, seen = [], set()
    for node in soup.find_all("img", src=True):
        reference = str(node["src"]).strip()
        if not reference or reference.startswith("#") or reference.lower().startswith("data:image/"):
            continue
        url = _body_media_url(urljoin(canonical, reference))
        if url in seen:
            continue
        seen.add(url)
        ext = PurePosixPath(urlsplit(url).path).suffix.lower().lstrip(".")
        ordered.append(typed.get(url) or {"_class": "ImageRef", "fields": {
            "url": url, "ext": ext if ext in _IMAGES else "",
        }})
    ordered.extend(item for item in media if _object(item)[1].get("url", "") not in seen)
    return ordered


def _variant(row, url, source, variant_id, observations, aliases):
    if not _url(url):
        raise ValueError("INVALID_MEDIA_URL")
    label = _text(_first(row, ("quality_label", "qualityLabel", "quality", "format_note")))
    if label.casefold() in PLACEHOLDER_LABELS:
        label = ""
    size, exact = _size(row)
    policy, policy_source = _host_policy(source, url)
    # A sibling observation cannot supply another observation's missing context.
    browser_fetchable = (Fetchable.FALSE if any(_browser_fetchable(observation) is Fetchable.FALSE
                                               for observation in observations) else Fetchable.UNKNOWN)
    facts = tuple(fact for observation in observations for fact in observation)
    methods = {_text(a.get("request_method") or a.get("method")).upper() for a in facts} - {"", "GET"}
    tracks = {_tracks(a) for a in aliases} - {Tracks.UNKNOWN}
    if len(methods) > 1 or len(tracks) > 1:
        raise ValueError("CONFLICTING_MEDIA_FACTS")
    expirations = [expiry for alias in aliases if (expiry := _expiry(alias, url)) is not None]
    variant = Variant(variant_id, url, container=_container(row, url), tracks=next(iter(tracks), Tracks.UNKNOWN),
        height=_number(row.get("height"), integer=True) or height_from_label(label),
        width=_number(row.get("width"), integer=True), fps=_number(row.get("fps")),
        bitrate=_number(row.get("bitrate"), integer=True), quality_label=label,
        is_original=row.get("is_original") is True, size_bytes=size, size_exact=exact,
        required_headers=_required_headers(facts), browser_fetchable=browser_fetchable,
        drm=any(a.get("drm") is True or a.get("has_drm") is True for a in facts),
        segmented=any(a.get("segmented") is True or bool(a.get("fragments"))
            or any(p in _text(a.get("protocol")).lower() for p in ("m3u8", "dash", "hls")) for a in facts)
            or urlsplit(url).path.lower().endswith(SEGMENTED_PATH_SUFFIXES),
        request_method=next(iter(methods), "GET"), expires_at=min(expirations, default=None), host_policy=policy)
    return variant, policy_source


def _scoped_context(context, url):
    return tuple(record for record in context if not (
        address := _url(record.get("url") or record.get("urlDefault") or record.get("masterUrl"))) or address == url)


def _finish(source, identity, fields, rows, groups, name, identity_source, extra_aliases=(), inferred_containers=()):
    if not identity:
        raise ValueError("MISSING_WORK_ID")
    items, sources = [], {"source_url": "request", "work_id": identity_source, "items": name}
    by_url = {}
    for _, alternatives, context in rows:
        for row, url in alternatives:
            by_url.setdefault(url, []).append((row, context))
    for row, url, context in extra_aliases:
        by_url.setdefault(url, []).append((row, context))
    for index, (kind, alternatives, context) in enumerate(rows, 1):
        item_id = f"{kind.value}:{index}"
        variants = []
        for ordinal, (row, url) in enumerate(alternatives, 1):
            aliases = tuple(other for other, _ in by_url[url])
            observations = tuple((*_scoped_context(parents, url), other) for other, parents in by_url[url])
            facts = tuple(fact for observation in observations for fact in observation)
            variant, policy_source = _variant(row, url, source, f"{item_id}:{ordinal}", observations, aliases)
            variants.append(variant)
            for field in Variant.__dataclass_fields__:
                value = getattr(variant, field)
                origin = name if value not in (None, "", (), False) else "contract:default"
                if field == "variant_id":
                    origin = "adapter:resource_identity"
                elif field == "host_policy":
                    origin = policy_source
                elif field == "browser_fetchable" and value is Fetchable.UNKNOWN:
                    origin = "contract:unknown"
                elif field == "tracks" and value is Tracks.UNKNOWN:
                    origin = name if any(a.get("tracks") == "unknown" for a in aliases) else "contract:unknown"
                elif field == "container" and value and (id(row) in inferred_containers or not (row.get("ext") or row.get("container"))):
                    origin = "adapter:url_extension"
                elif field == "expires_at" and value:
                    origin = name if any(_number(a.get("expires_at")) and _expiry(a, url) == value
                                         for a in aliases) else "adapter:url_deadline"
                elif field == "request_method" and not any(a.get("request_method") or a.get("method") for a in facts):
                    origin = "contract:default"
                elif field in {"drm", "segmented"} and value is False:
                    keys = ("drm", "has_drm") if field == "drm" else ("segmented",)
                    if any(type(a.get(key)) is bool for a in facts for key in keys):
                        origin = name
                elif field == "is_original" and type(row.get(field)) is bool:
                    origin = name
                elif field == "size_exact" and variant.size_bytes is not None:
                    origin = name if type(row.get(field)) is bool else "adapter:size_representation"
                elif field == "required_headers" and any(a.get(field) == [] or a.get(field) == () for a in facts):
                    origin = name
                sources[f"items.{item_id}.variants.{variant.variant_id}.{field}"] = origin
        items.append(Item(item_id, kind, index, tuple(variants)))
    presentation = work_caption_presentation(fields.get("title"), fields.get("content") or fields.get("desc"))
    title = presentation["title"] if _text(fields.get("title")) else ""
    if title.casefold() in PLACEHOLDER_TITLES:
        title = ""
    author = fields.get("author") or fields.get("user") or ""
    if isinstance(author, dict):
        author = author.get("nickname") or author.get("name") or ""
    cover = _url(fields.get("cover_url") or fields.get("cover"))
    mapped_groups = tuple(Group("live_photo", (items[a].item_id, items[b].item_id)) for a, b in groups)
    work = Work(urldefrag(source)[0], identity, tuple(items), title=title,
        description=_TOPIC.sub("", presentation["description"]).strip(), topics=tuple(presentation["topics"]),
        author=_text(author), cover_url=cover, groups=mapped_groups,
        duration_seconds=_number(fields.get("duration")))
    for field in ("title", "description", "topics", "author", "cover_url", "duration_seconds", "groups"):
        sources[field] = name if getattr(work, field) else "contract:default"
    if cover:
        sources["cover"] = name
    if any(v.height or v.quality_label for item in items for v in item.variants):
        sources["quality"] = name
    if any(v.size_bytes for item in items for v in item.variants):
        sources["size"] = name
    work = replace(work, field_sources=MappingProxyType(sources))
    violations = validate_work(work)
    return Failure(FailureKind.INVALID_RESULT, "PARSEHUB_" + violations[0].upper()) if violations else work


def _map_result(raw, source):
    if not _url(source):
        raise ValueError("INVALID_SOURCE")
    cls, fields = _object(raw)
    if cls not in _RESULTS:
        raise ValueError("NOT_RAW_RESULT")
    dl = fields.get("dl")
    if dl is not None:
        dl_fields = dl if isinstance(dl, dict) and "info_json" in dl else _object(dl)[1]
        info = dl_fields.get("info_json")
        if not isinstance(info, dict):
            raise ValueError("INVALID_INFO_JSON")
        _resolved_identity(fields, source, info)
        mapped = YtDlpAdapter().map(info, source)
        if isinstance(mapped, Failure):
            return mapped
        sources = {key: value if value.startswith(("request", "contract:", "adapter:", "existing:"))
                   else NAME + ":dl.info_json:" + value for key, value in mapped.field_sources.items()}
        sources["backend"] = NAME + ":dl.info_json:yt-dlp"
        return replace(mapped, field_sources=MappingProxyType(sources))
    identity, identity_source = _resolved_identity(fields, source)
    canonical = _url(fields.get("raw_url")) or source
    media = fields.get("media") or []
    if not isinstance(media, (list, tuple)):
        media = [media]
    rows, groups = [], []
    metadata = dict(fields)
    # Keep alias facts before body-order deduplication can select one typed row.
    aliases = [(_object(item)[1], _object(item)[1].get("url"), (fields,)) for item in media]
    typed_rows = {id(row) for row, _, _ in aliases}
    inferred_containers = set()
    if cls in _RICHTEXT_RESULTS:
        media = _richtext_media(fields, media, canonical)
        inferred_containers = {id(_object(item)[1]) for item in media} - typed_rows
    for value in media:
        kind_name, row = _object(value)
        url = _url(row.get("url"))
        ext = _container(row, url)
        if kind_name in {"ImageRef", "LivePhotoRef"}:
            kind = Kind.IMAGE
        elif kind_name == "VideoRef":
            kind = Kind.VIDEO
        elif kind_name == "AniRef":
            kind = Kind.VIDEO if ext in _VIDEOS else Kind.IMAGE
        else:
            raise ValueError("UNKNOWN_MEDIA_KIND")
        rows.append((kind, [(row, url)], (fields,)))
        if not metadata.get("cover_url") and _url(row.get("thumb_url")):
            metadata["cover_url"] = row["thumb_url"]
        if kind_name == "LivePhotoRef" and row.get("video_url"):
            still = len(rows) - 1
            motion = {"ext": row.get("video_ext"), "duration": row.get("duration")}
            rows.append((Kind.VIDEO, [(motion, row["video_url"])], (fields,)))
            # XHS pairing must come from its original imageList item, not the
            # already converted upstream MediaRef or positional parallel lists.
            if not (urlsplit(source).hostname or "").endswith(("xiaohongshu.com", "xhslink.com", "xhslink.cn")):
                groups.append((still, len(rows) - 1))
    return _finish(source, identity, metadata, rows, groups, NAME, identity_source, aliases, inferred_containers)


def _map_xhs(raw, source):
    if not _url(source) or not isinstance(raw, dict):
        raise ValueError("INVALID_SOURCE_OR_STATE")
    detail = (raw.get("note") or {}).get("noteDetailMap")
    if not isinstance(detail, dict):
        raise ValueError("NOT_RAW_STATE")
    identity = _source_id(source)
    if not identity or identity not in detail:
        raise ValueError("IDENTITY_MISMATCH")
    note = detail[identity].get("note")
    if not isinstance(note, dict) or str(note.get("noteId") or identity) != identity:
        raise ValueError("IDENTITY_MISMATCH")
    images = note.get("imageList")
    if not isinstance(images, list):
        raise ValueError("INVALID_IMAGE_LIST")
    rows, groups, aliases = [], [], []
    for image in images:
        if not isinstance(image, dict):
            raise ValueError("INVALID_IMAGE")
        if image.get("urlDefault"):
            aliases.append((image, image["urlDefault"], (note,)))
        stills = []
        for info in image.get("infoList") or []:
            if not isinstance(info, dict):
                raise ValueError("INVALID_IMAGE_INFO")
            if info.get("url"):
                dimensions = {key: image[key] for key in ("width", "height") if key in image}
                stills.append(({**dimensions, **info}, info["url"]))
        if image.get("urlDefault") and image["urlDefault"] not in [url for _, url in stills]:
            stills.insert(0, (image, image["urlDefault"]))
        if not stills:
            raise ValueError("MISSING_STILL")
        still_index = len(rows)
        rows.append((Kind.IMAGE, stills, (note, image)))
        streams = image.get("stream") or {}
        if not isinstance(streams, dict):
            raise ValueError("INVALID_STREAMS")
        motion = []
        for variants in streams.values():
            if not isinstance(variants, list):
                raise ValueError("INVALID_STREAMS")
            for variant in variants:
                if not isinstance(variant, dict):
                    raise ValueError("INVALID_STREAM")
                urls = ([variant["masterUrl"]] if variant.get("masterUrl") else []) + (variant.get("backupUrls") or [])
                for url in dict.fromkeys(urls):
                    motion.append((variant, url))
        if motion:
            rows.append((Kind.VIDEO, motion, (note,)))
            if image.get("livePhoto") is True:
                groups.append((still_index, len(rows) - 1))
    identity_source = XHS_NAME if note.get("noteId") else XHS_NAME + ":noteDetailMap"
    return _finish(source, identity, note, rows, groups, XHS_NAME, identity_source, aliases)


class ParseHubMapper:
    name = NAME
    platforms = ("*",)

    def map(self, raw, source_url):
        try:
            return _map_result(decode_raw(raw), source_url)
        except (TypeError, ValueError, KeyError, AttributeError, OverflowError) as error:
            code = str(error) if type(error) is ValueError and re.fullmatch(r"[A-Z_]+", str(error)) else "INVALID_RAW"
            return Failure(FailureKind.INVALID_RESULT, "PARSEHUB_" + code)


class ParseHubXhsMapper:
    name = XHS_NAME
    platforms = ("xiaohongshu",)

    def map(self, raw, source_url):
        try:
            return _map_xhs(decode_raw(raw), source_url)
        except (TypeError, ValueError, KeyError, AttributeError, OverflowError) as error:
            code = str(error) if type(error) is ValueError and re.fullmatch(r"[A-Z_]+", str(error)) else "INVALID_RAW"
            return Failure(FailureKind.INVALID_RESULT, "PARSEHUB_" + code)


class ParseHubAdapter(ParseHubMapper, Adapter):
    def __init__(self, *, match):
        self.match = dict(match)

    def fetch(self, source_url, budget):
        from ..upstream_parsehub import parse_upstream_url
        if self.match.get("provider_id") != self.name:
            return Failure(FailureKind.INVALID_RESULT, "PARSEHUB_INVALID_MATCH")
        return captured_fetch(self.name, source_url, budget,
            lambda: parse_upstream_url(self.match, source_url, timeout_seconds=budget), prefix="PARSEHUB")


class ParseHubXhsAdapter(ParseHubXhsMapper, Adapter):
    def __init__(self, *, note_id=None):
        self.note_id = note_id

    def fetch(self, source_url, budget):
        import time
        from ..platforms.xhs_parsehub_images import load_parsehub_worker_payload
        note_id = self.note_id or _source_id(source_url)
        if not isinstance(note_id, str) or not re.fullmatch(r"[0-9a-f]{24}", note_id):
            return Failure(FailureKind.INVALID_RESULT, "PARSEHUB_XHS_INVALID_SOURCE")
        return captured_fetch(self.name, source_url, budget,
            lambda: load_parsehub_worker_payload(source_url=source_url, note_id=note_id,
                                                deadline_monotonic=time.monotonic() + budget),
            prefix="PARSEHUB_XHS")


def failure_for_exception(error):
    if isinstance(error, TimeoutError) or type(error).__name__ in {"ReadTimeout", "ConnectTimeout", "PoolTimeout"}:
        return Failure(FailureKind.TIMEOUT, "PARSEHUB_TIMEOUT")
    response = getattr(error, "response", None)
    status = getattr(response, "status_code", None)
    if status is not None:
        return failure_for_http(status) or Failure(FailureKind.INVALID_RESULT, "PARSEHUB_INVALID_RESPONSE")
    if type(error).__module__.startswith("parsehub.") and type(error).__name__ == "UnknownPlatform":
        return Failure(FailureKind.UNSUPPORTED, "PARSEHUB_UNSUPPORTED")
    return Failure(FailureKind.UNAVAILABLE, "PARSEHUB_UNAVAILABLE")
