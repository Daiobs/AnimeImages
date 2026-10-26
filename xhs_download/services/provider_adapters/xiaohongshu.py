"""Native XHS fetching and pure HTML/page-state mapping."""
from dataclasses import dataclass, replace
import re
from types import MappingProxyType
from urllib.parse import parse_qs, urldefrag, urljoin, urlsplit

from bs4 import BeautifulSoup

from ..pipeline import Failure, FailureKind, Fetchable, Group, Item, Kind, Tracks, Variant, Work, height_from_label, validate_work
from ..pipeline.model import PLACEHOLDER_LABELS, PLACEHOLDER_TITLES, SEGMENTED_PATH_SUFFIXES
from ..platforms.xhs_page_state import (
    XHS_STATE_ASSIGNMENT_RE, _extract_balanced_json, _is_note_candidate,
    _iter_nodes, _iter_script_payloads, _json_within_limits, _load_json_payload,
    _page_identity, extract_xhs_note_id, normalize_xhs_note_id,
)
from ..work_caption import _TOPIC, work_caption_presentation
from .failures import failure_for_http
from .base import Adapter
from .service_facts import _browser_fetchable, _expiry, _host_matches, _host_policy, _number, _required_headers, _text, _tracks, _url


NAME = "xhs_native"
MAX_HTML_CHARS = 1_000_000
MAX_DOM_NODES = 10_000
MAX_DOM_DEPTH = 32
_EXTENSIONS = {"jpg", "jpeg", "png", "webp", "avif", "heic", "gif", "mp4", "mov", "webm", "m4a", "mp3", "aac"}
_EXCLUDED_IMAGES = ("avatar", "favicon", "logo", "icon", "sprite", "emoji", "qrcode")
_NATIVE_TOPIC = re.compile(r"#([^#\n]+?)\[话题\]#")
_JS_VALUE = re.compile(r'"(?:\\.|[^"\\])*"|\bundefined\b')
_DEFAULT = "contract:default"


def _page_url(value):
    url = _url(value)
    if not url or not any(_host_matches(urlsplit(url).hostname, host)
                          for host in ("xiaohongshu.com", "xhslink.com", "xhslink.cn")):
        raise ValueError("INVALID_SOURCE")
    if urlsplit(url).port not in (None, 80, 443):
        raise ValueError("INVALID_SOURCE")
    parsed = urlsplit(url)
    claims = {extract_xhs_note_id(parsed._replace(query="", fragment="").geturl())} - {""}
    query = parse_qs(parsed.query, keep_blank_values=True)
    for key in ("note_id", "noteId", "noteid"):
        for value in query.get(key, ()):
            identity = normalize_xhs_note_id(value)
            if not identity:
                raise ValueError("IDENTITY_MISMATCH")
            claims.add(identity)
    if len(claims) > 1:
        raise ValueError("IDENTITY_MISMATCH")
    return url


def _media_url(value, base=""):
    value = _text(value)
    url = urljoin(base, value) if base else value
    if not _url(url) or any(ord(char) <= 32 for char in url) or "\\" in url:
        raise ValueError("INVALID_MEDIA_URL")
    return url


def _payloads(soup):
    payloads = list(_iter_script_payloads(soup))
    # Native assignments can contain undefined. Keep the complete tree so a
    # matching key inside recommendations never loses its enclosing scope.
    for script in soup.find_all("script"):
        raw = script.string if script.string is not None else script.get_text()
        if not isinstance(raw, str) or "undefined" not in raw:
            continue
        for match in XHS_STATE_ASSIGNMENT_RE.finditer(raw):
            start = match.end()
            while start < len(raw) and raw[start].isspace():
                start += 1
            candidate = _extract_balanced_json(raw, start)
            normalized = _JS_VALUE.sub(lambda token: "null" if token[0] == "undefined" else token[0], candidate)
            payload = _load_json_payload(normalized)
            if payload is not None and payload not in payloads:
                payloads.append(payload)
    return payloads


def _input(raw, source):
    source = _page_url(source)
    if isinstance(raw, str):
        envelope = {"html": raw}
    elif type(raw) is dict and ("html" in raw or "page_state" in raw):
        allowed = {"html", "page_state", "page_url", "http_status", "request_headers", "response_headers"}
        if set(raw) - allowed:
            raise ValueError("NOT_RAW_PAGE")
        envelope = raw
    elif type(raw) is dict and "note" in raw:
        envelope = {"page_state": raw}
    else:
        raise ValueError("NOT_RAW_PAGE")
    if "http_status" in envelope:
        failure = failure_for_http(envelope["http_status"])
        if failure:
            return failure
    page_url = _page_url(envelope["page_url"]) if "page_url" in envelope else source
    requested, final = extract_xhs_note_id(source), extract_xhs_note_id(page_url)
    if requested and final and requested != final:
        raise ValueError("IDENTITY_MISMATCH")
    html = envelope.get("html", "")
    if not isinstance(html, str):
        raise ValueError("INVALID_HTML")
    if len(html) > MAX_HTML_CHARS:
        raise ValueError("HTML_LIMIT")
    soup = BeautifulSoup(html, "html.parser")
    stack, count = [(soup, 0)], 0
    while stack:
        node, depth = stack.pop()
        count += 1
        if count > MAX_DOM_NODES or depth > MAX_DOM_DEPTH:
            raise ValueError("DOM_LIMIT")
        if hasattr(node, "children"):
            stack.extend((child, depth + 1) for child in node.children)
    for node in soup.select("link[rel], meta[property], meta[name]"):
        is_canonical = node.name == "link" and "canonical" in [str(value).lower() for value in node.get("rel", ())]
        is_meta = node.name == "meta" and str(node.get("property") or node.get("name")).lower() in {"og:url", "twitter:url"}
        if is_canonical or is_meta:
            value = urljoin(page_url, node.get("href" if is_canonical else "content") or "")
            if _host_matches(urlsplit(value).hostname or "", "xiaohongshu.com"):
                _page_url(value)
    page_id, page_origin, conflict = _page_identity(soup, requested or final)
    if conflict:
        raise ValueError("IDENTITY_MISMATCH")
    target = requested or final or page_id
    payloads = _payloads(soup)
    if "page_state" in envelope:
        state = envelope["page_state"]
        if type(state) is not dict or not _json_within_limits(state):
            raise ValueError("INVALID_PAGE_STATE")
        payloads.append(state)
    return source, page_url, soup, payloads, target, page_origin


def _select_note(payloads, target):
    candidates, foreign = [], False
    for payload in payloads:
        for row, path in _iter_nodes(payload):
            if any(part.lower() in {"feed", "feeds", "recommend", "recommendations"} for part in path):
                continue
            native_entry = len(path) >= 3 and path[-3] == "noteDetailMap" and path[-1] == "note"
            if not native_entry and not _is_note_candidate(row, path):
                continue
            explicit = [normalize_xhs_note_id(row[key]) for key in ("noteId", "note_id") if row.get(key) is not None]
            keyed = [normalize_xhs_note_id(part) for part in path if normalize_xhs_note_id(part)]
            claims = set(explicit + keyed)
            if "" in claims or len(claims) > 1:
                if not target or target in claims:
                    raise ValueError("IDENTITY_MISMATCH")
                continue
            identity = next(iter(claims), "")
            if not identity or target and identity != target:
                foreign = True
                continue
            # Do not turn an arbitrary identified object into a current note.
            if not keyed and not any(part.lower() in {"note", "currentnote", "notedata", "notedetail"} for part in path):
                continue
            origin = NAME + ":page_state." + ".".join("<note_id>" if normalize_xhs_note_id(part) else part for part in path)
            if not any(row == old[0] for old in candidates):
                candidates.append((row, identity, origin, origin + (".noteId" if "noteId" in row else ".note_id")
                                   if explicit else origin + ":map_key"))
    if len(candidates) > 1:
        raise ValueError("AMBIGUOUS_NOTE")
    if candidates:
        return candidates[0]
    if foreign:
        raise ValueError("IDENTITY_MISMATCH")
    return {}, target, NAME + ":page_state", ""


def _mapping(row, key):
    value = row.get(key)
    if value is None:
        return {}
    if type(value) is not dict:
        raise ValueError("INVALID_MEDIA_OBJECT")
    return value


@dataclass(frozen=True)
class _Resource:
    url: str
    origin: str
    metadata: tuple
    context: tuple


@dataclass(eq=False)
class _Content:
    kind: Kind
    resources: list


def _scoped(facts, url):
    result = []
    for row, path in facts:
        address = next((row[key] for key in ("url", "urlDefault", "urlPre", "masterUrl") if row.get(key)), None)
        if address is None or address == url:
            result.append((row, path))
    return tuple(result)


def _streams(parent, origin, context):
    streams = _mapping(parent, "stream")
    resources = []
    for ordinal, variants in enumerate(streams.values()):
        if type(variants) is not list:
            raise ValueError("INVALID_STREAMS")
        for index, row in enumerate(variants):
            if type(row) is not dict:
                raise ValueError("INVALID_STREAM")
            path = origin + f".stream[{ordinal}][{index}]"
            addresses = [(row["masterUrl"], path + ".masterUrl")] if row.get("masterUrl") else []
            backups = row.get("backupUrls") or []
            if type(backups) is not list:
                raise ValueError("INVALID_BACKUPS")
            addresses.extend((url, path + f".backupUrls[{i}]") for i, url in enumerate(backups))
            if not addresses:
                raise ValueError("STREAM_WITHOUT_URL")
            for url, url_origin in addresses:
                url = _media_url(url)
                # A stream row describes its master and backup copies, but a
                # separate URL-bearing ancestor only describes its own resource.
                local = {key: value for key, value in row.items() if key != "masterUrl"}
                resources.append(_Resource(url, url_origin, ((row, path),), (*context, (local, path))))
    return resources


def _resources(note, origin):
    base = ((note, origin),)
    items, groups = [], []
    images = note.get("imageList") or []
    if type(images) is not list:
        raise ValueError("INVALID_IMAGE_LIST")
    if note.get("type") in {"video", "video_note"}:
        images = []
    for index, row in enumerate(images):
        if type(row) is not dict:
            raise ValueError("INVALID_IMAGE")
        path = origin + f".imageList[{index}]"
        context = (*base, (row, path))
        stills = []
        if row.get("urlDefault"):
            stills.append(_Resource(_media_url(row["urlDefault"]), path + ".urlDefault", ((row, path),), context))
        if row.get("urlPre"):
            metadata = ((row, path),) if row["urlPre"] == row.get("urlDefault") else ()
            stills.append(_Resource(_media_url(row["urlPre"]), path + ".urlPre", metadata, context))
        infos = row.get("infoList") or []
        if type(infos) is not list:
            raise ValueError("INVALID_IMAGE_INFO")
        for i, info in enumerate(infos):
            if type(info) is not dict or not info.get("url"):
                raise ValueError("INVALID_IMAGE_INFO")
            address = _media_url(info["url"])
            info_path = path + f".infoList[{i}]"
            metadata = ((info, info_path), (row, path)) if address == row.get("urlDefault") else ((info, info_path),)
            stills.append(_Resource(address, info_path + ".url", metadata, (*context, (info, info_path))))
        if not stills:
            raise ValueError("IMAGE_WITHOUT_URL")
        still = _Content(Kind.IMAGE, stills)
        items.append(still)
        motion = _streams(row, path, context)
        if motion:
            video = _Content(Kind.VIDEO, motion)
            items.append(video)
            if row.get("livePhoto") is True:
                groups.append((still, video))
    video = _mapping(note, "video")
    media = _mapping(video, "media")
    video_path = origin + ".video"
    videos = _streams(media, video_path + ".media", (*base, (video, video_path), (media, video_path + ".media")))
    for key in ("masterUrl", "originVideoKey"):
        if video.get(key) and _url(video[key]):
            videos.append(_Resource(_media_url(video[key]), video_path + "." + key, ((video, video_path),), (*base, (video, video_path))))
    consumer = _mapping(video, "consumer")
    if _url(consumer.get("originVideoKey")):
        path = video_path + ".consumer"
        videos.append(_Resource(_media_url(consumer["originVideoKey"]), path + ".originVideoKey", ((consumer, path),),
                                (*base, (video, video_path), (consumer, path))))
    if videos:
        items.append(_Content(Kind.VIDEO, videos))
    return items, groups, video


def _body(soup, identity, page_url, *, include_media=True):
    roots = soup.select(".note-content")
    roots = [root for root in roots if not any(parent in roots for parent in root.parents)]
    if len(roots) > 1:
        raise ValueError("AMBIGUOUS_BODY")
    if not roots or not identity:
        return "", "", "", []
    root = roots[0]
    title_node = root.select_one("#detail-title, .title")
    author_node = root.select_one(".username, .author-container .name")
    title = title_node.get_text(" ", strip=True) if title_node else ""
    author = author_node.get_text(" ", strip=True) if author_node else ""
    for node in root.select("script, style, template, noscript, nav, header, footer, button, #detail-title, .title, .username, .author-container"):
        node.decompose()
    resources, seen = [], set()
    for index, node in enumerate(root.find_all("img") if include_media else ()):
        marker = " ".join((str(node.get("class", "")), str(node.get("alt", "")))).lower()
        if any(hint in marker for hint in _EXCLUDED_IMAGES):
            continue
        for attribute in ("src", "data-src", "data-original"):
            value = _text(node.get(attribute))
            if not value or value.startswith("#") or value.lower().startswith("data:image/"):
                continue
            url = _media_url(value, page_url)
            if url not in seen:
                seen.add(url)
                resources.append(_Resource(url, NAME + f":html.note-content.img[{index}].{attribute}", (), ()))
            break
    for node in root.find_all("br"):
        node.replace_with("\n")
    for node in root.find_all(("p", "div", "li", "blockquote")):
        node.append("\n")
    description = "\n".join(line.strip() for line in root.get_text().splitlines() if line.strip())
    return title, description, author, resources


def _field(metadata, keys, convert):
    for row, origin in metadata:
        for key in keys:
            value = convert(row.get(key))
            if value is not None and value != "":
                return value, origin + "." + key
    return None, _DEFAULT


def _native_tracks(row):
    return _tracks({**row, "vcodec": _text(row.get("videoCodec")) or row.get("vcodec"),
                    "acodec": _text(row.get("audioCodec")) or row.get("acodec")})


def _stream_duration(video, origin):
    media = _mapping(video, "media")
    observations = [(value / 1000, origin + f".media.stream[{codec}][{index}].duration:milliseconds")
                    for codec, rows in enumerate(_mapping(media, "stream").values())
                    for index, row in enumerate(rows) if (value := _number(row.get("duration"))) is not None]
    return observations[0] if observations and len({value for value, _ in observations}) == 1 else (None, _DEFAULT)


def _variant(resource, aliases, kind, variant_id):
    metadata = resource.metadata
    observations = [_scoped(alias.context, resource.url) for alias in aliases]
    facts = tuple(fact for observation in observations for fact in observation)
    rows = tuple(row for row, _ in facts)
    tracks = {_native_tracks(row) for row in rows} - {Tracks.UNKNOWN} if kind is not Kind.IMAGE else set()
    methods = {_text(row.get("request_method") or row.get("method")).upper() for row in rows} - {"", "GET"}
    if len(tracks) > 1 or len(methods) > 1:
        raise ValueError("CONFLICTING_MEDIA_FACTS")
    values, sources = {}, {}
    for field in ("width", "height", "fps"):
        values[field], sources[field] = _field(metadata, (field,), lambda value: _number(value, integer=field != "fps"))
    values["bitrate"], sources["bitrate"] = _field(metadata, ("avgBitrate", "bitrate_bps"), lambda value: _number(value, integer=True))
    label, label_origin = _field(metadata, ("qualityType", "quality_label", "qualityLabel"), _text)
    label = label or ""
    if label.casefold() in PLACEHOLDER_LABELS:
        label, label_origin = "", _DEFAULT
    if values["height"] is None and height_from_label(label):
        values["height"], sources["height"] = height_from_label(label), "adapter:height_from_label"
    container, container_origin = _field(metadata, ("container", "ext"),
        lambda value: _text(value).lower().lstrip(".") if _text(value).lower().lstrip(".") in _EXTENSIONS else None)
    if not container:
        extension = urlsplit(resource.url).path.rpartition(".")[2].lower()
        container = extension if extension in _EXTENSIONS else ""
        container_origin = "adapter:url_extension" if container else _DEFAULT
    size, size_origin = _field(metadata, ("size_bytes", "size"), lambda value: _number(value, integer=True))
    exact, exact_origin = _field(metadata, ("size_exact",), lambda value: value if type(value) is bool else None)
    if size is None:
        exact, exact_origin = False, _DEFAULT
    elif exact is None:
        exact, exact_origin = True, size_origin + ":bytes"
    policy, policy_origin = _host_policy("https://www.xiaohongshu.com/", resource.url)
    expirations = [(expiry, path + ".expires_at") for row, path in facts if (expiry := _expiry(row, resource.url)) is not None]
    expiry, expiry_origin = min(expirations, key=lambda pair: pair[0]) if expirations else (None, _DEFAULT)
    browser = Fetchable.FALSE if any(_browser_fetchable(tuple(row for row, _ in observation)) is Fetchable.FALSE
                                     for observation in observations) else Fetchable.UNKNOWN
    flags = {}
    for name, keys, observed in (("is_original", ("is_original",), metadata), ("drm", ("drm", "has_drm"), facts),
                                 ("segmented", ("segmented",), facts)):
        declarations = [(row[key], path + "." + key) for row, path in observed for key in keys if type(row.get(key)) is bool]
        flags[name] = any(value for value, _ in declarations)
        sources[name] = next((path for value, path in declarations if value), declarations[0][1] if declarations else _DEFAULT)
    if urlsplit(resource.url).path.lower().endswith(SEGMENTED_PATH_SUFFIXES):
        flags["segmented"], sources["segmented"] = True, "adapter:url_segmented_path"
    for row, path in facts:
        if row.get("fragments") or any(token in _text(row.get("protocol")).lower() for token in ("hls", "dash", "m3u8")):
            flags["segmented"], sources["segmented"] = True, path + ":segmented"
    variant = Variant(variant_id, resource.url, container=container, **values, **flags,
        tracks=next(iter(tracks), Tracks.UNKNOWN), quality_label=label, size_bytes=size, size_exact=exact,
        required_headers=_required_headers(rows), browser_fetchable=browser, request_method=next(iter(methods), "GET"),
        expires_at=expiry, host_policy=policy)
    sources.update(variant_id="adapter:resource_identity", url=resource.origin, container=container_origin,
        quality_label=label_origin, size_bytes=size_origin, size_exact=exact_origin, host_policy=policy_origin,
        expires_at=expiry_origin, merge_with=_DEFAULT, tracks="contract:unknown", browser_fetchable="contract:unknown",
        required_headers=_DEFAULT, request_method=_DEFAULT)
    if kind is not Kind.IMAGE:
        sources["tracks"] = next((path + ":tracks" for row, path in facts if _native_tracks(row) is variant.tracks
                                  and (variant.tracks is not Tracks.UNKNOWN or row.get("tracks") == "unknown")), "contract:unknown")
    if variant.required_headers or any("required_headers" in row for row in rows):
        sources["required_headers"] = ";".join(dict.fromkeys(path for row, path in facts
            if _required_headers((row,)) or "required_headers" in row))
    if browser is Fetchable.FALSE:
        sources["browser_fetchable"] = ";".join(dict.fromkeys(path for observation in observations
            if _browser_fetchable(tuple(row for row, _ in observation)) is Fetchable.FALSE for _, path in observation))
    sources["request_method"] = next((path for row, path in facts
        if _text(row.get("request_method") or row.get("method")).upper() == variant.request_method), _DEFAULT)
    return variant, sources


def _map(raw, source):
    parsed = _input(raw, source)
    if isinstance(parsed, Failure):
        return parsed
    source, page_url, soup, payloads, target, page_origin = parsed
    note, identity, origin, identity_origin = _select_note(payloads, target)
    if not identity:
        raise ValueError("MISSING_WORK_ID")
    if not identity_origin:
        identity_origin = NAME + ":html." + page_origin if page_origin else "request:work_id"
    for row in (note, *payloads):
        state = row.get("pageState") if type(row) is dict else None
        if state in {"deleted", "not_found", "private"}:
            return Failure(FailureKind.NOT_FOUND, "XHS_" + state.upper())
        if state in {"login_required", "unavailable"}:
            return Failure(FailureKind.UNAVAILABLE, "XHS_" + state.upper())
    contents, groups, video = _resources(note, origin)
    dom_title, dom_description, dom_author, body_media = _body(
        soup, identity if note or page_origin else "", page_url, include_media=not contents)
    if not contents:
        context = ((note, origin),) if note else ()
        contents = [_Content(Kind.IMAGE, [replace(resource, context=context)]) for resource in body_media]
    if not contents:
        raise ValueError("NO_MEDIA")
    aliases = {}
    for content in contents:
        for resource in content.resources:
            aliases.setdefault(resource.url, []).append(resource)
    items, item_ids = [], {}
    sources = {"source_url": "execution:page_url" if page_url != source else "request", "work_id": identity_origin,
               "items": NAME + ":raw_content_order", "platform": "router", "provider": "router", "completeness": _DEFAULT}
    for index, content in enumerate(contents, 1):
        item_id = f"{content.kind.value}:{index}"
        item_ids[id(content)] = item_id
        variants = []
        for ordinal, resource in enumerate(content.resources, 1):
            variant, observed = _variant(resource, aliases[resource.url], content.kind, f"{item_id}:{ordinal}")
            variants.append(variant)
            sources.update({f"items.{item_id}.variants.{variant.variant_id}.{field}": path for field, path in observed.items()})
        items.append(Item(item_id, content.kind, index, tuple(variants)))
        for field in ("item_id", "index", "kind", "variants", "default_variant"):
            sources[f"items.{item_id}.{field}"] = "contract:default_selection" if field == "default_variant" else "adapter:raw_content_order"
    title, title_origin = (_text(note.get("title")), origin + ".title") if _text(note.get("title")) else (dom_title, NAME + ":html.note-content.title")
    description, desc_origin = (_text(note.get("desc")), origin + ".desc") if _text(note.get("desc")) else (dom_description, NAME + ":html.note-content.text")
    native_topics = tuple(dict.fromkeys(match[1].strip() for match in _NATIVE_TOPIC.finditer(description)))
    text = work_caption_presentation(title, _NATIVE_TOPIC.sub("", description))
    title = text["title"] if title else ""
    if title.casefold() in PLACEHOLDER_TITLES | {"xiaohongshu", "小红书"}:
        title = ""
    user = _mapping(note, "user")
    author = _text(user.get("nickname")) or dom_author
    cover, cover_origin = _field(((note, origin), (video, origin + ".video")), ("coverUrl", "cover"), _url)
    if not cover and note.get("type") in {"video", "video_note"}:
        images = note.get("imageList") or []
        if images:
            first = images[0]
            if type(first) is not dict:
                raise ValueError("INVALID_IMAGE")
            if first.get("urlDefault"):
                cover, cover_origin = _media_url(first["urlDefault"]), origin + ".imageList[0].urlDefault"
            elif first.get("urlPre"):
                cover, cover_origin = _media_url(first["urlPre"]), origin + ".imageList[0].urlPre"
            elif first.get("infoList"):
                cover, cover_origin = _media_url(first["infoList"][0]["url"]), origin + ".imageList[0].infoList[0].url"
    duration, duration_origin = _field(((video, origin + ".video"),), ("duration_seconds",), _number)
    if duration is None:
        milliseconds, duration_origin = _field(((video, origin + ".video"),), ("duration_ms",), _number)
        duration = milliseconds / 1000 if milliseconds is not None else None
    if duration is None:
        duration, duration_origin = _stream_duration(video, origin + ".video")
    mapped_groups = tuple(Group("live_photo", (item_ids[id(still)], item_ids[id(motion)])) for still, motion in groups)
    work = Work(urldefrag(page_url)[0], identity, tuple(items), title=title,
        description=_TOPIC.sub("", text["description"]).strip(), topics=tuple(dict.fromkeys((*native_topics, *text["topics"]))), author=author,
        cover_url=cover or "", duration_seconds=duration, groups=mapped_groups)
    for field, path in (("title", title_origin), ("description", desc_origin), ("topics", desc_origin),
                        ("author", origin + ".user.nickname" if _text(user.get("nickname")) else NAME + ":html.note-content.author"),
                        ("cover_url", cover_origin), ("duration_seconds", duration_origin),
                        ("groups", origin + ".imageList:livePhoto_same_item")):
        sources[field] = path if getattr(work, field) not in (None, "", ()) else _DEFAULT
    if cover:
        sources["cover"] = cover_origin
    if any(v.height or v.quality_label for item in items for v in item.variants):
        sources["quality"] = NAME + ":raw_media"
    if any(v.size_bytes for item in items for v in item.variants):
        sources["size"] = NAME + ":raw_media"
    work = replace(work, field_sources=MappingProxyType(sources))
    violations = validate_work(work)
    return Failure(FailureKind.INVALID_RESULT, "XHS_" + violations[0].upper()) if violations else work


class XiaohongshuMapper:
    name = NAME
    platforms = ("xiaohongshu",)

    def map(self, raw, source_url):
        try:
            return _map(raw, source_url)
        except (TypeError, ValueError, KeyError, AttributeError, OverflowError, RecursionError) as error:
            code = str(error) if type(error) is ValueError and re.fullmatch(r"[A-Z_]+", str(error)) else "INVALID_RAW"
            return Failure(FailureKind.INVALID_RESULT, "XHS_" + code)


class XiaohongshuAdapter(XiaohongshuMapper, Adapter[dict]):
    def fetch(self, source_url, budget):
        import math
        import time
        from ..pipeline_shadow_capture import collect_raw
        from ..platforms.xiaohongshu import XiaohongshuAdapter

        if not math.isfinite(budget) or budget <= 0:
            return Failure(FailureKind.TIMEOUT, "ADAPTER_BUDGET_EXHAUSTED")
        failure = None
        with collect_raw() as session:
            try:
                XiaohongshuAdapter().parse_url(source_url, timeout_seconds=budget,
                                              deadline_monotonic=time.monotonic() + budget)
            except Exception as error:
                if isinstance(error, TimeoutError) or type(error).__name__ in {"ReadTimeout", "ConnectTimeout", "XhsProviderTimeoutError"}:
                    failure = Failure(FailureKind.TIMEOUT, "XHS_TIMEOUT")
                else:
                    status = getattr(error, "http_status", None)
                    failure = failure_for_http(status) if type(status) is int else None
                    failure = failure or Failure(FailureKind.UNAVAILABLE, "XHS_UNAVAILABLE")
        records = [record for record in session.records if record.provider == self.name]
        return records[-1].raw if records else failure or Failure(FailureKind.INVALID_RESULT, "XHS_NO_RAW")
