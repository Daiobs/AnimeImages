"""Xiaohongshu on the unified pipeline: Providers, admission, facts and result.

Native reads the note page once per request; that one result feeds both the
floor and the gate that decides whether a service Provider may run, so service
calls stay where the existing XHS path makes them:

- TianTian enhances an ordinary video note for a request admitted to 原画/超清;
- an admitted independent service (KuKuTool, TianTian) runs when Native has
  no result, as the existing independent fallback does.
"""
from dataclasses import replace
import logging
import threading
import time
from types import MappingProxyType
from urllib.parse import urlsplit

from ..pipeline import Failure, FailureKind, Kind, Variant, Work
from ..provider_adapters.kukutool import KukutoolAdapter
from ..provider_adapters.xiaohongshu import XiaohongshuAdapter as NativeXhsAdapter
from . import routes
from .errors import UnifiedParseError, failure_error
from .presentation import present
from .runner import run_chain
from .tiantian import CachedTianTianAdapter, service_video_facts

logger = logging.getLogger(__name__)

PLATFORM = "xiaohongshu"
PROVIDER_LABELS = {"tiantianjiexi": "天天解析", "kukutool": "KuKuTool", "native": "小红书原生"}
SERVICE_IDENTITY_PREFIX = "xiaohongshu_unified:"
ORIGINAL_SOURCE = "adapter:xhs_original_image_rule"


def with_original_images(work):
    """Add each photo's original, by the existing XHS image rule.

    The page lists a 1080-px WEBP (urlDefault) and its preview; the original
    JPEG lives at the address normalize_xhs_image_url derives from it. The
    row's width and height describe that original.
    """
    from ..platforms.xiaohongshu import normalize_xhs_image_url

    items, sources = [], dict(work.field_sources)
    for item in work.items:
        if item.kind is not Kind.IMAGE or any(variant.is_original for variant in item.variants):
            items.append(item)
            continue
        base = next((variant for variant in item.variants
                     if sources.get(f"items.{item.item_id}.variants.{variant.variant_id}.url", "").endswith(".urlDefault")),
                    item.variants[0])
        original_url = normalize_xhs_image_url(base.url)
        if not original_url or original_url in {variant.url for variant in item.variants}:
            items.append(item)
            continue
        variant_id = f"{item.item_id}:original"
        original = Variant(variant_id, original_url, container="jpg", width=base.width, height=base.height,
                           is_original=True, host_policy=base.host_policy)
        prefix = f"items.{item.item_id}.variants."
        sources.update({prefix + f"{variant_id}.{field}": ORIGINAL_SOURCE
                        for field in ("variant_id", "url", "container", "is_original")})
        for field in ("width", "height", "host_policy"):
            sources[prefix + f"{variant_id}.{field}"] = sources.get(prefix + f"{base.variant_id}.{field}", ORIGINAL_SOURCE)
        items.append(replace(item, variants=(original, *item.variants)))
    return replace(work, items=tuple(items), field_sources=MappingProxyType(sources))


def with_https_media(work):
    """XHS pages list http:// media links; the existing XHS path asks for https."""
    from ..common import host_matches
    from ..platforms.xiaohongshu import XHS_MEDIA_HOST_SUFFIXES

    items, sources = [], dict(work.field_sources)
    for item in work.items:
        variants = []
        for variant in item.variants:
            parts = urlsplit(variant.url)
            if parts.scheme == "http" and host_matches((parts.hostname or "").lower(), XHS_MEDIA_HOST_SUFFIXES):
                variant = replace(variant, url=parts._replace(scheme="https").geturl())
                key = f"items.{item.item_id}.variants.{variant.variant_id}.url"
                sources[key] = sources.get(key, "") + ":https"
            variants.append(variant)
        items.append(replace(item, variants=tuple(variants)))
    return replace(work, items=tuple(items), field_sources=MappingProxyType(sources))


def is_ordinary_video(work):
    """The note shape the existing enhancer accepts: one video, nothing else."""
    return len(work.items) == 1 and work.items[0].kind is Kind.VIDEO and not work.groups


def xhs_media_only(work):
    """Keep only media the existing XHS policies accept."""
    from ..platforms.xhs_image_download_authority import classify_xhs_image_candidate
    from ..platforms.xhs_tiantian_media_policy import xhs_tiantian_media_url_is_allowed
    from ..platforms.xiaohongshu import normalize_video_url

    def allowed(kind, url):
        if kind is Kind.IMAGE:
            return classify_xhs_image_candidate(url) is not None
        if work.provider == "native":
            return normalize_video_url(url) is not None
        return xhs_tiantian_media_url_is_allowed(url) or normalize_video_url(url) is not None

    items = []
    for item in work.items:
        variants = tuple(variant for variant in item.variants if allowed(item.kind, variant.url))
        if variants:
            items.append(replace(item, variants=variants))
    kept = {item.item_id for item in items}
    groups = tuple(group for group in work.groups if set(group.item_ids) <= kept)
    return replace(work, items=tuple(items), groups=groups)


class NativeOnce:
    """One Native page read per request, shared by the floor and the service gate."""

    def __init__(self, source_url):
        self.source_url = source_url
        self._lock = threading.Lock()
        self._done = threading.Event()
        self._started = False
        self._result = None

    def get(self, budget):
        with self._lock:
            owner = not self._started
            self._started = True
        if owner:
            try:
                result = NativeXhsAdapter().parse(self.source_url, budget)
                self._result = with_original_images(with_https_media(result)) if isinstance(result, Work) else result
            finally:
                self._done.set()
        elif not self._done.wait(timeout=max(0.0, budget)):
            return Failure(FailureKind.TIMEOUT, "XHS_NATIVE_PENDING")
        return self._result or Failure(FailureKind.INVALID_RESULT, "XHS_NATIVE_NO_RESULT")


class SharedNative:
    def __init__(self, once):
        self.once = once

    def parse(self, source_url, budget):
        return self.once.get(budget)


class XhsServiceTianTian(CachedTianTianAdapter):
    def map(self, raw, source_url):
        work = super().map(raw, source_url)
        return work if isinstance(work, Failure) else service_video_facts(work)


class GatedService:
    """A service Provider that runs only where the existing XHS path calls one."""

    def __init__(self, build, once, *, video_admitted, fallback_admitted):
        self.build = build
        self.once = once
        self.video_admitted = video_admitted
        self.fallback_admitted = fallback_admitted

    def parse(self, source_url, budget):
        from ..platforms.xhs_page_state import extract_xhs_note_id
        from ..platforms.xhs_tiantian_hybrid import build_xhs_tiantian_request_url, xhs_tiantian_titles_correspond

        started = time.monotonic()
        native = self.once.get(budget)
        if isinstance(native, Work):
            if not (self.video_admitted and is_ordinary_video(native)):
                return Failure(FailureKind.UNSUPPORTED, "XHS_SERVICE_NOT_ADMITTED")
            note_id, for_video = native.work_id, True
        else:
            if not self.fallback_admitted:
                return Failure(FailureKind.UNSUPPORTED, "XHS_SERVICE_NOT_ADMITTED")
            note_id, for_video = extract_xhs_note_id(source_url), False
        remaining = budget - (time.monotonic() - started)
        if remaining <= 0.1:
            return Failure(FailureKind.TIMEOUT, "ADAPTER_BUDGET_EXHAUSTED")
        request_url = build_xhs_tiantian_request_url(note_id) if note_id else source_url
        work = self.build(note_id, for_video).parse(request_url, remaining)
        if isinstance(work, Work) and isinstance(native, Work) and not xhs_tiantian_titles_correspond(
                native.title, work.title):
            # The existing enhancer accepts a service result only for the same note.
            return Failure(FailureKind.INVALID_RESULT, "XHS_SERVICE_TITLE_MISMATCH")
        return work


def build_adapters(source_url, *, prefer_ultra_hd, attempt_controller, service_ids):
    from ..platforms.xhs_tiantian_hybrid import xhs_tiantian_provider_is_available

    once = NativeOnce(source_url)
    adapters = {"native": SharedNative(once)}
    service_ids = set(service_ids or ())
    video_admitted = bool(prefer_ultra_hd) and xhs_tiantian_provider_is_available()
    if video_admitted or "tiantianjiexi_generic" in service_ids:
        def tiantian(note_id, for_video):
            # The 原画/超清 quota counts only the enhancer, as on the existing path.
            return XhsServiceTianTian(attempt_controller=attempt_controller if for_video else None,
                                      source_identity=SERVICE_IDENTITY_PREFIX + note_id if note_id else None)
        adapters["tiantianjiexi"] = GatedService(tiantian, once, video_admitted=video_admitted,
                                                 fallback_admitted="tiantianjiexi_generic" in service_ids)
    if "kukutool_generic" in service_ids:
        adapters["kukutool"] = GatedService(lambda note_id, for_video: KukutoolAdapter(), once,
                                            video_admitted=False, fallback_admitted=True)
    return adapters


def parse(url, route, *, prefer_ultra_hd=False, attempt_controller=None, service_ids=()):
    """Parse one XHS link on its route; returns the existing ParseResult shape."""
    from ..platforms.xiaohongshu import XiaohongshuAdapter as Platform

    started = time.monotonic()
    platform_adapter = Platform()
    platform_adapter.require_supported_url(url)
    adapters = build_adapters(url, prefer_ultra_hd=prefer_ultra_hd, attempt_controller=attempt_controller,
                              service_ids=service_ids)
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
        raise failure_error(run.failure, "小红书")
    work = xhs_media_only(run.shown)
    notices = []
    if attempt_controller is not None and getattr(attempt_controller, "denied", False):
        notices.append("超清解析请求过于频繁，本次返回普通清晰度。")
    result = present(
        work, platform_adapter=platform_adapter, provider_label=PROVIDER_LABELS.get(work.provider, work.provider),
        fallback_extensions={"video": "mp4", "image": "jpg"}, attempts=attempts, notices=notices,
        extra_metadata={"note_id": work.work_id, "source_identity_verified": True,
                        "provider_total_duration_ms": int((time.monotonic() - started) * 1000)},
    )
    if result is None:
        raise UnifiedParseError("小红书解析失败，请稍后重试。", "XHS_NO_PRESENTABLE_MEDIA")
    return result
