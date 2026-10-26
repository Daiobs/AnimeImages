"""Pure mapping of VideoInfo objects observed before Worker normalization.

The caller supplies the exact executed backend identity, never an auto selector.
No videodl import, worker launch, raw capture, probing or fallback lives here.
"""
import re
from dataclasses import replace
from hashlib import sha256
from types import MappingProxyType
from urllib.parse import urldefrag, urlsplit

from ..pipeline import (
    Failure, FailureKind, Fetchable, Item, Kind, Tracks, Variant, Work,
    height_from_label, validate_work,
)
from ..pipeline.model import PLACEHOLDER_LABELS, PLACEHOLDER_TITLES, SEGMENTED_PATH_SUFFIXES
from ..work_caption import _TOPIC, work_caption_presentation
from .service_facts import (
    _browser_fetchable, _expiry, _first, _host_policy, _number,
    _required_headers, _size, _source_id, _text, _tracks, _url,
)


NAME = "videodl_repo"
_BACKEND = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,79}")
_CONTAINERS = frozenset({
    "mp4", "webm", "mov", "mkv", "flv", "m4v", "ts", "m4a", "mp3",
    "aac", "ogg", "opus", "flac", "wav", "jpg", "jpeg", "png", "webp", "gif",
})
_SEGMENTED_EXTENSIONS = frozenset({suffix[1:] for suffix in SEGMENTED_PATH_SUFFIXES} | {"m3u"})
_CONTEXT_FIELDS = (
    "required_headers", "requires_cookie", "requires_context", "ip_bound", "headers_present",
)


def _snapshot(info):
    # Read stored fields, not VideoInfo validity properties or backend methods.
    if isinstance(info, (dict, str, bytes)):
        raise ValueError("NOT_VIDEOINFO")
    fields = vars(info)
    if not isinstance(fields, dict) or "source" not in fields:
        raise ValueError("NOT_VIDEOINFO")
    extra = fields.get("_extra", {})
    if not isinstance(extra, dict):
        raise ValueError("INVALID_EXTRA")
    return {**extra, **fields}


def _context(info, audio=False):
    suffix = "audio_download" if audio else "download"
    prefix = "audio_" if audio else ""
    context = {key: info[prefix + key] for key in _CONTEXT_FIELDS if prefix + key in info}
    context["headers"] = info.get(f"default_{suffix}_headers")
    if info.get(f"default_{suffix}_cookies"):
        context["requires_cookie"] = True
    return context


def _segmented(info, url):
    return (info.get("segmented") is True or info.get("download_with_ffmpeg") is True
            or info.get("enable_nm3u8dlre") is True
            or _text(info.get("ext")).lower().lstrip(".") in _SEGMENTED_EXTENSIONS
            or urlsplit(url).path.lower().endswith(SEGMENTED_PATH_SUFFIXES))


def _drm(info):
    if any(info.get(key) is True for key in (
        "drm", "has_drm", "download_with_mp4decrypt", "enable_mp4decrypt", "requires_decryption",
    )):
        return True
    for key in ("ffmpeg_settings", "nm3u8dlre_settings"):
        settings = info.get(key)
        if not isinstance(settings, dict):
            continue
        if any(settings.get(name) for name in ("decryption_key", "decryption_keys", "custom_keys", "-decryption_key")):
            return True
        if key == "nm3u8dlre_settings" and settings.get("--key"):
            return True
        if any(_text(settings.get(name)).lower() in {"mp4decrypt", "shaka_packager"}
               for name in ("decryption_engine", "--decryption-engine")):
            return True
    return False


def _container(info, url, audio):
    extension = _text(info.get("audio_ext" if audio else "ext")).lower().lstrip(".")
    if extension in _CONTAINERS:
        return extension, True
    extension = urlsplit(url).path.rpartition(".")[2].lower()
    return (extension, False) if extension in _CONTAINERS else ("", False)


def _variant(info, source_url, index, audio, origin, *, paired=False):
    url = _url(info.get("audio_download_url" if audio else "download_url"))
    if not url:
        raise ValueError("INVALID_MEDIA_URL")
    prefix = "audio_" if audio else ""
    # Audio fields never inherit video dimensions, codecs, sizes or headers.
    facts = ({key[len(prefix):]: value for key, value in info.items() if key.startswith(prefix)}
             if audio else info)
    label = _text(_first(facts, ("quality_label", "quality", "resolution")))
    if label.casefold() in PLACEHOLDER_LABELS:
        label = ""
    container, declared_container = _container(info, url, audio)
    size, exact = _size(facts)
    context = _context(info, audio)
    policy, policy_origin = _host_policy(source_url, url)
    method = _text(facts.get("request_method") or facts.get("method")).upper() or "GET"
    tracks = _tracks(facts)
    merge_with = ""
    if paired or (audio and not _url(info.get("download_url"))):
        required_tracks = Tracks.AUDIO_ONLY if audio else Tracks.VIDEO_ONLY
        if tracks not in (Tracks.UNKNOWN, required_tracks):
            raise ValueError("CONFLICTING_TRACKS")
        tracks = required_tracks
        if not audio and paired:
            merge_with = f"{index + 1}:1"
    variant = Variant(
        f"{index}:1", url, container=container, tracks=tracks, merge_with=merge_with,
        height=_number(facts.get("height"), integer=True) or height_from_label(label),
        width=_number(facts.get("width"), integer=True), fps=_number(facts.get("fps")),
        bitrate=_number(facts.get("bitrate"), integer=True), quality_label=label,
        is_original=facts.get("is_original") is True,
        size_bytes=size, size_exact=exact, required_headers=_required_headers((context,)),
        browser_fetchable=_browser_fetchable((context,)),
        drm=_drm(facts), segmented=_segmented(facts, url),
        request_method=method, expires_at=_expiry(facts, url), host_policy=policy,
    )
    sources = {}
    for field in Variant.__dataclass_fields__:
        value = getattr(variant, field)
        source = origin if value not in (None, "", (), False) else "contract:default"
        if field == "variant_id":
            source = "adapter:resource_identity"
        elif field == "host_policy":
            source = policy_origin
        elif field == "container" and container and not declared_container:
            source = "adapter:url_extension"
        elif field == "tracks" and value is Tracks.UNKNOWN and facts.get("tracks") != "unknown":
            source = "contract:unknown"
        elif field == "browser_fetchable" and value is Fetchable.UNKNOWN:
            source = "contract:unknown"
        elif field == "request_method" and not facts.get("request_method") and not facts.get("method"):
            source = "contract:default"
        elif (field in ("is_original", "drm", "segmented")
              and type(facts.get(field)) is bool and facts[field] is value):
            source = origin
        elif field == "size_exact" and (size is not None or facts.get("size_exact") is False):
            source = origin
        elif field == "required_headers" and isinstance(context.get("required_headers"), (list, tuple)):
            source = origin
        elif (field == "segmented" and value
              and _text(facts.get("ext")).lower().lstrip(".") not in _SEGMENTED_EXTENSIONS
              and not any(facts.get(key) is True for key in (
                  "segmented", "download_with_ffmpeg", "enable_nm3u8dlre"))):
            source = "adapter:url_extension"
        elif field == "expires_at" and value and not _number(facts.get("expires_at")):
            source = "adapter:url_deadline"
        sources[field] = source
    return variant, sources


def _restrict_aliases(items, sources):
    # Preserve content items, but a second observation cannot loosen the same URL.
    aliases = {}
    for item in items:
        variant, = item.variants
        aliases.setdefault(variant.url, []).append((item, variant))
    result = []
    for item in items:
        variant, = item.variants
        entries = aliases[variant.url]
        variants = [other for _, other in entries]
        methods = {other.request_method for other in variants if other.request_method != "GET"}
        tracks = {other.tracks for other in variants if other.tracks is not Tracks.UNKNOWN}
        if len(methods) > 1 or len(tracks) > 1:
            raise ValueError("CONFLICTING_ALIAS_FACTS")
        changes = {
            "required_headers": tuple(sorted({name for other in variants for name in other.required_headers})),
            "browser_fetchable": (Fetchable.FALSE if any(other.browser_fetchable is Fetchable.FALSE for other in variants)
                                  else Fetchable.UNKNOWN),
            "drm": any(other.drm for other in variants),
            "segmented": any(other.segmented for other in variants),
            "request_method": next(iter(methods), "GET"),
            "tracks": next(iter(tracks), Tracks.UNKNOWN),
            "expires_at": min((other.expires_at for other in variants if other.expires_at), default=None),
        }
        prefix = f"items.{item.item_id}.variants.{variant.variant_id}."
        for field, value in changes.items():
            evidence = [sources[f"items.{other_item.item_id}.variants.{other.variant_id}.{field}"]
                        for other_item, other in entries if getattr(other, field) == value
                        or (field == "required_headers" and other.required_headers)]
            if evidence:
                sources[prefix + field] = next((source for source in evidence if not source.startswith("contract:")), evidence[0])
        result.append(replace(item, variants=(replace(variant, **changes),)))
    return result


def _map(raw, source_url, backend_id, backend_category):
    if not _url(source_url) or type(raw) not in (list, tuple) or not raw:
        return Failure(FailureKind.INVALID_RESULT, "VIDEODL_INVALID_RAW")
    infos = [_snapshot(info) for info in raw]
    retained = [(ordinal, info) for ordinal, info in enumerate(infos, 1)
                if any(_url(info.get(field)) for field in ("download_url", "audio_download_url"))]
    if not retained:
        if any(info.get("err_msg") for info in infos):
            return Failure(FailureKind.UNAVAILABLE, "VIDEODL_ITEM_ERROR")
        return Failure(FailureKind.INVALID_RESULT, "VIDEODL_NO_MEDIA")
    origin = f"videodl:{backend_category}:{backend_id}"
    sources = {"source_url": "request", "backend": origin, "items": origin}
    work_id = _source_id(source_url)
    identity_source = "request:work_id"
    if not work_id:
        identifiers = {str(info["identifier"]).strip() for _, info in retained
                       if type(info.get("identifier")) in (str, int) and str(info["identifier"]).strip()}
        if len(identifiers) != 1:
            return Failure(FailureKind.INVALID_RESULT, "VIDEODL_MISSING_WORK_ID")
        work_id = identifiers.pop()
        identity_source = origin
    sources["work_id"] = identity_source
    items = []
    for ordinal, info in retained:
        native_source = _text(info.get("source"))
        if native_source:
            fingerprint = sha256(native_source.encode("utf-8", errors="surrogatepass")).hexdigest()
            sources[f"observations.{ordinal}.source"] = f"videodl:source:sha256:{fingerprint}"
        video_url, audio_url = _url(info.get("download_url")), _url(info.get("audio_download_url"))
        # Native _downloadwithnaiveallinone pairs only this VideoInfo's two URLs.
        paired = bool(video_url and audio_url and video_url != audio_url)
        for audio, field in ((False, "download_url"), (True, "audio_download_url")):
            if not _url(info.get(field)):
                continue
            index = len(items) + 1
            variant, variant_sources = _variant(info, source_url, index, audio, origin, paired=paired)
            item_id = str(index)
            kind = Kind.AUDIO if audio or variant.tracks is Tracks.AUDIO_ONLY else Kind.VIDEO
            items.append(Item(item_id, kind, index, (variant,)))
            for name, source in variant_sources.items():
                sources[f"items.{item_id}.variants.{variant.variant_id}.{name}"] = source
    items = _restrict_aliases(items, sources)
    first = retained[0][1]
    presentation = work_caption_presentation(_text(first.get("title")), _text(first.get("description")))
    title = presentation["title"]
    if title.casefold() in PLACEHOLDER_TITLES:
        title = ""
    cover = _url(first.get("cover_url"))
    author = _text(first.get("author"))
    duration = _number(first.get("duration_seconds") or first.get("duration"))
    for name, value in (("title", title), ("description", presentation["description"]),
                        ("topics", presentation["topics"]), ("author", author), ("cover", cover),
                        ("duration_seconds", duration),
                        ("quality", any(v.height or v.quality_label for i in items for v in i.variants)),
                        ("size", any(v.size_bytes for i in items for v in i.variants))):
        if value:
            sources[name] = origin
    work = Work(
        source_url=urldefrag(source_url)[0], work_id=work_id, items=tuple(items), title=title,
        description=_TOPIC.sub("", presentation["description"]).strip(), topics=tuple(presentation["topics"]),
        author=author, cover_url=cover, duration_seconds=duration, field_sources=MappingProxyType(sources),
    )
    for name in Work.__dataclass_fields__:
        if name not in sources and name != "field_sources":
            value = getattr(work, name)
            sources[name] = (origin if value and name in {
                "title", "description", "topics", "author", "cover_url", "duration_seconds",
            } else "contract:default")
    violations = validate_work(work)
    return Failure(FailureKind.INVALID_RESULT, "VIDEODL_" + violations[0].upper()) if violations else work


class VideodlMapper:
    name = NAME
    platforms = ("*",)

    def __init__(self, *, backend_id, backend_category):
        if (not isinstance(backend_id, str) or not _BACKEND.fullmatch(backend_id)
                or backend_category not in {"dedicated", "common"}):
            raise ValueError("VIDEODL_INVALID_BACKEND")
        self.backend_id = backend_id
        self.backend_category = backend_category

    def map(self, raw, source_url):
        try:
            return _map(raw, source_url, self.backend_id, self.backend_category)
        except (TypeError, ValueError, KeyError, AttributeError, OverflowError):
            return Failure(FailureKind.INVALID_RESULT, "VIDEODL_INVALID_RAW")
