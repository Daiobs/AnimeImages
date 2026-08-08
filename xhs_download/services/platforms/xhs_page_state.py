import json
import re
from dataclasses import dataclass
from html import unescape
from urllib.parse import parse_qs, urlparse

from ..common import normalize_text


XHS_NOTE_ID_RE = re.compile(r"^[0-9a-fA-F]{24}$")
XHS_STATE_ASSIGNMENT_RE = re.compile(
    r"(?:window\s*\.\s*)?"
    r"(?:__INITIAL_STATE__|__INITIAL_SSR_STATE__|__NEXT_DATA__)\s*=\s*"
)
XHS_JSON_SCRIPT_TYPES = {"application/json", "application/ld+json"}
XHS_JSON_SCRIPT_IDS = {"__next_data__", "__initial_state__", "__initial_ssr_state__"}
XHS_MAX_SCRIPT_CHARS = 1_000_000
XHS_MAX_JSON_SCAN_CHARS = 1_000_000
XHS_MAX_JSON_NODES = 10_000
XHS_MAX_JSON_DEPTH = 32
XHS_MAX_TITLE_LENGTH = 200
XHS_MAX_AUTHOR_LENGTH = 80
XHS_MAX_VIDEO_SIZE_BYTES = 100 * 1024 * 1024 * 1024
XHS_TERMINAL_PAGE_STATES = frozenset(
    {"login_required", "private", "deleted", "unavailable"}
)


@dataclass(frozen=True)
class XhsPageState:
    note_id: str = ""
    title: str = ""
    author_name: str = ""
    post_type: str = "unknown"
    image_urls: tuple[str, ...] = ()
    video_urls: tuple[str, ...] = ()
    cover_urls: tuple[str, ...] = ()
    video_size_bytes: int = 0
    page_state: str = "unknown"
    current_note_page_state: str = "unknown"
    explicit_page_state: str = "unknown"
    structured_state_found: bool = False
    structured_payload_found: bool = False
    current_note_found: bool = False
    note_candidate_found: bool = False
    other_note_candidate_found: bool = False
    structured_image_list_present: bool = False
    structured_video_present: bool = False
    terminal_page_state: bool = False


def normalize_xhs_note_id(value):
    value = str(value or "").strip()
    return value.lower() if XHS_NOTE_ID_RE.fullmatch(value) else ""


def extract_xhs_note_id(source_url):
    parsed = urlparse(str(source_url or ""))
    path_parts = [part for part in parsed.path.split("/") if part]
    for index, part in enumerate(path_parts):
        normalized_part = part.lower()
        if normalized_part in {"explore", "note"} and index + 1 < len(path_parts):
            note_id = normalize_xhs_note_id(path_parts[index + 1])
            if note_id:
                return note_id
        if (
            normalized_part == "discovery"
            and index + 2 < len(path_parts)
            and path_parts[index + 1].lower() == "item"
        ):
            note_id = normalize_xhs_note_id(path_parts[index + 2])
            if note_id:
                return note_id

    query = parse_qs(parsed.query, keep_blank_values=False)
    for key in ("note_id", "noteId", "noteid"):
        for value in query.get(key, ()):
            note_id = normalize_xhs_note_id(value)
            if note_id:
                return note_id
    return ""


def normalize_xhs_state_text(value, *, max_length):
    if not isinstance(value, str):
        return ""
    value = normalize_text(unescape(value))
    value = re.sub(r"<[^>]{0,200}>", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value[:max_length].strip()


def _extract_balanced_json(text, start_index):
    if start_index >= len(text) or text[start_index] not in "{[":
        return ""
    stack = []
    in_string = False
    escaped = False
    scan_end = min(len(text), start_index + XHS_MAX_JSON_SCAN_CHARS)
    pairs = {"}": "{", "]": "["}

    for index in range(start_index, scan_end):
        character = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character in "{[":
            stack.append(character)
        elif character in "}]":
            if not stack or stack[-1] != pairs[character]:
                return ""
            stack.pop()
            if not stack:
                return text[start_index:index + 1]
    return ""


def _json_within_limits(payload):
    stack = [(payload, 0)]
    node_count = 0
    while stack:
        value, depth = stack.pop()
        node_count += 1
        if node_count > XHS_MAX_JSON_NODES or depth > XHS_MAX_JSON_DEPTH:
            return False
        if isinstance(value, dict):
            stack.extend((child, depth + 1) for child in value.values())
        elif isinstance(value, list):
            stack.extend((child, depth + 1) for child in value)
    return True


def _load_json_payload(value):
    try:
        payload = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError, RecursionError):
        return None
    if not isinstance(payload, (dict, list)) or not _json_within_limits(payload):
        return None
    return payload


def _iter_script_payloads(soup, *, source_note_id=""):
    for script in soup.find_all("script"):
        raw_value = script.string if script.string is not None else script.get_text()
        if not isinstance(raw_value, str) or not raw_value.strip():
            continue
        if len(raw_value) > XHS_MAX_SCRIPT_CHARS:
            continue
        raw_value = raw_value.strip()
        script_type = str(script.get("type") or "").split(";", 1)[0].strip().lower()
        script_id = str(script.get("id") or "").strip().lower()

        if script_type in XHS_JSON_SCRIPT_TYPES or script_id in XHS_JSON_SCRIPT_IDS:
            if raw_value.startswith(("{", "[")):
                payload = _load_json_payload(raw_value)
                if payload is not None:
                    yield payload

        assignment_found = False
        for match in XHS_STATE_ASSIGNMENT_RE.finditer(raw_value):
            assignment_found = True
            start_index = match.end()
            while start_index < len(raw_value) and raw_value[start_index].isspace():
                start_index += 1
            candidate = _extract_balanced_json(raw_value, start_index)
            payload = _load_json_payload(candidate) if candidate else None
            if payload is not None:
                yield payload

        if assignment_found and source_note_id:
            note_entry_re = re.compile(rf'"{re.escape(source_note_id)}"\s*:\s*')
            for match in note_entry_re.finditer(raw_value):
                start_index = match.end()
                while start_index < len(raw_value) and raw_value[start_index].isspace():
                    start_index += 1
                candidate = _extract_balanced_json(raw_value, start_index)
                payload = _load_json_payload(candidate) if candidate else None
                if payload is not None:
                    yield {source_note_id: payload}


def _key_token(value):
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def _iter_nodes(root):
    stack = [(root, ())]
    while stack:
        value, path = stack.pop()
        if isinstance(value, dict):
            yield value, path
            children = list(value.items())
            for key, child in reversed(children):
                stack.append((child, path + (str(key),)))
        elif isinstance(value, list):
            for index in range(len(value) - 1, -1, -1):
                stack.append((value[index], path + (str(index),)))


def _mapping_value(mapping, keys):
    for key in keys:
        value = mapping.get(key)
        if value not in (None, ""):
            return value
    return None


def _candidate_note_id(mapping, path):
    note_id = normalize_xhs_note_id(_mapping_value(mapping, ("noteId", "note_id")))
    if note_id:
        return note_id
    for part in reversed(path):
        note_id = normalize_xhs_note_id(part)
        if note_id:
            return note_id
    return ""


def _looks_like_note(mapping):
    keys = {_key_token(key) for key in mapping}
    identity = "noteid" in keys
    content_groups = (
        {"title", "displaytitle", "desc", "description"},
        {"user", "userinfo", "author"},
        {"imagelist", "images", "imageitems", "video", "videoinfo"},
        {"type", "notetype"},
    )
    group_count = sum(bool(keys & group) for group in content_groups)
    return (identity and group_count >= 1) or group_count >= 3


def _is_note_candidate(mapping, path):
    if not _looks_like_note(mapping):
        return False
    if _candidate_note_id(mapping, path):
        return True
    path_tokens = {_key_token(part) for part in path}
    return bool(
        path_tokens
        & {
            "currentnote",
            "feed",
            "feeds",
            "note",
            "notedata",
            "notedetail",
            "notedetailmap",
            "recommend",
            "recommendations",
        }
    )


def _candidate_score(mapping, path, *, source_note_id):
    note_id = _candidate_note_id(mapping, path)
    if source_note_id:
        if note_id != source_note_id:
            return -1
        score = 1_000
    else:
        path_tokens = {_key_token(part) for part in path}
        if path_tokens & {"recommend", "recommendations", "feed", "feeds"}:
            return -1
        current_markers = {"currentnote", "notedetail", "notedetailmap", "notedata"}
        is_direct_note = len(path) <= 3 and "note" in path_tokens
        if not note_id or not (path_tokens & current_markers or is_direct_note):
            return -1
        score = 500

    keys = {_key_token(key) for key in mapping}
    for group in (
        {"title", "displaytitle", "desc"},
        {"user", "userinfo", "author"},
        {"imagelist", "images", "imageitems"},
        {"video", "videoinfo"},
        {"type", "notetype"},
    ):
        score += 10 if keys & group else 0
    score -= min(len(path), 20)
    return score


def _select_current_note(payloads, source_note_id):
    candidates = []
    note_candidate_found = False
    other_note_candidate_found = False
    for payload in payloads:
        for mapping, path in _iter_nodes(payload):
            if not _is_note_candidate(mapping, path):
                continue
            note_candidate_found = True
            candidate_note_id = _candidate_note_id(mapping, path)
            if source_note_id and candidate_note_id != source_note_id:
                path_tokens = {_key_token(part) for part in path}
                if candidate_note_id or path_tokens & {
                    "feed",
                    "feeds",
                    "recommend",
                    "recommendations",
                }:
                    other_note_candidate_found = True
            score = _candidate_score(mapping, path, source_note_id=source_note_id)
            if score >= 0:
                candidates.append((score, candidate_note_id, path, mapping))
    if not candidates:
        return None, "", note_candidate_found, other_note_candidate_found

    if not source_note_id:
        candidate_ids = {candidate[1] for candidate in candidates if candidate[1]}
        if len(candidate_ids) != 1:
            return None, "", note_candidate_found, len(candidate_ids) > 1

    candidates.sort(key=lambda candidate: (-candidate[0], tuple(candidate[2])))
    selected = candidates[0]
    return (
        selected[3],
        selected[1],
        note_candidate_found,
        other_note_candidate_found,
    )


def _unique_strings(values):
    results = []
    for value in values:
        if not isinstance(value, str):
            continue
        value = normalize_text(value).strip()
        if value and value not in results:
            results.append(value)
    return tuple(results)


def _urls_from_value(value):
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list):
        results = []
        for item in value:
            results.extend(_urls_from_value(item))
        return tuple(results)
    if isinstance(value, dict):
        results = []
        for key in ("url", "urlDefault", "url_default", "masterUrl", "master_url"):
            results.extend(_urls_from_value(value.get(key)))
        return tuple(results)
    return ()


def _best_image_url(item):
    if isinstance(item, str):
        return normalize_text(item).strip()
    if not isinstance(item, dict):
        return ""

    for key in (
        "urlDefault",
        "url_default",
        "originalUrl",
        "original_url",
        "originUrl",
        "origin_url",
        "url",
        "urlPre",
        "url_pre",
    ):
        urls = _urls_from_value(item.get(key))
        if urls:
            return normalize_text(urls[0]).strip()

    info_list = _mapping_value(item, ("infoList", "info_list"))
    if isinstance(info_list, list):
        ranked = []
        scene_priority = {"WB_DFT": 0, "WB_PRV": 1, "CRD_WM": 2}
        for index, info in enumerate(info_list):
            if not isinstance(info, dict):
                continue
            urls = _urls_from_value(info)
            if not urls:
                continue
            scene = str(_mapping_value(info, ("imageScene", "image_scene")) or "")
            ranked.append((scene_priority.get(scene, 10), index, urls[0]))
        if ranked:
            ranked.sort(key=lambda candidate: (candidate[0], candidate[1]))
            return normalize_text(ranked[0][2]).strip()
    return ""


def _extract_image_urls(note):
    image_list = _mapping_value(note, ("imageList", "image_list", "imageItems", "images"))
    if not isinstance(image_list, list):
        return ()
    return _unique_strings(_best_image_url(item) for item in image_list)


def _structured_image_list_present(note):
    return isinstance(
        _mapping_value(note, ("imageList", "image_list", "imageItems", "images")),
        list,
    )


def _structured_video_present(note):
    return isinstance(_mapping_value(note, ("video", "videoInfo", "video_info")), dict)


def _iter_nested_mappings(root, max_depth=6):
    stack = [(root, 0)]
    while stack:
        value, depth = stack.pop()
        if not isinstance(value, dict) or depth > max_depth:
            continue
        yield value
        children = [child for child in value.values() if isinstance(child, (dict, list))]
        for child in reversed(children):
            if isinstance(child, dict):
                stack.append((child, depth + 1))
            else:
                for item in reversed(child):
                    if isinstance(item, dict):
                        stack.append((item, depth + 1))


def _valid_video_size(value):
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value if 0 < value <= XHS_MAX_VIDEO_SIZE_BYTES else 0


def _extract_video_details(note):
    video = _mapping_value(note, ("video", "videoInfo", "video_info"))
    if not isinstance(video, dict):
        return (), 0

    candidates = []
    for mapping in _iter_nested_mappings(video):
        for key in ("originVideoKey", "origin_video_key", "videoKey", "video_key"):
            value = mapping.get(key)
            if isinstance(value, str) and value.strip():
                candidates.append((value, 0))

    for mapping in _iter_nested_mappings(video):
        for key in ("masterUrl", "master_url", "videoUrl", "video_url"):
            value = mapping.get(key)
            if isinstance(value, str) and value.strip():
                candidates.append((value, _valid_video_size(mapping.get("size"))))
        for key in ("backupUrls", "backup_urls", "backupUrl", "backup_url"):
            for value in _urls_from_value(mapping.get(key)):
                candidates.append((value, _valid_video_size(mapping.get("size"))))

    unique_candidates = []
    seen = set()
    for value, size_bytes in candidates:
        value = normalize_text(value).strip()
        if not value or value in seen:
            continue
        seen.add(value)
        unique_candidates.append((value, size_bytes))
    if not unique_candidates:
        return (), 0
    return tuple(value for value, _size in unique_candidates), unique_candidates[0][1]


def _extract_cover_urls(note):
    cover_keys = (
        "cover",
        "coverUrl",
        "cover_url",
        "videoCover",
        "video_cover",
        "posterUrl",
        "poster_url",
        "poster",
        "firstFrame",
        "first_frame",
        "firstFrameUrl",
        "first_frame_url",
        "thumbnailUrl",
        "thumbnail_url",
    )
    candidates = []
    for key in cover_keys:
        candidates.extend(_urls_from_value(note.get(key)))

    video = _mapping_value(note, ("video", "videoInfo", "video_info"))
    if not isinstance(video, dict):
        return _unique_strings(candidates)
    for mapping in _iter_nested_mappings(video):
        for key in cover_keys:
            candidates.extend(_urls_from_value(mapping.get(key)))
    return _unique_strings(candidates)


def _extract_title(note):
    value = _mapping_value(
        note,
        ("title", "displayTitle", "display_title", "noteTitle", "note_title", "desc", "description"),
    )
    return normalize_xhs_state_text(value, max_length=XHS_MAX_TITLE_LENGTH)


def _extract_author(note):
    user = _mapping_value(note, ("user", "userInfo", "user_info", "author"))
    if isinstance(user, dict):
        value = _mapping_value(user, ("nickname", "nickName", "nick_name", "name"))
    else:
        value = user
    return normalize_xhs_state_text(value, max_length=XHS_MAX_AUTHOR_LENGTH)


def _extract_post_type(note, image_urls, video_urls):
    raw_value = str(_mapping_value(note, ("type", "noteType", "note_type")) or "").lower()
    if raw_value in {"video", "video_note"} or video_urls:
        return "video"
    if raw_value in {"normal", "image", "images", "image_note"} or image_urls:
        return "image"
    return "unknown"


def _normalize_page_state(value):
    token = _key_token(value)
    aliases = {
        "available": "available",
        "public": "available",
        "normal": "available",
        "success": "available",
        "loginrequired": "login_required",
        "needlogin": "login_required",
        "private": "private",
        "privatenote": "private",
        "nopermission": "private",
        "deleted": "deleted",
        "deletednote": "deleted",
        "notfound": "deleted",
        "unavailable": "unavailable",
        "forbidden": "unavailable",
        "blocked": "unavailable",
    }
    return aliases.get(token, "unknown")


def _mapping_page_state(mapping, *, allow_status=True):
    keys = ["pageState", "page_state", "noteStatus", "note_status", "availability"]
    if allow_status:
        keys.append("status")
    for key in keys:
        state = _normalize_page_state(mapping.get(key))
        if state != "unknown":
            return state
    return "unknown"


def _extract_explicit_page_state(payloads):
    for payload in payloads:
        for mapping, path in _iter_nodes(payload):
            path_tokens = {_key_token(part) for part in path}
            if len(path) > 4 or path_tokens & {
                "feed",
                "feeds",
                "recommend",
                "recommendations",
                "notedetailmap",
            }:
                continue
            allow_status = bool(path_tokens & {"error", "page"})
            state = _mapping_page_state(mapping, allow_status=allow_status)
            if state != "unknown":
                return state
    return "unknown"


def extract_xhs_page_state(soup, source_url=None):
    source_note_id = extract_xhs_note_id(source_url)
    payloads = tuple(_iter_script_payloads(soup, source_note_id=source_note_id))
    (
        note,
        selected_note_id,
        note_candidate_found,
        other_note_candidate_found,
    ) = _select_current_note(payloads, source_note_id)
    explicit_page_state = _extract_explicit_page_state(payloads)
    if note is None:
        return XhsPageState(
            note_id=source_note_id,
            page_state=explicit_page_state,
            explicit_page_state=explicit_page_state,
            structured_state_found=explicit_page_state != "unknown",
            structured_payload_found=bool(payloads),
            note_candidate_found=note_candidate_found,
            other_note_candidate_found=other_note_candidate_found,
            terminal_page_state=explicit_page_state in XHS_TERMINAL_PAGE_STATES,
        )

    image_urls = _extract_image_urls(note)
    video_urls, video_size_bytes = _extract_video_details(note)
    current_note_page_state = _mapping_page_state(note)
    page_state = current_note_page_state
    if page_state == "unknown":
        page_state = explicit_page_state
    if page_state == "unknown":
        page_state = "available"

    return XhsPageState(
        note_id=selected_note_id or source_note_id,
        title=_extract_title(note),
        author_name=_extract_author(note),
        post_type=_extract_post_type(note, image_urls, video_urls),
        image_urls=image_urls,
        video_urls=video_urls,
        cover_urls=_extract_cover_urls(note),
        video_size_bytes=video_size_bytes,
        page_state=page_state,
        current_note_page_state=current_note_page_state,
        explicit_page_state=explicit_page_state,
        structured_state_found=True,
        structured_payload_found=True,
        current_note_found=True,
        note_candidate_found=note_candidate_found,
        other_note_candidate_found=other_note_candidate_found,
        structured_image_list_present=_structured_image_list_present(note),
        structured_video_present=_structured_video_present(note),
        terminal_page_state=(
            current_note_page_state in XHS_TERMINAL_PAGE_STATES
            or explicit_page_state in XHS_TERMINAL_PAGE_STATES
        ),
    )
