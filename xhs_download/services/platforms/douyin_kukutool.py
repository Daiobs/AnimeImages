from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass, replace
from functools import lru_cache
import json
import logging
from pathlib import Path
import re
import shutil
import subprocess
from threading import BoundedSemaphore, Lock
from time import monotonic, time
from urllib.parse import parse_qs, urlsplit

from django.core.cache import cache

from ..candidates import MediaCandidate
from ..common import validate_external_url
from ..provider_settings import get_platform_provider_settings
from ..quality import QualityProbe


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


@lru_cache(maxsize=8)
def find_node_runtime(configured_binary=""):
    candidates = []
    if configured_binary:
        candidates.append(configured_binary)
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
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if check.returncode == 0:
            return candidate
    return ""


def run_kukutool_helper(aweme_id, node_binary="", timeout_seconds=12.0):
    if not AWEME_ID_PATTERN.fullmatch(str(aweme_id or "")):
        raise ValueError("invalid aweme_id")

    node_runtime = find_node_runtime(node_binary)
    if not node_runtime:
        raise OSError("KuKuTool Provider requires Node.js 18 or newer")
    if not KUKUTOOL_HELPER.is_file():
        raise OSError("KuKuTool Provider helper script is missing")

    process = subprocess.run(
        [node_runtime, str(KUKUTOOL_HELPER), str(aweme_id)],
        capture_output=True,
        text=True,
        timeout=max(0.1, timeout_seconds),
        check=False,
    )
    if process.returncode != 0:
        message = (process.stderr or process.stdout or "KuKuTool request failed").strip()
        raise OSError(message[:2000])
    if len(process.stdout) > 10 * 1024 * 1024:
        raise ValueError("KuKuTool response is too large")
    return json.loads(process.stdout)


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

    def resolve_validated_candidate(self, adapter, context):
        cache_key = build_kukutool_cache_key(context.aweme_id)
        cached = get_cached_validated_candidate(
            cache_key,
            adapter.media_host_suffixes,
        )
        if cached:
            log_kukutool_event(
                "douyin_provider_validated_cache_hit",
                context,
                cache_hit=True,
                selected_size_bytes=cached.candidate.size_bytes,
                candidate_count=cached.candidate_count,
            )
            return cached

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
            payload = self.runner(
                context.aweme_id,
                node_binary=self.provider_settings.kukutool_node_binary,
                timeout_seconds=runner_timeout,
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
            cache_validated_candidate(
                cache_key,
                validated,
                self.provider_settings.kukutool_validated_cache_ttl_seconds,
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
            cache_validated_candidate(
                cache_key,
                validated,
                self.provider_settings.kukutool_validated_cache_ttl_seconds,
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
            return validated
        log_kukutool_event(
            "douyin_provider_no_validated_candidate",
            context,
            candidate_count=len(resources),
            probe_duration_ms=int((time() - probe_started_at) * 1000),
        )
        return None

    def parse(self, adapter, context):
        if (
            not self.provider_settings.provider_is_enabled(self.name)
            or not context.aweme_id
        ):
            return None

        from .douyin_providers import (
            DOUYIN_ULTRA_EXECUTOR,
            build_external_parse_result,
            get_fast_native_result,
            log_provider_event,
            log_provider_failure,
            with_parse_metadata,
        )

        parse_started_at = time()
        native_future = DOUYIN_ULTRA_EXECUTOR.submit(
            get_fast_native_result,
            adapter,
            context,
            min(
                self.provider_settings.ultra_native_timeout_seconds,
                self.remaining_budget(
                    context,
                    self.provider_settings.ultra_native_timeout_seconds,
                ),
            ),
        )

        validated = None
        try:
            validated = self.resolve_validated_candidate(adapter, context)
        except subprocess.TimeoutExpired as error:
            log_provider_failure(self.name, context, error, parse_started_at)
        except Exception as error:
            log_provider_failure(self.name, context, error, parse_started_at)

        if validated:
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
                native_future.cancel()
            except Exception as error:
                log_provider_failure("native_reference", context, error)

            result = build_external_parse_result(
                adapter,
                context,
                validated.candidate,
                reference_result,
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

        native_result = None
        try:
            remaining = self.remaining_budget(context, 0)
            if native_future.done():
                native_result = native_future.result(timeout=0)
            elif remaining > 0:
                native_result = native_future.result(timeout=remaining)
        except FutureTimeoutError:
            native_future.cancel()
        except Exception as error:
            log_provider_failure("native_fast", context, error)

        if native_result:
            return with_parse_metadata(
                native_result,
                selected_provider="native",
                fallback_reason="kukutool_unavailable",
            )
        return None
