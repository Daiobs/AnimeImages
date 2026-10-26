"""Native view/playurl fetching and pure JSON mapping.

Input is {view: API JSON, playurl: API JSON, selection: {page,
playurl_params: {cid, bvid or avid}, page_url: optional resolved video URL}}.
Optional view_status/playurl_status are observed HTTP statuses, not media facts.
selection contains executed choices/parameters, never a legacy result or media
context. In particular, API cookies and API request headers are not inherited.
"""
from dataclasses import replace
from fractions import Fraction
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
from ..work_caption import _TOPIC, work_caption_presentation
from .failures import failure_for_code, failure_for_http
from .base import Adapter
from .service_facts import (
    _browser_fetchable, _expiry, _first, _host_matches, _host_policy, _number,
    _required_headers, _size, _text, _tracks, _url,
)


NAME = "bilibili_native"
_BVID = re.compile(r"BV[0-9A-Za-z]{10}\Z")
_EXTENSIONS = {"mp4", "flv", "webm", "m4a", "mp3", "aac", "flac", "ogg", "opus"}
_MIME = {"video/mp4": "mp4", "video/x-flv": "flv", "video/webm": "webm",
         "audio/mp4": "m4a", "audio/mpeg": "mp3", "audio/flac": "flac", "audio/ogg": "ogg"}


def _positive_id(value):
    if type(value) not in (str, int) or not re.fullmatch(r"[1-9][0-9]*", str(value)):
        raise ValueError("INVALID_ID")
    return str(value)


def _source_identity(source):
    if not _url(source):
        raise ValueError("INVALID_SOURCE")
    parsed = urlsplit(source)
    query = parse_qs(parsed.query, keep_blank_values=True)
    pages = query.get("p", ["1"])
    if len(set(pages)) != 1:
        raise ValueError("PAGE_MISMATCH")
    page = int(_positive_id(pages[0]))
    if parsed.hostname == "b23.tv":
        return "", "", page
    if not _host_matches(parsed.hostname, "bilibili.com"):
        raise ValueError("UNSUPPORTED_SOURCE")
    match = re.fullmatch(r"/video/(BV[0-9A-Za-z]{10}|av[1-9][0-9]*)/?", parsed.path)
    if not match:
        raise ValueError("UNSUPPORTED_SOURCE")
    identity = match[1]
    return (identity, "", page) if identity.startswith("BV") else ("", identity[2:], page)


def _check_ids(record, bvid, aid):
    for key in ("bvid", "aid", "avid"):
        value = record.get(key)
        if value is None:
            continue
        if key == "bvid":
            if not isinstance(value, str) or not _BVID.fullmatch(value) or value != bvid:
                raise ValueError("IDENTITY_MISMATCH")
        elif _positive_id(value) != aid:
            raise ValueError("IDENTITY_MISMATCH")


def _selected_page(raw, source, view, play):
    bvid = view.get("bvid") or ""
    aid = _positive_id(view["aid"]) if view.get("aid") is not None else ""
    if bvid and (not isinstance(bvid, str) or not _BVID.fullmatch(bvid)) or not (bvid or aid):
        raise ValueError("INVALID_IDENTITY")
    selection = raw.get("selection")
    if not isinstance(selection, dict) or not isinstance(selection.get("playurl_params"), dict):
        raise ValueError("MISSING_SELECTION")
    params = selection["playurl_params"]
    if not any(params.get(key) is not None for key in ("bvid", "aid", "avid")):
        raise ValueError("MISSING_REQUEST_IDENTITY")
    _check_ids(params, bvid, aid)
    _check_ids(play, bvid, aid)
    page_number = int(_positive_id(selection.get("page")))
    cid = _positive_id(params.get("cid"))
    if play.get("cid") is not None and _positive_id(play["cid"]) != cid:
        raise ValueError("CID_MISMATCH")
    if play.get("page") is not None and int(_positive_id(play["page"])) != page_number:
        raise ValueError("PAGE_MISMATCH")
    source_bvid, source_aid, source_page = _source_identity(source)
    if source_bvid or source_aid:
        _check_ids({"bvid": source_bvid or None, "aid": source_aid or None}, bvid, aid)
        if source_page != page_number:
            raise ValueError("PAGE_MISMATCH")
    elif not selection.get("page_url"):
        raise ValueError("MISSING_RESOLVED_SOURCE")
    if selection.get("page_url"):
        resolved_bvid, resolved_aid, resolved_page = _source_identity(selection["page_url"])
        if not (resolved_bvid or resolved_aid):
            raise ValueError("MISSING_RESOLVED_SOURCE")
        _check_ids({"bvid": resolved_bvid or None, "aid": resolved_aid or None}, bvid, aid)
        if resolved_page != page_number:
            raise ValueError("PAGE_MISMATCH")
    pages = view.get("pages") or []
    if not isinstance(pages, list) or any(not isinstance(page, dict) for page in pages):
        raise ValueError("INVALID_PAGES")
    numbers = [int(_positive_id(page.get("page"))) for page in pages]
    if len(numbers) != len(set(numbers)):
        raise ValueError("INVALID_PAGES")
    if pages:
        selected = [page for page in pages if int(_positive_id(page["page"])) == page_number]
        if len(selected) != 1:
            raise ValueError("PAGE_MISMATCH")
        page = selected[0]
    else:
        if page_number != 1:
            raise ValueError("PAGE_MISMATCH")
        page = view
    if _positive_id(page.get("cid")) != cid:
        raise ValueError("CID_MISMATCH")
    return bvid or "av" + aid, page, cid


def _api_data(raw, stage):
    if stage + "_status" in raw:
        failure = failure_for_http(raw[stage + "_status"])
        if failure:
            return failure
    response = raw.get(stage)
    if not isinstance(response, dict) or type(response.get("code")) is not int:
        raise ValueError("INVALID_API_RESPONSE")
    if response["code"] != 0:
        return failure_for_code(response["code"], {
            "-404": Failure(FailureKind.NOT_FOUND, "BILIBILI_NOT_FOUND"),
        })
    if not isinstance(response.get("data"), dict):
        raise ValueError("INVALID_API_DATA")
    return response["data"]


def _media_url(value):
    value = _text(value)
    # Native accepts protocol-relative CDN references; preserve explicit http.
    return _url("https:" + value if value.startswith("//") else value)


def _urls(row, dash):
    urls = []
    for key in (("baseUrl", "base_url") if dash else ("url",)):
        if row.get(key):
            urls.append(row[key])
    for key in (("backupUrl", "backup_url") if dash else ("backup_url",)):
        backups = row.get(key) or []
        if not isinstance(backups, list):
            raise ValueError("INVALID_BACKUP_URLS")
        urls.extend(backups)
    normalized = [_media_url(url) for url in urls]
    if not normalized or any(not url for url in normalized):
        raise ValueError("INVALID_MEDIA_URL")
    return tuple(dict.fromkeys(normalized))


def _quality(play, row, origin):
    label = _text(_first(row, ("quality_label", "qualityLabel")))
    label_source = origin + ":quality_label"
    quality = row.get("id", play.get("quality"))
    if not label:
        for info in play.get("support_formats") or []:
            if isinstance(info, dict) and str(info.get("quality")) == str(quality):
                label = _text(info.get("new_description") or info.get("display_desc"))
                if label:
                    label_source = NAME + ":playurl.support_formats"
                    break
    if not label:
        for key, text in zip(play.get("accept_quality") or [], play.get("accept_description") or []):
            if str(key) == str(quality):
                label = _text(text)
                label_source = NAME + ":playurl.accept_description"
                break
    return ("", "contract:default") if label.casefold() in PLACEHOLDER_LABELS or not label else (label, label_source)


def _fps(row):
    for key in ("frame_rate", "frameRate", "fps"):
        value = row.get(key)
        if isinstance(value, str) and re.fullmatch(r"[0-9]+/[0-9]+", value):
            try:
                return _number(float(Fraction(value))), key
            except (ValueError, ZeroDivisionError, OverflowError):
                return None, key
        if _number(value):
            return _number(value), key
    return None, "fps"


def _container(row, url, play, dash, origin):
    ext = _text(row.get("ext") or row.get("container")).lower().lstrip(".")
    if ext in _EXTENSIONS:
        return ext, origin + ":container"
    mime = _text(row.get("mimeType") or row.get("mime_type")).lower()
    if mime in _MIME:
        return _MIME[mime], origin + ":mime_type"
    ext = urlsplit(url).path.rpartition(".")[2].lower()
    if ext in _EXTENSIONS:
        return ext, "adapter:url_extension"
    if not dash and _text(play.get("format")) in _EXTENSIONS:
        return play["format"], NAME + ":playurl.format"
    return "", "contract:default"


def _context(parents, url):
    return tuple(parent for parent in parents if not (
        declared := _media_url(parent.get("url") or parent.get("baseUrl") or parent.get("base_url"))) or declared == url)


def _variant(resource, aliases, play, source, variant_id):
    row, url, kind, native_tracks, origin, parents = resource
    observations = [(*_context(other[5], url), other[0]) for other in aliases]
    facts = tuple(fact for observation in observations for fact in observation)
    methods = {_text(fact.get("request_method") or fact.get("method")).upper() for fact in facts} - {"", "GET"}
    tracks = {other[3] for other in aliases} | {_tracks(other[0]) for other in aliases}
    tracks.discard(Tracks.UNKNOWN)
    if len(methods) > 1 or len(tracks) > 1:
        raise ValueError("CONFLICTING_RESOURCE_FACTS")
    label, label_source = _quality(play, row, origin) if kind is Kind.VIDEO else ("", "contract:default")
    container, container_source = _container(row, url, play, native_tracks is not Tracks.UNKNOWN, origin)
    size, exact = _size(row)
    fps, fps_key = _fps(row)
    policy, policy_source = _host_policy(source, url)
    expirations = [value for other in aliases if (value := _expiry(other[0], url)) is not None]
    variant = Variant(
        variant_id, url, container=container, tracks=next(iter(tracks), Tracks.UNKNOWN),
        height=_number(row.get("height"), integer=True) or height_from_label(label),
        width=_number(row.get("width"), integer=True), fps=fps,
        bitrate=_number(row.get("bandwidth") or row.get("bitrate"), integer=True),
        quality_label=label, is_original=row.get("is_original") is True,
        size_bytes=size, size_exact=exact, required_headers=_required_headers(facts),
        browser_fetchable=Fetchable.FALSE if any(_browser_fetchable(observation) is Fetchable.FALSE
                                                for observation in observations) else Fetchable.UNKNOWN,
        drm=any(fact.get(key) is True for fact in facts for key in ("drm", "has_drm", "is_drm")),
        segmented=urlsplit(url).path.lower().endswith(SEGMENTED_PATH_SUFFIXES) or any(
            fact.get("segmented") is True or bool(fact.get("fragments"))
            or bool(fact.get("SegmentBase") or fact.get("segment_base"))
            or any(token in _text(fact.get("protocol")).lower() for token in ("hls", "dash", "m3u8")) for fact in facts),
        request_method=next(iter(methods), "GET"), expires_at=min(expirations, default=None), host_policy=policy,
    )
    sources = {field: origin if getattr(variant, field) not in (None, "", (), False) else "contract:default"
               for field in Variant.__dataclass_fields__}
    sources.update(variant_id="adapter:resource_identity", container=container_source, host_policy=policy_source,
                   quality_label=label_source, merge_with="contract:default")
    if variant.tracks is Tracks.UNKNOWN:
        sources["tracks"] = "contract:unknown"
    if variant.browser_fetchable is Fetchable.UNKNOWN:
        sources["browser_fetchable"] = "contract:unknown"
    else:
        sources["browser_fetchable"] = NAME + ":playurl.resource_context"
    if variant.height and not _number(row.get("height")):
        sources["height"] = "adapter:height_from_label"
    if fps:
        sources["fps"] = origin + ":" + fps_key
    if variant.required_headers:
        sources["required_headers"] = NAME + ":playurl.resource_context"
    if not any(fact.get("request_method") or fact.get("method") for fact in facts):
        sources["request_method"] = "contract:default"
    for field, keys in (("drm", ("drm", "has_drm", "is_drm")), ("segmented", ("segmented",))):
        if any(type(fact.get(key)) is bool for fact in facts for key in keys):
            sources[field] = NAME + ":playurl.resource_context"
    if type(row.get("is_original")) is bool:
        sources["is_original"] = origin + ":is_original"
    if size is not None:
        sources["size_exact"] = origin + (":size_exact" if type(row.get("size_exact")) is bool else ":size")
    if variant.expires_at:
        sources["expires_at"] = (NAME + ":playurl.expires_at" if any(
            _number(other[0].get("expires_at")) and _expiry(other[0], url) == variant.expires_at
            for other in aliases) else "adapter:url_deadline")
    return variant, sources


def _resources(play, cid, view):
    durls = play.get("durl") or []
    if not isinstance(durls, list) or any(not isinstance(row, dict) for row in durls):
        raise ValueError("INVALID_DURL")
    for row in durls:
        _check_ids(row, view.get("bvid") or "", str(view.get("aid") or ""))
        if row.get("cid") is not None and _positive_id(row["cid"]) != cid:
            raise ValueError("CID_MISMATCH")
    if len(durls) > 1:
        return Failure(FailureKind.UNSUPPORTED, "BILIBILI_SEGMENTED_DURL")
    video, audios = [], []
    for index, row in enumerate(durls):
        urls = _urls(row, False)
        if any(urlsplit(url).path.lower().endswith(SEGMENTED_PATH_SUFFIXES) for url in urls):
            return Failure(FailureKind.UNSUPPORTED, "BILIBILI_SEGMENTED_PATH")
        video.extend((row, url, Kind.VIDEO, Tracks.UNKNOWN, NAME + f":playurl.durl[{index}]", (play,)) for url in urls)
    dash = play.get("dash") or {}
    if not isinstance(dash, dict):
        raise ValueError("INVALID_DASH")
    sections = [("video", dash.get("video") or [], Kind.VIDEO, (play, dash)),
                ("audio", dash.get("audio") or [], Kind.AUDIO, (play, dash))]
    for key in ("dolby", "flac"):
        extra = dash.get(key) or {}
        if not isinstance(extra, dict):
            raise ValueError("INVALID_DASH_AUDIO")
        audio = extra.get("audio") or []
        if key == "flac" and isinstance(audio, dict):
            audio = [audio]
        sections.append((key + ".audio", audio, Kind.AUDIO, (play, dash, extra)))
    for path, rows, kind, context in sections:
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError("INVALID_DASH_ROWS")
        for index, row in enumerate(rows):
            _check_ids(row, view.get("bvid") or "", str(view.get("aid") or ""))
            if row.get("cid") is not None and _positive_id(row["cid"]) != cid:
                raise ValueError("CID_MISMATCH")
            tracks = Tracks.VIDEO_ONLY if kind is Kind.VIDEO else Tracks.AUDIO_ONLY
            resources = [(row, url, kind, tracks, NAME + f":playurl.dash.{path}[{index}]", context)
                         for url in _urls(row, True)]
            if kind is Kind.VIDEO:
                video.extend(resources)
            else:
                audios.extend(resources)
    if not durls and (video or audios):
        # Native playurl does not supply an executed audio/video pairing.
        return Failure(FailureKind.UNSUPPORTED, "BILIBILI_SEPARATE_TRACKS_ONLY")
    if not video:
        raise ValueError("NO_MEDIA")
    return [video, audios] if audios else [video]


def _map(raw, source):
    _source_identity(source)
    if not isinstance(raw, dict):
        raise ValueError("NOT_RAW")
    view = _api_data(raw, "view")
    if isinstance(view, Failure):
        return view
    play = _api_data(raw, "playurl")
    if isinstance(play, Failure):
        return play
    identity, page, cid = _selected_page(raw, source, view, play)
    resources = _resources(play, cid, view)
    if isinstance(resources, Failure):
        return resources
    by_url = {}
    for rows in resources:
        for resource in rows:
            by_url.setdefault(resource[1], []).append(resource)
    items, sources = [], {"source_url": "request", "work_id": NAME + ":view.identity", "items": NAME + ":playurl"}
    for index, rows in enumerate(resources, 1):
        kind = rows[0][2]
        item_id = f"{kind.value}:{index}"
        variants = []
        for ordinal, resource in enumerate(rows, 1):
            variant, observed = _variant(resource, by_url[resource[1]], play, source, f"{item_id}:{ordinal}")
            variants.append(variant)
            sources.update({f"items.{item_id}.variants.{variant.variant_id}.{key}": value for key, value in observed.items()})
        items.append(Item(item_id, kind, index, tuple(variants)))
    if all(variant.tracks in {Tracks.VIDEO_ONLY, Tracks.AUDIO_ONLY} for variant in items[0].variants):
        return Failure(FailureKind.UNSUPPORTED, "BILIBILI_SEPARATE_TRACKS_ONLY")
    text = work_caption_presentation(view.get("title"), view.get("desc"))
    title = text["title"] if _text(view.get("title")) else ""
    if title.casefold() in PLACEHOLDER_TITLES:
        title = ""
    owner = view.get("owner") or {}
    if not isinstance(owner, dict):
        raise ValueError("INVALID_OWNER")
    duration = _number(page.get("duration"))
    duration_source = NAME + (":view.duration" if page is view else ":view.pages.duration")
    if duration is None and _number(play.get("timelength")):
        duration = _number(play["timelength"]) / 1000
        duration_source = NAME + ":playurl.timelength_ms"
    cover = _media_url(view.get("pic"))
    work = Work(urldefrag(source)[0], identity, tuple(items), title=title,
                description=_TOPIC.sub("", text["description"]).strip(), topics=tuple(text["topics"]),
                author=_text(owner.get("name")), cover_url=cover, duration_seconds=duration)
    for field, origin in (("title", ":view.title"), ("description", ":view.desc"), ("topics", ":view.desc"),
                          ("author", ":view.owner.name"), ("cover_url", ":view.pic")):
        sources[field] = NAME + origin if getattr(work, field) else "contract:default"
    sources["duration_seconds"] = duration_source if duration else "contract:default"
    sources["groups"] = "contract:default"
    if cover:
        sources["cover"] = sources["cover_url"]
    if any(v.height or v.quality_label for item in items for v in item.variants):
        sources["quality"] = NAME + ":playurl"
    if any(v.size_bytes for item in items for v in item.variants):
        sources["size"] = NAME + ":playurl"
    work = replace(work, field_sources=MappingProxyType(sources))
    violations = validate_work(work)
    return Failure(FailureKind.INVALID_RESULT, "BILIBILI_" + violations[0].upper()) if violations else work


class BilibiliMapper:
    name = NAME
    platforms = ("bilibili",)

    def map(self, raw, source_url):
        try:
            return _map(raw, source_url)
        except (TypeError, ValueError, KeyError, AttributeError, OverflowError) as error:
            code = str(error) if type(error) is ValueError and re.fullmatch(r"[A-Z_]+", str(error)) else "INVALID_RAW"
            kind = FailureKind.UNSUPPORTED if code == "UNSUPPORTED_SOURCE" else FailureKind.INVALID_RESULT
            return Failure(kind, "BILIBILI_" + code)


class BilibiliAdapter(BilibiliMapper, Adapter[dict]):
    def fetch(self, source_url, budget):
        from ..pipeline_shadow_capture import collect_raw
        from ..platforms.bilibili import BilibiliAdapter

        if not math.isfinite(budget) or budget <= 0:
            return Failure(FailureKind.TIMEOUT, "ADAPTER_BUDGET_EXHAUSTED")
        failure = None
        with collect_raw() as session:
            try:
                BilibiliAdapter().parse_url(source_url, timeout_seconds=budget,
                                           deadline_monotonic=time.monotonic() + budget)
            except Exception as error:
                failure = failure_for_exception(error)
        records = [record for record in session.records if record.provider == self.name]
        return records[-1].raw if records else failure or Failure(FailureKind.INVALID_RESULT, "BILIBILI_NO_RAW")

def failure_for_exception(error):
    if isinstance(error, TimeoutError) or type(error).__name__ in {"ReadTimeout", "ConnectTimeout", "XhsProviderTimeoutError"}:
        return Failure(FailureKind.TIMEOUT, "BILIBILI_TIMEOUT")
    status = getattr(error, "http_status", None) or getattr(getattr(error, "response", None), "status_code", None)
    if type(status) is int:
        return failure_for_http(status) or Failure(FailureKind.INVALID_RESULT, "BILIBILI_INVALID_RESPONSE")
    return Failure(FailureKind.UNAVAILABLE, "BILIBILI_UNAVAILABLE")
