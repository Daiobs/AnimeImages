from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import date
from threading import Lock
from time import time
from urllib.parse import urljoin

import requests

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
TIKHUB_CACHE = {}
TIKHUB_USAGE = {}
TIKHUB_LOCK = Lock()


@dataclass
class DouyinParseContext:
    url: str
    final_url: str = ""
    html: str = ""
    aweme_id: str = ""
    direct_result: object | None = None
    direct_error: Exception | None = None


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
    return tuple(sorted((key, value) for key, value in params.items() if value))


def get_cached_tikhub_data(cache_key, ttl_seconds):
    if ttl_seconds <= 0:
        return None
    now = time()
    with TIKHUB_LOCK:
        cached = TIKHUB_CACHE.get(cache_key)
        if not cached:
            return None
        created_at, data = cached
        if now - created_at > ttl_seconds:
            TIKHUB_CACHE.pop(cache_key, None)
            return None
        return data


def cache_tikhub_data(cache_key, data, ttl_seconds):
    if ttl_seconds <= 0:
        return
    with TIKHUB_LOCK:
        TIKHUB_CACHE[cache_key] = (time(), data)


def reserve_tikhub_call(daily_limit):
    if daily_limit <= 0:
        return True
    today = date.today().isoformat()
    with TIKHUB_LOCK:
        count = TIKHUB_USAGE.get(today, 0)
        if count >= daily_limit:
            return False
        TIKHUB_USAGE[today] = count + 1
        for day in tuple(TIKHUB_USAGE):
            if day != today:
                TIKHUB_USAGE.pop(day, None)
        return True


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


def get_reference_result(adapter, context):
    if context.direct_result and context.direct_result.assets:
        return context.direct_result
    if context.aweme_id:
        try:
            result = adapter.fetch_mobile_feed_result(context.aweme_id)
            if result and result.assets:
                return result
        except Exception:
            return None
    return None


def get_fast_native_result(adapter, context):
    try:
        result = DouyinNativeProvider().parse(adapter, context)
        if result and result.assets:
            return result
    except Exception:
        return None
    return None


def get_best_native_result(adapter, context):
    try:
        result = DouyinNativeEnhancedProvider().parse(adapter, context)
        if result and result.assets:
            return result
    except Exception:
        pass
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
            return cached_data

        if not reserve_tikhub_call(self.provider_settings.tikhub_daily_limit):
            raise RuntimeError("TikHub daily limit reached")

        endpoint = urljoin(build_tikhub_base_url(self.provider_settings.tikhub_base_url), TIKHUB_HIGH_QUALITY_ENDPOINT.lstrip("/"))
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
            except Exception:
                try:
                    return None, native_future.result()
                except Exception:
                    return None, None

            try:
                native_result = native_future.result()
            except Exception:
                native_result = None

            return data, native_result

    def parse(self, adapter, context):
        if not self.provider_settings.provider_is_enabled(self.name):
            return None

        data, native_result = self.fetch_high_quality_data_with_native_result(adapter, context)
        if not data:
            return native_result

        candidates = build_tikhub_candidates(data)
        if not candidates:
            return native_result

        selection = select_best_media_candidate(
            candidates,
            headers=adapter.headers,
            allowed_final_host_suffixes=adapter.media_host_suffixes,
            timeout=6,
            max_candidates=4,
        )
        if not selection.candidate or not selection.candidate.size_bytes:
            return native_result

        selected_diagnostic = selection.selected_diagnostic
        if selected_diagnostic and selected_diagnostic.content_type and "video" not in selected_diagnostic.content_type.lower():
            return native_result

        native_size = get_largest_video_size(native_result)
        if native_result and native_result.video_assets and native_size > selection.candidate.size_bytes:
            return native_result

        if not native_size and selection.candidate.size_bytes < TIKHUB_LOW_CONFIDENCE_BYTES:
            native_result = get_best_native_result(adapter, context)
            native_size = get_largest_video_size(native_result)
            if native_result and native_result.video_assets and native_size > selection.candidate.size_bytes:
                return native_result

        return build_external_parse_result(adapter, context, selection.candidate, native_result)


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
            try:
                result = provider.parse(adapter, context)
                if result and result.assets:
                    return result
            except Exception:
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
                try:
                    result = fetcher(context.aweme_id, probe_video_candidates=True)
                    if result and result.assets:
                        results.append(result)
                except Exception:
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
