"""yt-dlp info dict to Work; no legacy result, probe, token or delivery input."""
from dataclasses import replace
from datetime import datetime, timezone
import math
import re
from types import MappingProxyType
from urllib.parse import parse_qs, urldefrag, urlsplit

from ..pipeline import (
    Failure, FailureKind, Fetchable, Item, Kind, Tracks, Variant, Work,
    height_from_label, validate_work,
)
from ..pipeline.model import PLACEHOLDER_LABELS, PLACEHOLDER_TITLES, SEGMENTED_PATH_SUFFIXES
from ..request_context import _DEFAULT_MEDIA_HEADERS, inherit_request_context
from ..work_caption import _TOPIC, work_caption_presentation
from .base import Adapter
from .failures import failure_for_http


NAME = "yt_dlp_generic"
_AUDIO_EXTS = {"m4a", "mp3", "aac", "opus", "ogg", "oga", "flac", "wav"}


def _text(value):
    return value.strip() if isinstance(value, str) else ""


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
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            return ""
        if parsed.username is not None or parsed.password is not None:
            return ""
        parsed.port
    except ValueError:
        return ""
    return value


def _tracks(row):
    def present(codec):
        value = _text(row.get(codec)).lower()
        return None if not value or value == "unknown" else value != "none"
    # yt-dlp synthesizes audio_ext='none' even when acodec is None. Codec
    # absence is not an explicit no-audio fact (the real PeerTube recording).
    video, audio = present("vcodec"), present("acodec")
    if video is True and audio is True:
        return Tracks.AV
    if video is True and audio is False:
        return Tracks.VIDEO_ONLY
    if video is False and audio is True:
        return Tracks.AUDIO_ONLY
    return Tracks.UNKNOWN


def _segmented(row):
    protocol = _text(row.get("protocol")).lower()
    return (any(marker in protocol for marker in ("m3u8", "dash_segments", "f4m", "ism"))
            or bool(row.get("fragments"))
            or urlsplit(row["url"]).path.lower().endswith(SEGMENTED_PATH_SUFFIXES))


def _required_headers(raw, rows):
    # CLI info_dict.update(best_format) makes a root URL's headers resource-local.
    sources = rows if raw.get("formats") and raw.get("url") else (raw, *rows)
    context = inherit_request_context(*sources)
    names = context.get("required_headers") or ()
    if not isinstance(names, (list, tuple)) or any(not isinstance(name, str) for name in names):
        raise ValueError("INVALID_REQUIRED_HEADERS")
    required = {name.lower() for name in names}
    for source in sources:
        required.update(name.lower() for name, value in inherit_request_context(source)["headers"].items()
                        if value not in (None, "") and name.lower() not in _DEFAULT_MEDIA_HEADERS)
    if any(_text(source.get("cookies")) for source in sources):
        required.add("cookie")
    return tuple(sorted(required))


def _expires(url):
    # Google video URLs explicitly encode their signed expiry in Unix seconds.
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    if host != "googlevideo.com" and not host.endswith(".googlevideo.com"):
        return None
    value = parse_qs(parsed.query).get("expire", [""])[0]
    if not re.fullmatch(r"\d{10}", value):
        return None
    try:
        return datetime.fromtimestamp(int(value), timezone.utc)
    except (OverflowError, ValueError, OSError):
        return None


def _variant(raw, row, variant_id, aliases):
    label = _text(row.get("format_note"))
    if label.casefold() in PLACEHOLDER_LABELS or label.casefold() in {"audio only", "storyboard"}:
        label = ""
    size = _number(row.get("filesize"), integer=True)
    rate = _number(row.get("tbr"))
    height = _number(row.get("height"), integer=True) or height_from_label(label)
    container = _text(row.get("ext")).lower()
    if not re.fullmatch(r"[a-z0-9]{1,12}", container) or container == "none":
        container = ""
    return Variant(
        variant_id=variant_id, url=row["url"], container=container, tracks=_tracks(row),
        height=height, width=_number(row.get("width"), integer=True),
        fps=_number(row.get("fps")), bitrate=round(rate * 1000) if rate else None,
        quality_label=label, is_original=row.get("is_original") is True,
        size_bytes=size or _number(row.get("filesize_approx"), integer=True),
        size_exact=size is not None, required_headers=_required_headers(raw, aliases),
        browser_fetchable=Fetchable.UNKNOWN,
        drm=any(source.get("has_drm") is True for source in aliases),
        segmented=any(_segmented(source) for source in aliases), expires_at=_expires(row["url"]),
        host_policy=(urlsplit(row["url"]).hostname,),
    )


def _rows(raw):
    formats = raw.get("formats")
    if formats is not None and not isinstance(formats, list):
        raise ValueError("INVALID_FORMATS")
    if any(not isinstance(row, dict) for row in (formats or [])):
        raise ValueError("INVALID_FORMAT")
    aliases = {}
    for row in (formats or []):
        aliases.setdefault((row.get("format_id"), row.get("url")), []).append(row)
    rows = [(row, list(aliases[(row.get("format_id"), row.get("url"))])) for row in (formats or [])]

    def add(row, *, root=False):
        if not isinstance(row, dict):
            raise ValueError("INVALID_FORMAT")
        key = (row.get("format_id"), row.get("url"))
        matches = [index for index, (existing, _) in enumerate(rows)
                   if (existing.get("format_id"), existing.get("url")) == key]
        if root and not matches:
            matches = [index for index, (existing, _) in enumerate(rows)
                       if existing.get("url") == row.get("url")]
        if not matches:
            rows.append((row, [row]))
        else:
            for index in matches:
                existing, aliases = rows[index]
                # Keep distinct format labels and enrich missing fields. Restrictions
                # are evaluated over every original alias, never overwritten here.
                merged = {**row, **{key: value for key, value in existing.items() if value is not None}}
                rows[index] = (merged, [*aliases, row])
    downloads = raw.get("requested_downloads") or []
    if not isinstance(downloads, list):
        raise ValueError("INVALID_SELECTION")
    for selection in [raw, *downloads]:
        if not isinstance(selection, dict):
            raise ValueError("INVALID_SELECTION")
        selected = selection.get("requested_formats") or []
        if not isinstance(selected, list):
            raise ValueError("INVALID_SELECTION")
        if selection.get("url") and not selected:
            add(selection, root=selection is raw)
        for row in selected:
            add(row)
    return rows


def _item(raw, index):
    variants, by_key = [], {}
    observed = {}
    for ordinal, (row, aliases) in enumerate(_rows(raw), 1):
        if not isinstance(row, dict):
            raise ValueError("INVALID_FORMAT")
        # yt-dlp storyboards are thumbnail grids, not alternate video files.
        if row.get("protocol") == "mhtml" or row.get("ext") == "mhtml":
            continue
        if not _url(row.get("url")):
            raise ValueError("INVALID_FORMAT_URL")
        variant_id = f"{index}:{_text(row.get('format_id')) or 'format'}:{ordinal}"
        variant = _variant(raw, row, variant_id, aliases)
        variants.append(variant)
        observed[variant_id] = aliases
        by_key.setdefault((row.get("format_id"), row["url"]), variant)
    if not variants:
        raise ValueError("NO_MEDIA")

    pairs = {}
    for selection in [raw, *(raw.get("requested_downloads") or [])]:
        chosen = [by_key.get((row.get("format_id"), row.get("url")))
                  for row in (selection.get("requested_formats") or [])]
        audio = [variant for variant in chosen if variant and variant.tracks is Tracks.AUDIO_ONLY]
        video = [variant for variant in chosen if variant and variant.tracks is Tracks.VIDEO_ONLY]
        # Pair only an explicit upstream selection; never pick a new audio track.
        if len(audio) == len(video) == 1:
            pairs.setdefault(video[0].variant_id, audio[0].variant_id)
    variants = tuple(replace(v, merge_with=pairs.get(v.variant_id, "")) for v in variants)
    audio_only = all(v.tracks is Tracks.AUDIO_ONLY for v in variants)
    audio_container = all(v.container in _AUDIO_EXTS and v.tracks is Tracks.UNKNOWN for v in variants)
    kind = Kind.AUDIO if audio_only or audio_container else Kind.VIDEO
    return Item(f"{index}:{raw['id']}", kind, index, variants), observed


def _cover(raw):
    cover = _url(raw.get("thumbnail"))
    if cover:
        return cover
    thumbnails = raw.get("thumbnails") or []
    if isinstance(thumbnails, list):
        for entry in reversed(thumbnails):
            if isinstance(entry, dict) and _url(entry.get("url")):
                return entry["url"]
    return ""


def _source_matches(raw, source_url):
    parsed = urlsplit(source_url)
    host = (parsed.hostname or "").lower()
    identity = ""
    if host == "youtu.be":
        identity = parsed.path.strip("/")
    elif host in {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}:
        identity = parse_qs(parsed.query).get("v", [""])[0]
        if not identity and parsed.path.startswith(("/shorts/", "/embed/", "/live/")):
            identity = parsed.path.split("/")[2]
    return not identity or str(raw.get("id")) == identity


def _map(raw, source_url):
    if (not isinstance(raw, dict) or isinstance(raw.get("id"), bool)
            or not isinstance(raw.get("id"), (str, int)) or not str(raw["id"]).strip()):
        return Failure(FailureKind.INVALID_RESULT, "YTDLP_MISSING_WORK_ID")
    if not _url(source_url):
        return Failure(FailureKind.INVALID_RESULT, "YTDLP_INVALID_SOURCE")
    if not _source_matches(raw, source_url):
        return Failure(FailureKind.INVALID_RESULT, "YTDLP_IDENTITY_MISMATCH")
    if raw.get("_type") in {"url", "url_transparent"}:
        return Failure(FailureKind.INVALID_RESULT, "YTDLP_UNEXPANDED_RESULT")
    entries = raw.get("entries") if raw.get("_type") in {"playlist", "multi_video", "compat_list"} else [raw]
    if not isinstance(entries, list) or not entries:
        return Failure(FailureKind.INVALID_RESULT, "YTDLP_EMPTY_OR_UNEXPANDED_COLLECTION")
    if any(not isinstance(entry, dict) or not entry.get("id") or entry.get("entries") is not None for entry in entries):
        return Failure(FailureKind.INVALID_RESULT, "YTDLP_INVALID_ENTRY")
    mapped = [_item(entry, index) for index, entry in enumerate(entries, 1)]
    items = tuple(item for item, observed in mapped)
    text = work_caption_presentation(raw.get("title"), raw.get("description"))
    title = text["title"] if _text(raw.get("title")) else ""
    if title.casefold() in PLACEHOLDER_TITLES:
        title = ""
    sources = {"source_url": "request", "work_id": NAME, "items": NAME}
    for name, present in (
        ("title", title), ("description", text["description"]), ("topics", text["topics"]),
        ("author", raw.get("uploader") or raw.get("channel") or raw.get("creator")),
        ("cover", _cover(raw)), ("duration_seconds", _number(raw.get("duration"))),
        ("quality", any(v.quality_label or v.height for i in items for v in i.variants)),
        ("size", any(v.size_bytes for i in items for v in i.variants)),
    ):
        if present:
            sources[name] = NAME
    for item, observed in mapped:
        for variant in item.variants:
            for field in Variant.__dataclass_fields__:
                value = getattr(variant, field)
                source = NAME if value not in (None, "", (), False) else "contract:default"
                if field in {"variant_id", "host_policy"}:
                    source = "adapter:resource_identity"
                elif field == "browser_fetchable" or field == "tracks" and value is Tracks.UNKNOWN:
                    source = "contract:unknown"
                elif field == "request_method":
                    # yt-dlp does not state a method; GET is the contract default (D-03).
                    source = "contract:default"
                elif field == "drm" and any("has_drm" in row and row["has_drm"] is not None
                                            for row in observed[variant.variant_id]):
                    source = NAME
                sources[f"items.{item.item_id}.variants.{variant.variant_id}.{field}"] = source
    work = Work(
        source_url=urldefrag(source_url)[0], work_id=str(raw["id"]), items=items,
        title=title, description=_TOPIC.sub("", text["description"]).strip(),
        topics=tuple(text["topics"]), author=_text(raw.get("uploader") or raw.get("channel") or raw.get("creator")),
        cover_url=_cover(raw), duration_seconds=_number(raw.get("duration")),
        field_sources=MappingProxyType(sources),
    )
    violations = validate_work(work)
    return Failure(FailureKind.INVALID_RESULT, "YTDLP_" + violations[0].upper()) if violations else work


def failure_for_exception(error):
    """Expected CLI failures: classify locally, never return upstream text."""
    import json
    import subprocess
    cause = error.__cause__
    if isinstance(cause, subprocess.TimeoutExpired):
        return Failure(FailureKind.TIMEOUT, "YTDLP_TIMEOUT")
    if isinstance(cause, json.JSONDecodeError) or type(error).__name__ == "ExternalCommandOutputTooLarge":
        return Failure(FailureKind.INVALID_RESULT, "YTDLP_INVALID_RESULT")
    message = str(error).lower()
    if isinstance(error, TimeoutError) or "timed out" in message or "解析超时" in message:
        return Failure(FailureKind.TIMEOUT, "YTDLP_TIMEOUT")
    status = re.search(r"http error ([1-5]\d{2})", message)
    if status:
        return failure_for_http(int(status.group(1))) or Failure(FailureKind.INVALID_RESULT, "YTDLP_INVALID_RESPONSE")
    if "unsupported url" in message:
        return Failure(FailureKind.UNSUPPORTED, "YTDLP_UNSUPPORTED_URL")
    if any(marker in message for marker in ("video has been removed", "private video", "does not exist")):
        return Failure(FailureKind.NOT_FOUND, "YTDLP_NOT_FOUND")
    if any(marker in message for marker in ("invalid json", "result format is invalid", "no video formats", "requested format is not available")):
        return Failure(FailureKind.INVALID_RESULT, "YTDLP_INVALID_RESULT")
    return Failure(FailureKind.UNAVAILABLE, "YTDLP_UNAVAILABLE")


class YtDlpAdapter(Adapter[dict]):
    name = NAME
    platforms = ("*",)

    def __init__(self, *, binary=None):
        self.binary = binary

    def fetch(self, source_url, budget):
        # The existing bounded CLI returns its complete JSON, not Worker rows.
        from django.conf import settings
        from ..platforms.ytdlp_generic import run_ytdlp_json
        from ..common import XhsProviderTimeoutError
        try:
            return run_ytdlp_json(source_url, binary=(self.binary if self.binary is not None
                                   else getattr(settings, "YTDLP_BINARY", "")), timeout_seconds=budget)
        except XhsProviderTimeoutError:
            return Failure(FailureKind.TIMEOUT, "YTDLP_TIMEOUT")
        except (RuntimeError, OSError) as error:
            return failure_for_exception(error)

    def map(self, raw, source_url):
        try:
            return _map(raw, source_url)
        except (TypeError, ValueError, KeyError, OverflowError):
            return Failure(FailureKind.INVALID_RESULT, "YTDLP_INVALID_RAW")
