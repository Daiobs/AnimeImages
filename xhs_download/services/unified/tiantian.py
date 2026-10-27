"""TianTian on the unified pipeline, shared by the platforms that route to it."""
from copy import deepcopy
from dataclasses import replace
import logging
import time
from types import MappingProxyType

from django.core.cache import cache

from ..pipeline import Failure, FailureKind, Kind
from ..provider_adapters.tiantian import TianTianAdapter

logger = logging.getLogger(__name__)

RAW_CACHE_PREFIX = "unified:tiantian-raw:"


class CachedTianTianAdapter(TianTianAdapter):
    """TianTian with the raw response cached beside the shared normalized cache.

    The existing TianTian cache keeps normalized payloads, while the mapping
    needs the raw response; give the adapter a source identity of its own so
    both caches fill and expire together.
    """

    def fetch(self, source_url, budget):
        from ..platforms.tiantianjiexi_generic import (
            TianTianJieXiGenericProvider, runtime_int, source_key_for_request,
        )
        started = time.monotonic()
        source_key = source_key_for_request(source_url, self.source_identity)
        raw_key = RAW_CACHE_PREFIX + source_key
        try:
            cached = cache.get(raw_key)
        except Exception:
            cached = None
        if isinstance(cached, dict):
            if self.attempt_controller is not None:
                self.attempt_controller.reuse_cached_result("tiantianjiexi", reason="unified_raw_cache")
            return deepcopy(cached)
        raw = super().fetch(source_url, budget)
        if isinstance(raw, Failure) and raw.code == "TIANTIAN_NO_RAW":
            # The normalized cache answered but the raw copy is gone; ask again
            # once, inside the same budget, without the per-request quota hook
            # that would treat the earlier cache answer as final.
            TianTianJieXiGenericProvider().delete_cached_payload(source_key)
            remaining = budget - (time.monotonic() - started)
            if remaining <= 0.1:
                return Failure(FailureKind.TIMEOUT, "ADAPTER_BUDGET_EXHAUSTED")
            retry = TianTianAdapter(session=self.session, source_identity=self.source_identity)
            raw = retry.fetch(source_url, remaining)
        if isinstance(raw, dict):
            try:
                cache.set(raw_key, deepcopy(raw),
                          timeout=max(1, runtime_int("TIANTIANJIEXI_PAYLOAD_CACHE_TTL_SECONDS", 120)))
            except Exception:
                logger.warning("unified_tiantian_raw_cache_write_failed")
        return raw


def service_video_facts(work):
    """TianTian responses, as the existing Douyin and XHS paths read them.

    Beside a video, the images are the work's cover, not photos; and a video
    whose links carry no quality facts is the original upload ("原始上传画质"),
    its links being mirrors of that one file.
    """
    if not any(item.kind is Kind.VIDEO for item in work.items):
        return work
    sources = dict(work.field_sources)
    cover = work.cover_url
    items = []
    removed = []
    for item in work.items:
        if item.kind is Kind.IMAGE:
            if not cover and item.default_variant is not None:
                cover = item.default_variant.url
                sources["cover_url"] = sources["cover"] = "adapter:service_cover_image"
            removed.append(f"items.{item.item_id}.")
            continue
        if item.kind is Kind.VIDEO and not any(variant.quality_label or variant.height or variant.is_original
                                               for variant in item.variants):
            item = replace(item, variants=tuple(replace(variant, is_original=True) for variant in item.variants))
            for variant in item.variants:
                sources[f"items.{item.item_id}.variants.{variant.variant_id}.is_original"] = (
                    "adapter:service_unlabeled_video")
        items.append(item)
    sources = {key: value for key, value in sources.items() if not any(key.startswith(prefix) for prefix in removed)}
    return replace(work, items=tuple(items), cover_url=cover, field_sources=MappingProxyType(sources))
