"""A unified Work on the existing result page (§3 delivery rules, §4 filenames).

Photos become the existing image cards. Each video item becomes one quality
picker listing the displayed result's variants; the picker's selection
endpoint hands the browser an ordinary video item, so saving goes through the
existing browser download (Service Worker streaming, then blob). The motion of
a live photo stays a video card beside the photos, not a picker of its own.

Selection records stay on the server; the page only holds signed references.
"""
import math
import time
from urllib.parse import urlsplit
from uuid import uuid4

from django.core import signing
from django.core.cache import cache
from django.urls import reverse

from ..assets import MediaAsset, ParseResult
from ..pipeline import (
    DEFAULT_QUALITY_LABEL, Kind, Method, UnknownExtension, base_name, decide_delivery, file_name,
)
from ..pipeline.filenames import item_numbers, sanitize
from ..ytdlp_selection import size_label

SCHEMA = "unified_selected_format_v1"
PROVIDER_ID = "unified"
SALT = "unified-selection-v1"
BUNDLE_PREFIX = "unified:selection-bundle:"
RECORD_TTL_SECONDS = 1800
MIN_RECORD_TTL_SECONDS = 60
BROWSER_METHODS = frozenset({Method.BROWSER_STREAMING, Method.BROWSER_BLOB, Method.BROWSER_DIRECT})
LIMITATIONS = {
    "DRM_PROTECTED": "此格式有 DRM 保护，不提供下载。",
    "SEGMENTED_STREAM": "此格式是分片流，当前不提供下载。",
    "UNSUPPORTED_REQUEST": "此格式需要特殊请求方式，不提供下载。",
    "NEEDS_AUDIO_MERGE": "此格式只有画面，需要合并音频，当前不提供下载。",
    "BROWSER_CANNOT_READ": "浏览器无法读取此格式。",
    "SERVER_DELIVERY": "此格式需要服务器转交，当前不提供下载。",
}


def quality_label(variant):
    if variant.quality_label:
        return variant.quality_label
    if variant.is_original:
        return "原画"
    if variant.height and variant.width:
        return f"{min(variant.height, variant.width)}p"
    if variant.height:
        return f"{variant.height}p"
    return DEFAULT_QUALITY_LABEL


def detail_label(variant):
    facts = []
    if variant.width and variant.height:
        facts.append(f"{variant.width}×{variant.height}")
    if variant.bitrate:
        facts.append(f"{variant.bitrate / 1000:.0f} kbps")
    if variant.container:
        facts.append(variant.container.upper())
    return " · ".join(facts)


def host_allowed(variant):
    """An https link with a host. Which hosts a platform serves from is decided
    before presentation by its own media policy (douyin_media_only and the
    like), which knows reviewed exact hosts and mirror patterns that a plain
    suffix list does not (v3-dy-a-x.ixigua.com, Bilibili's Akamai mirrors)."""
    parts = urlsplit(variant.url)
    return parts.scheme == "https" and bool(parts.hostname)


MAX_MIRRORS = 4


def _file_key(variant):
    return ((variant.height, variant.width, variant.size_bytes)
            if variant.size_bytes and variant.size_exact else ("url", variant.url))


def distinct_variants(item):
    """One entry per file: mirrors of one file share its exact size and dimensions.

    A link that says less about the same file (no bitrate, say) is still that file.
    """
    kept = {}
    for variant in item.variants:
        if host_allowed(variant):
            kept.setdefault(_file_key(variant), variant)
    return list(kept.values())


def mirrors(item, variant):
    """The variant's URL first, then other links to the same file, in adapter order.

    Hosts of one CDN differ in whether a browser may read them (CORS), so the
    selection endpoint picks among these when the user saves the file.
    """
    key = _file_key(variant)
    urls = [variant.url] + [other.url for other in item.variants
                            if other.url != variant.url and host_allowed(other) and _file_key(other) == key]
    return list(dict.fromkeys(urls))[:MAX_MIRRORS]


def download_name(work, item, variant, fallback_extensions):
    try:
        return file_name(work, item, variant)
    except UnknownExtension:
        number = item_numbers(work)[item.item_id]
        stem = base_name(work) + (f"_{number}" if number is not None else "")
        return f"{stem}.{fallback_extensions.get(item.kind.value, 'bin')}"


def delivery_facts(item, variant):
    delivery = decide_delivery(item.kind, variant, allow_server_fallback=False)
    if delivery.method is Method.NONE:
        return False, LIMITATIONS.get(delivery.reason, LIMITATIONS["BROWSER_CANNOT_READ"]), delivery
    if delivery.method not in BROWSER_METHODS:
        return False, LIMITATIONS["SERVER_DELIVERY"], delivery
    return True, "", delivery


def _epoch(value):
    """Variant expiry is a datetime; records keep epoch seconds."""
    return value.timestamp() if value is not None else None


def _record_ttl(variants, now):
    expiries = [_epoch(variant.expires_at) for variant in variants if variant.expires_at]
    ttl = RECORD_TTL_SECONDS
    if expiries:
        ttl = min(ttl, int(min(expiries) - now))
    return max(MIN_RECORD_TTL_SECONDS, ttl)


def _token(candidate_id, bundle_id):
    return signing.dumps({"id": candidate_id, "bundle": bundle_id}, salt=SALT)


def grouped_item_ids(work):
    return {item_id for group in work.groups for item_id in group.item_ids}


def build_picker(work, fallback_extensions, *, now=None):
    """Picker groups (one per ungrouped video item) plus the server-side selection records."""
    now = time.time() if now is None else now
    bundle_id = uuid4().hex
    records, groups, defaults = {}, [], []
    grouped = grouped_item_ids(work)
    video_items = sorted((item for item in work.items if item.kind is Kind.VIDEO and item.item_id not in grouped),
                         key=lambda item: item.index)
    for position, item in enumerate(video_items, 1):
        variants = distinct_variants(item)
        if not variants:
            continue
        default = item.default_variant
        if default not in variants:
            default = max(variants, key=lambda variant: variant.rank_key())
        choices = []
        for variant in sorted(variants, key=lambda variant: variant.rank_key(), reverse=True):
            candidate_id = uuid4().hex
            can_download, limitation, delivery = delivery_facts(item, variant)
            records[candidate_id] = {
                "v": 1, "platform": work.platform, "provider": work.provider, "work_id": work.work_id,
                "source_url": work.source_url, "item_id": item.item_id, "variant_id": variant.variant_id,
                "kind": item.kind.value, "url": variant.url, "mirrors": mirrors(item, variant),
                "filename": download_name(work, item, variant, fallback_extensions),
                "size_bytes": int(variant.size_bytes or 0), "size_exact": bool(variant.size_exact),
                "label": quality_label(variant), "expires_at": _epoch(variant.expires_at),
                "method": delivery.method.value, "fallback": delivery.fallback.value if delivery.fallback else "",
                "rule": delivery.rule, "can_download": can_download,
            }
            facts = {"size_bytes": variant.size_bytes or None, "size_is_estimate": not variant.size_exact}
            choices.append({
                "candidate_id": candidate_id, "resource_token": _token(candidate_id, bundle_id),
                "format_id": variant.variant_id, "media_type": "video",
                "label": quality_label(variant), "variant_label": detail_label(variant),
                "detail_label": detail_label(variant), "size_label": size_label(facts),
                "size_bytes": variant.size_bytes, "size_is_estimate": not variant.size_exact,
                "height": variant.height, "width": variant.width,
                "can_download": can_download, "can_legacy_download": False, "legacy_asset_id": "",
                "can_preview": False, "can_check": False, "can_open": can_download,
                "limitation": limitation, "selected": variant is default, "initial_visible": True,
            })
        selected = next(choice for choice in choices if choice["selected"])
        groups.append({
            "provider_id": PROVIDER_ID, "selection_schema": SCHEMA, "unified": True, "service_picker": False,
            "item_id": item.item_id, "work": {"title": work.title, "cover_url": work.cover_url,
                                              "cover_available": bool(work.cover_url)},
            "videos": choices, "audios": [], "selected": selected, "first": selected, "preview": None,
            "is_audio": False, "video_primary": choices, "video_other": [], "audio_primary": [], "audio_other": [],
            "all_unavailable": not any(choice["can_download"] for choice in choices),
            "group_id": f"unified-{bundle_id[:12]}-{position}", "video_group_count": 1,
            "format_count": len(choices), "legacy_video": None, "cover_video": None,
        })
        defaults.append((item, default))
    if not groups:
        return None, {}, ()
    presentation = dict(groups[0]) if len(groups) == 1 else {**groups[0], "groups": groups}
    presentation["video_group_count"] = len(groups)
    presentation["video_resource_count"] = len(groups)
    presentation["represented_urls"] = [variant.url for _item, variant in defaults]
    presentation["selection_url"] = reverse("service_selected_format")
    for group in groups:
        group["selection_url"] = presentation["selection_url"]
    ttl = _record_ttl([variant for item in video_items for variant in item.variants], now)
    cache.set(BUNDLE_PREFIX + bundle_id, records, timeout=ttl)
    return presentation, records, tuple(defaults)


def picker_with_covers(picker, video_items):
    """A display copy of the stored picker, each group showing its video's cover card.

    The video cards are the default variants, built in group order.
    """
    if not isinstance(picker, dict) or not picker.get("unified"):
        return None
    groups = [dict(group) for group in (picker.get("groups") or [picker])]
    for index, group in enumerate(groups):
        group.pop("groups", None)
        group["cover_video"] = video_items[index] if index < len(video_items) else None
    represented = picker.get("represented_urls", [])
    if len(groups) == 1 and "groups" not in picker:
        return {**groups[0], "video_resource_count": picker.get("video_resource_count", 1),
                "represented_urls": represented}
    return {**groups[0], "groups": groups, "video_group_count": picker.get("video_group_count", len(groups)),
            "video_resource_count": picker.get("video_resource_count", len(groups)),
            "selection_url": picker.get("selection_url"), "represented_urls": represented}


TECHNICAL_ROW_LIMIT = 200


def technical_details(shown, kept, *, policy, attempts, duration_ms, hidden=None):
    """Staff facts for the result page's technical panel (hosts only, never links).

    The chain's attempts, then every link the shown Provider returned and why
    a link is or is not offered: ``kept`` is ``shown`` after the platform's
    media policy, before the result page's save mode. ``hidden`` maps a link
    the platform left out of the picker to its reason.
    """
    hidden = hidden or {}
    kept_items = {item.item_id: item for item in kept.items}
    grouped = grouped_item_ids(kept)
    rows = []
    for item in sorted(shown.items, key=lambda item: item.index):
        kept_item = kept_items.get(item.item_id)
        kept_urls = {variant.url for variant in kept_item.variants} if kept_item else set()
        offered = distinct_variants(kept_item) if kept_item else []
        # A photo or a live photo's motion is one card; a video lists every file in its picker.
        card = kept_item is not None and (kept_item.kind is Kind.IMAGE or kept_item.item_id in grouped)
        shown_variant = (kept_item.default_variant if kept_item and kept_item.default_variant in offered
                         else (offered or [None])[0])
        for variant in item.variants:
            if variant.url in hidden:
                status = hidden[variant.url]
            elif variant.url not in kept_urls:
                status = "平台媒体策略未放行"
            elif not host_allowed(variant):
                status = "链接不是 https"
            elif variant not in offered:
                status = "同一文件的镜像"
            elif card:
                status = "展示为卡片" if variant == shown_variant else "未展示：同一内容的其他版本"
            else:
                can_download, limitation, _delivery = delivery_facts(kept_item, variant)
                status = "可下载" if can_download else f"不提供下载：{limitation}"
            rows.append({
                "item": f"{item.kind.value} {item.index + 1}", "label": quality_label(variant),
                "container": variant.container, "width": variant.width, "height": variant.height,
                "size_bytes": variant.size_bytes, "size_exact": variant.size_exact,
                "host": urlsplit(variant.url).hostname or "", "status": status,
            })
    return {
        "policy": policy, "shown": shown.provider, "duration_ms": int(duration_ms),
        "attempts": [{"provider": a["provider"], "outcome": a["outcome"], "code": a.get("code") or ""}
                     for a in attempts],
        "variants": rows[:TECHNICAL_ROW_LIMIT], "variants_truncated": max(0, len(rows) - TECHNICAL_ROW_LIMIT),
    }


def present(work, *, platform_adapter, provider_label, fallback_extensions, attempts=(), notices=(),
            extra_metadata=None):
    """The existing ParseResult shape for one unified Work."""
    common = {"platform": work.platform, "headers": platform_adapter.headers,
              "media_host_suffixes": platform_adapter.media_host_suffixes}
    assets, numbers = [], {}
    for item in sorted(work.items, key=lambda item: item.index):
        if item.kind is not Kind.IMAGE:
            continue
        variants = distinct_variants(item)
        variant = item.default_variant if item.default_variant in variants else (variants or [None])[0]
        if variant is None:
            continue
        assets.append(MediaAsset(media_type="image", url=variant.url, preview_url=variant.url,
                                 ext=variant.container, **common))
        numbers[item.item_id] = len(assets)
    picker, _records, defaults = build_picker(work, fallback_extensions)
    grouped = grouped_item_ids(work)
    motions = [(item, item.default_variant if item.default_variant in distinct_variants(item)
                else (distinct_variants(item) or [None])[0])
               for item in sorted(work.items, key=lambda item: item.index)
               if item.kind is Kind.VIDEO and item.item_id in grouped]
    # A live photo's motion shows its own still as the cover and shares its number (§4).
    still, number = {}, {}
    for group in work.groups:
        stills = [work_item for work_item in work.items if work_item.item_id in group.item_ids
                  and work_item.kind is Kind.IMAGE and work_item.default_variant is not None]
        for item_id in group.item_ids:
            if stills:
                still[item_id] = stills[0].default_variant.url
                number[item_id] = numbers.get(stills[0].item_id)
    video_numbers = {}
    # Picker defaults first: picker_with_covers pairs groups with these cards in order.
    for item, variant in [*defaults, *motions]:
        if variant is None:
            continue
        cover = still.get(item.item_id, work.cover_url)
        if number.get(item.item_id):
            video_numbers[variant.url] = number[item.item_id]
        assets.append(MediaAsset(
            media_type="video", url=variant.url, cover_url=cover, cover_preview_url=cover,
            quality_label=quality_label(variant), size_bytes=int(variant.size_bytes or 0),
            size_is_estimate=not variant.size_exact, ext=variant.container, **common))
    if not assets:
        return None
    # The Douyin display reads topics out of the text (douyin_caption_presentation);
    # the other platforms show metadata topics beside the description, so only once.
    topics = " ".join(f"#{topic}" for topic in work.topics) if work.platform == "douyin" else ""
    # A one-line caption maps to the same title and description; show it once.
    description = "" if work.description.strip() == work.title.strip() else work.description
    metadata = {
        "title": work.title, "description": "\n".join(part for part in (description, topics) if part),
        # Marks separated work text for the shared display (see build_result_display_context).
        "caption": "\n".join(part for part in (work.title, description) if part),
        "topics": list(work.topics),
        "author_name": work.author, "cover_url": work.cover_url, "work_id": work.work_id,
        "selected_provider": work.provider, "source_provider": work.provider, "provider_label": provider_label,
        "unified_pipeline": True, "unified_completeness": work.completeness.value,
        "pipeline_attempts": list(attempts), "unified_notices": list(notices),
        **(extra_metadata or {}),
    }
    if picker:
        metadata["unified_picker"] = picker
    if video_numbers:
        # Read by the result page's video cards (views.build_video_item).
        metadata["video_display_ordinals"] = video_numbers
    if work.duration_seconds is not None and math.isfinite(work.duration_seconds):
        metadata["duration_seconds"] = work.duration_seconds
    has_video = any(asset.media_type == "video" for asset in assets)
    has_image = any(asset.media_type == "image" for asset in assets)
    return ParseResult(
        platform=work.platform, post_type="video" if has_video and not has_image else "image",
        base_filename=sanitize(base_name(work)) or work.platform,
        assets=tuple(assets), metadata=metadata,
    )


def resolve_selection(token, candidate_id):
    """The issued record, or ValueError when it is unknown or expired."""
    payload = signing.loads(str(token), salt=SALT, max_age=RECORD_TTL_SECONDS)
    if (not isinstance(payload, dict) or set(payload) != {"id", "bundle"}
            or payload["id"] != str(candidate_id) or not isinstance(payload["bundle"], str)):
        raise ValueError("unified_selection_invalid")
    records = cache.get(BUNDLE_PREFIX + payload["bundle"])
    record = records.get(payload["id"]) if isinstance(records, dict) else None
    if not isinstance(record, dict) or record.get("v") != 1:
        raise ValueError("unified_selection_expired")
    if record.get("expires_at") and record["expires_at"] <= time.time():
        raise ValueError("unified_selection_expired")
    return {**record, "candidate_id": payload["id"]}


def link_item(record):
    """What "copy link" and "open" need; not a download authority."""
    return {"selection_schema": SCHEMA, "provider": PROVIDER_ID, "candidate_id": record["candidate_id"],
            "type": record["kind"], "selected_format_id": record["variant_id"],
            "filename": record["filename"], "url": record["url"]}


def download_item(record):
    """An ordinary browser video item for the selected variant.

    No selection binding: like the existing video cards, the browser fetches
    the public media URL itself; the server never relays the body here. Like
    them it names no Provider: on these items a Provider means a manifest the
    server must check, and the platform media policy is the authority here.
    """
    from ..provider_strategy import browser_streaming_resource_url_is_allowed

    if (not record.get("can_download") or record.get("kind") != "video"
            or Method(record["method"]) not in BROWSER_METHODS
            or not browser_streaming_resource_url_is_allowed(record["url"])):
        raise ValueError("unified_format_not_browser")
    return {
        "type": "video", "url": record["url"], "filename": record["filename"],
        "size_bytes": record["size_bytes"], "size_is_estimate": not record["size_exact"],
        "browser_direct_fetch_safe": True, "cors_expected": True,
        "requires_merge": False, "requires_cookie": False, "request_context_status": "no_known_requirement",
        "source_platform": record["platform"], "unified_provider": record["provider"],
        "asset_id": f"unified:{record['platform']}:{record['work_id']}:{record['variant_id']}",
        "candidate_id": record["candidate_id"], "selected_format_id": record["variant_id"],
        "unified_schema": SCHEMA, "selection_delivery": "ordinary",
    }
