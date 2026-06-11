from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
import hashlib
import json
import logging
from time import time
from urllib.parse import urljoin, urlsplit

import requests
from django.core.cache import cache
from django.utils import timezone

from ..assets import MediaAsset, ParseResult
from ..candidates import MediaCandidate
from ..diagnostics import select_best_media_candidate
from ..provider_settings import get_platform_provider_settings


DEFAULT_TIKHUB_BASE_URL = "https://api.tikhub.io"
TIKHUB_HIGH_QUALITY_ENDPOINT = "/api/v1/douyin/app/v3/fetch_video_high_quality_play_url"
TIKHUB_LOW_CONFIDENCE_BYTES = 8 * 1024 * 1024
TIKHUB_VIDEO_URL_KEYS = (
    "original_video_url",
    "originalVideoUrl",
    "original_url",
    "origin_url",
    "high_quality_url",
    "highest_quality_url",
    "play_url",
    "video_url",
    "url",
)
logger = logging.getLogger(__name__)


def duration_ms(started_at):
    return int((time() - started_at) * 1000)


def context_log_fields(context):
    parsed_url = urlsplit(getattr(context, "url", "") or "")
    return {
        "aweme_id": getattr(context, "aweme_id", ""),
        "source_host": parsed_url.hostname or "",
        "source_path": parsed_url.path or "",
    }


def log_provider_failure(provider, context, error, started_at=None, **extra_fields):
    fields = {
        "provider": provider,
        **context_log_fields(context),
        "error_type": type(error).__name__,
        "error": str(error),
        **extra_fields,
    }
    if started_at is not None:
        fields["duration_ms"] = duration_ms(started_at)
    logger.info("douyin_provider_failed", extra=fields)


def log_provider_event(event, provider, context, started_at=None, **extra_fields):
    fields = {
        "provider": provider,
        **context_log_fields(context),
        **extra_fields,
    }
    if started_at is not None:
        fields["duration_ms"] = duration_ms(started_at)
    logger.info(event, extra=fields)


@dataclass
class DouyinParseContext:
    url: str
    final_url: str = ""
    html: str = ""
    aweme_id: str = ""
    direct_result: object | None = None
    direct_error: Exception | None = None


@dataclass(frozen=True)
class ProviderResponse:
    ok: bool
    code: str = ""
    message: str = ""
    request_id: str = ""
    candidates: tuple[MediaCandidate, ...] = ()


class DouyinResultProvider:
    name = ""

    def parse(self, adapter, context):
        raise NotImplementedError


def iter_nested_values(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from iter_nested_values(child)
    elif isinstance(value, list):
        for item in value:
            yield from iter_nested_values(item)
    else:
        yield value


def iter_http_strings(value):
    for item in iter_nested_values(value):
        if isinstance(item, str) and item.startswith(("http://", "https://")):
            yield item


def get_nested_value(value, key):
    if isinstance(value, dict):
        if key in value:
            return value[key]
        for child in value.values():
            found = get_nested_value(child, key)
            if found is not None:
                return found
    elif isinstance(value, list):
        for item in value:
            found = get_nested_value(item, key)
            if found is not None:
                return found
    return None


def build_tikhub_base_url(base_url):
    return (base_url or DEFAULT_TIKHUB_BASE_URL).rstrip("/") + "/"


def build_tikhub_cache_key(params):
    normalized = tuple(sorted((key, str(value)) for key, value in params.items() if value))
    digest = hashlib.sha256(
        json.dumps(normalized, ensure_ascii=True).encode("utf-8")
    ).hexdigest()
    return f"tikhub:cache:{digest}"


def get_cached_tikhub_data(cache_key, ttl_seconds):
    if ttl_seconds <= 0:
        return None
    return cache.get(cache_key)


def cache_tikhub_data(cache_key, data, ttl_seconds):
    if ttl_seconds <= 0:
        return
    cache.set(cache_key, data, timeout=ttl_seconds)


def reserve_tikhub_call(daily_limit):
    if daily_limit <= 0:
        return True

    today = timezone.localdate().isoformat()
    cache_key = f"tikhub:usage:{today}"
    timeout = 2 * 24 * 60 * 60
    try:
        cache.add(cache_key, 0, timeout=timeout)
        count = cache.incr(cache_key)
    except Exception:
        logger.exception("tikhub_usage_cache_failed")
        return False
    return count <= daily_limit


def build_tikhub_candidates(data):
    candidates = []
    seen = set()

    for key in TIKHUB_VIDEO_URL_KEYS:
        value = get_nested_value(data, key)
        if value is None:
            continue
        for url in iter_http_strings(value):
            if url in seen:
                continue
            seen.add(url)
            candidates.append(
                MediaCandidate(
                    url=url,
                    quality_label="原始上传画质",
                    source=f"tikhub.{key}",
                    priority=len(TIKHUB_VIDEO_URL_KEYS) - TIKHUB_VIDEO_URL_KEYS.index(key),
                )
            )

    return tuple(candidates)


def build_tikhub_provider_response(data):
    code = ""
    message = ""
    request_id = ""
    ok = True

    if isinstance(data, dict):
        raw_code = data.get("code") or data.get("status_code") or data.get("statusCode")
        if raw_code is not None:
            code = str(raw_code)
        message = str(data.get("message") or data.get("msg") or data.get("detail") or "")
        request_id = str(data.get("request_id") or data.get("requestId") or data.get("log_id") or data.get("logId") or "")
        if raw_code not in (None, 0, "0", 200, "200"):
            ok = False

    return ProviderResponse(
        ok=ok,
        code=code,
        message=message,
        request_id=request_id,
        candidates=build_tikhub_candidates(data),
    )


def with_parse_metadata(result, **metadata):
    if not result:
        return result
    return replace(
        result,
        metadata={
            **(result.metadata or {}),
            **metadata,
        },
    )


def get_reference_result(adapter, context):
    if context.direct_result and context.direct_result.assets:
        return context.direct_result
    if context.aweme_id:
        started_at = time()
        try:
            result = adapter.fetch_mobile_feed_result(context.aweme_id)
            if result and result.assets:
                return result
        except Exception as error:
            log_provider_failure("native_reference_feed", context, error, started_at)
            return None
    return None


def get_fast_native_result(adapter, context):
    started_at = time()
    try:
        result = DouyinNativeProvider().parse(adapter, context)
        if result and result.assets:
            return with_parse_metadata(result, selected_provider="native", fallback_reason="fast_native")
    except Exception as error:
        log_provider_failure("native_fast", context, error, started_at)
        return None
    return None


def get_best_native_result(adapter, context):
    started_at = time()
    try:
        result = DouyinNativeEnhancedProvider().parse(adapter, context)
        if result and result.assets:
            return with_parse_metadata(result, selected_provider="native_enhanced", fallback_reason="enhanced_comparison")
    except Exception as error:
        log_provider_failure("native_enhanced_comparison", context, error, started_at)
    return get_reference_result(adapter, context)


def get_reference_video_asset(result):
    if not result:
        return None
    return result.video_assets[0] if result.video_assets else None


def build_external_parse_result(adapter, context, candidate, reference_result=None):
    reference_asset = get_reference_video_asset(reference_result)
    base_filename = getattr(reference_result, "base_filename", "")
    if not base_filename:
        base_filename = f"douyin_{context.aweme_id}" if context.aweme_id else "douyin"
    return ParseResult(
        platform=adapter.platform_id,
        post_type="video",
        base_filename=base_filename,
        assets=(
            MediaAsset(
                media_type="video",
                url=candidate.url,
                platform=adapter.platform_id,
                cover_url=getattr(reference_asset, "cover_url", "") if reference_asset else "",
                cover_preview_url=getattr(reference_asset, "cover_preview_url", "") if reference_asset else "",
                quality_label=candidate.quality_label,
                size_bytes=candidate.size_bytes,
                headers=adapter.headers,
                media_host_suffixes=adapter.media_host_suffixes,
            ),
        ),
    )


class DouyinTikHubProvider(DouyinResultProvider):
    name = "tikhub"

    def __init__(self, provider_settings=None, timeout=20):
        self.provider_settings = provider_settings or get_platform_provider_settings("douyin")
        self.timeout = timeout

    def build_request_params(self, context):
        params = {
            "region": self.provider_settings.tikhub_region or "CN",
        }
        if context.aweme_id:
            params["aweme_id"] = context.aweme_id
        share_url = context.final_url or context.url
        if share_url:
            params["share_url"] = share_url
        return params

    def fetch_high_quality_data(self, context):
        params = self.build_request_params(context)
        cache_key = build_tikhub_cache_key(params)
        cached_data = get_cached_tikhub_data(cache_key, self.provider_settings.tikhub_cache_ttl_seconds)
        if cached_data is not None:
            log_provider_event("douyin_provider_cache_hit", self.name, context, cache_hit=True)
            return cached_data

        if not reserve_tikhub_call(self.provider_settings.tikhub_daily_limit):
            log_provider_event("douyin_provider_limit_reached", self.name, context, daily_limit=self.provider_settings.tikhub_daily_limit)
            raise RuntimeError("TikHub daily limit reached")

        endpoint = urljoin(build_tikhub_base_url(self.provider_settings.tikhub_base_url), TIKHUB_HIGH_QUALITY_ENDPOINT.lstrip("/"))
        started_at = time()
        response = requests.get(
            endpoint,
            params=params,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self.provider_settings.tikhub_api_key}",
            },
            timeout=self.timeout,
        )
        try:
            response.raise_for_status()
            data = response.json()
            provider_response = build_tikhub_provider_response(data)
            log_provider_event(
                "douyin_provider_response",
                self.name,
                context,
                started_at,
                cache_hit=False,
                code=provider_response.code,
                provider_message=provider_response.message,
                request_id=provider_response.request_id,
                candidate_count=len(provider_response.candidates),
            )
            cache_tikhub_data(cache_key, data, self.provider_settings.tikhub_cache_ttl_seconds)
            return data
        finally:
            response.close()

    def fetch_high_quality_data_with_native_result(self, adapter, context):
        with ThreadPoolExecutor(max_workers=2) as executor:
            tikhub_future = executor.submit(self.fetch_high_quality_data, context)
            native_future = executor.submit(get_fast_native_result, adapter, context)

            try:
                data = tikhub_future.result()
            except Exception as error:
                log_provider_failure(self.name, context, error)
                try:
                    return None, native_future.result()
                except Exception as native_error:
                    log_provider_failure("native_fast", context, native_error)
                    return None, None

            try:
                native_result = native_future.result()
            except Exception as error:
                log_provider_failure("native_fast", context, error)
                native_result = None

            return data, native_result

    def parse(self, adapter, context):
        if not self.provider_settings.provider_is_enabled(self.name):
            return None

        parse_started_at = time()
        data, native_result = self.fetch_high_quality_data_with_native_result(adapter, context)
        if not data:
            return native_result

        provider_response = build_tikhub_provider_response(data)
        if not provider_response.ok:
            log_provider_event(
                "douyin_provider_rejected",
                self.name,
                context,
                parse_started_at,
                fallback_reason="provider_response_not_ok",
                code=provider_response.code,
                provider_message=provider_response.message,
                request_id=provider_response.request_id,
            )
            return native_result

        candidates = provider_response.candidates
        if not candidates:
            log_provider_event(
                "douyin_provider_no_candidates",
                self.name,
                context,
                parse_started_at,
                code=provider_response.code,
                provider_message=provider_response.message,
                request_id=provider_response.request_id,
            )
            return native_result

        probe_started_at = time()
        selection = select_best_media_candidate(
            candidates,
            headers=adapter.headers,
            allowed_final_host_suffixes=adapter.media_host_suffixes,
            timeout=6,
            max_candidates=4,
            expected_type="video",
        )
        probe_duration_ms = duration_ms(probe_started_at)
        if not selection.candidate or not selection.candidate.size_bytes:
            log_provider_event(
                "douyin_provider_unvalidated",
                self.name,
                context,
                parse_started_at,
                candidate_count=len(candidates),
                probe_duration_ms=probe_duration_ms,
                fallback_reason="no_validated_candidate",
            )
            return native_result

        selected_diagnostic = selection.selected_diagnostic
        if selected_diagnostic and selected_diagnostic.content_type and "video" not in selected_diagnostic.content_type.lower():
            log_provider_event(
                "douyin_provider_unvalidated",
                self.name,
                context,
                parse_started_at,
                candidate_count=len(candidates),
                probe_duration_ms=probe_duration_ms,
                fallback_reason="invalid_content_type",
                content_type=selected_diagnostic.content_type,
            )
            return native_result

        native_size = get_largest_video_size(native_result)
        if native_result and native_result.video_assets and native_size > selection.candidate.size_bytes:
            result = with_parse_metadata(
                native_result,
                selected_provider="native",
                fallback_reason="native_larger_than_tikhub",
                tikhub_size_bytes=selection.candidate.size_bytes,
                native_size_bytes=native_size,
                tikhub_candidate_count=len(candidates),
                tikhub_probe_duration_ms=probe_duration_ms,
            )
            log_provider_event(
                "douyin_provider_selected",
                "native",
                context,
                parse_started_at,
                fallback_reason="native_larger_than_tikhub",
                tikhub_size_bytes=selection.candidate.size_bytes,
                native_size_bytes=native_size,
                candidate_count=len(candidates),
                probe_duration_ms=probe_duration_ms,
            )
            return result

        if not native_size and selection.candidate.size_bytes < TIKHUB_LOW_CONFIDENCE_BYTES:
            native_result = get_best_native_result(adapter, context)
            native_size = get_largest_video_size(native_result)
            if native_result and native_result.video_assets and native_size > selection.candidate.size_bytes:
                return with_parse_metadata(
                    native_result,
                    selected_provider="native_enhanced",
                    fallback_reason="enhanced_native_larger_than_low_confidence_tikhub",
                    tikhub_size_bytes=selection.candidate.size_bytes,
                    native_size_bytes=native_size,
                    tikhub_candidate_count=len(candidates),
                    tikhub_probe_duration_ms=probe_duration_ms,
                )

        result = build_external_parse_result(adapter, context, selection.candidate, native_result)
        result = with_parse_metadata(
            result,
            selected_provider=self.name,
            fallback_reason="tikhub_validated",
            tikhub_size_bytes=selection.candidate.size_bytes,
            native_size_bytes=native_size,
            tikhub_candidate_count=len(candidates),
            tikhub_probe_duration_ms=probe_duration_ms,
            tikhub_code=provider_response.code,
            tikhub_message=provider_response.message,
            tikhub_request_id=provider_response.request_id,
        )
        log_provider_event(
            "douyin_provider_selected",
            self.name,
            context,
            parse_started_at,
            fallback_reason="tikhub_validated",
            selected_size_bytes=selection.candidate.size_bytes,
            native_size_bytes=native_size,
            candidate_count=len(candidates),
            probe_duration_ms=probe_duration_ms,
        )
        return result


class DouyinMobileFeedProvider(DouyinResultProvider):
    name = "mobile_feed"

    def parse(self, adapter, context):
        if not context.aweme_id:
            return None
        return adapter.fetch_mobile_feed_result(context.aweme_id)


class DouyinMobileShareProvider(DouyinResultProvider):
    name = "mobile_share"

    def parse(self, adapter, context):
        if not context.aweme_id:
            return None
        return adapter.fetch_mobile_share_result(context.aweme_id)


class DouyinNativeProvider(DouyinResultProvider):
    name = "native"

    def __init__(self, fallback_providers=None):
        self.fallback_providers = tuple(fallback_providers or (
            DouyinMobileFeedProvider(),
            DouyinMobileShareProvider(),
        ))

    def parse(self, adapter, context):
        for provider in self.fallback_providers:
            started_at = time()
            try:
                result = provider.parse(adapter, context)
                if result and result.assets:
                    return with_parse_metadata(result, selected_provider=getattr(provider, "name", "native"))
            except Exception as error:
                log_provider_failure(getattr(provider, "name", "native"), context, error, started_at)
                continue

        if context.direct_result is not None and context.direct_result.assets:
            return context.direct_result
        if context.direct_result is not None:
            return context.direct_result
        if context.direct_error:
            raise context.direct_error
        return None


def get_largest_video_size(result):
    if not result:
        return 0
    return max((asset.size_bytes for asset in result.video_assets), default=0)


class DouyinNativeEnhancedProvider(DouyinResultProvider):
    name = "native_enhanced"

    def parse(self, adapter, context):
        results = []
        if context.aweme_id:
            for fetcher in (
                adapter.fetch_mobile_feed_result,
                adapter.fetch_mobile_share_result,
            ):
                started_at = time()
                try:
                    result = fetcher(context.aweme_id, probe_video_candidates=True)
                    if result and result.assets:
                        results.append(result)
                except Exception as error:
                    log_provider_failure(f"native_enhanced.{getattr(fetcher, '__name__', 'fetcher')}", context, error, started_at)
                    continue

        if context.html:
            result = adapter.parse_html(
                context.html,
                source_url=context.final_url,
                probe_video_candidates=True,
            )
            if result and result.assets:
                results.append(result)

        video_results = [result for result in results if result.video_assets]
        if video_results:
            return max(video_results, key=get_largest_video_size)

        if results:
            return results[0]

        return None


def build_douyin_result_providers(provider_settings=None, prefer_ultra_hd=True):
    provider_settings = provider_settings or get_platform_provider_settings("douyin")
    if not prefer_ultra_hd:
        provider_settings = replace(
            provider_settings,
            provider_order=tuple(
                provider_name
                for provider_name in provider_settings.provider_order
                if provider_name == "native"
            ),
        )
    elif "tikhub" in provider_settings.provider_order and provider_settings.provider_is_enabled("tikhub"):
        provider_settings = replace(
            provider_settings,
            provider_order=(
                "tikhub",
                *(
                    provider_name
                    for provider_name in provider_settings.provider_order
                    if provider_name != "tikhub"
                ),
            ),
        )
    provider_map = {
        "tikhub": lambda: DouyinTikHubProvider(provider_settings=provider_settings),
        "native_enhanced": DouyinNativeEnhancedProvider,
        "native": DouyinNativeProvider,
    }

    providers = []
    for provider_name in provider_settings.enabled_provider_order:
        provider_factory = provider_map.get(provider_name)
        if provider_factory:
            providers.append(provider_factory())

    if providers and not any(isinstance(provider, DouyinNativeProvider) for provider in providers):
        providers.append(DouyinNativeProvider())

    return tuple(providers or (DouyinNativeProvider(),))
