from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from dataclasses import dataclass, replace
import inspect
import json
import logging
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
from threading import BoundedSemaphore, Lock, RLock
from time import monotonic, sleep, time
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from django.conf import settings
from django.core.cache import cache

from ..candidates import MediaCandidate
from ..common import XhsProviderTimeoutError, validate_external_url
from ..provider_settings import get_platform_provider_settings
from ..quality import QualityProbe
from ..rate_limits import (
    acquire_concurrency,
    consume_fixed_window,
    release_concurrency,
)


AWEME_ID_PATTERN = re.compile(r"^\d{15,22}$")
BYTE_CDN_HOST_SUFFIXES = ("365yg.com", "douyinvod.com")
KUKUTOOL_HELPER = Path(__file__).with_name("kukutool_provider.js")
KUKUTOOL_BUNDLED_NODE = (
    Path.home()
    / ".cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node"
)
logger = logging.getLogger(__name__)
KUKUTOOL_SEMAPHORES = {}
KUKUTOOL_SEMAPHORES_LOCK = Lock()
PROVIDER_EXECUTORS = {}
PROVIDER_EXECUTORS_LOCK = Lock()
PROVIDER_CAPACITY = {}
PROVIDER_CAPACITY_LOCK = Lock()
LOCAL_KUKUTOOL_FUTURES = {}
LOCAL_KUKUTOOL_FUTURES_LOCK = RLock()
KUKUTOOL_RUNTIME_LOCK = Lock()
KUKUTOOL_RUNTIME_STATE = {
    "checked": False,
    "available": False,
    "node_binary": "",
    "error": "not checked",
    "checked_at": 0,
}


def get_provider_executor(name, max_workers):
    max_workers = max(1, int(max_workers or 1))
    key = (name, max_workers)
    with PROVIDER_EXECUTORS_LOCK:
        executor = PROVIDER_EXECUTORS.get(key)
        if executor is None:
            executor = ThreadPoolExecutor(
                max_workers=max_workers,
                thread_name_prefix=name,
            )
            PROVIDER_EXECUTORS[key] = executor
        return executor


def get_provider_capacity(name, max_pending_tasks):
    max_pending_tasks = max(1, int(max_pending_tasks or 1))
    key = (name, max_pending_tasks)
    with PROVIDER_CAPACITY_LOCK:
        capacity = PROVIDER_CAPACITY.get(key)
        if capacity is None:
            capacity = BoundedSemaphore(max_pending_tasks)
            PROVIDER_CAPACITY[key] = capacity
        return capacity


def submit_provider_task(
    name,
    max_workers,
    max_pending_tasks,
    callback,
    *args,
):
    capacity = get_provider_capacity(name, max_pending_tasks)
    if not capacity.acquire(blocking=False):
        return None
    try:
        future = get_provider_executor(name, max_workers).submit(callback, *args)
    except Exception:
        capacity.release()
        raise
    future.add_done_callback(lambda _future: capacity.release())
    return future


def log_kukutool_event(event, context, started_at=None, **extra_fields):
    source_url = urlsplit(getattr(context, "url", "") or "")
    fields = {
        "provider": "kukutool",
        "aweme_id": getattr(context, "aweme_id", ""),
        "source_host": source_url.hostname or "",
        **extra_fields,
    }
    if started_at is not None:
        fields["duration_ms"] = int((time() - started_at) * 1000)
    logger.info(event, extra=fields)


def get_kukutool_semaphore(max_concurrency):
    max_concurrency = max(1, int(max_concurrency or 1))
    with KUKUTOOL_SEMAPHORES_LOCK:
        semaphore = KUKUTOOL_SEMAPHORES.get(max_concurrency)
        if semaphore is None:
            semaphore = BoundedSemaphore(max_concurrency)
            KUKUTOOL_SEMAPHORES[max_concurrency] = semaphore
        return semaphore


@dataclass(frozen=True)
class KukuToolResource:
    url: str
    quality_label: str
    size_bytes: int = 0
    bit_rate: int = 0
    quality_score: int = 0
    object_key: str = ""
    signed_url_expires_at: int = 0

    @property
    def score(self):
        return (
            self.bit_rate,
            self.size_bytes,
            self.quality_score,
            1 if self.quality_label in {"超高清", "原画", "Original"} else 0,
        )

    def to_candidate(self):
        return MediaCandidate(
            url=self.url,
            quality_label=self.quality_label or "超高清源",
            size_bytes=self.size_bytes,
            bit_rate=self.bit_rate,
            quality_type=self.quality_score,
            source="kukutool.video_fullinfo",
        )


@dataclass(frozen=True)
class ValidatedKukuToolCandidate:
    candidate: MediaCandidate
    final_url: str = ""
    content_type: str = ""
    object_key: str = ""
    signed_url_expires_at: int = 0
    cache_hit: bool = False
    candidate_count: int = 0
    probe_duration_ms: int = 0


def integer_value(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def parse_byte_cdn_url(url):
    try:
        parsed = urlsplit(url or "")
    except ValueError:
        return {"object_key": "", "expires_at": 0}

    parts = [part for part in parsed.path.split("/") if part]
    try:
        video_index = parts.index("video")
    except ValueError:
        return {"object_key": "", "expires_at": 0}

    expires_at = 0
    if video_index > 0 and re.fullmatch(r"[0-9a-fA-F]{8}", parts[video_index - 1]):
        expires_at = int(parts[video_index - 1], 16)
    return {
        "object_key": "/".join(parts[video_index:]),
        "expires_at": expires_at,
    }


def is_byte_cdn_url(url):
    try:
        validate_external_url(
            url,
            BYTE_CDN_HOST_SUFFIXES,
            "KuKuTool 返回了不受信任的媒体域名",
        )
    except (TypeError, ValueError):
        return False
    return str(url).startswith("https://")


def normalize_kukutool_payload(payload):
    if not isinstance(payload, dict):
        return ()

    resources_by_identity = {}
    for video in payload.get("videos") or ():
        if not isinstance(video, dict):
            continue
        for raw_resource in video.get("video_fullinfo") or ():
            if not isinstance(raw_resource, dict):
                continue
            url = raw_resource.get("url")
            if not isinstance(url, str) or not is_byte_cdn_url(url):
                continue

            parsed_url = urlsplit(url)
            query = parse_qs(parsed_url.query)
            metadata = parse_byte_cdn_url(url)
            resource = KukuToolResource(
                url=url,
                quality_label=str(raw_resource.get("type") or "超高清源"),
                size_bytes=integer_value(raw_resource.get("size")),
                bit_rate=integer_value((query.get("br") or (0,))[0]) * 1000,
                quality_score=integer_value((query.get("qs") or (0,))[0]),
                object_key=metadata["object_key"],
                signed_url_expires_at=metadata["expires_at"],
            )
            identity = resource.object_key or resource.url
            current = resources_by_identity.get(identity)
            if current is None or resource.score > current.score:
                resources_by_identity[identity] = resource

    return tuple(
        sorted(
            resources_by_identity.values(),
            key=lambda resource: resource.score,
            reverse=True,
        )
    )


def build_kukutool_cache_key(aweme_id):
    return f"douyin:kukutool:validated:{aweme_id}"


def build_kukutool_single_flight_key(aweme_id):
    return f"douyin:kukutool:single-flight:{aweme_id}"


def build_kukutool_failure_cache_key(aweme_id):
    return f"douyin:kukutool:failure:{aweme_id}"


def build_kukutool_profile_cache_key():
    return "douyin:kukutool:active-profile"


def build_kukutool_circuit_open_key():
    return "douyin:kukutool:circuit:open"


def build_kukutool_circuit_failure_key(window):
    return f"douyin:kukutool:circuit:failures:{window}"


def build_kukutool_admin_disabled_key():
    return "douyin:kukutool:admin-disabled"


def is_kukutool_admin_disabled():
    try:
        return bool(cache.get(build_kukutool_admin_disabled_key()))
    except Exception:
        logger.exception("kukutool_admin_state_read_failed")
        return True


def set_kukutool_admin_disabled(disabled):
    key = build_kukutool_admin_disabled_key()
    if disabled:
        cache.set(key, True, timeout=None)
    else:
        cache.delete(key)


def get_kukutool_circuit_status():
    now = int(time())
    try:
        open_until = int(cache.get(build_kukutool_circuit_open_key(), 0) or 0)
    except Exception:
        logger.exception("kukutool_circuit_state_read_failed")
        return {
            "open": True,
            "open_until": 0,
            "reason": "cache unavailable",
        }
    if open_until and open_until <= now:
        try:
            cache.delete(build_kukutool_circuit_open_key())
        except Exception:
            logger.exception("kukutool_circuit_expiry_delete_failed")
        open_until = 0
    return {
        "open": open_until > now,
        "open_until": open_until,
        "reason": "failure threshold reached" if open_until > now else "",
    }


def record_kukutool_success(provider_settings=None):
    provider_settings = provider_settings or get_platform_provider_settings("douyin")
    now = int(time())
    window = now // provider_settings.kukutool_circuit_window_seconds
    try:
        cache.delete_many(
            (
                build_kukutool_circuit_open_key(),
                build_kukutool_circuit_failure_key(window),
            )
        )
    except Exception:
        logger.exception("kukutool_circuit_success_reset_failed")


def record_kukutool_failure(provider_settings=None):
    provider_settings = provider_settings or get_platform_provider_settings("douyin")
    now = int(time())
    window_seconds = provider_settings.kukutool_circuit_window_seconds
    window = now // window_seconds
    key = build_kukutool_circuit_failure_key(window)
    try:
        cache.add(key, 0, timeout=window_seconds + 5)
        count = cache.incr(key)
        if count >= provider_settings.kukutool_circuit_failure_threshold:
            cache.set(
                build_kukutool_circuit_open_key(),
                now + provider_settings.kukutool_circuit_open_seconds,
                timeout=provider_settings.kukutool_circuit_open_seconds,
            )
        return count
    except Exception:
        logger.exception("kukutool_circuit_failure_record_failed")
        return 0


def get_kukutool_provider_health():
    runtime = get_kukutool_runtime_status()
    circuit = get_kukutool_circuit_status()
    admin_disabled = is_kukutool_admin_disabled()
    return {
        "provider": "kukutool",
        "enabled": bool(settings.KUKUTOOL_PROVIDER_ENABLED),
        "ultra_hd_enabled": bool(settings.ULTRA_HD_PROVIDER_ENABLED),
        "public_access_enabled": bool(settings.ULTRA_HD_PUBLIC_ACCESS_ENABLED),
        "admin_disabled": admin_disabled,
        "runtime": runtime,
        "circuit": circuit,
        "available": bool(
            settings.KUKUTOOL_PROVIDER_ENABLED
            and settings.ULTRA_HD_PROVIDER_ENABLED
            and runtime["available"]
            and not admin_disabled
            and not circuit["open"]
        ),
    }


def valid_kukutool_profile(profile):
    if not isinstance(profile, dict):
        return False
    auth_fields = profile.get("authResponseFields")
    parse_fields = profile.get("parseRequestFields")
    return bool(
        profile.get("id")
        and profile.get("authRoute")
        and isinstance(auth_fields, dict)
        and auth_fields.get("key")
        and auth_fields.get("seed")
        and isinstance(parse_fields, dict)
        and parse_fields.get("key")
        and parse_fields.get("payload")
        and parse_fields.get("iv")
        and parse_fields.get("version")
    )


def serialize_validated_candidate(validated):
    return {
        "url": validated.candidate.url,
        "size_bytes": validated.candidate.size_bytes,
        "quality_label": validated.candidate.quality_label,
        "bit_rate": validated.candidate.bit_rate,
        "quality_type": validated.candidate.quality_type,
        "source": validated.candidate.source,
        "final_url": validated.final_url,
        "content_type": validated.content_type,
        "object_key": validated.object_key,
        "signed_url_expires_at": validated.signed_url_expires_at,
        "candidate_count": validated.candidate_count,
    }


def get_cached_validated_candidate(cache_key, allowed_host_suffixes):
    data = cache.get(cache_key)
    if not isinstance(data, dict):
        return None

    try:
        url = str(data["url"])
        size_bytes = int(data["size_bytes"])
        signed_url_expires_at = int(data.get("signed_url_expires_at") or 0)
        object_key = str(data.get("object_key") or "")
        validate_external_url(
            url,
            allowed_host_suffixes,
            "缓存的 KuKuTool URL 不在允许的媒体域名内",
        )
        if size_bytes <= 0:
            raise ValueError("cached resource size is missing")
        if not object_key:
            raise ValueError("cached resource object key is missing")
        if signed_url_expires_at and signed_url_expires_at <= int(time()) + 30:
            raise ValueError("cached signed URL has expired")
    except (KeyError, TypeError, ValueError):
        cache.delete(cache_key)
        return None

    return ValidatedKukuToolCandidate(
        candidate=MediaCandidate(
            url=url,
            size_bytes=size_bytes,
            quality_label=str(data.get("quality_label") or "超高清源"),
            bit_rate=int(data.get("bit_rate") or 0),
            quality_type=int(data.get("quality_type") or 0),
            source=str(data.get("source") or "kukutool.validated_cache"),
        ),
        final_url=str(data.get("final_url") or url),
        content_type=str(data.get("content_type") or ""),
        object_key=object_key,
        signed_url_expires_at=signed_url_expires_at,
        cache_hit=True,
        candidate_count=int(data.get("candidate_count") or 1),
    )


def cache_validated_candidate(cache_key, validated, ttl_seconds):
    ttl_seconds = int(ttl_seconds or 0)
    if ttl_seconds <= 0:
        return

    if validated.signed_url_expires_at:
        signed_ttl = validated.signed_url_expires_at - int(time()) - 30
        ttl_seconds = min(ttl_seconds, signed_ttl)
    if ttl_seconds <= 0:
        return

    cache.set(
        cache_key,
        serialize_validated_candidate(validated),
        timeout=ttl_seconds,
    )


def development_runtime_fallbacks_allowed():
    return not bool(
        getattr(
            settings,
            "REQUIRE_PRODUCTION_CONFIG",
            not settings.DEBUG,
        )
    )


def find_node_runtime(configured_binary="", allow_development_fallbacks=None):
    if allow_development_fallbacks is None:
        allow_development_fallbacks = development_runtime_fallbacks_allowed()
    candidates = []
    if configured_binary:
        candidates.append(configured_binary)
    elif allow_development_fallbacks:
        system_node = shutil.which("node")
        if system_node:
            candidates.append(system_node)
        if KUKUTOOL_BUNDLED_NODE.is_file():
            candidates.append(str(KUKUTOOL_BUNDLED_NODE))

    for candidate in dict.fromkeys(candidates):
        try:
            check = subprocess.run(
                [
                    candidate,
                    "-e",
                    "process.exit(typeof fetch === 'function' ? 0 : 1)",
                ],
                capture_output=True,
                timeout=3,
                check=False,
                env=build_kukutool_safe_env(),
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if check.returncode == 0:
            return candidate
    return ""


def initialize_kukutool_runtime(force=False):
    with KUKUTOOL_RUNTIME_LOCK:
        if KUKUTOOL_RUNTIME_STATE["checked"] and not force:
            return dict(KUKUTOOL_RUNTIME_STATE)

        if not (
            settings.ULTRA_HD_PROVIDER_ENABLED
            and settings.KUKUTOOL_PROVIDER_ENABLED
        ):
            KUKUTOOL_RUNTIME_STATE.update(
                checked=True,
                available=False,
                node_binary="",
                error="provider disabled",
                checked_at=int(time()),
            )
            return dict(KUKUTOOL_RUNTIME_STATE)

        node_runtime = find_node_runtime(
            settings.KUKUTOOL_NODE_BINARY,
            allow_development_fallbacks=development_runtime_fallbacks_allowed(),
        )
        error = ""
        if not node_runtime:
            error = (
                "KUKUTOOL_NODE_BINARY is required outside local development"
                if (
                    not development_runtime_fallbacks_allowed()
                    and not settings.KUKUTOOL_NODE_BINARY
                )
                else "Node.js 18+ with fetch support was not found"
            )
        elif not KUKUTOOL_HELPER.is_file():
            error = "KuKuTool helper script is missing"
            node_runtime = ""

        KUKUTOOL_RUNTIME_STATE.update(
            checked=True,
            available=bool(node_runtime),
            node_binary=node_runtime,
            error=error,
            checked_at=int(time()),
        )
        return dict(KUKUTOOL_RUNTIME_STATE)


def get_kukutool_runtime_status():
    with KUKUTOOL_RUNTIME_LOCK:
        return dict(KUKUTOOL_RUNTIME_STATE)


def build_kukutool_safe_env(cached_profile=None):
    safe_env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
    }
    if os.environ.get("LC_ALL"):
        safe_env["LC_ALL"] = os.environ["LC_ALL"]
    if valid_kukutool_profile(cached_profile):
        safe_env["KUKUTOOL_PROFILE_JSON"] = json.dumps(
            cached_profile,
            ensure_ascii=True,
            separators=(",", ":"),
        )
    return safe_env


def run_kukutool_helper(
    aweme_id,
    node_binary="",
    timeout_seconds=12.0,
    profile_cache_ttl_seconds=86400,
):
    if not AWEME_ID_PATTERN.fullmatch(str(aweme_id or "")):
        raise ValueError("invalid aweme_id")

    runtime_status = get_kukutool_runtime_status()
    if not runtime_status["checked"]:
        runtime_status = initialize_kukutool_runtime()
    node_runtime = runtime_status["node_binary"] if runtime_status["available"] else ""
    if node_binary and node_runtime != node_binary:
        node_runtime = ""
    if not node_runtime:
        raise OSError("KuKuTool Provider requires Node.js 18 or newer")
    if not KUKUTOOL_HELPER.is_file():
        raise OSError("KuKuTool Provider helper script is missing")

    try:
        cached_profile = cache.get(build_kukutool_profile_cache_key())
    except Exception:
        logger.exception(
            "douyin_provider_profile_cache_read_failed",
            extra={"provider": "kukutool", "aweme_id": aweme_id},
        )
        cached_profile = None
    process_env = build_kukutool_safe_env(cached_profile)

    process = subprocess.run(
        [node_runtime, str(KUKUTOOL_HELPER), str(aweme_id)],
        capture_output=True,
        text=True,
        timeout=max(0.1, timeout_seconds),
        check=False,
        env=process_env,
    )
    if process.returncode != 0:
        message = (process.stderr or process.stdout or "KuKuTool request failed").strip()
        raise OSError(message[:2000])
    if len(process.stdout) > 10 * 1024 * 1024:
        raise ValueError("KuKuTool response is too large")
    payload = json.loads(process.stdout)
    if isinstance(payload, dict) and "result" in payload:
        profile = payload.get("profile")
        if valid_kukutool_profile(profile) and profile_cache_ttl_seconds > 0:
            try:
                cache.set(
                    build_kukutool_profile_cache_key(),
                    profile,
                    timeout=int(profile_cache_ttl_seconds),
                )
            except Exception:
                logger.exception(
                    "douyin_provider_profile_cache_write_failed",
                    extra={"provider": "kukutool", "aweme_id": aweme_id},
                )
        return payload["result"]
    return payload


def call_kukutool_runner(runner, aweme_id, **options):
    try:
        parameters = inspect.signature(runner).parameters.values()
    except (TypeError, ValueError):
        parameters = ()
    supports_extra_options = any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in parameters
    )
    accepted_names = {parameter.name for parameter in parameters}
    if not supports_extra_options:
        options = {
            key: value
            for key, value in options.items()
            if key in accepted_names
        }
    return runner(aweme_id, **options)


class DouyinKukuToolProvider:
    name = "kukutool"

    def __init__(self, provider_settings=None, runner=None, quality_probe=None):
        self.provider_settings = provider_settings or get_platform_provider_settings("douyin")
        self.runner = runner or run_kukutool_helper
        self.quality_probe = quality_probe

    def remaining_budget(self, context, fallback):
        if not context.deadline_monotonic:
            return max(0.1, fallback)
        return max(0.0, context.deadline_monotonic - monotonic())

    def get_cached_candidate(self, adapter, context):
        try:
            return get_cached_validated_candidate(
                build_kukutool_cache_key(context.aweme_id),
                adapter.media_host_suffixes,
            )
        except Exception as error:
            log_kukutool_event(
                "douyin_provider_validated_cache_read_failed",
                context,
                error_type=type(error).__name__,
                error=str(error),
            )
            return None

    def resolve_validated_candidate(self, adapter, context):
        cache_key = build_kukutool_cache_key(context.aweme_id)
        cached = self.get_cached_candidate(adapter, context)
        if cached:
            log_kukutool_event(
                "douyin_provider_validated_cache_hit",
                context,
                cache_hit=True,
                selected_size_bytes=cached.candidate.size_bytes,
                candidate_count=cached.candidate_count,
            )
            return cached

        if is_kukutool_admin_disabled():
            raise RuntimeError("KuKuTool Provider is disabled by an administrator")
        circuit = get_kukutool_circuit_status()
        if circuit["open"]:
            raise RuntimeError("KuKuTool Provider circuit is open")
        request_limit = consume_fixed_window(
            "kukutool-upstream-global",
            "all",
            self.provider_settings.kukutool_global_request_limit_per_minute,
            60,
        )
        if not request_limit.allowed:
            raise RuntimeError("KuKuTool Provider global request limit reached")

        timeout_seconds = min(
            max(0.1, self.provider_settings.kukutool_timeout_seconds),
            self.remaining_budget(
                context,
                self.provider_settings.kukutool_timeout_seconds,
            ),
        )
        upstream_started_at = time()
        semaphore = get_kukutool_semaphore(
            self.provider_settings.kukutool_max_concurrency
        )
        queue_timeout = min(1.0, timeout_seconds)
        queue_started_at = monotonic()
        if not semaphore.acquire(timeout=queue_timeout):
            raise RuntimeError("KuKuTool Provider is busy")
        try:
            runner_timeout = max(
                0.1,
                timeout_seconds - (monotonic() - queue_started_at),
            )
            payload = call_kukutool_runner(
                self.runner,
                context.aweme_id,
                node_binary=self.provider_settings.kukutool_node_binary,
                timeout_seconds=runner_timeout,
                profile_cache_ttl_seconds=(
                    self.provider_settings.kukutool_profile_cache_ttl_seconds
                ),
            )
        finally:
            semaphore.release()
        resources = normalize_kukutool_payload(payload)
        log_kukutool_event(
            "douyin_provider_response",
            context,
            upstream_started_at,
            cache_hit=False,
            candidate_count=len(resources),
        )
        if not resources:
            log_kukutool_event(
                "douyin_provider_schema_unrecognized",
                context,
                payload_type=type(payload).__name__,
            )
            record_kukutool_failure(self.provider_settings)
            return None

        preferred = resources[0]
        signed_url_is_current = (
            preferred.signed_url_expires_at > int(time()) + 30
        )
        if (
            preferred.size_bytes > 0
            and preferred.object_key
            and signed_url_is_current
        ):
            validate_external_url(
                preferred.url,
                adapter.media_host_suffixes,
                "KuKuTool URL 不在允许的媒体域名内",
            )
            validated = ValidatedKukuToolCandidate(
                candidate=preferred.to_candidate(),
                final_url=preferred.url,
                content_type="video/mp4",
                object_key=preferred.object_key,
                signed_url_expires_at=preferred.signed_url_expires_at,
                cache_hit=False,
                candidate_count=len(resources),
            )
            try:
                cache_validated_candidate(
                    cache_key,
                    validated,
                    self.provider_settings.kukutool_validated_cache_ttl_seconds,
                )
            except Exception as error:
                log_kukutool_event(
                    "douyin_provider_validated_cache_write_failed",
                    context,
                    error_type=type(error).__name__,
                    error=str(error),
                )
            log_kukutool_event(
                "douyin_provider_declared_candidate_accepted",
                context,
                candidate_count=len(resources),
                selected_size_bytes=preferred.size_bytes,
                selected_bit_rate=preferred.bit_rate,
                selected_quality=preferred.quality_label,
                object_key=preferred.object_key,
            )
            record_kukutool_success(self.provider_settings)
            return validated

        probe = self.quality_probe or QualityProbe(
            allowed_final_host_suffixes=adapter.media_host_suffixes
        )
        probe_started_at = time()
        for resource in resources[:self.provider_settings.kukutool_max_probe_candidates]:
            remaining = self.remaining_budget(
                context,
                self.provider_settings.kukutool_probe_timeout_seconds,
            )
            if remaining <= 0:
                break
            diagnostic = probe.probe(
                resource.url,
                headers=adapter.headers,
                timeout=min(
                    max(0.1, self.provider_settings.kukutool_probe_timeout_seconds),
                    remaining,
                ),
                expected_type="video",
                strategy="range_first",
                allow_fallback=False,
            )
            if not diagnostic.reachable or diagnostic.size_bytes <= 0:
                log_kukutool_event(
                    "douyin_provider_candidate_probe_failed",
                    context,
                    candidate_quality=resource.quality_label,
                    candidate_size_bytes=resource.size_bytes,
                    candidate_bit_rate=resource.bit_rate,
                    probe_error=diagnostic.error,
                )
                continue

            final_url = diagnostic.final_url or resource.url
            validate_external_url(
                final_url,
                adapter.media_host_suffixes,
                "KuKuTool URL 跳转到了不允许的媒体域名",
            )
            final_metadata = parse_byte_cdn_url(final_url)
            candidate = replace(
                resource.to_candidate(),
                url=final_url,
                size_bytes=diagnostic.size_bytes,
            )
            validated = ValidatedKukuToolCandidate(
                candidate=candidate,
                final_url=final_url,
                content_type=diagnostic.content_type,
                object_key=final_metadata["object_key"] or resource.object_key,
                signed_url_expires_at=(
                    final_metadata["expires_at"]
                    or resource.signed_url_expires_at
                ),
                cache_hit=False,
                candidate_count=len(resources),
                probe_duration_ms=int((time() - probe_started_at) * 1000),
            )
            try:
                cache_validated_candidate(
                    cache_key,
                    validated,
                    self.provider_settings.kukutool_validated_cache_ttl_seconds,
                )
            except Exception as error:
                log_kukutool_event(
                    "douyin_provider_validated_cache_write_failed",
                    context,
                    error_type=type(error).__name__,
                    error=str(error),
                )
            log_kukutool_event(
                "douyin_provider_candidate_probe_accepted",
                context,
                candidate_count=len(resources),
                selected_size_bytes=candidate.size_bytes,
                selected_bit_rate=candidate.bit_rate,
                selected_quality=candidate.quality_label,
                probe_duration_ms=validated.probe_duration_ms,
                object_key=validated.object_key,
            )
            record_kukutool_success(self.provider_settings)
            return validated
        log_kukutool_event(
            "douyin_provider_no_validated_candidate",
            context,
            candidate_count=len(resources),
            probe_duration_ms=int((time() - probe_started_at) * 1000),
        )
        record_kukutool_failure(self.provider_settings)
        return None

    def release_single_flight(self, lock_key, token, context):
        try:
            if cache.get(lock_key) == token:
                cache.delete(lock_key)
        except Exception as error:
            log_kukutool_event(
                "douyin_provider_single_flight_release_failed",
                context,
                error_type=type(error).__name__,
                error=str(error),
            )

    def cache_failure(self, context, reason):
        ttl_seconds = self.provider_settings.kukutool_failure_cache_ttl_seconds
        if ttl_seconds <= 0:
            return
        try:
            cache.set(
                build_kukutool_failure_cache_key(context.aweme_id),
                {"reason": str(reason or "unavailable"), "failed_at": int(time())},
                timeout=ttl_seconds,
            )
        except Exception as error:
            log_kukutool_event(
                "douyin_provider_failure_cache_write_failed",
                context,
                error_type=type(error).__name__,
                error=str(error),
            )

    def resolve_in_background(self, adapter, context, lock_key, lock_token):
        from .douyin_providers import log_provider_failure

        global_slot_acquired = False
        try:
            global_limit = self.provider_settings.kukutool_global_max_concurrency
            if global_limit > 0:
                try:
                    capacity = acquire_concurrency(
                        "kukutool-provider-global",
                        "all",
                        global_limit,
                        ttl_seconds=max(
                            30,
                            int(self.provider_settings.kukutool_timeout_seconds) + 10,
                        ),
                    )
                except Exception as error:
                    self.cache_failure(context, "global_concurrency_cache_failed")
                    log_provider_failure(
                        self.name,
                        context,
                        error,
                        coordination="global_concurrency",
                    )
                    return None
                if not capacity.allowed:
                    self.cache_failure(context, "global_concurrency_limit")
                    log_kukutool_event(
                        "douyin_provider_global_capacity_full",
                        context,
                        current=capacity.current,
                        limit=capacity.limit,
                    )
                    return None
                global_slot_acquired = True

            validated = self.resolve_validated_candidate(adapter, context)
            if validated:
                try:
                    cache.delete(build_kukutool_failure_cache_key(context.aweme_id))
                except Exception as error:
                    log_kukutool_event(
                        "douyin_provider_failure_cache_delete_failed",
                        context,
                        error_type=type(error).__name__,
                        error=str(error),
                    )
                return validated
            self.cache_failure(context, "no_validated_candidate")
            return None
        except Exception as error:
            record_kukutool_failure(self.provider_settings)
            self.cache_failure(context, type(error).__name__)
            log_provider_failure(self.name, context, error)
            return None
        finally:
            if global_slot_acquired:
                release_concurrency("kukutool-provider-global", "all")
            self.release_single_flight(lock_key, lock_token, context)

    def get_or_start_kukutool_future(self, adapter, context):
        failure_key = build_kukutool_failure_cache_key(context.aweme_id)
        try:
            if cache.get(failure_key):
                log_kukutool_event(
                    "douyin_provider_failure_cache_hit",
                    context,
                )
                return None, "failure_cache_hit"
        except Exception as error:
            log_kukutool_event(
                "douyin_provider_failure_cache_read_failed",
                context,
                error_type=type(error).__name__,
                error=str(error),
            )
            return None, "coordination_unavailable"

        with LOCAL_KUKUTOOL_FUTURES_LOCK:
            current = LOCAL_KUKUTOOL_FUTURES.get(context.aweme_id)
            if current is not None and not current.done():
                return current, "local_single_flight"

            lock_key = build_kukutool_single_flight_key(context.aweme_id)
            lock_token = uuid4().hex
            queue_waves = math.ceil(
                self.provider_settings.kukutool_max_pending_tasks
                / self.provider_settings.kukutool_executor_workers
            )
            lock_ttl_seconds = max(
                self.provider_settings.kukutool_single_flight_ttl_seconds,
                math.ceil(
                    self.provider_settings.kukutool_timeout_seconds
                    * max(1, queue_waves)
                )
                + 5,
            )
            try:
                lock_acquired = cache.add(
                    lock_key,
                    lock_token,
                    timeout=lock_ttl_seconds,
                )
            except Exception as error:
                log_kukutool_event(
                    "douyin_provider_single_flight_acquire_failed",
                    context,
                    error_type=type(error).__name__,
                    error=str(error),
                )
                return None, "coordination_unavailable"
            if not lock_acquired:
                return None, "remote_single_flight"

            background_context = replace(context, deadline_monotonic=0)
            try:
                future = submit_provider_task(
                    "douyin-kukutool",
                    self.provider_settings.kukutool_executor_workers,
                    self.provider_settings.kukutool_max_pending_tasks,
                    self.resolve_in_background,
                    adapter,
                    background_context,
                    lock_key,
                    lock_token,
                )
            except Exception as error:
                self.release_single_flight(lock_key, lock_token, context)
                log_kukutool_event(
                    "douyin_provider_background_submit_failed",
                    context,
                    error_type=type(error).__name__,
                    error=str(error),
                )
                return None, "coordination_unavailable"
            if future is None:
                self.release_single_flight(lock_key, lock_token, context)
                return None, "local_capacity_full"
            LOCAL_KUKUTOOL_FUTURES[context.aweme_id] = future

            def clear_local_future(completed_future):
                with LOCAL_KUKUTOOL_FUTURES_LOCK:
                    if LOCAL_KUKUTOOL_FUTURES.get(context.aweme_id) is completed_future:
                        LOCAL_KUKUTOOL_FUTURES.pop(context.aweme_id, None)

            future.add_done_callback(clear_local_future)
            return future, "started"

    def refresh_for_download(self, adapter, context):
        from .douyin_providers import log_provider_failure

        future, flight_state = self.get_or_start_kukutool_future(
            adapter,
            context,
        )
        if future is None and flight_state != "remote_single_flight":
            return None

        deadline = monotonic() + max(
            0.1,
            self.provider_settings.kukutool_timeout_seconds,
        )
        poll_interval = self.provider_settings.kukutool_cache_poll_interval_seconds
        while monotonic() < deadline:
            if future is not None and future.done():
                try:
                    return future.result(timeout=0)
                except Exception as error:
                    log_provider_failure(
                        "kukutool_download_refresh",
                        context,
                        error,
                    )
                    return None

            cached = self.get_cached_candidate(adapter, context)
            if cached:
                return cached
            sleep(min(poll_interval, max(0.0, deadline - monotonic())))
        return self.get_cached_candidate(adapter, context)

    def build_result(self, adapter, context, validated, native_future, parse_started_at):
        from .douyin_providers import (
            build_external_parse_result,
            log_provider_event,
            log_provider_failure,
            with_parse_metadata,
        )

        reference_result = None
        reference_wait = min(
            max(0.0, self.provider_settings.kukutool_reference_wait_seconds),
            self.remaining_budget(context, 0),
        )
        try:
            if native_future.done():
                reference_result = native_future.result(timeout=0)
            elif reference_wait > 0:
                reference_result = native_future.result(timeout=reference_wait)
        except FutureTimeoutError:
            pass
        except Exception as error:
            log_provider_failure("native_reference", context, error)

        result = build_external_parse_result(
            adapter,
            context,
            validated.candidate,
            reference_result,
            provider_name=self.name,
        )
        result = with_parse_metadata(
            result,
            selected_provider=self.name,
            fallback_reason="kukutool_validated",
            kukutool_cache_hit=validated.cache_hit,
            kukutool_candidate_count=validated.candidate_count,
            kukutool_probe_duration_ms=validated.probe_duration_ms,
            kukutool_object_key=validated.object_key,
            kukutool_signed_url_expires_at=validated.signed_url_expires_at,
        )
        log_provider_event(
            "douyin_provider_selected",
            self.name,
            context,
            parse_started_at,
            fallback_reason="kukutool_validated",
            selected_size_bytes=validated.candidate.size_bytes,
            candidate_count=validated.candidate_count,
            cache_hit=validated.cache_hit,
            probe_duration_ms=validated.probe_duration_ms,
        )
        return result

    def get_finished_native_result(self, native_future, context):
        from .douyin_providers import log_provider_failure

        if not native_future.done():
            return None
        try:
            return native_future.result(timeout=0)
        except Exception as error:
            log_provider_failure("native_fast", context, error)
            return None

    def parse(self, adapter, context):
        if (
            not self.provider_settings.provider_is_enabled(self.name)
            or not context.aweme_id
        ):
            return None

        from .douyin_providers import (
            get_cached_douyin_metadata,
            get_fast_native_result,
            log_provider_event,
            log_provider_failure,
            with_parse_metadata,
        )

        parse_started_at = time()
        deadline = context.deadline_monotonic or (
            monotonic()
            + max(0.1, self.provider_settings.ultra_total_timeout_seconds)
        )

        cache_key = build_kukutool_cache_key(context.aweme_id)
        cached = self.get_cached_candidate(adapter, context)
        if cached:
            cached_metadata = get_cached_douyin_metadata(context.aweme_id)
            if cached_metadata:
                metadata_future = Future()
                metadata_future.set_result(cached_metadata)
                return self.build_result(
                    adapter,
                    context,
                    cached,
                    metadata_future,
                    parse_started_at,
                )

        native_budget = min(
            max(0.1, self.provider_settings.ultra_native_timeout_seconds),
            max(0.1, deadline - monotonic()),
        )
        native_future = submit_provider_task(
            "douyin-native",
            self.provider_settings.native_executor_workers,
            self.provider_settings.native_max_pending_tasks,
            get_fast_native_result,
            adapter,
            context,
            native_budget,
        )
        if native_future is None:
            native_future = Future()
            native_future.set_result(None)
            log_provider_event(
                "douyin_native_local_capacity_full",
                "native_fast",
                context,
            )

        if cached:
            return self.build_result(
                adapter,
                context,
                cached,
                native_future,
                parse_started_at,
            )

        kukutool_future, flight_state = self.get_or_start_kukutool_future(
            adapter,
            context,
        )
        upstream_finished = flight_state in {
            "failure_cache_hit",
            "coordination_unavailable",
            "local_capacity_full",
        }
        poll_interval = self.provider_settings.kukutool_cache_poll_interval_seconds

        while monotonic() < deadline:
            validated = None
            if kukutool_future is not None and kukutool_future.done():
                upstream_finished = True
                try:
                    validated = kukutool_future.result(timeout=0)
                except Exception as error:
                    log_provider_failure(
                        "kukutool_background",
                        context,
                        error,
                    )
                    validated = None
            elif flight_state == "remote_single_flight":
                validated = self.get_cached_candidate(adapter, context)

            if validated:
                return self.build_result(
                    adapter,
                    context,
                    validated,
                    native_future,
                    parse_started_at,
                )

            native_result = self.get_finished_native_result(native_future, context)
            if upstream_finished and native_result:
                return with_parse_metadata(
                    native_result,
                    selected_provider="native",
                    fallback_reason="kukutool_unavailable",
                )

            sleep(min(poll_interval, max(0.0, deadline - monotonic())))

        validated = None
        if kukutool_future is not None and kukutool_future.done():
            try:
                validated = kukutool_future.result(timeout=0)
            except Exception as error:
                log_provider_failure(
                    "kukutool_background",
                    context,
                    error,
                )
                validated = None
        if validated:
            return self.build_result(
                adapter,
                context,
                validated,
                native_future,
                parse_started_at,
            )

        native_result = self.get_finished_native_result(native_future, context)
        if native_result:
            log_provider_event(
                "douyin_provider_deadline_native_fallback",
                self.name,
                context,
                parse_started_at,
                flight_state=flight_state,
                background_refresh=bool(
                    kukutool_future is not None and not kukutool_future.done()
                ),
            )
            return with_parse_metadata(
                native_result,
                selected_provider="native",
                fallback_reason="kukutool_deadline",
                kukutool_background_refresh=bool(
                    kukutool_future is not None and not kukutool_future.done()
                ),
            )

        log_provider_event(
            "douyin_provider_total_budget_exceeded",
            self.name,
            context,
            parse_started_at,
            flight_state=flight_state,
            background_refresh=bool(
                kukutool_future is not None and not kukutool_future.done()
            ),
        )
        raise XhsProviderTimeoutError(
            "超高清与本地解析均超过 "
            f"{self.provider_settings.ultra_total_timeout_seconds:g} 秒，"
            "请稍后重试。"
        )
