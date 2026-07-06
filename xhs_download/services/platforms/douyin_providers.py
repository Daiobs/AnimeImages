from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from dataclasses import dataclass, replace
import hashlib
import inspect
import json
import logging
from time import monotonic, time
from urllib.parse import urljoin, urlsplit

import requests
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from ..assets import MediaAsset, ParseResult
from ..candidates import MediaCandidate
from ..common import validate_external_url
from ..diagnostics import select_best_media_candidate
from ..provider_settings import get_platform_provider_settings
from ..provider_manifest import build_provider_manifest_token
from .douyin_kukutool import DouyinKukuToolProvider
from .tiantianjiexi_generic import (
    TianTianJieXiGenericProvider,
    quality_rank as tiantianjiexi_quality_rank,
    size_value as tiantianjiexi_size_value,
)


DEFAULT_TIKHUB_BASE_URL = "https://api.tikhub.io"
DEFAULT_TIKHUB_CHINA_BASE_URL = "https://api.tikhub.dev"
TIKHUB_HIGH_QUALITY_ENDPOINT = "/api/v1/douyin/app/v3/fetch_video_high_quality_play_url"
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
DOUYIN_ULTRA_EXECUTOR = ThreadPoolExecutor(
    max_workers=8,
    thread_name_prefix="douyin-ultra",
)
logger = logging.getLogger(__name__)


def build_douyin_metadata_cache_key(aweme_id):
    return f"douyin:metadata:{aweme_id}"


def serialize_douyin_metadata(result):
    if not result or not result.assets:
        return None
    reference_asset = get_reference_video_asset(result)
    return {
        "platform": result.platform,
        "post_type": result.post_type,
        "base_filename": result.base_filename,
        "cover_url": getattr(reference_asset, "cover_url", "") if reference_asset else "",
        "cover_preview_url": (
            getattr(reference_asset, "cover_preview_url", "")
            if reference_asset
            else ""
        ),
        "reference_url": getattr(reference_asset, "url", "") if reference_asset else "",
        "metadata": result.metadata or {},
    }


def cache_douyin_metadata(aweme_id, result):
    if not aweme_id:
        return
    data = serialize_douyin_metadata(result)
    if not data:
        return
    ttl_seconds = int(
        getattr(settings, "DOUYIN_METADATA_CACHE_TTL_SECONDS", 1800) or 0
    )
    if ttl_seconds <= 0:
        return
    try:
        cache.set(
            build_douyin_metadata_cache_key(aweme_id),
            data,
            timeout=ttl_seconds,
        )
    except Exception:
        logger.exception(
            "douyin_metadata_cache_write_failed",
            extra={"aweme_id": aweme_id},
        )


def get_cached_douyin_metadata(aweme_id):
    if not aweme_id:
        return None
    try:
        data = cache.get(build_douyin_metadata_cache_key(aweme_id))
    except Exception:
        logger.exception(
            "douyin_metadata_cache_read_failed",
            extra={"aweme_id": aweme_id},
        )
        return None
    if not isinstance(data, dict):
        return None

    reference_url = str(data.get("reference_url") or "")
    cover_url = str(data.get("cover_url") or "")
    if not reference_url and not cover_url:
        return None
    return ParseResult(
        platform=str(data.get("platform") or "douyin"),
        post_type=str(data.get("post_type") or "video"),
        base_filename=str(data.get("base_filename") or f"douyin_{aweme_id}"),
        assets=(
            MediaAsset(
                media_type="video",
                url=reference_url,
                platform="douyin",
                cover_url=cover_url,
                cover_preview_url=str(data.get("cover_preview_url") or cover_url),
            ),
        ),
        metadata=data.get("metadata") if isinstance(data.get("metadata"), dict) else {},
    )


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
    deadline_monotonic: float = 0
    client_country_code: str = ""


@dataclass(frozen=True)
class ProviderResponse:
    ok: bool
    code: str = ""
    message: str = ""
    request_id: str = ""
    candidates: tuple[MediaCandidate, ...] = ()


@dataclass(frozen=True)
class ValidatedProviderCandidate:
    candidate: MediaCandidate
    final_url: str = ""
    content_type: str = ""
    cache_hit: bool = False
    candidate_count: int = 0
    probe_duration_ms: int = 0
    code: str = ""
    message: str = ""
    request_id: str = ""


@dataclass(frozen=True)
class TikHubRequestRoute:
    country_code: str
    region: str
    base_url: str


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


def tikhub_response_status_code(response):
    try:
        return int(response.status_code)
    except (TypeError, ValueError):
        return 200


def is_retryable_tikhub_failure(response, provider_response):
    status_code = tikhub_response_status_code(response)
    if status_code in {429, 500, 502, 503, 504}:
        return True
    if status_code != 400 and provider_response.code != "400":
        return False

    message = (provider_response.message or "").lower()
    return "retry" in message or "重试" in message


def is_unusable_tikhub_payload(data, provider_response):
    if not provider_response.ok or not provider_response.candidates:
        return False

    content_type = get_tikhub_declared_content_type(data)
    if content_type in {"text/html", "text/plain", "application/json"}:
        return True

    declared_size = provider_response.candidates[0].size_bytes
    return 0 < declared_size < 1024 * 1024


def get_tikhub_request_timeout(provider_settings, context):
    connect_timeout = max(
        0.1,
        provider_settings.tikhub_connect_timeout_seconds,
    )
    read_timeout = max(
        0.1,
        provider_settings.tikhub_read_timeout_seconds,
    )
    if not context.deadline_monotonic:
        return connect_timeout, read_timeout

    remaining = max(0.1, context.deadline_monotonic - monotonic())
    connect_timeout = min(connect_timeout, remaining)
    read_timeout = min(read_timeout, max(0.1, remaining - connect_timeout))
    return connect_timeout, read_timeout


def build_tikhub_cache_key(params):
    normalized = tuple(sorted((key, str(value)) for key, value in params.items() if value))
    digest = hashlib.sha256(
        json.dumps(normalized, ensure_ascii=True).encode("utf-8")
    ).hexdigest()
    return f"tikhub:cache:{digest}"


def build_tikhub_validated_cache_key(context, params, base_url=""):
    identity = context.aweme_id or build_tikhub_cache_key(params).rsplit(":", 1)[-1]
    region = str(params.get("region") or "default").lower()
    api_host = (urlsplit(base_url).hostname or "default").lower()
    return f"tikhub:validated:{api_host}:{region}:{identity}"


def serialize_validated_candidate(validated):
    return {
        "url": validated.candidate.url,
        "size_bytes": validated.candidate.size_bytes,
        "quality_label": validated.candidate.quality_label,
        "source": validated.candidate.source,
        "final_url": validated.final_url,
        "content_type": validated.content_type,
        "candidate_count": validated.candidate_count,
        "selected_at": timezone.now().isoformat(),
    }


def get_cached_validated_candidate(cache_key, allowed_host_suffixes):
    data = cache.get(cache_key)
    if not isinstance(data, dict):
        return None

    try:
        url = str(data["url"])
        size_bytes = int(data["size_bytes"])
        validate_external_url(
            url,
            allowed_host_suffixes,
            "缓存的 TikHub URL 不在允许的媒体域名内",
        )
        if size_bytes <= 0:
            return None
    except (KeyError, TypeError, ValueError):
        cache.delete(cache_key)
        return None

    return ValidatedProviderCandidate(
        candidate=MediaCandidate(
            url=url,
            size_bytes=size_bytes,
            quality_label=str(data.get("quality_label") or "原始上传画质"),
            source=str(data.get("source") or "tikhub.validated_cache"),
            priority=len(TIKHUB_VIDEO_URL_KEYS),
        ),
        final_url=str(data.get("final_url") or url),
        content_type=str(data.get("content_type") or ""),
        cache_hit=True,
        candidate_count=int(data.get("candidate_count") or 1),
    )


def cache_validated_candidate(cache_key, validated, ttl_seconds):
    if ttl_seconds <= 0:
        return
    cache.set(
        cache_key,
        serialize_validated_candidate(validated),
        timeout=ttl_seconds,
    )


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
    payload = data.get("data") if isinstance(data, dict) else None
    declared_size = 0
    if isinstance(payload, dict):
        try:
            declared_size = int(
                payload.get("file_size")
                or payload.get("fileSize")
                or payload.get("size_bytes")
                or 0
            )
        except (TypeError, ValueError):
            declared_size = 0

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
                    size_bytes=declared_size if key in {"original_video_url", "originalVideoUrl"} else 0,
                    quality_label="原始上传画质",
                    source=f"tikhub.{key}",
                    priority=len(TIKHUB_VIDEO_URL_KEYS) - TIKHUB_VIDEO_URL_KEYS.index(key),
                )
            )

    return tuple(candidates)


def get_tikhub_declared_content_type(data):
    payload = data.get("data") if isinstance(data, dict) else None
    if not isinstance(payload, dict):
        return ""
    return str(
        payload.get("content_type")
        or payload.get("contentType")
        or payload.get("mime_type")
        or ""
    ).split(";", 1)[0].strip().lower()


def build_tikhub_provider_response(data):
    code = ""
    message = ""
    request_id = ""
    ok = True

    if isinstance(data, dict):
        detail = data.get("detail")
        detail = detail if isinstance(detail, dict) else {}
        raw_code = (
            data.get("code")
            or data.get("status_code")
            or data.get("statusCode")
            or detail.get("code")
            or detail.get("status_code")
            or detail.get("statusCode")
        )
        if raw_code is not None:
            code = str(raw_code)
        message = str(
            data.get("message")
            or data.get("msg")
            or detail.get("message")
            or detail.get("message_zh")
            or data.get("detail")
            or ""
        )
        request_id = str(
            data.get("request_id")
            or data.get("requestId")
            or data.get("log_id")
            or data.get("logId")
            or detail.get("request_id")
            or detail.get("requestId")
            or ""
        )
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


def call_native_fetcher(fetcher, aweme_id, timeout_seconds):
    parameters = inspect.signature(fetcher).parameters
    request_options = {}
    if "request_timeout" in parameters:
        request_options["request_timeout"] = max(0.1, timeout_seconds)
    if "request_retries" in parameters:
        request_options["request_retries"] = 1
    return fetcher(aweme_id, **request_options)


def get_fast_native_result(adapter, context, timeout_seconds=5.0):
    started_at = time()
    try:
        if context.direct_result and context.direct_result.assets:
            result = context.direct_result
        elif context.aweme_id:
            native_deadline = monotonic() + max(0.1, timeout_seconds)
            result = None
            for provider_name, fetcher_name, budget_ratio in (
                ("native_fast_feed", "fetch_mobile_feed_result", 0.6),
                ("native_fast_share", "fetch_mobile_share_result", 1.0),
            ):
                fetcher = getattr(adapter, fetcher_name, None)
                if not fetcher:
                    continue
                remaining = native_deadline - monotonic()
                if remaining <= 0:
                    break
                request_budget = min(
                    remaining,
                    max(0.1, timeout_seconds * budget_ratio),
                )
                fetch_started_at = time()
                try:
                    result = call_native_fetcher(
                        fetcher,
                        context.aweme_id,
                        request_budget,
                    )
                except Exception as error:
                    log_provider_failure(
                        provider_name,
                        context,
                        error,
                        fetch_started_at,
                    )
                    continue
                if result and result.assets:
                    break
        else:
            result = None
        if result and result.assets:
            result = with_parse_metadata(
                result,
                selected_provider="native",
                fallback_reason="fast_native",
            )
            cache_douyin_metadata(context.aweme_id, result)
            return result
    except Exception as error:
        log_provider_failure("native_fast", context, error, started_at)
        return None
    return None


def get_reference_video_asset(result):
    if not result:
        return None
    return result.video_assets[0] if result.video_assets else None


def build_external_parse_result(
    adapter,
    context,
    candidate,
    reference_result=None,
    provider_name="",
    base_filename="",
    cover_url="",
):
    reference_asset = get_reference_video_asset(reference_result)
    reference_filename = getattr(reference_result, "base_filename", "")
    base_filename = reference_filename or base_filename
    if not base_filename:
        base_filename = f"douyin_{context.aweme_id}" if context.aweme_id else "douyin"
    provider_fields = {"provider": provider_name}
    if provider_name in {"kukutool", "tiantianjiexi"}:
        asset_id = f"douyin:{context.aweme_id}:video:{provider_name}"
        provider_cache_key = f"douyin:{provider_name}:validated:{context.aweme_id}"
        provider_fields.update(
            asset_id=asset_id,
            provider_resource_id=context.aweme_id,
            provider_cache_key=provider_cache_key,
            provider_manifest_token=build_provider_manifest_token(
                asset_id=asset_id,
                provider=provider_name,
                provider_resource_id=context.aweme_id,
                provider_cache_key=provider_cache_key,
                url=candidate.url,
                media_type="video",
            ),
        )
    return ParseResult(
        platform=adapter.platform_id,
        post_type="video",
        base_filename=base_filename,
        assets=(
            MediaAsset(
                media_type="video",
                url=candidate.url,
                platform=adapter.platform_id,
                cover_url=(
                    getattr(reference_asset, "cover_url", "")
                    if reference_asset
                    else cover_url
                ),
                cover_preview_url=(
                    getattr(reference_asset, "cover_preview_url", "")
                    if reference_asset
                    else cover_url
                ),
                quality_label=candidate.quality_label,
                size_bytes=candidate.size_bytes,
                headers=adapter.headers,
                media_host_suffixes=adapter.media_host_suffixes,
                **provider_fields,
            ),
        ),
    )


class DouyinTianTianJieXiProvider(DouyinResultProvider):
    name = "tiantianjiexi"

    def __init__(self, provider_settings=None, provider=None):
        self.provider_settings = provider_settings or get_platform_provider_settings("douyin")
        self.provider = provider or TianTianJieXiGenericProvider()

    def remaining_budget(self, context, fallback):
        if not context.deadline_monotonic:
            return max(0.1, fallback)
        return max(0.0, context.deadline_monotonic - monotonic())

    def build_candidates(self, payload, adapter):
        candidates = []
        for index, resource in enumerate(payload.get("videos") or (), start=1):
            if not isinstance(resource, dict):
                continue
            url = str(resource.get("url") or "").strip()
            if not url:
                continue
            try:
                validate_external_url(
                    url,
                    adapter.media_host_suffixes,
                    "天天解析返回了不受信任的抖音媒体域名",
                )
            except (TypeError, ValueError):
                continue
            quality_label = str(
                resource.get("quality")
                or resource.get("quality_label")
                or "原始上传画质"
            )
            size_bytes = tiantianjiexi_size_value(resource.get("size"))
            candidates.append(
                MediaCandidate(
                    url=url,
                    quality_label=quality_label,
                    size_bytes=size_bytes,
                    priority=tiantianjiexi_quality_rank(quality_label) + index,
                    source="tiantianjiexi.official_api",
                )
            )
        return tuple(
            sorted(
                candidates,
                key=lambda item: (
                    tiantianjiexi_quality_rank(item.quality_label),
                    item.size_bytes,
                    item.priority,
                ),
                reverse=True,
            )
        )

    def clean_cover_url(self, payload, adapter):
        cover_url = str(payload.get("cover") or "").strip()
        if not cover_url:
            return ""
        try:
            validate_external_url(
                cover_url,
                adapter.media_host_suffixes,
                "天天解析封面 URL 不在允许的抖音媒体域名内",
            )
        except (TypeError, ValueError):
            return ""
        return cover_url

    def parse(self, adapter, context):
        if not self.provider_settings.provider_is_enabled(self.name):
            return None

        source_url = context.final_url or context.url
        if not source_url:
            return None

        parse_started_at = time()
        timeout_seconds = min(
            max(0.1, self.provider_settings.ultra_total_timeout_seconds),
            self.remaining_budget(
                context,
                self.provider_settings.ultra_total_timeout_seconds,
            ),
        )
        payload = self.provider.parse(source_url, timeout_seconds=timeout_seconds)
        candidates = self.build_candidates(payload, adapter)
        if not candidates:
            raise ValueError("天天解析未返回可用抖音视频资源")

        reference_result = get_cached_douyin_metadata(context.aweme_id)
        candidate = candidates[0]
        result = build_external_parse_result(
            adapter,
            context,
            candidate,
            reference_result,
            provider_name=self.name,
            base_filename=str(payload.get("title") or ""),
            cover_url=self.clean_cover_url(payload, adapter),
        )
        result = with_parse_metadata(
            result,
            selected_provider=self.name,
            fallback_reason="tiantianjiexi_official_api",
            source_provider=self.name,
            provider_label="天天解析",
            tiantianjiexi_candidate_count=len(candidates),
            tiantianjiexi_size_bytes=candidate.size_bytes,
            tiantianjiexi_quality_label=candidate.quality_label,
        )
        log_provider_event(
            "douyin_provider_selected",
            self.name,
            context,
            parse_started_at,
            fallback_reason="tiantianjiexi_official_api",
            selected_size_bytes=candidate.size_bytes,
            candidate_count=len(candidates),
            quality_label=candidate.quality_label,
        )
        return result


class DouyinTikHubProvider(DouyinResultProvider):
    name = "tikhub"

    def __init__(self, provider_settings=None):
        self.provider_settings = provider_settings or get_platform_provider_settings("douyin")

    def get_request_route(self, context):
        country_code = str(context.client_country_code or "").strip().upper()
        if len(country_code) != 2 or not country_code.isalpha():
            country_code = str(
                self.provider_settings.tikhub_region or "CN"
            ).strip().upper()

        if country_code == "CN":
            return TikHubRequestRoute(
                country_code="CN",
                region="CN",
                base_url=(
                    self.provider_settings.tikhub_china_base_url
                    or DEFAULT_TIKHUB_CHINA_BASE_URL
                ),
            )

        return TikHubRequestRoute(
            country_code=country_code,
            region=country_code,
            base_url=(
                self.provider_settings.tikhub_base_url
                or DEFAULT_TIKHUB_BASE_URL
            ),
        )

    def build_request_params(self, context, route=None):
        route = route or self.get_request_route(context)
        params = {
            "region": route.region,
        }
        if context.aweme_id:
            params["aweme_id"] = context.aweme_id
        else:
            share_url = context.final_url or context.url
            if share_url:
                params["share_url"] = share_url
        return params

    def get_attempt_route(self, context, attempt):
        route = self.get_request_route(context)
        if route.country_code == "CN" and attempt > 1:
            return replace(route, region="HK")
        return route

    def fetch_high_quality_data(self, context):
        max_attempts = max(1, self.provider_settings.tikhub_max_attempts)
        for attempt in range(1, max_attempts + 1):
            if context.deadline_monotonic and context.deadline_monotonic <= monotonic():
                raise FutureTimeoutError("Douyin ultra-HD deadline reached before TikHub request")
            if not reserve_tikhub_call(
                self.provider_settings.tikhub_daily_limit
            ):
                log_provider_event(
                    "douyin_provider_limit_reached",
                    self.name,
                    context,
                    daily_limit=self.provider_settings.tikhub_daily_limit,
                    attempt=attempt,
                )
                raise RuntimeError("TikHub daily limit reached")

            route = self.get_attempt_route(context, attempt)
            params = self.build_request_params(context, route=route)
            endpoint = urljoin(
                build_tikhub_base_url(route.base_url),
                TIKHUB_HIGH_QUALITY_ENDPOINT.lstrip("/"),
            )
            started_at = time()
            try:
                response = requests.get(
                    endpoint,
                    params=params,
                    headers={
                        "Accept": "application/json",
                        "Authorization": f"Bearer {self.provider_settings.tikhub_api_key}",
                    },
                    timeout=get_tikhub_request_timeout(
                        self.provider_settings,
                        context,
                    ),
                )
            except (requests.Timeout, requests.ConnectionError) as error:
                log_provider_failure(
                    self.name,
                    context,
                    error,
                    started_at,
                    client_country_code=route.country_code,
                    region=route.region,
                    api_host=urlsplit(route.base_url).hostname or "",
                    attempt=attempt,
                    retryable=True,
                )
                if attempt < max_attempts:
                    continue
                raise
            try:
                try:
                    data = response.json()
                except ValueError as error:
                    log_provider_failure(
                        self.name,
                        context,
                        error,
                        started_at,
                        client_country_code=route.country_code,
                        region=route.region,
                        api_host=urlsplit(route.base_url).hostname or "",
                        attempt=attempt,
                        retryable=True,
                        response_status=tikhub_response_status_code(response),
                    )
                    if attempt < max_attempts:
                        continue
                    raise
                provider_response = build_tikhub_provider_response(data)
                retryable = is_retryable_tikhub_failure(
                    response,
                    provider_response,
                )
                unusable_payload = is_unusable_tikhub_payload(
                    data,
                    provider_response,
                )
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
                    client_country_code=route.country_code,
                    region=route.region,
                    api_host=urlsplit(route.base_url).hostname or "",
                    attempt=attempt,
                    retryable=retryable,
                    unusable_payload=unusable_payload,
                )
                try:
                    response.raise_for_status()
                except requests.HTTPError:
                    if retryable and attempt < max_attempts:
                        continue
                    raise
                if unusable_payload and attempt < max_attempts:
                    continue
                if provider_response.ok or not retryable or attempt >= max_attempts:
                    return data
            finally:
                response.close()

        raise RuntimeError("TikHub request attempts exhausted")

    def resolve_validated_candidate(self, adapter, context):
        route = self.get_request_route(context)
        params = self.build_request_params(context, route=route)
        validated_cache_key = build_tikhub_validated_cache_key(
            context,
            params,
            route.base_url,
        )
        cached = get_cached_validated_candidate(
            validated_cache_key,
            adapter.media_host_suffixes,
        )
        if cached:
            log_provider_event(
                "douyin_provider_validated_cache_hit",
                self.name,
                context,
                cache_hit=True,
                selected_size_bytes=cached.candidate.size_bytes,
            )
            return cached

        data = self.fetch_high_quality_data(context)
        provider_response = build_tikhub_provider_response(data)
        if not provider_response.ok:
            log_provider_event(
                "douyin_provider_rejected",
                self.name,
                context,
                fallback_reason="provider_response_not_ok",
                code=provider_response.code,
                provider_message=provider_response.message,
                request_id=provider_response.request_id,
            )
            return None

        candidates = provider_response.candidates
        if not candidates:
            log_provider_event(
                "douyin_provider_no_candidates",
                self.name,
                context,
                code=provider_response.code,
                provider_message=provider_response.message,
                request_id=provider_response.request_id,
            )
            return None

        declared_content_type = get_tikhub_declared_content_type(data)
        declared_candidate = candidates[0]
        if (
            declared_candidate.size_bytes > 0
            and declared_content_type in {"video/mp4", "application/octet-stream"}
        ):
            validate_external_url(
                declared_candidate.url,
                adapter.media_host_suffixes,
                "TikHub URL 不在允许的媒体域名内",
            )
            validated = ValidatedProviderCandidate(
                candidate=declared_candidate,
                final_url=declared_candidate.url,
                content_type=declared_content_type,
                cache_hit=False,
                candidate_count=len(candidates),
                code=provider_response.code,
                message=provider_response.message,
                request_id=provider_response.request_id,
            )
            cache_validated_candidate(
                validated_cache_key,
                validated,
                self.provider_settings.tikhub_validated_cache_ttl_seconds,
            )
            log_provider_event(
                "douyin_provider_declared_candidate_accepted",
                self.name,
                context,
                candidate_count=len(candidates),
                selected_size_bytes=declared_candidate.size_bytes,
                content_type=declared_content_type,
            )
            return validated

        remaining_budget = (
            context.deadline_monotonic - monotonic()
            if context.deadline_monotonic
            else self.provider_settings.tikhub_probe_timeout_seconds
        )
        if remaining_budget <= 0:
            raise FutureTimeoutError("Douyin ultra-HD deadline reached before CDN probe")

        probe_started_at = time()
        selection = select_best_media_candidate(
            candidates,
            headers=adapter.headers,
            allowed_final_host_suffixes=adapter.media_host_suffixes,
            timeout=min(
                self.provider_settings.tikhub_probe_timeout_seconds,
                remaining_budget,
            ),
            max_candidates=self.provider_settings.tikhub_max_probe_candidates,
            expected_type="video",
            probe_strategy="range_first",
            probe_fallback=False,
        )
        probe_duration_ms = duration_ms(probe_started_at)
        selected_diagnostic = selection.selected_diagnostic
        if (
            not selection.candidate
            or not selection.candidate.size_bytes
            or not selected_diagnostic
            or not selected_diagnostic.reachable
        ):
            log_provider_event(
                "douyin_provider_unvalidated",
                self.name,
                context,
                candidate_count=len(candidates),
                probe_duration_ms=probe_duration_ms,
                fallback_reason="no_validated_candidate",
            )
            return None

        selected_url = selected_diagnostic.final_url or selection.candidate.url
        validate_external_url(
            selected_url,
            adapter.media_host_suffixes,
            "TikHub URL 跳转到了不允许的媒体域名",
        )
        candidate = replace(selection.candidate, url=selected_url)
        validated = ValidatedProviderCandidate(
            candidate=candidate,
            final_url=selected_diagnostic.final_url,
            content_type=selected_diagnostic.content_type,
            cache_hit=False,
            candidate_count=len(candidates),
            probe_duration_ms=probe_duration_ms,
            code=provider_response.code,
            message=provider_response.message,
            request_id=provider_response.request_id,
        )
        cache_validated_candidate(
            validated_cache_key,
            validated,
            self.provider_settings.tikhub_validated_cache_ttl_seconds,
        )
        return validated

    def run_fast_paths(self, adapter, context):
        deadline = context.deadline_monotonic or (
            monotonic() + max(
                0.1,
                self.provider_settings.ultra_total_timeout_seconds,
            )
        )
        remaining_budget = deadline - monotonic()
        if remaining_budget <= 0:
            direct_result = (
                context.direct_result
                if context.direct_result and context.direct_result.assets
                else None
            )
            return None, direct_result

        validated_cache_key = build_tikhub_validated_cache_key(
            context,
            self.build_request_params(context),
            self.get_request_route(context).base_url,
        )
        cached = get_cached_validated_candidate(
            validated_cache_key,
            adapter.media_host_suffixes,
        )
        if cached:
            log_provider_event(
                "douyin_provider_validated_cache_hit",
                self.name,
                context,
                cache_hit=True,
                selected_size_bytes=cached.candidate.size_bytes,
            )
            return cached, None

        native_future = DOUYIN_ULTRA_EXECUTOR.submit(
            get_fast_native_result,
            adapter,
            context,
            min(
                self.provider_settings.ultra_native_timeout_seconds,
                remaining_budget,
            ),
        )
        tikhub_future = DOUYIN_ULTRA_EXECUTOR.submit(
            self.resolve_validated_candidate,
            adapter,
            context,
        )

        validated = None
        try:
            validated = tikhub_future.result(
                timeout=max(0, deadline - monotonic()),
            )
        except FutureTimeoutError:
            tikhub_future.cancel()
            log_provider_event(
                "douyin_provider_budget_exceeded",
                self.name,
                context,
                total_timeout_seconds=self.provider_settings.ultra_total_timeout_seconds,
            )
        except Exception as error:
            log_provider_failure(self.name, context, error)

        if validated:
            native_result = None
            if native_future.done():
                try:
                    native_result = native_future.result(timeout=0)
                except Exception as error:
                    log_provider_failure("native_fast", context, error)
            else:
                native_future.cancel()
            return validated, native_result

        native_result = None
        try:
            native_result = native_future.result(
                timeout=max(0, deadline - monotonic()),
            )
        except FutureTimeoutError:
            native_future.cancel()
            log_provider_event(
                "douyin_native_budget_exceeded",
                "native_fast",
                context,
                total_timeout_seconds=self.provider_settings.ultra_total_timeout_seconds,
            )
        except Exception as error:
            log_provider_failure("native_fast", context, error)

        return validated, native_result

    def parse(self, adapter, context):
        if not self.provider_settings.provider_is_enabled(self.name):
            return None

        parse_started_at = time()
        validated, native_result = self.run_fast_paths(adapter, context)
        if not validated:
            return native_result

        candidate = validated.candidate
        native_size = get_largest_video_size(native_result)
        result = build_external_parse_result(
            adapter,
            context,
            candidate,
            native_result,
            provider_name=self.name,
        )
        result = with_parse_metadata(
            result,
            selected_provider=self.name,
            fallback_reason="tikhub_validated",
            tikhub_size_bytes=candidate.size_bytes,
            native_size_bytes=native_size,
            tikhub_candidate_count=validated.candidate_count,
            tikhub_probe_duration_ms=validated.probe_duration_ms,
            tikhub_cache_hit=validated.cache_hit,
            tikhub_code=validated.code,
            tikhub_message=validated.message,
            tikhub_request_id=validated.request_id,
        )
        log_provider_event(
            "douyin_provider_selected",
            self.name,
            context,
            parse_started_at,
            fallback_reason="tikhub_validated",
            selected_size_bytes=candidate.size_bytes,
            native_size_bytes=native_size,
            candidate_count=validated.candidate_count,
            probe_duration_ms=validated.probe_duration_ms,
            cache_hit=validated.cache_hit,
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
            provider_order=("native",),
        )
    else:
        provider_settings = replace(
            provider_settings,
            # The current ultra-HD path prefers the official TianTianJieXi
            # API, keeps KuKuTool as fallback, and leaves TikHub/enhanced
            # native for diagnostics outside the synchronous request path.
            provider_order=("tiantianjiexi", "kukutool", "native"),
        )

    provider_map = {
        "tiantianjiexi": lambda: DouyinTianTianJieXiProvider(provider_settings=provider_settings),
        "kukutool": lambda: DouyinKukuToolProvider(provider_settings=provider_settings),
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
