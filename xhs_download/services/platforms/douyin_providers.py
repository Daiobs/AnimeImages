from concurrent.futures import (
    FIRST_COMPLETED,
    ThreadPoolExecutor,
    TimeoutError as FutureTimeoutError,
    wait,
)
from dataclasses import dataclass, replace
import hashlib
import inspect
import json
import logging
import re
from time import monotonic, sleep, time
from urllib.parse import urljoin, urlsplit

import requests
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from ..assets import MediaAsset, ParseResult
from ..candidates import MediaCandidate
from ..common import XhsProviderTimeoutError, clean_filename, validate_external_url
from ..diagnostics import select_best_media_candidate
from ..douyin_continuation import (
    DouyinContinuationCoordinator,
    DouyinContinuationPendingError,
)
from ..env_config import runtime_bool
from ..provider_settings import get_platform_provider_settings, normalize_provider_order
from ..provider_manifest import build_provider_manifest_token
from ..provider_reliability import (
    ProviderFailureClass,
    ProviderOutcome,
    classify_provider_failure,
    provider_continuation_active,
    record_provider_attempt,
    safe_media_host,
)
from .douyin_kukutool import DouyinKukuToolProvider, submit_provider_task
from .douyin_media_policy import validate_douyin_media_url
from .tiantianjiexi_generic import (
    TianTianJieXiFailureCooldownError,
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
DOUYIN_AWEME_ID_PATTERN = re.compile(r"\d{15,25}")
DOUYIN_INPUT_KINDS = frozenset(
    {
        "short_link",
        "video_url",
        "note_url",
        "share_url",
        "modal_url",
        "query_id",
        "other",
    }
)
SENSITIVE_LOG_QUERY_RE = re.compile(
    r"(?i)([?&](?:token|sign|signature|auth_key|x-signature|x_bogus|x-bogus|msToken|verifyFp|password|phone)=)[^&\s]+"
)
URL_LOG_RE = re.compile(r"https?://[^\s]+")


def sanitize_provider_log_text(value):
    text = str(value or "")
    text = SENSITIVE_LOG_QUERY_RE.sub(r"\1[redacted]", text)
    text = URL_LOG_RE.sub("[url-redacted]", text)
    return text[:300]


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


def normalize_douyin_aweme_id(value):
    value = str(value or "").strip()
    return value if DOUYIN_AWEME_ID_PATTERN.fullmatch(value) else ""


def build_douyin_source_identity(aweme_id):
    aweme_id = normalize_douyin_aweme_id(aweme_id)
    return f"douyin:{aweme_id}" if aweme_id else ""


def build_douyin_canonical_url(aweme_id):
    aweme_id = normalize_douyin_aweme_id(aweme_id)
    return f"https://www.douyin.com/video/{aweme_id}" if aweme_id else ""


def context_log_fields(context):
    safe_source_url = (
        getattr(context, "canonical_url", "")
        or getattr(context, "resolved_url", "")
        or getattr(context, "url", "")
        or ""
    )
    parsed_url = urlsplit(safe_source_url)
    input_kind = str(getattr(context, "input_kind", "other") or "other")
    if input_kind not in DOUYIN_INPUT_KINDS:
        input_kind = "other"
    return {
        "aweme_id": getattr(context, "aweme_id", ""),
        "source_host": parsed_url.hostname or "",
        "source_path": parsed_url.path or "",
        "input_kind": input_kind,
        "source_resolution_ms": max(
            0,
            int(getattr(context, "source_resolution_duration_ms", 0) or 0),
        ),
        "aweme_identity_resolved": bool(
            getattr(context, "source_identity_resolved", False)
        ),
        "ultra_window_ms": max(
            0,
            int(getattr(context, "ultra_window_ms", 0) or 0),
        ),
        "provider_window_started_after_resolution": bool(
            getattr(
                context,
                "provider_window_started_after_resolution",
                False,
            )
        ),
        "provider_budget_ms": max(
            0,
            int(getattr(context, "provider_budget_ms", 0) or 0),
        ),
    }


def log_provider_failure(provider, context, error, started_at=None, **extra_fields):
    fields = {
        "provider": provider,
        **context_log_fields(context),
        "error_type": type(error).__name__,
        "error": sanitize_provider_log_text(error),
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
    raw_url: str = ""
    final_url: str = ""
    resolved_url: str = ""
    html: str = ""
    aweme_id: str = ""
    canonical_url: str = ""
    source_identity: str = ""
    input_kind: str = "other"
    source_resolution_duration_ms: int = 0
    source_identity_resolved: bool = False
    source_resolution_completed_monotonic: float = 0
    provider_window_started_monotonic: float = 0
    provider_window_started_after_resolution: bool = False
    ultra_window_ms: int = 0
    provider_budget_ms: int = 0
    prefer_ultra_hd: bool = False
    direct_result: object | None = None
    direct_error: Exception | None = None
    deadline_monotonic: float = 0
    client_country_code: str = ""
    allow_continuation: bool = False
    continuation_identity: str = ""
    ultra_attempt_controller: object | None = None
    native_fallback_future: object | None = None
    ultra_retry_after_seconds: int = 0
    continuation_deadline_monotonic: float = 0

    def __post_init__(self):
        self.raw_url = self.raw_url or self.url
        self.resolved_url = self.resolved_url or self.final_url or self.url
        normalized_aweme_id = normalize_douyin_aweme_id(self.aweme_id)
        if normalized_aweme_id:
            self.aweme_id = normalized_aweme_id
            self.canonical_url = (
                self.canonical_url
                or build_douyin_canonical_url(normalized_aweme_id)
            )
            self.source_identity = (
                self.source_identity
                or build_douyin_source_identity(normalized_aweme_id)
            )
            identity_sources = (
                self.raw_url,
                self.final_url,
                self.resolved_url,
            )
            self.source_identity_resolved = bool(
                self.source_identity_resolved
                or any(
                    re.search(
                        rf"(?<!\d){re.escape(normalized_aweme_id)}(?!\d)",
                        str(source or ""),
                    )
                    for source in identity_sources
                )
            )
        if self.input_kind not in DOUYIN_INPUT_KINDS:
            self.input_kind = "other"

    def record_ultra_retry_after(self, retry_after):
        self.ultra_retry_after_seconds = max(
            self.ultra_retry_after_seconds,
            max(0, int(retry_after or 0)),
        )


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


def get_reference_result(adapter, context, *, allow_network=True):
    cached = get_cached_douyin_metadata(context.aweme_id)
    if cached and cached.assets:
        return cached
    if context.direct_result and context.direct_result.assets:
        return context.direct_result
    if allow_network and context.aweme_id:
        started_at = time()
        try:
            request_options = {}
            if context.deadline_monotonic:
                remaining = context.deadline_monotonic - monotonic()
                if remaining <= 0:
                    raise XhsProviderTimeoutError("抖音参考解析超过时间限制")
                request_options = {
                    "request_timeout": max(0.001, min(15.0, remaining)),
                    "request_deadline": context.deadline_monotonic,
                }
            result = adapter.fetch_mobile_feed_result(
                context.aweme_id,
                **request_options,
            )
            if result and result.assets:
                return result
        except Exception as error:
            log_provider_failure("native_reference_feed", context, error, started_at)
            return None
    return None


def call_native_fetcher(fetcher, aweme_id, timeout_seconds, deadline_monotonic=0):
    parameters = inspect.signature(fetcher).parameters
    request_options = {}
    if "request_timeout" in parameters:
        request_options["request_timeout"] = max(0.1, timeout_seconds)
    if "request_retries" in parameters:
        request_options["request_retries"] = 1
    if "request_deadline" in parameters:
        request_options["request_deadline"] = deadline_monotonic
    return fetcher(aweme_id, **request_options)


def complete_native_attempt_observation(observation, **fields):
    if observation is None:
        record_provider_attempt(**fields)
        return
    observation["fields"] = fields


def record_native_attempt_observation(fields):
    record_provider_attempt(**fields)


def attach_native_attempt_observation(future, observation):
    def record_after_completion(_future):
        fields = observation.pop("fields", None)
        if fields:
            submit_provider_task(
                "provider-observation",
                1,
                256,
                record_native_attempt_observation,
                fields,
            )

    future.add_done_callback(record_after_completion)
    return future


def get_fast_native_result(
    adapter,
    context,
    timeout_seconds=5.0,
    attempt_observation=None,
):
    started_at = time()
    external_call = False
    last_error = None
    try:
        if context.direct_result and context.direct_result.assets:
            result = context.direct_result
        elif context.aweme_id:
            native_deadline = monotonic() + max(0.1, timeout_seconds)
            if context.deadline_monotonic:
                native_deadline = min(native_deadline, context.deadline_monotonic)
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
                    external_call = True
                    result = call_native_fetcher(
                        fetcher,
                        context.aweme_id,
                        request_budget,
                        native_deadline,
                    )
                except Exception as error:
                    last_error = error
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
            asset = get_reference_video_asset(result)
            complete_native_attempt_observation(
                attempt_observation,
                provider="native",
                outcome=(
                    ProviderOutcome.SUCCESS_FRESH
                    if external_call
                    else ProviderOutcome.SUCCESS_EXISTING_RESULT
                ),
                external_call=external_call,
                elapsed_ms=duration_ms(started_at),
                candidate_count=len(result.video_assets),
                media_host=safe_media_host(getattr(asset, "url", "")),
                expected_bytes=getattr(asset, "size_bytes", 0),
            )
            return result
    except Exception as error:
        log_provider_failure("native_fast", context, error, started_at)
        outcome, failure_class = classify_provider_failure(error)
        complete_native_attempt_observation(
            attempt_observation,
            provider="native",
            outcome=outcome,
            external_call=external_call,
            elapsed_ms=duration_ms(started_at),
            failure_class=failure_class,
        )
        return None
    outcome = ProviderOutcome.FAILURE_NO_MEDIA
    failure_class = ProviderFailureClass.NO_MEDIA
    if last_error is not None:
        outcome, failure_class = classify_provider_failure(last_error)
    complete_native_attempt_observation(
        attempt_observation,
        provider="native",
        outcome=outcome,
        external_call=external_call,
        elapsed_ms=duration_ms(started_at),
        failure_class=failure_class,
    )
    return None


def get_reference_video_asset(result):
    if not result:
        return None
    return result.video_assets[0] if result.video_assets else None


def safe_external_base_filename(value):
    value = normalize_douyin_display_title(value)
    if not value:
        return ""
    return clean_filename(value)[:200]


def normalize_douyin_display_title(value):
    value = " ".join(str(value or "").split())
    if not value:
        return ""
    parsed_value = value
    if value.startswith("//"):
        parsed_value = f"https:{value}"
    elif value.lower().startswith("www."):
        parsed_value = f"https://{value}"
    parsed = urlsplit(parsed_value)
    if parsed.scheme in {"http", "https"} and parsed.hostname:
        return ""
    return value[:200]


def source_url_matches_aweme_id(source_url, aweme_id):
    aweme_id = str(aweme_id or "").strip()
    if not re.fullmatch(r"\d{8,24}", aweme_id):
        return False
    return bool(
        re.search(
            rf"(?<!\d){re.escape(aweme_id)}(?!\d)",
            str(source_url or ""),
        )
    )


def build_external_parse_result(
    adapter,
    context,
    candidate,
    reference_result=None,
    provider_name="",
    base_filename="",
    cover_url="",
):
    reference_metadata = (
        reference_result.metadata
        if reference_result and isinstance(reference_result.metadata, dict)
        else {}
    )
    reference_title = normalize_douyin_display_title(
        reference_metadata.get("title")
    )
    provider_title = normalize_douyin_display_title(base_filename)
    display_title = reference_title or provider_title
    reference_asset = get_reference_video_asset(reference_result)
    reference_cover_url = (
        str(getattr(reference_asset, "cover_url", "") or "")
        if reference_asset
        else ""
    )
    resolved_cover_url = reference_cover_url or cover_url
    reference_preview_url = (
        str(getattr(reference_asset, "cover_preview_url", "") or "")
        if reference_asset
        else ""
    )
    resolved_preview_url = reference_preview_url or resolved_cover_url
    reference_filename = safe_external_base_filename(
        getattr(reference_result, "base_filename", "")
    )
    base_filename = reference_filename or safe_external_base_filename(provider_title)
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
    result_metadata = {}
    if display_title:
        result_metadata = {
            "title": display_title,
            "title_source": (
                "reference_metadata" if reference_title else "provider_payload"
            ),
        }
    return ParseResult(
        platform=adapter.platform_id,
        post_type="video",
        base_filename=base_filename,
        assets=(
            MediaAsset(
                media_type="video",
                url=candidate.url,
                platform=adapter.platform_id,
                cover_url=resolved_cover_url,
                cover_preview_url=resolved_preview_url,
                quality_label=candidate.quality_label,
                size_bytes=candidate.size_bytes,
                headers=adapter.headers,
                media_host_suffixes=adapter.media_host_suffixes,
                **provider_fields,
            ),
        ),
        metadata=result_metadata,
    )


class DouyinTianTianJieXiProvider(DouyinResultProvider):
    name = "tiantianjiexi"

    def __init__(
        self,
        provider_settings=None,
        provider=None,
        *,
        reserve_fallback_grace=True,
        suppress_selected_log=False,
    ):
        self.provider_settings = provider_settings or get_platform_provider_settings("douyin")
        self.provider = provider or TianTianJieXiGenericProvider()
        self.reserve_fallback_grace = bool(reserve_fallback_grace)
        self.suppress_selected_log = bool(suppress_selected_log)

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
            declared_size = resource.get("size")
            if (
                isinstance(declared_size, (int, float))
                and declared_size < 0
            ):
                continue
            try:
                validate_douyin_media_url(
                    url,
                    "天天解析返回了不受信任的抖音媒体域名",
                    existing_suffixes=adapter.media_host_suffixes,
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

    def clean_cover_url(
        self,
        payload,
        adapter,
        *,
        allow_response_image=False,
    ):
        cover_url = str(payload.get("cover") or "").strip()
        if not cover_url and allow_response_image and payload.get("videos"):
            raw_response_images = payload.get("images") or ()
            response_images = (
                (raw_response_images,)
                if isinstance(raw_response_images, str)
                else tuple(raw_response_images)
                if isinstance(raw_response_images, (list, tuple))
                else ()
            )
            if len(response_images) == 1:
                cover_url = str(response_images[0] or "").strip()
        if not cover_url:
            return ""
        try:
            validate_douyin_media_url(
                cover_url,
                "天天解析封面 URL 不在允许的抖音媒体域名内",
                existing_suffixes=adapter.media_host_suffixes,
            )
        except (TypeError, ValueError):
            return ""
        return cover_url

    def parse(self, adapter, context):
        if not self.provider_settings.provider_is_enabled(self.name):
            return None

        source_url = (
            context.canonical_url
            or build_douyin_canonical_url(context.aweme_id)
            or context.resolved_url
            or context.final_url
            or context.url
        )
        source_identity = (
            context.source_identity
            or build_douyin_source_identity(context.aweme_id)
        )
        if not source_url:
            return None

        parse_started_at = time()
        remaining_budget = self.remaining_budget(
            context,
            self.provider_settings.ultra_total_timeout_seconds,
        )
        fallback_grace = 0
        if (
            self.reserve_fallback_grace
            and "kukutool" in self.provider_settings.enabled_provider_order
        ):
            fallback_grace = max(
                self.provider_settings.ultra_kukutool_grace_seconds,
                self.provider_settings.kukutool_sidecar_timeout_seconds,
            )
        provider_budget = (
            max(0.1, remaining_budget - fallback_grace)
            if remaining_budget > fallback_grace
            else remaining_budget
        )
        timeout_seconds = min(
            max(0.1, self.provider_settings.ultra_total_timeout_seconds),
            provider_budget,
        )
        if timeout_seconds <= 0:
            log_provider_event(
                "douyin_provider_budget_exhausted",
                self.name,
                context,
                parse_started_at,
                remaining_budget_ms=int(max(0, remaining_budget) * 1000),
                assigned_timeout_ms=0,
                reserve_fallback_grace=self.reserve_fallback_grace,
                fallback_grace_ms=int(max(0, fallback_grace) * 1000),
                error_type="budget_exhausted",
            )
            raise XhsProviderTimeoutError("天天解析接口超时")
        log_provider_event(
            "douyin_provider_budget_assigned",
            self.name,
            context,
            parse_started_at,
            remaining_budget_ms=int(max(0, remaining_budget) * 1000),
            assigned_timeout_ms=int(max(0, timeout_seconds) * 1000),
            reserve_fallback_grace=self.reserve_fallback_grace,
            fallback_grace_ms=int(max(0, fallback_grace) * 1000),
            cache_hit=False,
        )
        try:
            parse_options = {"timeout_seconds": timeout_seconds}
            if context.ultra_attempt_controller is not None:
                parse_options["attempt_controller"] = (
                    context.ultra_attempt_controller
                )
            provider_parse_parameters = inspect.signature(
                self.provider.parse
            ).parameters
            if (
                "payload_validator" in provider_parse_parameters
                or any(
                    parameter.kind == inspect.Parameter.VAR_KEYWORD
                    for parameter in provider_parse_parameters.values()
                )
            ):
                parse_options["payload_validator"] = lambda payload: bool(
                    self.build_candidates(payload, adapter)
                )
            if source_identity and (
                "source_identity" in provider_parse_parameters
                or any(
                    parameter.kind == inspect.Parameter.VAR_KEYWORD
                    for parameter in provider_parse_parameters.values()
                )
            ):
                parse_options["source_identity"] = source_identity
            if (
                context.allow_continuation
                and context.continuation_deadline_monotonic
                and (
                    "deadline_monotonic" in provider_parse_parameters
                    or any(
                        parameter.kind == inspect.Parameter.VAR_KEYWORD
                        for parameter in provider_parse_parameters.values()
                    )
                )
            ):
                parse_options["deadline_monotonic"] = (
                    context.continuation_deadline_monotonic
                )
            if (
                context.allow_continuation
                and (
                    "continuation_checker" in provider_parse_parameters
                    or any(
                        parameter.kind == inspect.Parameter.VAR_KEYWORD
                        for parameter in provider_parse_parameters.values()
                    )
                )
            ):
                parse_options["continuation_checker"] = (
                    lambda: provider_continuation_active(context)
                )
            payload = self.provider.parse(source_url, **parse_options)
        except TianTianJieXiFailureCooldownError as error:
            context.record_ultra_retry_after(error.retry_after_seconds)
            raise
        except Exception as error:
            log_provider_failure(
                self.name,
                context,
                error,
                parse_started_at,
                remaining_budget_ms=int(max(0, remaining_budget) * 1000),
                assigned_timeout_ms=int(max(0, timeout_seconds) * 1000),
                reserve_fallback_grace=self.reserve_fallback_grace,
                fallback_grace_ms=int(max(0, fallback_grace) * 1000),
                cache_hit=False,
            )
            raise
        candidates = self.build_candidates(payload, adapter)
        if not candidates:
            log_provider_event(
                "douyin_provider_candidates_empty",
                self.name,
                context,
                parse_started_at,
                remaining_budget_ms=int(max(0, remaining_budget) * 1000),
                assigned_timeout_ms=int(max(0, timeout_seconds) * 1000),
                reserve_fallback_grace=self.reserve_fallback_grace,
                fallback_grace_ms=int(max(0, fallback_grace) * 1000),
                candidate_count=0,
                error_type="empty_candidates",
            )
            raise ValueError("天天解析未返回可用抖音视频资源")

        provider_title = safe_external_base_filename(payload.get("title"))
        if not provider_title:
            provider_title = safe_external_base_filename(
                payload.get("description")
            )
        source_identity_verified = bool(
            context.source_identity_resolved
            and source_url_matches_aweme_id(
                source_url,
                context.aweme_id,
            )
        )
        provider_cover_url = self.clean_cover_url(
            payload,
            adapter,
            allow_response_image=source_identity_verified,
        )
        # Reuse existing native metadata when available. When the provider
        # already supplied usable display metadata, do not spend the remaining
        # media deadline on an optional native enrichment request.
        reference_result = get_reference_result(
            adapter,
            context,
            allow_network=not (provider_title and provider_cover_url),
        )
        candidate = candidates[0]
        result = build_external_parse_result(
            adapter,
            context,
            candidate,
            reference_result,
            provider_name=self.name,
            base_filename=provider_title,
            cover_url=provider_cover_url,
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
        if not self.suppress_selected_log:
            log_provider_event(
                "douyin_provider_selected",
                self.name,
                context,
                parse_started_at,
                fallback_reason="tiantianjiexi_official_api",
                selected_size_bytes=candidate.size_bytes,
                candidate_count=len(candidates),
                quality_label=candidate.quality_label,
                selected_quality=candidate.quality_label,
                remaining_budget_ms=int(max(0, remaining_budget) * 1000),
                assigned_timeout_ms=int(max(0, timeout_seconds) * 1000),
                reserve_fallback_grace=self.reserve_fallback_grace,
                fallback_grace_ms=int(max(0, fallback_grace) * 1000),
                cache_hit=False,
            )
        return result


class DouyinUltraRaceProvider(DouyinResultProvider):
    name = "ultra_race"
    weak_candidate_settle_seconds = 1.0
    strong_candidate_min_size_bytes = 50 * 1024 * 1024
    strong_candidate_min_quality_rank = 500
    external_provider_names = frozenset(
        {"kukutool", "tiantianjiexi"}
    )

    def __init__(self, provider_settings=None, providers=None):
        self.provider_settings = provider_settings or get_platform_provider_settings("douyin")
        self.providers = tuple(providers or (
            DouyinTianTianJieXiProvider(
                provider_settings=self.provider_settings,
                reserve_fallback_grace=False,
                suppress_selected_log=True,
            ),
            DouyinKukuToolProvider(
                provider_settings=self.provider_settings,
                suppress_selected_log=True,
            ),
        ))

    def result_score(self, result):
        video_assets = getattr(result, "video_assets", ())
        if not video_assets:
            return (0, 0)
        best_asset = max(
            video_assets,
            key=lambda asset: (
                tiantianjiexi_quality_rank(getattr(asset, "quality_label", "")),
                int(getattr(asset, "size_bytes", 0) or 0),
            ),
        )
        return (
            tiantianjiexi_quality_rank(getattr(best_asset, "quality_label", "")),
            int(getattr(best_asset, "size_bytes", 0) or 0),
        )

    def result_provider_name(self, result, fallback_name=""):
        metadata = getattr(result, "metadata", None) or {}
        provider_name = str(
            metadata.get("selected_provider")
            or metadata.get("source_provider")
            or ""
        ).strip().lower()
        if not provider_name:
            video_assets = getattr(result, "video_assets", ())
            provider_name = next(
                (
                    str(getattr(asset, "provider", "") or "")
                    .strip()
                    .lower()
                    for asset in video_assets
                    if getattr(asset, "provider", "")
                ),
                "",
            )
        return provider_name or str(fallback_name or "").strip().lower()

    def result_is_strong_candidate(self, result):
        if (
            self.result_provider_name(result)
            not in self.external_provider_names
        ):
            return False
        quality_score, size_bytes = self.result_score(result)
        return (
            quality_score >= self.strong_candidate_min_quality_rank
            or size_bytes >= self.strong_candidate_min_size_bytes
        )

    def result_log_fields(self, result):
        video_assets = getattr(result, "video_assets", ())
        if not video_assets:
            return {
                "candidate_count": 0,
                "selected_quality": "",
                "selected_size_bytes": 0,
                "cache_hit": False,
            }
        best_asset = max(
            video_assets,
            key=lambda asset: (
                tiantianjiexi_quality_rank(getattr(asset, "quality_label", "")),
                int(getattr(asset, "size_bytes", 0) or 0),
            ),
        )
        metadata = getattr(result, "metadata", {}) or {}
        provider_name = str(
            metadata.get("selected_provider")
            or metadata.get("source_provider")
            or getattr(best_asset, "provider", "")
            or ""
        )
        return {
            "candidate_count": int(
                metadata.get(f"{provider_name}_candidate_count")
                or metadata.get("kukutool_candidate_count")
                or metadata.get("tiantianjiexi_candidate_count")
                or len(video_assets)
            ),
            "selected_quality": getattr(best_asset, "quality_label", "") or "",
            "selected_size_bytes": int(getattr(best_asset, "size_bytes", 0) or 0),
            "cache_hit": bool(
                metadata.get(f"{provider_name}_cache_hit")
                or metadata.get("kukutool_cache_hit")
                or metadata.get("tiantianjiexi_cache_hit")
            ),
        }

    def finalize_result(self, result, provider_name, enabled_count, context, started_at):
        effective_provider_name = self.result_provider_name(
            result,
            provider_name,
        )
        result_fields = self.result_log_fields(result)
        log_provider_event(
            "douyin_ultra_race_selected",
            effective_provider_name,
            context,
            started_at,
            race_provider_count=enabled_count,
            race_winner=effective_provider_name,
            race_loser_count=max(0, enabled_count - 1),
            **result_fields,
        )
        log_provider_event(
            "douyin_ultra_race_winner",
            effective_provider_name,
            context,
            started_at,
            race_winner=effective_provider_name,
            race_loser_count=max(0, enabled_count - 1),
            **result_fields,
        )
        retry_after = 0
        if not self.result_is_strong_candidate(result):
            retry_after = max(
                context.ultra_retry_after_seconds,
                getattr(
                    context.ultra_attempt_controller,
                    "cooldown_retry_after",
                    0,
                ),
            )
        return with_parse_metadata(
            self.decorate_result(
                result,
                effective_provider_name,
                enabled_count,
            ),
            ultra_retry_after_seconds=retry_after,
        )

    def decorate_result(self, result, provider_name, enabled_count):
        effective_provider_name = self.result_provider_name(
            result,
            provider_name,
        )
        return with_parse_metadata(
            result,
            selected_provider=effective_provider_name,
            source_provider=effective_provider_name,
            ultra_race_winner=effective_provider_name,
            ultra_race_provider_count=enabled_count,
        )

    def parse(self, adapter, context):
        enabled_providers = tuple(
            provider
            for provider in self.providers
            if self.provider_settings.provider_is_enabled(getattr(provider, "name", ""))
        )
        if not enabled_providers:
            return None

        deadline = context.deadline_monotonic or (
            monotonic() + max(0.1, self.provider_settings.ultra_total_timeout_seconds)
        )
        if deadline <= monotonic():
            raise XhsProviderTimeoutError("抖音超清 Provider 超时")

        race_started_at = time()
        controller = context.ultra_attempt_controller
        if controller is not None:
            controller.configure_providers(
                getattr(provider, "name", "unknown")
                for provider in enabled_providers
            )

        continuation = None
        if context.allow_continuation:
            continuation = DouyinContinuationCoordinator(
                len(enabled_providers),
                settle_seconds=self.weak_candidate_settle_seconds,
                source_identity=context.aweme_id,
                client_identity=context.continuation_identity,
                retry_after_getter=lambda: max(
                    context.ultra_retry_after_seconds,
                    getattr(
                        context.ultra_attempt_controller,
                        "cooldown_retry_after",
                        0,
                    ),
                ),
            )

        native_budget = min(
            max(
                0.1,
                float(
                    getattr(
                        self.provider_settings,
                        "ultra_native_timeout_seconds",
                        3.0,
                    )
                    or 0
                ),
            ),
            max(0.1, deadline - monotonic()),
        )
        native_attempt_observation = {}
        native_future = submit_provider_task(
            "douyin-native",
            max(
                1,
                int(
                    getattr(
                        self.provider_settings,
                        "native_executor_workers",
                        4,
                    )
                    or 1
                ),
            ),
            max(
                1,
                int(
                    getattr(
                        self.provider_settings,
                        "native_max_pending_tasks",
                        8,
                    )
                    or 1
                ),
            ),
            get_fast_native_result,
            adapter,
            context,
            native_budget,
            native_attempt_observation,
        )
        if native_future is None:
            native_future = DOUYIN_ULTRA_EXECUTOR.submit(
                get_fast_native_result,
                adapter,
                context,
                native_budget,
                native_attempt_observation,
            )
            log_provider_event(
                "douyin_native_local_capacity_full",
                "native_fast",
                context,
                race_started_at,
            )
        context.native_fallback_future = native_future

        native_observation_attached = False

        def observe_native_attempt():
            nonlocal native_observation_attached
            if native_observation_attached:
                return
            native_observation_attached = True
            attach_native_attempt_observation(
                native_future,
                native_attempt_observation,
            )

        future_to_provider = {
            DOUYIN_ULTRA_EXECUTOR.submit(provider.parse, adapter, context): provider
            for provider in enabled_providers
        }
        if continuation:
            for future, provider in future_to_provider.items():
                provider_name = getattr(
                    provider,
                    "name",
                    type(provider).__name__,
                )
                future.add_done_callback(
                    lambda completed_future, name=provider_name: (
                        continuation.capture_future(
                            completed_future,
                            provider_name=name,
                            score_result=self.result_score,
                            is_strong_result=self.result_is_strong_candidate,
                            prepare_result=lambda result, selected=name: (
                                self.decorate_result(
                                    result,
                                    selected,
                                    len(enabled_providers),
                                )
                            ),
                        )
                    )
                )
        last_timeout_error = None
        best_result = None
        best_provider_name = ""
        best_score = (0, 0)
        native_fallback_result = None
        try:
            while future_to_provider:
                now = monotonic()
                remaining_budget = deadline - now
                if remaining_budget <= 0:
                    break
                done, _pending = wait(
                    future_to_provider,
                    timeout=max(0, remaining_budget),
                    return_when=FIRST_COMPLETED,
                )
                if not done:
                    break
                for future in done:
                    provider = future_to_provider.pop(future)
                    provider_name = getattr(provider, "name", type(provider).__name__)
                    try:
                        result = future.result()
                        if result and result.assets:
                            effective_provider = self.result_provider_name(
                                result,
                                provider_name,
                            )
                            if effective_provider not in self.external_provider_names:
                                native_fallback_result = result
                                continue
                            score = self.result_score(result)
                            if not best_result or score > best_score:
                                best_result = result
                                best_provider_name = provider_name
                                best_score = score
                            if self.result_is_strong_candidate(result):
                                if continuation:
                                    continuation.abandon()
                                for pending_future in future_to_provider:
                                    pending_provider = future_to_provider[pending_future]
                                    pending_provider_name = getattr(
                                        pending_provider,
                                        "name",
                                        type(pending_provider).__name__,
                                    )
                                    cancelled = pending_future.cancel()
                                    log_provider_event(
                                        (
                                            "douyin_ultra_race_loser_cancelled"
                                            if cancelled
                                            else "douyin_ultra_race_loser_completed"
                                        ),
                                        pending_provider_name,
                                        context,
                                        race_started_at,
                                        race_winner=provider_name,
                                        race_loser_count=max(0, len(enabled_providers) - 1),
                                    )
                                future_to_provider.clear()
                                observe_native_attempt()
                                return self.finalize_result(
                                    result,
                                    provider_name,
                                    len(enabled_providers),
                                    context,
                                    race_started_at,
                                )
                    except XhsProviderTimeoutError as error:
                        last_timeout_error = error
                        log_provider_failure(
                            provider_name,
                            context,
                            error,
                            race_started_at,
                            race_winner=best_provider_name,
                            race_loser_count=max(0, len(enabled_providers) - 1),
                        )
                    except Exception as error:
                        log_provider_failure(
                            provider_name,
                            context,
                            error,
                            race_started_at,
                            race_winner=best_provider_name,
                            race_loser_count=max(0, len(enabled_providers) - 1),
                        )
        finally:
            for future, provider in tuple(future_to_provider.items()):
                provider_name = getattr(provider, "name", type(provider).__name__)
                cancelled = False if continuation else future.cancel()
                log_provider_event(
                    (
                        "douyin_ultra_race_continuation_active"
                        if continuation
                        else "douyin_ultra_race_loser_cancelled"
                        if cancelled
                        else "douyin_ultra_race_loser_completed"
                    ),
                    provider_name,
                    context,
                    race_started_at,
                    race_winner=best_provider_name,
                    race_loser_count=max(0, len(enabled_providers) - 1),
                )

        observe_native_attempt()

        if controller is not None:
            context.record_ultra_retry_after(
                controller.cooldown_retry_after
            )
            controller.raise_if_blocked()

        if (
            controller is not None
            and controller.started
            and not self.result_is_strong_candidate(best_result)
            and monotonic() < deadline
        ):
            sleep(max(0.0, deadline - monotonic()))

        if (
            controller is not None
            and not controller.started
            and native_fallback_result is None
            and not native_future.done()
        ):
            try:
                native_fallback_result = native_future.result(
                    timeout=max(
                        0.0,
                        min(native_budget, deadline - monotonic()),
                    )
                )
            except Exception as error:
                log_provider_failure(
                    "native_fast",
                    context,
                    error,
                    race_started_at,
                )

        if native_fallback_result is None and native_future.done():
            try:
                native_fallback_result = native_future.result(timeout=0)
            except Exception as error:
                log_provider_failure(
                    "native_fast",
                    context,
                    error,
                    race_started_at,
                )

        if best_result:
            if continuation:
                (
                    continued_result,
                    continuation_pending,
                    continued_result_is_strong,
                ) = continuation.snapshot_result_state()
                if continued_result and continued_result_is_strong:
                    continuation.abandon()
                    return self.finalize_result(
                        continued_result,
                        self.result_provider_name(
                            continued_result,
                            best_provider_name,
                        ),
                        len(enabled_providers),
                        context,
                        race_started_at,
                    )
                if continuation_pending:
                    log_provider_event(
                        "douyin_ultra_foreground_timeout_native_fallback",
                        "native",
                        context,
                        race_started_at,
                        retry_after=context.ultra_retry_after_seconds,
                    )
                    raise DouyinContinuationPendingError(
                        continuation.task_id,
                        fallback_result=(
                            with_parse_metadata(
                                self.decorate_result(
                                    native_fallback_result,
                                    "native",
                                    len(enabled_providers),
                                ),
                                ultra_retry_after_seconds=max(
                                    context.ultra_retry_after_seconds,
                                    getattr(
                                        controller,
                                        "cooldown_retry_after",
                                        0,
                                    ),
                                ),
                            )
                            if native_fallback_result is not None
                            else None
                            or continued_result
                            or self.decorate_result(
                                best_result,
                                best_provider_name,
                                len(enabled_providers),
                            )
                        ),
                    )
                continuation.abandon()
            if (
                not self.result_is_strong_candidate(best_result)
                and native_fallback_result is not None
            ):
                return self.finalize_result(
                    native_fallback_result,
                    "native",
                    len(enabled_providers),
                    context,
                    race_started_at,
                )
            return self.finalize_result(
                best_result,
                best_provider_name,
                len(enabled_providers),
                context,
                race_started_at,
            )

        if continuation:
            (
                continued_result,
                continuation_pending,
                continued_result_is_strong,
            ) = continuation.snapshot_result_state()
            if continued_result and (
                continued_result_is_strong
                or not continuation_pending
            ):
                continuation.abandon()
                provider_name = str(
                    (continued_result.metadata or {}).get(
                        "selected_provider",
                        "",
                    )
                )
                return self.finalize_result(
                    continued_result,
                    provider_name or self.name,
                    len(enabled_providers),
                    context,
                    race_started_at,
                )
            if continuation_pending:
                log_provider_event(
                    "douyin_ultra_foreground_timeout_native_fallback",
                    "native",
                    context,
                    race_started_at,
                    retry_after=context.ultra_retry_after_seconds,
                )
                raise DouyinContinuationPendingError(
                    continuation.task_id,
                    fallback_result=(
                        with_parse_metadata(
                            self.decorate_result(
                                native_fallback_result,
                                "native",
                                len(enabled_providers),
                            ),
                            ultra_retry_after_seconds=max(
                                context.ultra_retry_after_seconds,
                                getattr(
                                    controller,
                                    "cooldown_retry_after",
                                    0,
                                ),
                            ),
                        )
                        if native_fallback_result is not None
                        else continued_result
                    ),
                )
            continuation.abandon()

        if native_fallback_result:
            return self.finalize_result(
                native_fallback_result,
                "native",
                len(enabled_providers),
                context,
                race_started_at,
            )

        log_provider_event(
            "douyin_ultra_race_exhausted",
            self.name,
            context,
            race_started_at,
            race_provider_count=len(enabled_providers),
        )
        if last_timeout_error:
            raise last_timeout_error
        return None


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
            deadline_monotonic=context.deadline_monotonic or None,
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

        native_attempt_observation = {}
        native_future = DOUYIN_ULTRA_EXECUTOR.submit(
            get_fast_native_result,
            adapter,
            context,
            min(
                self.provider_settings.ultra_native_timeout_seconds,
                remaining_budget,
            ),
            native_attempt_observation,
        )
        tikhub_future = DOUYIN_ULTRA_EXECUTOR.submit(
            self.resolve_validated_candidate,
            adapter,
            context,
        )
        attach_native_attempt_observation(
            native_future,
            native_attempt_observation,
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
        request_options = {
            "probe_video_candidates": bool(context.prefer_ultra_hd),
        }
        if context.deadline_monotonic:
            remaining = context.deadline_monotonic - monotonic()
            if remaining <= 0:
                raise XhsProviderTimeoutError("抖音本地解析超过时间限制")
            request_options.update(
                request_timeout=max(0.001, min(15.0, remaining)),
                request_deadline=context.deadline_monotonic,
            )
        return adapter.fetch_mobile_feed_result(
            context.aweme_id,
            **request_options,
        )


class DouyinMobileShareProvider(DouyinResultProvider):
    name = "mobile_share"

    def parse(self, adapter, context):
        if not context.aweme_id:
            return None
        request_options = {
            "probe_video_candidates": bool(context.prefer_ultra_hd),
        }
        if context.deadline_monotonic:
            remaining = context.deadline_monotonic - monotonic()
            if remaining <= 0:
                raise XhsProviderTimeoutError("抖音本地解析超过时间限制")
            request_options.update(
                request_timeout=max(0.001, min(15.0, remaining)),
                request_deadline=context.deadline_monotonic,
            )
        return adapter.fetch_mobile_share_result(
            context.aweme_id,
            **request_options,
        )


class DouyinNativeProvider(DouyinResultProvider):
    name = "native"

    def __init__(self, fallback_providers=None):
        self.fallback_providers = tuple(fallback_providers or (
            DouyinMobileFeedProvider(),
            DouyinMobileShareProvider(),
        ))

    def parse(self, adapter, context):
        parse_started_at = time()
        external_call = False
        last_error = None
        for provider in self.fallback_providers:
            started_at = time()
            try:
                external_call = external_call or bool(context.aweme_id)
                result = provider.parse(adapter, context)
                if result and result.assets:
                    result = with_parse_metadata(
                        result,
                        selected_provider=getattr(provider, "name", "native"),
                    )
                    asset = get_reference_video_asset(result)
                    record_provider_attempt(
                        provider="native",
                        outcome=ProviderOutcome.SUCCESS_FRESH,
                        external_call=external_call,
                        elapsed_ms=duration_ms(parse_started_at),
                        candidate_count=len(result.video_assets),
                        media_host=getattr(asset, "url", ""),
                        expected_bytes=getattr(asset, "size_bytes", 0),
                    )
                    return result
            except Exception as error:
                last_error = error
                log_provider_failure(getattr(provider, "name", "native"), context, error, started_at)
                continue

        if context.direct_result is not None and context.direct_result.assets:
            asset = get_reference_video_asset(context.direct_result)
            record_provider_attempt(
                provider="native",
                outcome=ProviderOutcome.SUCCESS_EXISTING_RESULT,
                elapsed_ms=duration_ms(parse_started_at),
                candidate_count=len(context.direct_result.video_assets),
                media_host=getattr(asset, "url", ""),
                expected_bytes=getattr(asset, "size_bytes", 0),
            )
            return context.direct_result
        if context.direct_result is not None:
            record_provider_attempt(
                provider="native",
                outcome=ProviderOutcome.FAILURE_NO_MEDIA,
                external_call=external_call,
                elapsed_ms=duration_ms(parse_started_at),
                failure_class=ProviderFailureClass.NO_MEDIA,
            )
            return context.direct_result
        if context.direct_error:
            outcome, failure_class = classify_provider_failure(context.direct_error)
            record_provider_attempt(
                provider="native",
                outcome=outcome,
                external_call=external_call,
                elapsed_ms=duration_ms(parse_started_at),
                failure_class=failure_class,
            )
            raise context.direct_error
        outcome = ProviderOutcome.FAILURE_NO_MEDIA
        failure_class = ProviderFailureClass.NO_MEDIA
        if last_error is not None:
            outcome, failure_class = classify_provider_failure(last_error)
        record_provider_attempt(
            provider="native",
            outcome=outcome,
            external_call=external_call,
            elapsed_ms=duration_ms(parse_started_at),
            failure_class=failure_class,
        )
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
                    request_options = {"probe_video_candidates": True}
                    if context.deadline_monotonic:
                        remaining = context.deadline_monotonic - monotonic()
                        if remaining <= 0:
                            raise XhsProviderTimeoutError(
                                "抖音本地解析超过时间限制"
                            )
                        request_options.update(
                            request_timeout=max(0.001, min(15.0, remaining)),
                            request_deadline=context.deadline_monotonic,
                        )
                    result = fetcher(
                        context.aweme_id,
                        **request_options,
                    )
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


def build_douyin_result_providers(
    provider_settings=None,
    prefer_ultra_hd=True,
    provider_order=None,
):
    provider_settings = provider_settings or get_platform_provider_settings("douyin")
    if provider_order is not None:
        provider_settings = replace(
            provider_settings,
            provider_order=normalize_provider_order(provider_order),
        )
    if not prefer_ultra_hd:
        provider_settings = replace(
            provider_settings,
            provider_order=("native",),
        )
    else:
        configured_order = tuple(provider_settings.provider_order or ())
        if (
            runtime_bool("DOUYIN_ULTRA_RACE_ENABLED", True)
            and "tiantianjiexi" in configured_order
            and "kukutool" in configured_order
            and provider_settings.provider_is_enabled("tiantianjiexi")
            and provider_settings.provider_is_enabled("kukutool")
        ):
            provider_settings = replace(
                provider_settings,
                # Default ultra-HD path races TianTianJieXi against KuKuTool
                # so a slow primary provider does not block user-facing parse.
                provider_order=("ultra_race", "native"),
            )

    provider_map = {
        "ultra_race": lambda: DouyinUltraRaceProvider(provider_settings=provider_settings),
        "tiantianjiexi": lambda: DouyinTianTianJieXiProvider(
            provider_settings=provider_settings,
            reserve_fallback_grace=(
                "kukutool" in provider_settings.enabled_provider_order
            ),
        ),
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
