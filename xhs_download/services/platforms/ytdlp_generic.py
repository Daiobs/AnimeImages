import json
import logging
import subprocess
from concurrent.futures import TimeoutError
from urllib.parse import urlparse

from django.conf import settings

from ..assets import MediaAsset, ParseResult
from ..common import XhsProviderTimeoutError, clean_filename
from ..provider_manifest import build_provider_manifest_token
from .douyin_kukutool import submit_provider_task
from .external_tools import (
    cache_external_failure,
    cache_external_payload,
    clear_cached_external_failure,
    effective_external_timeout_seconds,
    external_command_health,
    get_cached_external_failure,
    get_cached_external_payload,
    external_command_output_max_bytes,
    probe_external_command_version,
    run_external_command,
    split_external_command,
)
from .kukutool_generic import public_url_or_empty, source_key_for_url


logger = logging.getLogger(__name__)
PROVIDER_ID = "yt_dlp_generic"
PROVIDER_LABEL = "yt-dlp"
DIRECT_HTTP_PROTOCOLS = {"http", "https"}
STREAMING_PROTOCOL_MARKERS = ("m3u8", "dash", "m3u")
VIDEO_ONLY_SUFFIXES_BY_PLATFORM = {
    "youtube": ("googlevideo.com",),
}


def run_ytdlp_json(url, *, binary="", timeout_seconds=15.0):
    try:
        command = split_external_command(binary, "yt-dlp")
    except ValueError as error:
        raise RuntimeError(f"yt-dlp command is invalid: {error}") from error
    args = [
        *command,
        "--ignore-config",
        "--dump-single-json",
        "--no-warnings",
        "--no-playlist",
        "--skip-download",
        url,
    ]
    try:
        completed = run_external_command(
            args,
            timeout_seconds=max(0.1, float(timeout_seconds or 15.0)),
        )
    except subprocess.TimeoutExpired as error:
        raise XhsProviderTimeoutError("yt-dlp 解析超时，请稍后再试") from error
    except FileNotFoundError as error:
        raise RuntimeError("yt-dlp executable was not found") from error

    if completed.returncode != 0:
        message = (completed.stderr or completed.stdout or "yt-dlp failed").strip()
        raise RuntimeError(message[-500:])

    try:
        payload = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError as error:
        raise RuntimeError("yt-dlp returned invalid JSON") from error
    if not isinstance(payload, dict):
        raise RuntimeError("yt-dlp result format is invalid")
    return payload


def integer_value(value):
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def float_value(value):
    try:
        return max(0.0, float(value or 0.0))
    except (TypeError, ValueError):
        return 0.0


def format_url_is_direct_http(item):
    if not isinstance(item, dict):
        return False
    protocol = str(item.get("protocol") or "").lower()
    if protocol and protocol not in DIRECT_HTTP_PROTOCOLS:
        return False
    if any(marker in protocol for marker in STREAMING_PROTOCOL_MARKERS):
        return False
    url = item.get("url")
    parsed = urlparse(str(url or ""))
    path = (parsed.path or "").lower()
    if path.endswith((".m3u8", ".mpd")):
        return False
    return parsed.scheme in {"http", "https"} and bool(parsed.hostname)


def ytdlp_format_score(item):
    size_bytes = integer_value(item.get("filesize") or item.get("filesize_approx"))
    height = integer_value(item.get("height"))
    width = integer_value(item.get("width"))
    tbr = float_value(item.get("tbr"))
    ext_bonus = 1 if str(item.get("ext") or "").lower() == "mp4" else 0
    return (
        height,
        width,
        tbr,
        size_bytes,
        ext_bonus,
    )


def ytdlp_format_has_audio(item):
    acodec = str(item.get("acodec") or "").strip().lower()
    if acodec:
        return acodec != "none"
    audio_ext = str(item.get("audio_ext") or "").strip().lower()
    if audio_ext:
        return audio_ext != "none"
    return False


def ytdlp_format_has_video(item):
    vcodec = str(item.get("vcodec") or "").strip().lower()
    if vcodec:
        return vcodec != "none"
    video_ext = str(item.get("video_ext") or "").strip().lower()
    if video_ext:
        return video_ext != "none"
    return False


def ytdlp_format_is_complete_video(item):
    if not isinstance(item, dict):
        return False
    return ytdlp_format_has_audio(item) and ytdlp_format_has_video(item)


def ytdlp_format_has_explicit_complete_video(item):
    if not isinstance(item, dict):
        return False
    has_audio_marker = bool(
        str(item.get("acodec") or item.get("audio_ext") or "").strip()
    )
    has_video_marker = bool(
        str(item.get("vcodec") or item.get("video_ext") or "").strip()
    )
    return (
        has_audio_marker
        and has_video_marker
        and ytdlp_format_is_complete_video(item)
    )


def video_allowed_suffixes(rule):
    explicit_suffixes = getattr(rule, "video_media_host_suffixes", ())
    if explicit_suffixes:
        return explicit_suffixes
    platform_id = getattr(rule, "platform_id", "")
    return VIDEO_ONLY_SUFFIXES_BY_PLATFORM.get(
        platform_id,
        getattr(rule, "media_host_suffixes", ()),
    )


class YtDlpGenericProvider:
    provider_id = PROVIDER_ID
    label = PROVIDER_LABEL

    def __init__(self, runner=None):
        self.runner = runner or run_ytdlp_json

    def normalize_timeout(self, timeout_seconds=None):
        if timeout_seconds is not None:
            return max(0.1, float(timeout_seconds or 0.1))
        return effective_external_timeout_seconds(
            getattr(settings, "YTDLP_TIMEOUT_SECONDS", 15.0)
        )

    def run(self, url, timeout_seconds=None):
        timeout_seconds = self.normalize_timeout(timeout_seconds)
        return self.runner(
            url,
            binary=getattr(settings, "YTDLP_BINARY", "") or "",
            timeout_seconds=timeout_seconds,
        )

    def parse(self, url, timeout_seconds=None):
        source_key = source_key_for_url(url)
        cached = get_cached_external_payload(PROVIDER_ID, source_key)
        if cached is not None:
            return cached
        failure = get_cached_external_failure(PROVIDER_ID, source_key)
        if failure:
            raise XhsProviderTimeoutError("yt-dlp 最近解析失败，请稍后再试")

        timeout_seconds = self.normalize_timeout(timeout_seconds)
        future = submit_provider_task(
            "third-party-ytdlp",
            getattr(settings, "YTDLP_EXECUTOR_WORKERS", 1),
            getattr(settings, "YTDLP_MAX_PENDING_TASKS", 2),
            self.run,
            url,
            timeout_seconds,
        )
        if future is None:
            raise XhsProviderTimeoutError("yt-dlp 解析队列已满，请稍后再试")
        try:
            payload = future.result(timeout=timeout_seconds)
            cache_external_payload(
                PROVIDER_ID,
                source_key,
                payload,
                getattr(settings, "YTDLP_PAYLOAD_CACHE_TTL_SECONDS", 300),
            )
            clear_cached_external_failure(PROVIDER_ID, source_key)
            return payload
        except TimeoutError as error:
            cache_external_failure(
                PROVIDER_ID,
                source_key,
                type(error).__name__,
                getattr(settings, "YTDLP_FAILURE_CACHE_TTL_SECONDS", 30),
            )
            raise XhsProviderTimeoutError("yt-dlp 解析超时，请稍后再试") from error
        except Exception as error:
            cache_external_failure(
                PROVIDER_ID,
                source_key,
                type(error).__name__,
                getattr(settings, "YTDLP_FAILURE_CACHE_TTL_SECONDS", 30),
            )
            raise

    def health(self, version_cache_seconds=60, force_version_probe=False):
        binary = str(getattr(settings, "YTDLP_BINARY", "") or "").strip()
        command_health = external_command_health(binary, "yt-dlp")
        enabled = bool(getattr(settings, "YTDLP_PROVIDER_ENABLED", False))
        should_probe_version = bool(enabled or force_version_probe)
        version_probe = {
            "version_probe_ok": None,
            "version_output": "",
            "error": "",
        }
        if should_probe_version and command_health["executable_found"]:
            version_probe = probe_external_command_version(
                command_health["command"],
                cache_seconds=version_cache_seconds,
            )
        ready = bool(
            command_health["executable_found"]
            and (not should_probe_version or version_probe["version_probe_ok"])
        )
        return {
            "provider": PROVIDER_ID,
            "label": PROVIDER_LABEL,
            "enabled": enabled,
            "ready": ready,
            **command_health,
            "version_probe_ok": version_probe["version_probe_ok"],
            "version_output": version_probe["version_output"],
            "version_probe_error": version_probe["error"],
            "timeout_seconds": float(getattr(settings, "YTDLP_TIMEOUT_SECONDS", 15.0) or 15.0),
            "output_max_bytes": external_command_output_max_bytes(),
            "executor_workers": int(getattr(settings, "YTDLP_EXECUTOR_WORKERS", 1) or 1),
            "max_pending_tasks": int(getattr(settings, "YTDLP_MAX_PENDING_TASKS", 2) or 2),
            "payload_cache_ttl_seconds": int(getattr(settings, "YTDLP_PAYLOAD_CACHE_TTL_SECONDS", 300) or 0),
            "failure_cache_ttl_seconds": int(getattr(settings, "YTDLP_FAILURE_CACHE_TTL_SECONDS", 30) or 0),
        }


class YtDlpResultNormalizer:
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
            "canonical_source_url": source_url,
        }

    def pick_cover_url(self, payload, allowed_suffixes, platform_id):
        candidates = []
        if payload.get("thumbnail"):
            candidates.append(payload.get("thumbnail"))
        thumbnails = payload.get("thumbnails")
        if isinstance(thumbnails, (list, tuple)):
            for item in reversed(thumbnails):
                if isinstance(item, dict):
                    candidates.append(item.get("url"))
                elif isinstance(item, str):
                    candidates.append(item)
        for url in candidates:
            cover_url = public_url_or_empty(
                url,
                allowed_suffixes,
                platform_id,
                "thumbnail",
            )
            if cover_url:
                return cover_url
        return ""

    def collect_video_formats(self, payload, allowed_suffixes, platform_id):
        candidates = []
        formats = payload.get("formats")
        if isinstance(formats, (list, tuple)):
            for item in formats:
                if not format_url_is_direct_http(item):
                    continue
                if not ytdlp_format_has_explicit_complete_video(item):
                    continue
                url = public_url_or_empty(
                    item.get("url"),
                    allowed_suffixes,
                    platform_id,
                    "formats",
                )
                if not url:
                    continue
                candidates.append({
                    "url": url,
                    "quality_label": str(
                        item.get("format_note")
                        or item.get("resolution")
                        or item.get("format_id")
                        or "yt-dlp resource"
                    ),
                    "size_bytes": integer_value(
                        item.get("filesize") or item.get("filesize_approx")
                    ),
                    "score": ytdlp_format_score(item),
                })

        fallback_url = public_url_or_empty(
            payload.get("url"),
            allowed_suffixes,
            platform_id,
            "url",
        )
        if (
            fallback_url
            and format_url_is_direct_http(payload)
            and ytdlp_format_has_explicit_complete_video(payload)
        ):
            candidates.append({
                "url": fallback_url,
                "quality_label": str(payload.get("format") or "yt-dlp resource"),
                "size_bytes": integer_value(
                    payload.get("filesize") or payload.get("filesize_approx")
                ),
                "score": (
                    integer_value(payload.get("height")),
                    integer_value(payload.get("width")),
                    float_value(payload.get("tbr")),
                    integer_value(payload.get("filesize") or payload.get("filesize_approx")),
                    1,
                    1 if str(payload.get("ext") or "").lower() == "mp4" else 0,
                ),
            })

        deduped = {}
        for candidate in candidates:
            current = deduped.get(candidate["url"])
            if current is None or candidate["score"] > current["score"]:
                deduped[candidate["url"]] = candidate
        return tuple(
            sorted(
                deduped.values(),
                key=lambda item: item["score"],
                reverse=True,
            )
        )

    def normalize(self, *, rule, platform_name, source_url, payload):
        if not isinstance(payload, dict):
            raise ValueError("yt-dlp result format is invalid")

        allowed_suffixes = video_allowed_suffixes(rule)
        cover_allowed_suffixes = getattr(rule, "media_host_suffixes", ())
        platform_id = getattr(rule, "platform_id", "")
        video_formats = self.collect_video_formats(payload, allowed_suffixes, platform_id)
        if not video_formats:
            raise ValueError("yt-dlp result does not contain direct video assets")

        title = clean_filename(str(payload.get("title") or platform_name))
        cover_url = self.pick_cover_url(payload, cover_allowed_suffixes, platform_id)
        selected = video_formats[0]
        asset = MediaAsset(
            media_type="video",
            url=selected["url"],
            platform=self.platform_id,
            cover_url=cover_url,
            cover_preview_url=cover_url,
            quality_label=selected["quality_label"],
            size_bytes=selected["size_bytes"],
            **self.build_provider_asset_fields(
                rule,
                source_url,
                "video",
                1,
                selected["url"],
            ),
        )
        return ParseResult(
            platform=self.platform_id,
            post_type="video",
            base_filename=title or "third_party",
            assets=(asset,),
            metadata=self.build_metadata(
                rule,
                platform_name,
                len(video_formats),
                source_url,
            ),
        )
