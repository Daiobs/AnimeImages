"""Douyin on the unified pipeline: identity, Providers, chain and result.

The route row decides the chain; Providers keep their existing admission,
quota, cache and timeouts. Facts the generic mappings cannot know about
Douyin are applied here, each the same rule the existing Douyin path uses.
"""
from dataclasses import replace
import logging
import time
from types import MappingProxyType

from django.conf import settings

from ..pipeline import Failure, FailureKind
from ..provider_adapters.douyin import DouyinAdapter as NativeDouyinAdapter
from ..provider_adapters.kukutool import KukutoolSidecarAdapter
from . import routes
from .errors import UnifiedParseError, failure_error as platform_failure_error
from .enrich import floor_work, with_floor_qualities, with_measured_sizes
from .presentation import present, technical_details
from .runner import run_chain
from .tiantian import CachedTianTianAdapter, service_video_facts

logger = logging.getLogger(__name__)

PLATFORM = "douyin"
PROVIDER_LABELS = {"tiantianjiexi": "天天解析", "kukutool": "KuKuTool", "native": "抖音原生"}
# Douyin's own watermarked copies of a work listed elsewhere without a watermark.
WATERMARKED_ORIGINS = (".download_addr.url_list", ".download_url_list")
# A namespace of its own: the shared TianTian cache keeps normalized payloads,
# while this mapping needs the raw response cached alongside it.
SERVICE_IDENTITY_PREFIX = "douyin_unified:"


def _sources_without(work, prefixes):
    return MappingProxyType({key: value for key, value in work.field_sources.items()
                             if not any(key.startswith(prefix) for prefix in prefixes)})


def without_watermarked_copies(work):
    """Drop variants first listed under download_addr / download_url_list.

    An item whose only variants are watermarked keeps them: a watermarked file
    is still better than losing the item.
    """
    items, dropped = [], []
    for item in work.items:
        kept = tuple(variant for variant in item.variants
                     if not work.field_sources.get(f"items.{item.item_id}.variants.{variant.variant_id}.url", "")
                     .endswith(WATERMARKED_ORIGINS))
        if kept and len(kept) < len(item.variants):
            dropped.extend(f"items.{item.item_id}.variants.{variant.variant_id}."
                           for variant in item.variants if variant not in kept)
            item = replace(item, variants=kept)
        items.append(item)
    if not dropped:
        return work
    return replace(work, items=tuple(items), field_sources=_sources_without(work, dropped))


def douyin_media_only(work):
    """Keep only media URLs the existing Douyin media policy accepts."""
    from ..platforms.douyin_media_policy import validate_douyin_media_url

    def allowed(url):
        try:
            validate_douyin_media_url(url)
        except (TypeError, ValueError):
            return False
        return True

    items = []
    for item in work.items:
        variants = tuple(variant for variant in item.variants if allowed(variant.url))
        if variants:
            items.append(replace(item, variants=variants))
    return replace(work, items=tuple(items), cover_url=work.cover_url if allowed(work.cover_url) else "")


def browser_cover(work, raw):
    """A cover the browser can show, picked the way the existing Native cards pick it.

    The mapping keeps the first cover link, which is often HEIC.
    """
    from ..platforms.douyin import find_aweme_by_id, get_video_cover_urls

    payload = raw.get("payload") if isinstance(raw, dict) and "page_url" in raw else raw
    aweme = find_aweme_by_id(payload, work.work_id) if isinstance(payload, (dict, list)) else {}
    _cover, preview = get_video_cover_urls((aweme or {}).get("video") or {})
    if not preview or preview == work.cover_url:
        return work
    sources = {**work.field_sources, "cover_url": "adapter:douyin_browser_cover", "cover": "adapter:douyin_browser_cover"}
    return replace(work, cover_url=preview, field_sources=MappingProxyType(sources))


class NativeAdapter(NativeDouyinAdapter):
    def map(self, raw, source_url):
        work = super().map(raw, source_url)
        return work if isinstance(work, Failure) else browser_cover(without_watermarked_copies(work), raw)


class ServiceTianTianAdapter(CachedTianTianAdapter):
    def map(self, raw, source_url):
        work = super().map(raw, source_url)
        return work if isinstance(work, Failure) else service_video_facts(work)


class ServiceKukutoolAdapter(KukutoolSidecarAdapter):
    def map(self, raw, source_url):
        work = super().map(raw, source_url)
        return work if isinstance(work, Failure) else service_video_facts(work)


def resolve_source(url):
    from ..platforms.douyin import DouyinAdapter
    from ..platforms.douyin_providers import select_tiantian_request_url

    adapter = DouyinAdapter()
    adapter.require_supported_url(url)
    deadline = time.monotonic() + max(2.0, float(getattr(settings, "DOUYIN_SHORT_LINK_TIMEOUT_SECONDS", 2.0) or 0))
    context = adapter.build_parse_context(url, fetch_direct_page=False, fast_short_link=True,
                                          request_deadline=deadline, probe_media_metadata=False)
    if not context.aweme_id:
        raise UnifiedParseError("没有识别到抖音作品，请检查链接后重试。", "DOUYIN_IDENTITY_MISSING")
    return adapter, context, select_tiantian_request_url(context)


def build_adapters(context, *, prefer_ultra_hd, attempt_controller):
    from ..provider_settings import get_platform_provider_settings

    adapters = {"native": NativeAdapter()}
    if prefer_ultra_hd:
        provider_settings = get_platform_provider_settings(PLATFORM)
        if provider_settings.provider_is_enabled("tiantianjiexi"):
            adapters["tiantianjiexi"] = ServiceTianTianAdapter(
                attempt_controller=attempt_controller,
                source_identity=SERVICE_IDENTITY_PREFIX + context.aweme_id)
        if (provider_settings.provider_is_enabled("kukutool")
                and getattr(settings, "KUKUTOOL_SIDECAR_BASE_URL", "")):
            adapters["kukutool"] = ServiceKukutoolAdapter()
    if attempt_controller is not None:
        # Only TianTian reports to the per-user quota, as on the existing path.
        attempt_controller.configure_providers({"tiantianjiexi"} & set(adapters))
    return adapters


def failure_error(failure):
    return platform_failure_error(failure, "抖音")


def parse(url, route, *, prefer_ultra_hd=False, attempt_controller=None):
    """Parse one Douyin link on its route; returns the existing ParseResult shape."""
    started = time.monotonic()
    platform_adapter, context, request_url = resolve_source(url)
    adapters = build_adapters(context, prefer_ultra_hd=prefer_ultra_hd, attempt_controller=attempt_controller)
    config = routes.chain_config(route, adapters)
    results = {}
    run = run_chain(config, adapters, {name: request_url for name in adapters}, routes.cooldowns(route),
                    results=results)
    attempts = [{"provider": attempt.provider, "outcome": attempt.outcome, "code": attempt.code}
                for attempt in run.attempts]
    logger.info("unified_chain_finished platform=%s policy=%s shown=%s failure=%s attempts=%s duration_ms=%d",
                PLATFORM, config.policy.value, getattr(run.shown, "provider", ""),
                getattr(run.failure, "code", ""),
                ",".join(f"{a['provider']}:{a['outcome']}" for a in attempts),
                int((time.monotonic() - started) * 1000))
    if run.shown is None:
        raise failure_error(run.failure)
    shown = with_floor_qualities(run.shown, floor_work(results, config))
    work = with_measured_sizes(douyin_media_only(shown))
    if not work.items:
        raise UnifiedParseError("抖音解析失败，请稍后重试。", "DOUYIN_NO_ALLOWED_MEDIA")
    notices = []
    if attempt_controller is not None and attempt_controller.denied:
        notices.append("超清解析请求过于频繁，本次返回普通清晰度。")
    result = present(
        work, platform_adapter=platform_adapter, provider_label=PROVIDER_LABELS.get(work.provider, work.provider),
        fallback_extensions={"video": "mp4", "image": "jpg"}, attempts=attempts, notices=notices,
        extra_metadata={"input_kind": context.input_kind, "aweme_identity_resolved": True,
                        "provider_total_duration_ms": int((time.monotonic() - started) * 1000),
                        "unified_diagnostics": technical_details(
                            shown, work, policy=config.policy.value, attempts=attempts,
                            duration_ms=(time.monotonic() - started) * 1000)},
    )
    if result is None:
        raise UnifiedParseError("抖音解析失败，请稍后重试。", "DOUYIN_NO_PRESENTABLE_MEDIA")
    return result
