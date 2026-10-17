import logging
import math
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


class ServiceFormatPayload(dict):
    """Internal normalized payload; a native JSON field cannot forge this type."""


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


def service_context(raw, parent=None):
    """Combine header aliases without losing a parent's required context."""
    result = {}
    headers = {}
    for item in (parent or {}, raw):
        for key in ("headers_present", "requires_cookie", "requires_context", "has_drm", "requires_merge"):
            if item.get(key) is True:
                result[key] = True
        supplied = False
        for key in ("headers", "http_headers"):
            value = item.get(key)
            if isinstance(value, dict):
                supplied = supplied or bool(value)
                headers.update(value)
            elif value:
                result["requires_context"] = True
        if item.get("headers_present") is True and not supplied:
            result["requires_context"] = True
    if headers:
        # Existing consumers prefer http_headers; make either spelling complete.
        result.update(headers=headers, http_headers=headers)
    return result


def collect_service_video_resources(payload, *, nested_keys, infer_quality=None):
    """Flatten explicit video containers, retaining native variant identity."""
    resources = []
    url_keys = ("url", "video_url", "videoUrl", "download_url", "downloadUrl", "play_url", "playUrl")
    label_keys = ("quality_label", "quality", "qualityLabel", "type", "name", "resolution", "clarity")
    size_keys = ("size_bytes", "size", "file_size", "fileSize", "filesize", "size_in_bytes", "sizeInBytes", "bytes")
    facts = ("width", "height", "fps", "duration", "vcodec", "acodec", "protocol", "ext",
             "extension", "language", "bitrate", "tbr", "audio_tracks", "video_tracks",
             "requires_merge", "requires_cookie", "requires_context", "headers", "http_headers", "headers_present",
             "has_drm", "role", "upstream_role", "relationships", "audio_asset_present", "format_id")

    def first(raw, keys):
        return next((raw[key] for key in keys if raw.get(key) not in (None, "")), None)

    def items(value):
        return value if isinstance(value, (list, tuple)) else (() if value is None else (value,))

    def add(raw, group_id="", group_index=None, depth=0, context=None):
        if depth > 8:
            return
        raw = {"url": raw} if isinstance(raw, str) else raw
        if not isinstance(raw, dict):
            return
        context = service_context(raw, context)
        url = first(raw, url_keys)
        label = first(raw, label_keys)
        generic_label = not label or str(label).lower() in {"video", "third-party resource"}
        if isinstance(url, str) and url.strip():
            value = first(raw, size_keys)
            estimate = raw.get("size_is_estimate") is True
            size = 0
            try:
                number = float(value or 0)
                size = max(0, int(number)) if math.isfinite(number) else 0
            except (TypeError, ValueError, OverflowError):
                text = str(value or "").strip().lower()
                for suffix, multiplier in (("gb", 1024**3), ("mb", 1024**2), ("kb", 1024)):
                    if text.endswith(suffix):
                        try:
                            number = float(text[:-len(suffix)].strip())
                            size = max(0, int(number * multiplier)) if math.isfinite(number) else 0
                            estimate = True
                        except (ValueError, OverflowError):
                            pass
                        break
            inferred = infer_quality(url) if generic_label and infer_quality else ""
            label = str(label or inferred or "video")
            resource = {**{key: raw[key] for key in facts if key in raw}, **context}
            resource.update(url=url, quality=label, quality_label=label, size=size, size_bytes=size,
                            size_is_estimate=estimate, media_type=raw.get("media_type", "video"),
                            quality_label_source="unknown" if generic_label and not inferred else
                            "url_inferred" if inferred else "upstream",
                            group_id=group_id, group_index=group_index,
                            work_id=group_id, work_label=(
                                str(payload.get("title") or "Video")
                                + (f" ({group_index})" if len(items(payload.get("videos"))) > 1 else "")
                                if group_id else ""),
                            _unlabelled_alias=generic_label and not inferred)
            resources.append(resource)
        for key in nested_keys:
            for child in items(raw.get(key)):
                add(child, group_id, group_index, depth + 1, context)

    videos = items(payload.get("videos"))
    context = service_context(payload)
    for index, video in enumerate(videos, 1):
        # Each video container is independent; only its own nested list is variants.
        add(video, f"native-video-{index}", index, context=context)
    for key in ("video",):
        for index, video in enumerate(items(payload.get(key)), 1):
            add(video, f"native-video-single-{index}", index, context=context)
    root_group = ("native-video-root" if videos else "native-video-1") if (
        not videos or any(payload.get(key) for key in nested_keys)) else ""
    root_url = first(payload, url_keys)
    root_label = first(payload, label_keys)
    if not (root_url and (not root_label or str(root_label).lower() == "video")
            and any(row["url"] == root_url for row in resources)):
        add(payload, root_group, 1 if root_group else None)

    # A bare root URL is only an alias. Explicit labels on the same URL remain.
    kept = []
    for resource in resources:
        if resource["_unlabelled_alias"] and any(
            other is not resource and other["url"] == resource["url"]
            and (not resource["group_id"] or other["group_id"] == resource["group_id"])
            and not other["_unlabelled_alias"] for other in resources
        ):
            continue
        clean = {key: value for key, value in resource.items() if key != "_unlabelled_alias"}
        if clean not in kept:
            kept.append(clean)
    return tuple(kept)


def collect_video_resources(payload, allowed_suffixes=(), platform_id=""):
    existing = payload.get("_service_format_rows") if isinstance(payload, ServiceFormatPayload) else None
    raw_resources = ([row for row in existing if row.get("media_type") == "video"]
                     if isinstance(existing, (list, tuple)) else
                     collect_service_video_resources(payload, nested_keys=("video_fullinfo",)))
    resources = []
    for raw in raw_resources:
        url = public_url_or_empty(raw.get("url"), allowed_suffixes, platform_id, "videos")
        if url:
            resources.append({**raw, "url": url})
    return tuple(sorted(resources, key=lambda item: item["size_bytes"], reverse=True))


def filter_service_format_rows(payload, rule):
    """The picker must not bypass the ordinary adapter's existing host grants."""
    rows = []
    platform_id = getattr(rule, "platform_id", "")
    for row in payload.get("_service_format_rows") or ():
        kind = row.get("media_type")
        suffixes = getattr(rule, f"{kind}_media_host_suffixes", ()) or getattr(rule, "media_host_suffixes", ())
        url = public_url_or_empty(row.get("url"), suffixes, platform_id, kind or "media")
        if url:
            rows.append({**row, "url": url})
    return ServiceFormatPayload({**payload, "_service_format_rows": rows})


def mixed_image_asset_allowed(payload, url):
    """Do not issue a context-free legacy asset for a restricted mixed image."""
    from ..ytdlp_selection import context_required
    rows = [row for row in payload.get("_service_format_rows", ())
            if row.get("media_type") == "image" and row.get("url") == url]
    return bool(rows) and not any(
        row.get("requires_context") is True or row.get("has_drm") is True
        or context_required(row.get("http_headers") or row.get("headers"),
                            headers_present=row.get("headers_present") is True,
                            requires_cookie=row.get("requires_cookie") is True)
        for row in rows
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

    def parse(self, url, timeout_seconds=None):
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
            timeout_seconds
            if timeout_seconds is not None
            else getattr(settings, "THIRD_PARTY_FRONTEND_TIMEOUT_SECONDS", 3.0) or 3.0
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
        cache_error = ""
        try:
            circuit_open_until = int(cache.get(build_circuit_open_key(), 0) or 0)
            circuit_failure_count = int(cache.get(build_circuit_failure_key(window), 0) or 0)
            failure_hit_count = int(cache.get(build_failure_hit_count_key(), 0) or 0)
            if source_key:
                failure_cache_reason = str(cache.get(build_failure_cache_key(source_key)) or "")
                single_flight_active = bool(cache.get(build_single_flight_key(source_key)))
        except Exception as error:
            cache_error = type(error).__name__
            logger.debug(
                "third_party_provider_health_cache_read_failed",
                extra={"error_type": cache_error},
                exc_info=True,
            )
        if source_key:
            with LOCAL_PROVIDER_FUTURES_LOCK:
                local_future = LOCAL_PROVIDER_FUTURES.get(source_key)
                single_flight_active = single_flight_active or bool(
                    local_future is not None and not local_future.done()
                )
        return {
            "provider": PROVIDER_ID,
            "enabled": bool(
                getattr(settings, "THIRD_PARTY_PROVIDER_ENABLED", False)
                and getattr(settings, "THIRD_PARTY_KUKUTOOL_PROVIDER_ENABLED", False)
            ),
            "provider_enabled": bool(
                getattr(settings, "THIRD_PARTY_KUKUTOOL_PROVIDER_ENABLED", False)
            ),
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
            "cache_error": cache_error,
        }


class KukutoolResultNormalizer:
    def __init__(self, platform_id="third_party"):
        self.platform_id = platform_id

    @classmethod
    def normalize_payload(cls, payload):
        if not isinstance(payload, dict):
            raise ValueError("KuKuTool result format is invalid")
        if isinstance(payload, ServiceFormatPayload) and isinstance(payload.get("_service_format_rows"), (list, tuple)):
            return payload
        videos = collect_service_video_resources(payload, nested_keys=("video_fullinfo",))
        rows = list(videos)
        for key in ("pics", "images", "image_urls", "imageUrls", "audios"):
            kind = "audio" if key == "audios" else "image"
            items = payload.get(key) or ()
            if not isinstance(items, (tuple, list)):
                continue
            for index, item in enumerate(items, 1):
                item = {"url": item} if isinstance(item, str) else item
                if isinstance(item, dict):
                    url = item.get("url") or item.get("src") or item.get("download_url")
                    row = {**item, **service_context(item, service_context(payload)),
                           "url": url, "media_type": kind}
                    if kind == "audio":
                        row.update(work_id=f"native-audio-{index}", work_label="Audio")
                    if url and row not in rows:
                        rows.append(row)
        if isinstance(payload.get("audio_url"), str) and payload["audio_url"]:
            rows.append({**service_context(payload), "url": payload["audio_url"], "media_type": "audio",
                         "work_id": "native-audio-primary", "work_label": "Audio"})
        return ServiceFormatPayload({**payload, "_service_format_rows": rows})

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
                media_type=media_type,
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
        from ..service_format_selection import attach_service_picker
        if not isinstance(payload, dict):
            raise ValueError("KuKuTool result format is invalid")
        payload = filter_service_format_rows(self.normalize_payload(payload), rule)

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
        image_urls = tuple(url for url in collect_picture_urls(payload, allowed_suffixes, platform_id)
                           if not video_resources or (url != cover_url and mixed_image_asset_allowed(payload, url)))
        image_assets = tuple(
            MediaAsset(media_type="image", url=url, platform=self.platform_id, preview_url=url,
                       **self.build_provider_asset_fields(rule, source_url, "image", index, url))
            for index, url in enumerate(image_urls, 1)
        )
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
            result = ParseResult(
                platform=self.platform_id,
                post_type="video",
                base_filename=title or "third_party",
                assets=assets + image_assets,
                metadata=self.build_metadata(
                    rule,
                    platform_name,
                    len(video_resources),
                    source_url,
                ),
            )
            return attach_service_picker(result, payload=payload, provider=PROVIDER_ID,
                                         source_url=source_url)

        if image_urls:
            result = ParseResult(
                platform=self.platform_id,
                post_type="image",
                base_filename=title or "third_party",
                assets=image_assets,
                metadata=self.build_metadata(
                    rule,
                    platform_name,
                    len(image_urls),
                    source_url,
                ),
            )
            return attach_service_picker(result, payload=payload, provider=PROVIDER_ID,
                                         source_url=source_url)

        audio_rows = [row for row in payload['_service_format_rows'] if row.get('media_type') == 'audio']
        if audio_rows:
            result = ParseResult(
                platform=self.platform_id,
                post_type="audio",
                base_filename=title or "third_party",
                assets=(),
                metadata=self.build_metadata(rule, platform_name, len(audio_rows), source_url),
            )
            return attach_service_picker(result, payload=payload, provider=PROVIDER_ID,
                                         source_url=source_url)

        raise ValueError("KuKuTool result does not contain media assets")
