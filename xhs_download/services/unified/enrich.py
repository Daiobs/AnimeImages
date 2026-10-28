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
from ..pipeline import Kind, validate_work
from ..quality import parse_content_range_total
from .presentation import distinct_variants, grouped_item_ids, quality_label

SIZE_BUDGET_SECONDS = 3.0
SIZE_LINK_LIMIT = 6
SIZE_LINK_TIMEOUT = (2.0, 3.0)
SIZE_SOURCE = "probe:content_range"


def floor_work(results, config):
    """The floor Provider's Work from this run, normalized like a shown one, or None."""
    work = results.get(config.floor_provider) if config.floor_provider else None
    if work is None:
        return None
    work = replace(work, platform=config.platform, provider=config.floor_provider)
    return None if validate_work(work, platform_names=config.platform_names) else work


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
