"""Bilibili-only service projection, independent of Native download authority."""
from hashlib import sha256
import re
from urllib.parse import parse_qs, urlsplit

from ..assets import MediaAsset, ParseResult
from ..common import clean_filename, host_matches
from ..provider_manifest import build_provider_manifest_token, provider_manifest_is_valid
from .bilibili import BILIBILI_HEADERS, BILIBILI_MEDIA_HOST_SUFFIXES
from .kukutool_generic import ServiceFormatPayload, source_key_for_url
from .tiantianjiexi_generic import TianTianJieXiResultNormalizer, unwrap_api_payload

PROVIDER = "tiantianjiexi_generic"
ASSET_PREFIX = "bilibili:service:"
VIDEO_ID = re.compile(r"(?:BV[0-9A-Za-z]{10}|av[1-9][0-9]*)")


def source_identity(source_url):
    parsed = urlsplit(source_url)
    match = re.fullmatch(r"/video/((?:BV[0-9A-Za-z]{10}|av[1-9][0-9]*))/?", parsed.path)
    if (parsed.scheme != "https" or parsed.hostname != "www.bilibili.com"
            or parsed.username or parsed.password or parsed.port not in (None, 443)
            or not match or parse_qs(parsed.query).get("p", ["1"]) != ["1"]):
        raise ValueError("bilibili_service_source_invalid")
    return match[1]


def validate_media_url(url):
    """Projection-only host policy; the download consumer still checks DNS."""
    parsed = urlsplit(str(url or ""))
    if (parsed.scheme not in {"http", "https"} or not parsed.path.strip("/")
            or not host_matches(parsed.hostname, BILIBILI_MEDIA_HOST_SUFFIXES)
            or parsed.username is not None or parsed.password is not None
            or parsed.port not in (None, 443 if parsed.scheme == "https" else 80) or parsed.fragment
            or "\\" in url or any(ord(c) <= 32 or ord(c) == 127 for c in url)):
        raise ValueError("bilibili_service_media_invalid")
    return url


def _identity_evidence(payload, expected):
    evidence = []

    def check(value, depth=0, work_context=True):
        if depth > 10:
            raise ValueError("bilibili_service_payload_too_deep")
        if isinstance(value, (list, tuple)):
            for child in value:
                check(child, depth + 1, work_context=False)
        elif isinstance(value, dict):
            for key, child in value.items():
                identity = ""
                if key in {"bvid", "bv", "avid", "av", "aid", "video_id", "videoId"}:
                    text = str(child or "")
                    if key in {"aid", "avid", "av"} and text.isdigit():
                        text = "av" + text
                    if VIDEO_ID.fullmatch(text):
                        identity = text
                if work_context and key == "p" and str(child or "").isdigit():
                    if int(child) != 1:
                        raise ValueError("bilibili_service_page_mismatch")
                    evidence.append("payload_page")
                if key in {"source_url", "sourceUrl", "canonical_url", "canonicalUrl",
                           "share_url", "shareUrl", "link", "url"} and isinstance(child, str):
                    parsed = urlsplit(child)
                    if host_matches(parsed.hostname, ("bilibili.com",)):
                        match = re.fullmatch(r"/video/([^/]+)/?", parsed.path)
                        if match:
                            identity = match[1]
                            if parse_qs(parsed.query).get("p", ["1"]) != ["1"]:
                                raise ValueError("bilibili_service_page_mismatch")
                if identity:
                    # BV and AV can identify the same work. Without a verified
                    # mapping, only a same-kind identity is comparable.
                    if identity[:2] == expected[:2]:
                        if identity != expected:
                            raise ValueError("bilibili_service_identity_mismatch")
                        evidence.append("payload_identity")
                if isinstance(child, (dict, list, tuple)):
                    check(child, depth + 1, work_context=(
                        work_context and key in {"data", "result", "detail", "info"}))

    check(payload)
    return "payload_identity" if "payload_identity" in evidence else "request_context"


def manifest_item_is_allowed(item):
    """Validate only this result namespace, never a Native Bilibili item."""
    if not isinstance(item, dict):
        return False
    parts = str(item.get("asset_id") or "").split(":")
    if (len(parts) != 6 or parts[:2] != ["bilibili", "service"]
            or not VIDEO_ID.fullmatch(parts[2]) or parts[3:5] != ["p1", "video"]
            or not re.fullmatch(r"[0-9a-f]{24}", parts[5])
            or item.get("type") != "video" or item.get("source_platform") != "bilibili"
            or item.get("provider") != PROVIDER):
        return False
    source_key = source_key_for_url(f"https://www.bilibili.com/video/{parts[2]}/")
    if (item.get("provider_resource_id") != source_key
            or item.get("provider_cache_key") != f"bilibili:service:{PROVIDER}:{source_key}"):
        return False
    try:
        validate_media_url(item.get("url"))
    except (TypeError, ValueError):
        return False
    return provider_manifest_is_valid(item)


def source_url_for_item(item):
    """Recover the signed source for the caller's current permission check."""
    if not manifest_item_is_allowed(item):
        return ""
    return f"https://www.bilibili.com/video/{item['asset_id'].split(':')[2]}/"


def video_resource_is_complete(row):
    from ..ytdlp_selection import VIDEO_EXTENSIONS, context_required
    from .gallerydl_generic import extension_from_url
    url = row["url"]
    extension = str(row.get("extension") or row.get("ext") or extension_from_url(url)).lower().lstrip(".")
    return not (
        row.get("requires_merge") or row.get("has_drm") or row.get("requires_context")
        or context_required(row.get("http_headers") or row.get("headers"),
                            headers_present=row.get("headers_present"),
                            requires_cookie=row.get("requires_cookie"))
        or row.get("audio_tracks") == "absent" or row.get("video_tracks") == "absent"
        or str(row.get("acodec") or "").lower() == "none"
        or str(row.get("vcodec") or "").lower() == "none"
        or any(marker in str(row.get("protocol") or "").lower() for marker in ("dash", "m3u8"))
        or urlsplit(url).path.lower().endswith((".m3u8", ".mpd", ".m4s"))
        or (extension and extension not in VIDEO_EXTENSIONS))


def build_service_result(payload, source_url):
    from ..service_format_selection import attach_service_picker

    identity = source_identity(source_url)
    source_url = f"https://www.bilibili.com/video/{identity}/"
    identity_basis = _identity_evidence(payload, identity)
    normalizer = TianTianJieXiResultNormalizer(platform_id="bilibili")
    normalized = normalizer.normalize_payload(unwrap_api_payload(payload))
    title = clean_filename(str(normalized.get("title") or normalized.get("description") or identity))
    rows = []
    for row in normalized.get("_service_format_rows", ()):
        if row.get("media_type") != "video":
            continue
        try:
            validate_media_url(row.get("url"))
        except (TypeError, ValueError):
            continue
        rows.append({**row, "work_label": title})
    if not rows:
        raise ValueError("bilibili_service_no_video")
    try:
        cover = validate_media_url(normalized.get("cover"))
    except (TypeError, ValueError):
        cover = ""
    source_key = source_key_for_url(source_url)
    assets = []
    seen = set()
    for row in rows:
        url = row["url"]
        if not video_resource_is_complete(row):
            continue
        binding = (row.get("work_id"), url)
        if binding in seen:
            continue
        seen.add(binding)
        digest = sha256(f"{binding[0]}\n{url}".encode()).hexdigest()[:24]
        fields = {
            "asset_id": f"{ASSET_PREFIX}{identity}:p1:video:{digest}",
            "provider": PROVIDER,
            "provider_resource_id": source_key,
            "provider_cache_key": f"bilibili:service:{PROVIDER}:{source_key}",
            "source_platform": "bilibili",
        }
        token = build_provider_manifest_token(**fields, url=url, media_type="video")
        assets.append(MediaAsset(
            media_type="video", platform="bilibili", url=url, cover_url=cover,
            cover_preview_url=cover, quality_label=row.get("quality") or "video",
            size_bytes=row.get("size_bytes") or 0, size_is_estimate=bool(row.get("size_is_estimate")),
            headers=dict(BILIBILI_HEADERS), media_host_suffixes=BILIBILI_MEDIA_HOST_SUFFIXES,
            provider_manifest_token=token, **fields))
    if not assets:
        raise ValueError("bilibili_service_no_complete_video")
    result = ParseResult(
        platform="bilibili", post_type="video", base_filename=title, assets=tuple(assets),
        metadata={"source_platform": "bilibili", "source_provider": PROVIDER,
                  "selected_provider": PROVIDER, "canonical_source_url": source_url,
                  "bilibili_video_id": identity, "bilibili_page": 1,
                  "bilibili_service_identity_basis": identity_basis,
                  "description": normalized.get("description") or ""})
    filtered = ServiceFormatPayload({**normalized, "title": title, "cover": cover,
                                     "_service_format_rows": rows})
    return attach_service_picker(result, payload=filtered, provider=PROVIDER, source_url=source_url)
