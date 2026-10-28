"""Facts the result page needs that the shown result lacks: lower qualities and sizes.

- A winner offering a single quality also lists the floor's qualities below it
  (Human, 2026-10-28: an exception to D-07 for single-quality winners, such as
  TianTian's lone 原画 for Douyin). Each option keeps its own Provider's links.
- A file without a size gets one from the first byte of its link (Range 0-0),
  asked in parallel within a short budget; a link that does not answer in time
  stays "大小未知" and is measured again when the user saves it.
"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import time
from types import MappingProxyType

from ..common import HEADERS, request_get_with_retry
from ..pipeline import Kind, Work, validate_work
from ..quality import parse_content_range_total
from .presentation import distinct_variants, grouped_item_ids, mirrors, quality_label

SIZE_BUDGET_SECONDS = 3.0
SIZE_LINK_LIMIT = 6
SIZE_LINK_TIMEOUT = (2.0, 3.0)
SIZE_SOURCE = "probe:content_range"


FLOOR_WAIT_SECONDS = 3.0


def floor_work(results, config):
    """The floor Provider's Work from this run, normalized like a shown one, or None."""
    work = results.get(config.floor_provider) if config.floor_provider else None
    if not isinstance(work, Work):
        return None
    work = replace(work, platform=config.platform, provider=config.floor_provider)
    return None if validate_work(work, platform_names=config.platform_names) else work


def wants_floor(shown, run, config):
    """A single-quality winner while the floor is still running: worth a short wait."""
    item = _single_video(shown) if shown is not None else None
    return bool(item is not None and config.floor_provider and shown.provider != config.floor_provider
                and config.floor_provider in run.running
                and len({quality_label(v) for v in distinct_variants(item)}) == 1)


def await_floor(results, config, *, timeout=FLOOR_WAIT_SECONDS, clock=time.monotonic, sleep=time.sleep):
    """Wait up to ``timeout`` for the floor's outcome (it finishes in the background)."""
    deadline = clock() + timeout
    while config.floor_provider not in results and clock() < deadline:
        sleep(0.05)
    return floor_work(results, config)


def _single_video(work):
    videos = [item for item in work.items if item.kind is Kind.VIDEO]
    if len(videos) != 1 or videos[0].item_id in grouped_item_ids(work):
        return None
    return videos[0]


def with_floor_qualities(shown, floor):
    """``shown`` with the floor's qualities below a single-quality winner."""
    if floor is None or floor.provider == shown.provider:
        return shown
    item, extra = _single_video(shown), _single_video(floor)
    if item is None or extra is None or len({quality_label(v) for v in distinct_variants(item)}) != 1:
        return shown
    known = {variant.url for variant in item.variants}
    sources, added = dict(shown.field_sources), []
    for variant in extra.variants:
        if variant.url in known:
            continue
        variant_id = f"{floor.provider}:{variant.variant_id}"
        source_prefix = f"items.{extra.item_id}.variants.{variant.variant_id}."
        target_prefix = f"items.{item.item_id}.variants.{variant_id}."
        for key, origin in floor.field_sources.items():
            if key.startswith(source_prefix):
                sources[target_prefix + key[len(source_prefix):]] = f"{floor.provider}:{origin}"
        added.append(replace(variant, variant_id=variant_id))
    if not added:
        return shown
    items = tuple(replace(entry, variants=(*entry.variants, *added)) if entry.item_id == item.item_id else entry
                  for entry in shown.items)
    return replace(shown, items=items, field_sources=MappingProxyType(sources))


def with_measured_sizes(work, *, request=request_get_with_retry, clock=time.monotonic):
    """``work`` with sizes for picker files that had none, as far as the budget allows."""
    grouped = grouped_item_ids(work)
    targets = []
    for item in work.items:
        if item.kind is not Kind.VIDEO or item.item_id in grouped:
            continue
        targets.extend(variant.url for variant in distinct_variants(item) if not variant.size_bytes)
    targets = list(dict.fromkeys(targets))[:SIZE_LINK_LIMIT]
    if not targets:
        return work
    deadline = clock() + SIZE_BUDGET_SECONDS
    headers = {key: value for key, value in HEADERS.items() if key.lower() not in {"referer", "cookie"}}
    headers["Range"] = "bytes=0-0"
    sizes = {}

    def measure(url):
        try:
            response = request(url, headers=headers, timeout=SIZE_LINK_TIMEOUT, retries=1, stream=True,
                               accepted_statuses=(206,), deadline_monotonic=deadline)
        except Exception:
            return
        try:
            total = parse_content_range_total(response.headers.get("Content-Range"))
            if total:
                sizes[url] = int(total)
        finally:
            response.close()

    with ThreadPoolExecutor(max_workers=len(targets), thread_name_prefix="unified-size") as pool:
        list(pool.map(measure, targets))
    if not sizes:
        return work
    items, sources = [], dict(work.field_sources)
    for item in work.items:
        variants = []
        for variant in item.variants:
            if not variant.size_bytes and variant.url in sizes:
                variant = replace(variant, size_bytes=sizes[variant.url], size_exact=True)
                prefix = f"items.{item.item_id}.variants.{variant.variant_id}."
                sources.update({prefix + "size_bytes": SIZE_SOURCE, prefix + "size_exact": SIZE_SOURCE})
            variants.append(variant)
        items.append(replace(item, variants=tuple(variants)))
    return replace(work, items=tuple(items), field_sources=MappingProxyType(sources))


QUALITY_TIERS = ("原画", "1080p", "720p", "540p")
# Portrait Douyin videos often top out at 576×1024; that file belongs to the 540p tier
# (Human, 2026-10-28) rather than leaving the picker.
TIER_ALIASES = {"576p": "540p"}


def _tier(variant):
    label = quality_label(variant)
    if variant.is_original or label == "原画":
        return "原画"
    label = TIER_ALIASES.get(label.lower(), label.lower())
    return label if label in QUALITY_TIERS else None


def with_quality_tiers(work):
    """One file per tier (原画, 1080p, 720p, 540p), the largest; other heights leave the picker.

    Human, 2026-10-28, for Douyin. Mirrors of a kept file stay. Returns the new
    Work and {url: reason} for the links taken out, for the technical panel.
    """
    grouped = grouped_item_ids(work)
    items, hidden = [], {}
    for item in work.items:
        files = distinct_variants(item)
        if item.kind is not Kind.VIDEO or item.item_id in grouped or len(files) < 2:
            items.append(item)
            continue
        best = {}
        for variant in files:
            tier = _tier(variant)
            if tier is None:
                continue
            held = best.get(tier)
            if held is None or (variant.size_bytes or 0, variant.bitrate or 0) > (held.size_bytes or 0, held.bitrate or 0):
                best[tier] = variant
        if not best:
            items.append(item)
            continue
        kept_urls = {url for variant in best.values() for url in mirrors(item, variant)}
        for variant in item.variants:
            if variant.url not in kept_urls:
                hidden[variant.url] = ("未展示：同一清晰度已有更大的文件" if _tier(variant) in best
                                       else "未展示：不在清晰度档位内")
        items.append(replace(item, variants=tuple(v for v in item.variants if v.url in kept_urls)))
    return replace(work, items=tuple(items)), hidden
