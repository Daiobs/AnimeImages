"""Pure mapping of complete gallery-dl extractor messages, before compaction."""
from dataclasses import replace
import math
import re
from types import MappingProxyType
from urllib.parse import urldefrag, urlsplit

from ..pipeline import (
    Failure, FailureKind, Fetchable, Item, Kind, Tracks, Variant, Work,
    height_from_label, validate_work,
)
from ..pipeline.model import PLACEHOLDER_LABELS, PLACEHOLDER_TITLES, SEGMENTED_PATH_SUFFIXES
from ..request_context import _DEFAULT_MEDIA_HEADERS, inherit_request_context
from ..work_caption import _TOPIC, work_caption_presentation
from .failures import failure_for_http
from .base import Adapter
from .service_facts import EMPTY_HOST_POLICY_SOURCE


NAME = "gallery_dl_generic"
_IMAGES = {"jpg", "jpeg", "png", "webp", "gif", "avif", "bmp", "tiff", "tif"}
_AUDIO = {"mp3", "m4a", "aac", "ogg", "oga", "opus", "wav", "flac"}
_VIDEO = {"mp4", "webm", "mov", "mkv", "m4v", "flv", "avi", "ts"}
_HEADER = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z")


def _text(value):
    return value.strip() if isinstance(value, str) else ""


def _identity(value):
    return str(value).strip() if isinstance(value, (str, int)) and not isinstance(value, bool) else ""


def _number(value, *, integer=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value <= 0:
        return None
    return (int(value) or None) if integer else value


def _url(value):
    value = _text(value)
    try:
        parsed = urlsplit(value)
        if (parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or any(char in value for char in "\r\n\t")):
            return ""
        parsed.port
    except ValueError:
        return ""
    return value


def _work_id(row):
    album = row.get("album")
    if isinstance(album, dict) and _identity(album.get("id")):
        return _identity(album["id"])
    for key in ("post_id", "tweet_id", "status_id", "pageid", "post_shortcode", "id", "shortcode", "slug"):
        if _identity(row.get(key)):
            return _identity(row[key])
    return ""


def _error(row):
    status = row.get("status") or row.get("status_code")
    if isinstance(status, int) and not isinstance(status, bool):
        failure = failure_for_http(status)
        if failure:
            return failure
    name = _text(row.get("error"))
    if not name:
        raise ValueError("INVALID_ERROR")
    if name == "HttpError":
        message = _text(row.get("message"))
        match = (re.match(r"'([1-5]\d{2}) [^'\r\n]+' for '", message)
                 or re.match(r"HTTP(?: Error)? ([1-5]\d{2})\b", message, re.I))
        if match:
            return failure_for_http(int(match[1])) or Failure(FailureKind.INVALID_RESULT, "GALLERYDL_INVALID_RESPONSE")
    kind = {
        "NotFoundError": FailureKind.NOT_FOUND,
        "NoExtractorError": FailureKind.UNSUPPORTED,
        "UnsupportedError": FailureKind.UNSUPPORTED,
        "TimeoutError": FailureKind.TIMEOUT,
        "Timeout": FailureKind.TIMEOUT,
        "RequestException": FailureKind.UNAVAILABLE,
        "HttpError": FailureKind.UNAVAILABLE,
        "AuthorizationError": FailureKind.UNAVAILABLE,
        "AuthenticationError": FailureKind.UNAVAILABLE,
        "JSONDecodeError": FailureKind.INVALID_RESULT,
        "ValueError": FailureKind.INVALID_RESULT,
    }.get(name, FailureKind.UNAVAILABLE)
    return Failure(kind, "GALLERYDL_" + kind.value.upper())


def _media_url(value, row):
    # A native explicit manifest is already a media address, unlike a ytdl:
    # webpage that still needs another extractor and must not be expanded here.
    if (_text(value).startswith("ytdl:")
            and row.get("_ytdl_manifest") in ("hls", "dash")):
        return _url(value[5:])
    return _url(value)


def _messages(raw):
    if not isinstance(raw, (list, tuple)) or not raw:
        raise ValueError("INVALID_MESSAGES")
    directories, files, current = [], [], {}
    for message in raw:
        if not isinstance(message, (list, tuple)) or not message or isinstance(message[0], bool):
            raise ValueError("INVALID_MESSAGE")
        code = message[0]
        if code == -1 and len(message) == 2 and isinstance(message[1], dict):
            return _error(message[1])
        if code == 2 and len(message) in (2, 3) and isinstance(message[-1], dict):
            current = message[-1]
            directories.append(current)
        elif code == 3 and len(message) == 3 and isinstance(message[2], dict) and _media_url(message[1], message[2]):
            files.append((_media_url(message[1], message[2]), message[2], current))
        else:
            # Queue (6) is an unexpanded extractor URL, never a downloadable file.
            raise ValueError("INVALID_OR_UNEXPANDED_MESSAGE")
    if not files:
        raise ValueError("NO_MEDIA")
    return directories, files


def _headers(row):
    names = row.get("required_headers") or ()
    if not isinstance(names, (tuple, list)) or any(not isinstance(name, str) or not _HEADER.fullmatch(name) for name in names):
        raise ValueError("INVALID_REQUIRED_HEADERS")
    required = {name.lower() for name in names}
    native = row.get("_http_headers") or {}
    if not isinstance(native, dict):
        raise ValueError("INVALID_HEADERS")
    for name, value in native.items():
        if not isinstance(name, str) or not _HEADER.fullmatch(name):
            raise ValueError("INVALID_HEADER_NAME")
        if value is not None:
            required.add(name.lower())
    # Only this Url message owns its request instructions. Directory metadata
    # and a different file's headers must not leak into this resource.
    for name, value in inherit_request_context(row)["headers"].items():
        if value not in (None, "") and name.lower() not in _DEFAULT_MEDIA_HEADERS:
            required.add(name.lower())
    return tuple(sorted(required))


def _tracks(row):
    stated = _text(row.get("tracks"))
    if stated in {track.value for track in Tracks}:
        return Tracks(stated)
    video, audio = _text(row.get("vcodec")).lower(), _text(row.get("acodec")).lower()
    if video and audio and "unknown" not in (video, audio):
        if video != "none" and audio != "none":
            return Tracks.AV
        if video != "none" and audio == "none":
            return Tracks.VIDEO_ONLY
        if video == "none" and audio != "none":
            return Tracks.AUDIO_ONLY
    return Tracks.UNKNOWN


def _variant(url, row, ordinal, headers, request_method):
    mime = _text(row.get("mime") or row.get("mimetype") or row.get("mime_type") or row.get("content_type")).lower().split(";", 1)[0]
    container = _text(row.get("extension") or row.get("ext")).lower().lstrip(".")
    if not container:
        container = urlsplit(url).path.rpartition(".")[2].lower()
    if not re.fullmatch(r"[a-z0-9]{1,12}", container):
        container = ""
    if not container:
        container = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp",
                     "video/mp4": "mp4", "video/webm": "webm", "audio/mpeg": "mp3",
                     "audio/mp4": "m4a", "audio/flac": "flac", "audio/ogg": "ogg"}.get(mime, "")
    label = _text(row.get("quality_label") or row.get("quality") or row.get("format_note"))
    if label.casefold() in PLACEHOLDER_LABELS:
        label = ""
    size = _number(row.get("filesize"), integer=True) or _number(row.get("size"), integer=True)
    approximate = _number(row.get("filesize_approx"), integer=True)
    segmented = (row.get("segmented") is True or bool(row.get("fragments"))
                 or row.get("_ytdl_manifest") in ("hls", "dash")
                 or any(value in _text(row.get("protocol")).lower() for value in ("m3u8", "dash", "f4m", "ism"))
                 or urlsplit(url).path.lower().endswith(SEGMENTED_PATH_SUFFIXES))
    tracks = _tracks(row)
    if container in _IMAGES or mime.startswith("image/"):
        kind = Kind.IMAGE
    elif container in _AUDIO or mime.startswith("audio/") or tracks is Tracks.AUDIO_ONLY:
        kind = Kind.AUDIO
    elif container in _VIDEO or mime.startswith("video/") or segmented or tracks in (Tracks.AV, Tracks.VIDEO_ONLY):
        kind = Kind.VIDEO
    else:
        raise ValueError("UNKNOWN_MEDIA_KIND")
    return kind, Variant(
        variant_id=f"file:{ordinal}", url=url, container=container, tracks=tracks,
        height=_number(row.get("height"), integer=True) or height_from_label(label),
        width=_number(row.get("width"), integer=True), fps=_number(row.get("fps")),
        bitrate=_number(row.get("bitrate"), integer=True), quality_label=label,
        is_original=row.get("is_original") is True, size_bytes=size or approximate,
        size_exact=size is not None and row.get("size_exact") is not False,
        required_headers=headers, browser_fetchable=Fetchable.UNKNOWN,
        drm=row.get("drm") is True or row.get("has_drm") is True, segmented=segmented,
        request_method=request_method,
        host_policy=(),
    )


def _file_facts(url, row):
    file = row.get("file")
    if isinstance(file, dict) and file.get("url") == url:
        return {**row, **file}
    return row


def _item_identity(row, work_id, ordinal):
    file = row.get("file")
    if isinstance(file, dict) and _identity(file.get("id")):
        return "file:" + _identity(file["id"])
    for key in ("media_id", "file_id", "num", "image_id", "video_id"):
        if _identity(row.get(key)):
            return key + ":" + _identity(row[key])
    if _identity(row.get("id")) and _identity(row["id"]) != work_id:
        return "id:" + _identity(row["id"])
    return f"ordinal:{ordinal}"


def _person(row):
    for name in ("author", "uploader", "owner_displayname", "owner_username", "user", "owner"):
        value = row.get(name)
        if isinstance(value, dict):
            value = value.get("name") or value.get("display_name") or value.get("username")
        if _text(value):
            return _text(value)
    return ""


def _cover(row):
    for name in ("cover", "thumbnail", "poster", "image"):
        if _url(row.get(name)):
            return row[name]
    return ""


def _source_matches(source_url, work_id):
    parsed = urlsplit(source_url)
    if parsed.hostname in ("itaku.ee", "www.itaku.ee"):
        match = re.fullmatch(r"/(?:images|posts)/(\d+)/?", parsed.path)
        return not match or match[1] == work_id
    return True


def _map(raw, source_url):
    if not _url(source_url):
        raise ValueError("INVALID_SOURCE")
    parsed = _messages(raw)
    if isinstance(parsed, Failure):
        return parsed
    directories, files = parsed
    identities = {_work_id(row) for row in directories if _work_id(row)}
    if not identities:
        identities = {_work_id(row) for _, row, _ in files if _work_id(row)}
    if len(identities) != 1:
        raise ValueError("MISSING_OR_AMBIGUOUS_WORK_ID")
    work_id = next(iter(identities))
    if not _source_matches(source_url, work_id):
        raise ValueError("WORK_IDENTITY_MISMATCH")
    metadata = next((row for row in directories if _work_id(row) == work_id), files[0][1])
    album = metadata.get("album")
    if isinstance(album, dict) and _identity(album.get("id")) == work_id:
        metadata = album
    groups, facts = {}, {}
    for ordinal, (url, row, directory) in enumerate(files, 1):
        file_album = row.get("album")
        if isinstance(file_album, dict) and _identity(file_album.get("id")) not in ("", work_id):
            raise ValueError("WORK_IDENTITY_MISMATCH")
        for key in ("post_id", "tweet_id", "status_id", "post_shortcode"):
            if row.get(key) and directory.get(key) and _identity(row[key]) != _identity(directory[key]):
                raise ValueError("WORK_IDENTITY_MISMATCH")
        method = _text(row.get("_http_method", "GET")).upper()
        kind, variant = _variant(url, _file_facts(url, row), ordinal, _headers(row), method)
        # A native num can describe both a video and its preview. They are
        # separate content items; only versions of the same kind can group.
        item_id = f"{_item_identity(row, work_id, ordinal)}:{kind.value}"
        groups.setdefault(item_id, (kind, []))[1].append(variant)
        facts[variant.variant_id] = row
    # Multiple labels may describe the same file. A less detailed alias must
    # not bypass an observed restriction on that exact item/resource.
    for kind, variants in groups.values():
        aliases = {}
        for variant in variants:
            aliases.setdefault((variant.url, variant.request_method), []).append(variant)
        variants[:] = [replace(v,
            drm=any(alias.drm for alias in aliases[v.url, v.request_method]),
            segmented=any(alias.segmented for alias in aliases[v.url, v.request_method]),
            required_headers=tuple(sorted({header for alias in aliases[v.url, v.request_method] for header in alias.required_headers})),
        ) for v in variants]
    items = tuple(Item(identity, kind, index, tuple(variants))
                  for index, (identity, (kind, variants)) in enumerate(groups.items(), 1))
    text = work_caption_presentation(metadata.get("title"), metadata.get("description"))
    title = text["title"] if _text(metadata.get("title")) else ""
    if title.casefold() in PLACEHOLDER_TITLES:
        title = ""
    tags = metadata.get("tags") or []
    topics = list(text["topics"])
    if isinstance(tags, (list, tuple)):
        topics.extend(_text(tag).lstrip("#") for tag in tags if _text(tag))
    sources = {"source_url": "request", "work_id": NAME, "items": NAME}
    for name, present in (
        ("title", title), ("description", text["description"]), ("topics", topics),
        ("author", _person(metadata)), ("cover", _cover(metadata)),
        ("duration_seconds", _number(metadata.get("duration"))),
        ("quality", any(v.height or v.quality_label for i in items for v in i.variants)),
        ("size", any(v.size_bytes for i in items for v in i.variants)),
    ):
        if present:
            sources[name] = NAME
    for item in items:
        for variant in item.variants:
            for field in Variant.__dataclass_fields__:
                value = getattr(variant, field)
                source = NAME if value not in (None, "", (), False) else "contract:default"
                if field == "variant_id":
                    source = "adapter:resource_identity"
                elif field == "host_policy":
                    source = EMPTY_HOST_POLICY_SOURCE
                elif field == "browser_fetchable" or field == "tracks" and value is Tracks.UNKNOWN:
                    source = "contract:unknown"
                elif field == "drm" and any(key in facts[variant.variant_id] for key in ("drm", "has_drm")):
                    source = NAME
                elif field == "request_method":
                    source = NAME if "_http_method" in facts[variant.variant_id] else "contract:default"
                sources[f"items.{item.item_id}.variants.{variant.variant_id}.{field}"] = source
    work = Work(
        source_url=urldefrag(source_url)[0], work_id=work_id, items=items, title=title,
        description=_TOPIC.sub("", text["description"]).strip(), topics=tuple(dict.fromkeys(topics)),
        author=_person(metadata), cover_url=_cover(metadata),
        duration_seconds=_number(metadata.get("duration")), field_sources=MappingProxyType(sources),
    )
    violations = validate_work(work)
    return Failure(FailureKind.INVALID_RESULT, "GALLERYDL_" + violations[0].upper()) if violations else work


class GalleryDlMapper:
    name = NAME
    platforms = ("*",)

    def map(self, raw, source_url):
        try:
            from .worker_raw import decode_raw
            return _map(decode_raw(raw, many=True), source_url)
        except (TypeError, ValueError, KeyError, OverflowError):
            return Failure(FailureKind.INVALID_RESULT, "GALLERYDL_INVALID_RAW")


class GalleryDlAdapter(GalleryDlMapper, Adapter):
    def __init__(self, *, match=None, runner=None):
        self.match = dict(match) if match is not None else None
        self.runner = runner

    def fetch(self, source_url, budget):
        from ..upstream_matching import parse_upstream_url
        from .worker_raw import captured_fetch
        if self.match is None:
            from ..platforms.gallerydl_generic import GalleryDlGenericProvider
            return captured_fetch(self.name, source_url, budget,
                lambda: GalleryDlGenericProvider(runner=self.runner).parse(source_url, timeout_seconds=budget),
                prefix="GALLERYDL", many=True)
        if self.match.get("provider_id") != self.name:
            return Failure(FailureKind.INVALID_RESULT, "GALLERYDL_INVALID_MATCH")
        return captured_fetch(self.name, source_url, budget,
            lambda: parse_upstream_url(self.match, source_url, timeout_seconds=budget),
            prefix="GALLERYDL", many=True)
