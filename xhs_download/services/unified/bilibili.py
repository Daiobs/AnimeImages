"""Bilibili on the unified pipeline: Native qualities, admitted service fallback.

Native's playurl answers one quality per request and lists the others it
would serve this anonymous context (accept_quality). The other complete-file
qualities are asked for in the same context, so the page can offer each one.
Separate DASH tracks need a server merge and stay out of the browser path.

TianTian stays the existing availability fallback: it runs only for a
first-part video, only when Native has no result, and only for a request the
existing third-party admission allows (its limits are reserved when it runs).
"""
from dataclasses import replace
import logging
import time
from types import MappingProxyType

from ..pipeline import Failure, FailureKind, Kind, Work
from ..provider_adapters.bilibili import BilibiliAdapter as NativeBilibiliAdapter
from . import routes
from .errors import UnifiedParseError, failure_error
from .presentation import present, technical_details
from .runner import run_chain
from .tiantian import CachedTianTianAdapter, https_media, service_cover_images

logger = logging.getLogger(__name__)

PLATFORM = "bilibili"
PROVIDER_LABELS = {"native": "哔哩哔哩原生", "tiantianjiexi": "天天解析"}
SERVICE_IDENTITY_PREFIX = "bilibili_unified:"
PLAYURL = "https://api.bilibili.com/x/player/playurl"
MAX_OTHER_QUALITIES = 3
# Leave time to map and present what was fetched.
OTHER_QUALITY_MARGIN_SECONDS = 0.3


def other_qualities(raw, deadline):
    """The other complete-file qualities playurl lists, each asked for once."""
    from ..platforms.bilibili import anonymous_cookie_context, api_get_json

    play = (raw.get("playurl") or {}).get("data") or {}
    selection = raw.get("selection") or {}
    params = selection.get("playurl_params")
    if not isinstance(params, dict) or len(play.get("durl") or []) != 1:
        return []
    returned = play.get("quality")
    wanted = [quality for quality in play.get("accept_quality") or ()
              if type(quality) is int and quality != returned][:MAX_OTHER_QUALITIES]
    if not wanted or time.monotonic() >= deadline - OTHER_QUALITY_MARGIN_SECONDS:
        return []
    found = []
    try:
        cookies = anonymous_cookie_context(deadline_monotonic=deadline)
        for quality in wanted:
            if time.monotonic() >= deadline - OTHER_QUALITY_MARGIN_SECONDS:
                break
            data = api_get_json(PLAYURL, {**params, "qn": quality}, referer=selection.get("page_url") or
                                "https://www.bilibili.com/", deadline_monotonic=deadline - OTHER_QUALITY_MARGIN_SECONDS,
                                cookies=cookies, retries=1)
            if data.get("quality") == quality and len(data.get("durl") or []) == 1:
                found.append((quality, {"code": 0, "data": data}))
    except Exception as error:
        # The quality already fetched still stands; never retry into a rate limit.
        logger.info("unified_bilibili_other_quality_stopped error_type=%s", type(error).__name__)
    return found


def with_rendition(work, other, quality):
    """Add another quality's file to the video item, as variants of the same work."""
    target = next((item for item in work.items if item.kind is Kind.VIDEO), None)
    source = next((item for item in other.items if item.kind is Kind.VIDEO), None)
    if target is None or source is None:
        return work
    present_urls = {variant.url for variant in target.variants}
    sources, added = dict(work.field_sources), []
    for ordinal, variant in enumerate(source.variants, 1):
        if variant.url in present_urls:
            continue
        variant_id = f"{target.item_id}:q{quality}:{ordinal}"
        prefix = f"items.{source.item_id}.variants.{variant.variant_id}."
        for key, value in other.field_sources.items():
            if key.startswith(prefix):
                sources[f"items.{target.item_id}.variants.{variant_id}." + key[len(prefix):]] = value
        added.append(replace(variant, variant_id=variant_id))
    if not added:
        return work
    items = tuple(replace(item, variants=(*item.variants, *added)) if item is target else item for item in work.items)
    return replace(work, items=items, field_sources=MappingProxyType(sources))


def bilibili_https(work):
    from ..platforms.bilibili import BILIBILI_MEDIA_HOST_SUFFIXES

    return https_media(work, BILIBILI_MEDIA_HOST_SUFFIXES)


class NativeAdapter(NativeBilibiliAdapter):
    def fetch(self, source_url, budget):
        started = time.monotonic()
        raw = super().fetch(source_url, budget)
        if isinstance(raw, dict) and raw.get("playurl"):
            raw = {**raw, "other_playurls": other_qualities(raw, started + budget)}
        return raw

    def map(self, raw, source_url):
        others = raw.get("other_playurls", ()) if isinstance(raw, dict) else ()
        base = {key: value for key, value in raw.items() if key != "other_playurls"} if isinstance(raw, dict) else raw
        work = super().map(base, source_url)
        if isinstance(work, Failure):
            return work
        params = (base.get("selection") or {}).get("playurl_params") or {}
        for quality, response in others:
            selection = {**base["selection"], "playurl_params": {**params, "qn": quality}}
            other = super().map({**base, "playurl": response, "selection": selection}, source_url)
            if isinstance(other, Work):
                work = with_rendition(work, other, quality)
        return bilibili_https(work)


class ServiceTianTian(CachedTianTianAdapter):
    def map(self, raw, source_url):
        work = super().map(raw, source_url)
        return work if isinstance(work, Failure) else bilibili_https(service_cover_images(work))


class AdmittedFallback:
    def __init__(self, authorize):
        self.authorize = authorize

    def parse(self, source_url, budget):
        from ..platforms.bilibili_fallback import PROVIDER, canonical_video_source

        canonical = canonical_video_source(source_url)
        if not canonical:
            return Failure(FailureKind.UNSUPPORTED, "BILIBILI_SERVICE_SOURCE")
        started = time.monotonic()
        # Reserves the existing third-party limits; only reached once Native has failed.
        if PROVIDER not in (self.authorize() or ()):
            return Failure(FailureKind.UNSUPPORTED, "BILIBILI_SERVICE_NOT_ADMITTED")
        remaining = budget - (time.monotonic() - started)
        if remaining <= 0.1:
            return Failure(FailureKind.TIMEOUT, "ADAPTER_BUDGET_EXHAUSTED")
        identity = canonical.rstrip("/").rsplit("/", 1)[-1]
        return ServiceTianTian(source_identity=SERVICE_IDENTITY_PREFIX + identity).parse(canonical, remaining)


def bilibili_media_only(work):
    """Keep only media the existing Bilibili policy accepts (Akamai mirrors included)."""
    from ..platforms.bilibili_service_result import validate_media_url

    def allowed(url):
        try:
            validate_media_url(url)
        except (TypeError, ValueError):
            return False
        return True

    items = []
    for item in work.items:
        variants = tuple(variant for variant in item.variants if allowed(variant.url))
        if variants:
            items.append(replace(item, variants=variants))
    return replace(work, items=tuple(items))


def build_adapters(authorize):
    adapters = {"native": NativeAdapter()}
    if authorize is not None:
        adapters["tiantianjiexi"] = AdmittedFallback(authorize)
    return adapters


def parse(url, route, *, authorize=None):
    """Parse one Bilibili link on its route; returns the existing ParseResult shape."""
    from ..platforms.bilibili import BilibiliAdapter as Platform

    started = time.monotonic()
    platform_adapter = Platform()
    platform_adapter.require_supported_url(url)
    adapters = build_adapters(authorize)
    config = routes.chain_config(route, adapters)
    run = run_chain(config, adapters, {name: url for name in adapters}, routes.cooldowns(route))
    attempts = [{"provider": attempt.provider, "outcome": attempt.outcome, "code": attempt.code}
                for attempt in run.attempts]
    logger.info("unified_chain_finished platform=%s policy=%s shown=%s failure=%s attempts=%s duration_ms=%d",
                PLATFORM, config.policy.value, getattr(run.shown, "provider", ""),
                getattr(run.failure, "code", ""),
                ",".join(f"{a['provider']}:{a['outcome']}" for a in attempts),
                int((time.monotonic() - started) * 1000))
    if run.shown is None:
        raise failure_error(run.failure, "哔哩哔哩")
    work = bilibili_media_only(run.shown)
    result = present(
        work, platform_adapter=platform_adapter, provider_label=PROVIDER_LABELS.get(work.provider, work.provider),
        fallback_extensions={"video": "mp4", "image": "jpg"}, attempts=attempts,
        extra_metadata={"provider_total_duration_ms": int((time.monotonic() - started) * 1000),
                        "unified_diagnostics": technical_details(
                            run.shown, work, policy=config.policy.value, attempts=attempts,
                            duration_ms=(time.monotonic() - started) * 1000)},
    )
    if result is None:
        raise UnifiedParseError("哔哩哔哩解析失败，请稍后重试。", "BILIBILI_NO_PRESENTABLE_MEDIA")
    return result
