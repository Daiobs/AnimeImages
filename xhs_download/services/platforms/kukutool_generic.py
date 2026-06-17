import logging
import time
from concurrent.futures import TimeoutError
from hashlib import sha256
from threading import RLock
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from uuid import uuid4

from django.conf import settings
from django.core.cache import cache

from ..assets import MediaAsset, ParseResult
from ..common import XhsProviderTimeoutError, clean_filename, host_matches, validate_public_url
from ..provider_manifest import build_provider_manifest_token
from ..rate_limits import acquire_concurrency, consume_fixed_window, release_concurrency
from .douyin_kukutool import run_kukutool_url_helper, submit_provider_task


logger = logging.getLogger(__name__)
PROVIDER_ID = "kukutool_generic"
PROVIDER_LABEL = "KuKuTool"
LOCAL_PROVIDER_FUTURES = {}
LOCAL_PROVIDER_FUTURES_LOCK = RLock()
TRACKING_QUERY_NAMES = {
    "area",
    "authorid",
    "fbclid",
    "gclid",
    "igshid",
    "is_copy_url",
    "is_from_webapp",
    "msclkid",
    "referer_video_id",
    "share_app_id",
    "share_iid",
    "sender_device",
    "sender_web_id",
    "streamsource",
    "timestamp",
    "ts",
}
TRACKING_QUERY_PREFIXES = ("utm_",)


def integer_value(value):
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def canonicalize_source_url(url):
    url = str(url or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return url

    hostname = parsed.hostname.lower()
    netloc = hostname
    try:
        port = parsed.port
    except ValueError:
        return url
    if port:
        netloc = f"{hostname}:{port}"

    query_items = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        key_lower = key.lower()
        if key_lower in TRACKING_QUERY_NAMES:
            continue
        if any(key_lower.startswith(prefix) for prefix in TRACKING_QUERY_PREFIXES):
            continue
        query_items.append((key, value))

    return urlunparse(
        (
            parsed.scheme.lower(),
            netloc,
            parsed.path or "/",
            "",
            urlencode(sorted(query_items), doseq=True),
            "",
        )
    )


def source_key_for_url(url):
    return sha256(canonicalize_source_url(url).encode("utf-8")).hexdigest()[:16]


def build_payload_cache_key(source_key):
    return f"third-party:kukutool:payload:{source_key}"


def build_single_flight_key(source_key):
    return f"third-party:kukutool:single-flight:{source_key}"


def build_failure_cache_key(source_key):
    return f"third-party:kukutool:failure:{source_key}"


def build_failure_hit_count_key():
    return "third-party:kukutool:failure-hit-count"


def build_profile_cache_key():
    return "third-party:kukutool:active-profile"


def build_circuit_open_key():
    return "third-party:kukutool:circuit:open"


def build_circuit_failure_key(window):
    return f"third-party:kukutool:circuit:failures:{window}"


def log_rejected_provider_url(platform_id, url, reason, field_name=""):
    parsed = urlparse(str(url or ""))
    logger.warning(
        "third_party_provider_url_rejected platform=%s host=%s reason=%s field=%s",
        platform_id or "unknown",
        (parsed.hostname or "").lower(),
        reason,
        field_name or "unknown",
    )


def public_url_or_empty(value, allowed_suffixes=(), platform_id="", field_name=""):
    if not isinstance(value, str) or not value.strip():
        return ""
    url = value.strip()
    try:
        validate_public_url(url, "Provider URL is not allowed")
    except Exception:
        log_rejected_provider_url(platform_id, url, "invalid_public_url", field_name)
        return ""
    if allowed_suffixes:
        hostname = urlparse(url).hostname
        if not host_matches(hostname, allowed_suffixes):
            log_rejected_provider_url(platform_id, url, "host_not_allowed", field_name)
            return ""
    return url


def iter_payload_urls(value, allowed_suffixes=(), platform_id="", field_name=""):
    if isinstance(value, str):
        url = public_url_or_empty(value, allowed_suffixes, platform_id, field_name)
        if url:
            yield url
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from iter_payload_urls(item, allowed_suffixes, platform_id, field_name)
    elif isinstance(value, dict):
        for key in (
            "url",
            "src",
            "download_url",
            "downloadUrl",
            "play_url",
            "playUrl",
        ):
            yield from iter_payload_urls(
                value.get(key),
                allowed_suffixes,
                platform_id,
                key,
            )


def collect_picture_urls(payload, allowed_suffixes=(), platform_id=""):
    seen = set()
    for key in ("pics", "images", "image_urls", "imageUrls"):
        for url in iter_payload_urls(
            payload.get(key),
            allowed_suffixes,
            platform_id,
            key,
        ):
            if url not in seen:
                seen.add(url)
                yield url


def collect_video_resources(payload, allowed_suffixes=(), platform_id=""):
    resources = []

    def add_resource(raw):
        if isinstance(raw, str):
            url = public_url_or_empty(raw, allowed_suffixes, platform_id, "videos")
            if not url:
                return
            resources.append(
                {
                    "url": url,
                    "quality_label": "video",
                    "size_bytes": 0,
                }
            )
            return
        if not isinstance(raw, dict):
            return
        url = public_url_or_empty(
            raw.get("url") or raw.get("download_url") or raw.get("play_url"),
            allowed_suffixes,
            platform_id,
            "videos",
        )
        if not url:
            return
        resources.append(
            {
                "url": url,
                "quality_label": str(
                    raw.get("type")
                    or raw.get("quality")
                    or raw.get("qualityLabel")
                    or "third-party resource"
                ),
                "size_bytes": integer_value(
                    raw.get("size")
                    or raw.get("file_size")
                    or raw.get("fileSize")
                ),
            }
        )

    for video in payload.get("videos") or ():
        add_resource(video)
        if isinstance(video, dict):
            for raw_resource in video.get("video_fullinfo") or ():
                add_resource(raw_resource)

    add_resource(payload)
    deduped = {}
    for resource in resources:
        current = deduped.get(resource["url"])
        if current is None or resource["size_bytes"] > current["size_bytes"]:
            deduped[resource["url"]] = resource
    return tuple(
        sorted(
            deduped.values(),
            key=lambda item: item["size_bytes"],
            reverse=True,
        )
    )


def should_retry_provider_error(error):
    message = str(error or "").lower()
    return any(
        marker in message
        for marker in (
            "parse failed (500)",
            "please retry",
            "retry",
            "timeout",
            "temporarily",
            "invalid json",
        )
    )


class KukutoolGenericProvider:
    provider_id = PROVIDER_ID
    label = PROVIDER_LABEL

    def __init__(self, runner=None):
        self.runner = runner or run_kukutool_url_helper

    def get_cached_payload(self, source_key):
        try:
            payload = cache.get(build_payload_cache_key(source_key))
        except Exception:
            logger.exception("third_party_provider_payload_cache_read_failed")
            return None
        return payload if isinstance(payload, dict) else None

    def cache_payload(self, source_key, payload):
        ttl_seconds = int(
            getattr(settings, "THIRD_PARTY_KUKUTOOL_PAYLOAD_CACHE_TTL_SECONDS", 120)
            or 0
        )
        if ttl_seconds <= 0 or not isinstance(payload, dict):
            return
        try:
            cache.set(build_payload_cache_key(source_key), payload, timeout=ttl_seconds)
        except Exception:
            logger.exception("third_party_provider_payload_cache_write_failed")

    def failure_cache_hit(self, source_key):
        try:
            hit = bool(cache.get(build_failure_cache_key(source_key)))
        except Exception:
            logger.exception("third_party_provider_failure_cache_read_failed")
            return True
        if hit:
            logger.warning(
                "third_party_provider_failure_cache_hit source_key=%s",
                source_key,
            )
            try:
                cache.add(build_failure_hit_count_key(), 0, timeout=24 * 60 * 60)
                cache.incr(build_failure_hit_count_key())
            except Exception:
                logger.exception("third_party_provider_failure_hit_count_failed")
        return hit

    def cache_failure(self, source_key, reason):
        ttl_seconds = int(
            getattr(settings, "THIRD_PARTY_KUKUTOOL_FAILURE_CACHE_TTL_SECONDS", 15)
            or 0
        )
        if ttl_seconds <= 0:
            return
        try:
            cache.set(
                build_failure_cache_key(source_key),
                str(reason or "failed")[:120],
                timeout=ttl_seconds,
            )
        except Exception:
            logger.exception("third_party_provider_failure_cache_write_failed")

    def clear_failure(self, source_key):
        try:
            cache.delete(build_failure_cache_key(source_key))
        except Exception:
            logger.exception("third_party_provider_failure_cache_delete_failed")

    def circuit_is_open(self):
        now = int(time.time())
        try:
            open_until = int(cache.get(build_circuit_open_key(), 0) or 0)
            if open_until and open_until <= now:
                cache.delete(build_circuit_open_key())
                return False
            return open_until > now
        except Exception:
            logger.exception("third_party_provider_circuit_read_failed")
            return True

    def record_success(self):
        now = int(time.time())
        window_seconds = max(
            1,
            int(getattr(settings, "THIRD_PARTY_KUKUTOOL_CIRCUIT_WINDOW_SECONDS", 60) or 60),
        )
        window = now // window_seconds
        try:
            cache.delete_many((build_circuit_open_key(), build_circuit_failure_key(window)))
        except Exception:
            logger.exception("third_party_provider_circuit_success_reset_failed")

    def record_failure(self):
        now = int(time.time())
        window_seconds = max(
            1,
            int(getattr(settings, "THIRD_PARTY_KUKUTOOL_CIRCUIT_WINDOW_SECONDS", 60) or 60),
        )
        threshold = max(
            1,
            int(getattr(settings, "THIRD_PARTY_KUKUTOOL_CIRCUIT_FAILURE_THRESHOLD", 5) or 5),
        )
        open_seconds = max(
            1,
            int(getattr(settings, "THIRD_PARTY_KUKUTOOL_CIRCUIT_OPEN_SECONDS", 120) or 120),
        )
        window = now // window_seconds
        try:
            key = build_circuit_failure_key(window)
            cache.add(key, 0, timeout=window_seconds + 5)
            count = cache.incr(key)
            if count >= threshold:
                cache.set(
                    build_circuit_open_key(),
                    now + open_seconds,
                    timeout=open_seconds,
                )
        except Exception:
            logger.exception("third_party_provider_circuit_failure_record_failed")

    def release_single_flight(self, lock_key, lock_token):
        if not lock_key or not lock_token:
            return
        try:
            if cache.get(lock_key) == lock_token:
                cache.delete(lock_key)
        except Exception:
            logger.exception("third_party_provider_single_flight_release_failed")

    def resolve_in_background(self, url, source_key, lock_key, lock_token):
        concurrency_acquired = False
        try:
            request_limit = consume_fixed_window(
                "third-party-provider-global",
                "all",
                getattr(settings, "THIRD_PARTY_GLOBAL_REQUEST_LIMIT_PER_MINUTE", 0),
                60,
            )
            if not request_limit.allowed:
                raise RuntimeError("third-party provider global rate limit reached")

            concurrency = acquire_concurrency(
                "third-party-kukutool-global",
                "all",
                getattr(settings, "THIRD_PARTY_KUKUTOOL_GLOBAL_MAX_CONCURRENCY", 4),
            )
            if not concurrency.allowed:
                raise RuntimeError("third-party provider global concurrency limit reached")
            concurrency_acquired = True

            attempts = max(
                1,
                int(getattr(settings, "THIRD_PARTY_KUKUTOOL_RETRIES", 2) or 1),
            )
            last_error = None
            for attempt in range(attempts):
                try:
                    payload = self.runner(
                        url,
                        node_binary=getattr(settings, "KUKUTOOL_NODE_BINARY", "") or "",
                        timeout_seconds=float(
                            getattr(
                                settings,
                                "THIRD_PARTY_KUKUTOOL_TIMEOUT_SECONDS",
                                12.0,
                            )
                            or 12.0
                        ),
                        profile_cache_ttl_seconds=int(
                            getattr(
                                settings,
                                "THIRD_PARTY_KUKUTOOL_PROFILE_CACHE_TTL_SECONDS",
                                86400,
                            )
                            or 0
                        ),
                        profile_cache_key=build_profile_cache_key(),
                    )
                    if not isinstance(payload, dict):
                        raise ValueError("KuKuTool result format is invalid")
                    self.cache_payload(source_key, payload)
                    self.clear_failure(source_key)
                    self.record_success()
                    return payload
                except Exception as error:
                    last_error = error
                    if attempt >= attempts - 1 or not should_retry_provider_error(error):
                        raise
            raise last_error
        except Exception as error:
            self.cache_failure(source_key, type(error).__name__)
            self.record_failure()
            raise
        finally:
            if concurrency_acquired:
                release_concurrency("third-party-kukutool-global", "all")
            self.release_single_flight(lock_key, lock_token)

    def wait_for_cached_payload(self, source_key, timeout_seconds):
        deadline = time.monotonic() + max(0.0, float(timeout_seconds or 0.0))
        poll_interval = max(
            0.02,
            float(getattr(settings, "THIRD_PARTY_KUKUTOOL_CACHE_POLL_INTERVAL_SECONDS", 0.1) or 0.1),
        )
        while time.monotonic() < deadline:
            payload = self.get_cached_payload(source_key)
            if payload:
                return payload
            time.sleep(min(poll_interval, max(0.0, deadline - time.monotonic())))
        return self.get_cached_payload(source_key)

    def get_or_start_future(self, url, source_key):
        with LOCAL_PROVIDER_FUTURES_LOCK:
            current = LOCAL_PROVIDER_FUTURES.get(source_key)
            if current is not None and not current.done():
                return current, "local_single_flight"

            lock_key = build_single_flight_key(source_key)
            lock_token = uuid4().hex
            lock_ttl = max(
                int(getattr(settings, "THIRD_PARTY_KUKUTOOL_SINGLE_FLIGHT_TTL_SECONDS", 30) or 30),
                int(float(getattr(settings, "THIRD_PARTY_KUKUTOOL_TIMEOUT_SECONDS", 12.0) or 12.0)) + 5,
            )
            try:
                lock_acquired = cache.add(lock_key, lock_token, timeout=lock_ttl)
            except Exception:
                logger.exception("third_party_provider_single_flight_acquire_failed")
                return None, "coordination_unavailable"
            if not lock_acquired:
                return None, "remote_single_flight"

            try:
                future = submit_provider_task(
                    "third-party-kukutool",
                    getattr(settings, "THIRD_PARTY_KUKUTOOL_EXECUTOR_WORKERS", 2),
                    getattr(settings, "THIRD_PARTY_KUKUTOOL_MAX_PENDING_TASKS", 4),
                    self.resolve_in_background,
                    url,
                    source_key,
                    lock_key,
                    lock_token,
                )
            except Exception:
                self.release_single_flight(lock_key, lock_token)
                raise
            if future is None:
                self.release_single_flight(lock_key, lock_token)
                return None, "local_capacity_full"
            LOCAL_PROVIDER_FUTURES[source_key] = future

            def clear_local_future(completed_future):
                with LOCAL_PROVIDER_FUTURES_LOCK:
                    if LOCAL_PROVIDER_FUTURES.get(source_key) is completed_future:
                        LOCAL_PROVIDER_FUTURES.pop(source_key, None)

            future.add_done_callback(clear_local_future)
            return future, "started"

    def parse(self, url):
        source_key = source_key_for_url(url)
        cached = self.get_cached_payload(source_key)
        if cached:
            return cached
        if self.circuit_is_open():
            raise XhsProviderTimeoutError("第三方通用解析暂时不可用，请稍后再试")
        if self.failure_cache_hit(source_key):
            raise XhsProviderTimeoutError("第三方通用解析刚刚失败，请稍后再试")

        future, state = self.get_or_start_future(url, source_key)
        timeout_seconds = float(
            getattr(settings, "THIRD_PARTY_FRONTEND_TIMEOUT_SECONDS", 3.0) or 3.0
        )
        if future is None:
            cached = self.wait_for_cached_payload(source_key, timeout_seconds)
            if cached:
                return cached
            raise XhsProviderTimeoutError(
                f"第三方解析正在排队（{state}），请 3-5 秒后重试同一链接。"
            )

        try:
            return future.result(timeout=max(0.1, timeout_seconds))
        except TimeoutError as error:
            raise XhsProviderTimeoutError("第三方解析正在后台处理中，请 3-5 秒后重试同一链接。") from error

    def health(self, source_key=""):
        now = int(time.time())
        window_seconds = max(
            1,
            int(getattr(settings, "THIRD_PARTY_KUKUTOOL_CIRCUIT_WINDOW_SECONDS", 60) or 60),
        )
        window = now // window_seconds
        circuit_open_until = 0
        circuit_failure_count = 0
        failure_cache_reason = ""
        failure_hit_count = 0
        single_flight_active = False
        try:
            circuit_open_until = int(cache.get(build_circuit_open_key(), 0) or 0)
            circuit_failure_count = int(cache.get(build_circuit_failure_key(window), 0) or 0)
            failure_hit_count = int(cache.get(build_failure_hit_count_key(), 0) or 0)
            if source_key:
                failure_cache_reason = str(cache.get(build_failure_cache_key(source_key)) or "")
                single_flight_active = bool(cache.get(build_single_flight_key(source_key)))
        except Exception:
            logger.exception("third_party_provider_health_cache_read_failed")
        if source_key:
            with LOCAL_PROVIDER_FUTURES_LOCK:
                local_future = LOCAL_PROVIDER_FUTURES.get(source_key)
                single_flight_active = single_flight_active or bool(
                    local_future is not None and not local_future.done()
                )
        return {
            "provider": PROVIDER_ID,
            "enabled": bool(getattr(settings, "THIRD_PARTY_PROVIDER_ENABLED", False)),
            "public_access_enabled": bool(
                getattr(settings, "THIRD_PARTY_PUBLIC_ACCESS_ENABLED", False)
            ),
            "circuit": {
                "open": circuit_open_until > now,
                "open_until": circuit_open_until,
                "failure_count": circuit_failure_count,
            },
            "failure_cache": {
                "source_key": source_key,
                "hit": bool(failure_cache_reason),
                "reason": failure_cache_reason,
                "hit_count": failure_hit_count,
            },
            "single_flight": {
                "source_key": source_key,
                "active": single_flight_active,
            },
        }


class KukutoolResultNormalizer:
    def __init__(self, platform_id="third_party"):
        self.platform_id = platform_id

    def build_provider_asset_fields(self, rule, source_url, media_type, index, url):
        source_key = source_key_for_url(source_url)
        asset_id = f"third_party:{rule.platform_id}:{source_key}:{media_type}:{index}"
        provider_cache_key = f"third_party:{PROVIDER_ID}:{source_key}"
        return {
            "asset_id": asset_id,
            "provider": PROVIDER_ID,
            "provider_resource_id": source_key,
            "provider_cache_key": provider_cache_key,
            "provider_manifest_token": build_provider_manifest_token(
                asset_id=asset_id,
                provider=PROVIDER_ID,
                provider_resource_id=source_key,
                provider_cache_key=provider_cache_key,
                url=url,
            ),
        }

    def build_metadata(self, rule, platform_name, candidate_count, source_url):
        return {
            "source_platform": rule.platform_id,
            "source_platform_name": platform_name,
            "source_provider": PROVIDER_ID,
            "provider_label": PROVIDER_LABEL,
            "candidate_count": candidate_count,
            "canonical_source_url": canonicalize_source_url(source_url),
        }

    def normalize(self, *, rule, platform_name, source_url, payload):
        if not isinstance(payload, dict):
            raise ValueError("KuKuTool result format is invalid")

        title = clean_filename(str(payload.get("title") or platform_name))
        allowed_suffixes = getattr(rule, "media_host_suffixes", ())
        platform_id = getattr(rule, "platform_id", "")
        cover_url = public_url_or_empty(
            payload.get("cover"),
            allowed_suffixes,
            platform_id,
            "cover",
        )
        video_resources = collect_video_resources(payload, allowed_suffixes, platform_id)
        if video_resources:
            assets = tuple(
                MediaAsset(
                    media_type="video",
                    url=resource["url"],
                    platform=self.platform_id,
                    cover_url=cover_url,
                    cover_preview_url=cover_url,
                    quality_label=resource["quality_label"],
                    size_bytes=resource["size_bytes"],
                    **self.build_provider_asset_fields(
                        rule,
                        source_url,
                        "video",
                        index,
                        resource["url"],
                    ),
                )
                for index, resource in enumerate(video_resources[:1], start=1)
            )
            return ParseResult(
                platform=self.platform_id,
                post_type="video",
                base_filename=title or "third_party",
                assets=assets,
                metadata=self.build_metadata(
                    rule,
                    platform_name,
                    len(video_resources),
                    source_url,
                ),
            )

        image_urls = tuple(collect_picture_urls(payload, allowed_suffixes, platform_id))
        if image_urls:
            assets = tuple(
                MediaAsset(
                    media_type="image",
                    url=image_url,
                    platform=self.platform_id,
                    preview_url=image_url,
                    **self.build_provider_asset_fields(
                        rule,
                        source_url,
                        "image",
                        index,
                        image_url,
                    ),
                )
                for index, image_url in enumerate(image_urls, start=1)
            )
            return ParseResult(
                platform=self.platform_id,
                post_type="image",
                base_filename=title or "third_party",
                assets=assets,
                metadata=self.build_metadata(
                    rule,
                    platform_name,
                    len(image_urls),
                    source_url,
                ),
            )

        raise ValueError("KuKuTool result does not contain media assets")
