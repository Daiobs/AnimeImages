from hashlib import sha256
import shutil
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from django.conf import settings

from .assets import MediaAsset, ParseResult
from .common import XhsDownloadError, clean_filename, host_matches, is_blocked_hostname
from .provider_manifest import build_provider_manifest_token, provider_manifest_is_valid
from .platforms.external_tools import (
    external_command_health,
    probe_external_command_version,
    run_external_command,
    split_external_command,
)
from .platforms.ytdlp_generic import (
    YtDlpGenericProvider,
    float_value,
    format_url_is_direct_http,
    integer_value,
    ytdlp_format_has_audio,
    ytdlp_format_has_explicit_complete_video,
    ytdlp_format_has_video,
    ytdlp_format_score,
)


PROVIDER_ID = "youtube_merge"
PROVIDER_LABEL = "YouTube 高清合并"
MERGE_FORMAT_SELECTOR_TEMPLATE = "bv*[height<={height}]+ba/b[height<={height}]/b"
YOUTUBE_PAGE_HOST_SUFFIXES = ("youtube.com", "youtu.be")
YOUTUBE_COVER_HOST_SUFFIXES = ("ytimg.com",)


def get_youtube_merge_health(*, force_version_probe=False):
    enabled = bool(getattr(settings, "YOUTUBE_MERGE_PROVIDER_ENABLED", False))
    public_access_enabled = bool(
        getattr(settings, "YOUTUBE_MERGE_PUBLIC_ACCESS_ENABLED", False)
    )
    staff_only = bool(getattr(settings, "YOUTUBE_MERGE_STAFF_ONLY", False))
    require_ffmpeg = bool(getattr(settings, "YOUTUBE_MERGE_REQUIRE_FFMPEG", True))
    should_probe = bool(enabled or force_version_probe)

    ytdlp_health = YtDlpGenericProvider().health(
        force_version_probe=should_probe,
    )

    ffmpeg_binary = str(getattr(settings, "YOUTUBE_MERGE_FFMPEG_BINARY", "") or "")
    ffmpeg_health = external_command_health(ffmpeg_binary, "ffmpeg")
    version_probe = {
        "version_probe_ok": None,
        "version_output": "",
        "error": "",
    }
    if require_ffmpeg and should_probe and ffmpeg_health["executable_found"]:
        version_probe = probe_external_command_version(
            ffmpeg_health["command"],
            cache_seconds=60,
            version_args=("-version",),
        )

    ffmpeg_ready = bool(
        (not require_ffmpeg)
        or (
            ffmpeg_health["executable_found"]
            and (not should_probe or version_probe["version_probe_ok"])
        )
    )
    ready = bool(enabled and ytdlp_health.get("ready") and ffmpeg_ready)
    return {
        "provider": PROVIDER_ID,
        "label": PROVIDER_LABEL,
        "enabled": enabled,
        "public_access_enabled": public_access_enabled,
        "staff_only": staff_only,
        "ready": ready,
        "public_ready": bool(ready and public_access_enabled and not staff_only),
        "implemented": True,
        "mode": "server_merge_task",
        "max_height": int(getattr(settings, "YOUTUBE_MERGE_MAX_HEIGHT", 4320) or 4320),
        "max_file_size_bytes": int(
            getattr(settings, "YOUTUBE_MERGE_MAX_FILE_SIZE_BYTES", 1024 * 1024 * 1024)
            or 0
        ),
        "timeout_seconds": float(
            getattr(settings, "YOUTUBE_MERGE_TIMEOUT_SECONDS", 300.0) or 300.0
        ),
        "executor_workers": int(
            getattr(settings, "YOUTUBE_MERGE_EXECUTOR_WORKERS", 1) or 1
        ),
        "max_pending_tasks": int(
            getattr(settings, "YOUTUBE_MERGE_MAX_PENDING_TASKS", 1) or 1
        ),
        "require_ffmpeg": require_ffmpeg,
        "ffmpeg": {
            **ffmpeg_health,
            "version_probe_ok": version_probe["version_probe_ok"],
            "version_output": version_probe["version_output"],
            "version_probe_error": version_probe["error"],
        },
        "ytdlp": ytdlp_health,
    }


def youtube_merge_source_key(source_url):
    return sha256(str(source_url or "").strip().encode("utf-8")).hexdigest()[:24]


def source_url_is_allowed(source_url):
    parsed = urlparse(str(source_url or ""))
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    if is_blocked_hostname(parsed.hostname):
        return False
    return host_matches(parsed.hostname, YOUTUBE_PAGE_HOST_SUFFIXES)


def cover_url_is_allowed(cover_url):
    parsed = urlparse(str(cover_url or ""))
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    if is_blocked_hostname(parsed.hostname):
        return False
    return host_matches(parsed.hostname, YOUTUBE_COVER_HOST_SUFFIXES)


def parse_youtube_merge_asset_id(asset_id):
    parts = str(asset_id or "").split(":")
    if len(parts) < 5 or parts[0] != PROVIDER_ID:
        return None
    extra_parts = list(parts[5:])
    is_cover = False
    if extra_parts and extra_parts[-1] == "cover":
        is_cover = True
        extra_parts = extra_parts[:-1]
    max_height = int(getattr(settings, "YOUTUBE_MERGE_MAX_HEIGHT", 4320) or 4320)
    if extra_parts:
        if len(extra_parts) != 1 or not extra_parts[0].startswith("h"):
            return None
        value = extra_parts[0][1:]
        if not value.isdigit():
            return None
        max_height = int(value)
    if max_height <= 0:
        return None
    platform_id = parts[1]
    source_key = parts[2]
    media_type = parts[3]
    index = parts[4]
    if platform_id != "youtube":
        return None
    if not source_key or len(source_key) > 64:
        return None
    if media_type != "video":
        return None
    if not index.isdigit():
        return None
    return {
        "platform_id": platform_id,
        "source_key": source_key,
        "media_type": media_type,
        "index": index,
        "is_cover": is_cover,
        "max_height": max_height,
    }


def youtube_merge_manifest_item_is_allowed(item):
    if not isinstance(item, dict):
        return False
    parsed_asset = parse_youtube_merge_asset_id(item.get("asset_id"))
    if not parsed_asset:
        return False
    item_type = str(item.get("type") or "").strip().lower()
    if item_type:
        if parsed_asset["is_cover"]:
            if item_type != "image":
                return False
        elif item_type != "video":
            return False
    if item.get("provider") != PROVIDER_ID:
        return False
    if item.get("provider_resource_id") != parsed_asset["source_key"]:
        return False
    expected_cache_key = (
        f"{PROVIDER_ID}:{parsed_asset['source_key']}:h{parsed_asset['max_height']}"
    )
    if item.get("provider_cache_key") != expected_cache_key:
        return False
    if parsed_asset["is_cover"]:
        return cover_url_is_allowed(item.get("url"))
    return source_url_is_allowed(item.get("url"))


def provider_manifest_allows_youtube_merge(item):
    if not isinstance(item, dict):
        return False
    if item.get("provider") != PROVIDER_ID:
        return False
    if not provider_manifest_is_valid(item):
        return False
    return youtube_merge_manifest_item_is_allowed(item)


def youtube_merge_manifest_identity(item):
    if not provider_manifest_allows_youtube_merge(item):
        return None
    parsed_asset = parse_youtube_merge_asset_id(item.get("asset_id"))
    if not parsed_asset or parsed_asset["is_cover"]:
        return None
    return parsed_asset["source_key"]


def build_youtube_merge_asset_fields(source_url, *, media_type="video", index=1, url="", max_height=None):
    source_key = youtube_merge_source_key(source_url)
    max_height = int(max_height or getattr(settings, "YOUTUBE_MERGE_MAX_HEIGHT", 4320) or 4320)
    asset_id = f"{PROVIDER_ID}:youtube:{source_key}:{media_type}:{index}:h{max_height}"
    provider_cache_key = f"{PROVIDER_ID}:{source_key}:h{max_height}"
    signed_url = url or source_url
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
            url=signed_url,
            media_type=media_type,
        ),
    }


def format_size_bytes(item):
    if not isinstance(item, dict):
        return 0
    return integer_value(item.get("filesize") or item.get("filesize_approx"))


def summarize_ytdlp_format(item):
    if not isinstance(item, dict):
        return {}
    return {
        "format_id": str(item.get("format_id") or ""),
        "format_note": str(item.get("format_note") or ""),
        "url": str(item.get("url") or ""),
        "ext": str(item.get("ext") or ""),
        "protocol": str(item.get("protocol") or ""),
        "height": integer_value(item.get("height")),
        "width": integer_value(item.get("width")),
        "tbr": float_value(item.get("tbr")),
        "abr": float_value(item.get("abr")),
        "vcodec": str(item.get("vcodec") or ""),
        "acodec": str(item.get("acodec") or ""),
        "size_bytes": format_size_bytes(item),
    }


def youtube_video_stream_score(item):
    ext = str(item.get("ext") or "").lower()
    vcodec = str(item.get("vcodec") or "").lower()
    return (
        integer_value(item.get("height")),
        integer_value(item.get("width")),
        1 if ext == "mp4" else 0,
        1 if vcodec.startswith(("avc", "h264")) else 0,
        float_value(item.get("tbr")),
        format_size_bytes(item),
    )


def youtube_audio_stream_score(item):
    ext = str(item.get("ext") or "").lower()
    acodec = str(item.get("acodec") or "").lower()
    return (
        1 if ext in {"m4a", "mp4"} else 0,
        1 if acodec.startswith(("mp4a", "aac")) else 0,
        float_value(item.get("abr") or item.get("tbr")),
        format_size_bytes(item),
    )


def iter_youtube_formats(payload):
    formats = payload.get("formats") if isinstance(payload, dict) else None
    if isinstance(formats, (list, tuple)):
        for item in formats:
            if isinstance(item, dict):
                yield item
    if isinstance(payload, dict) and payload.get("url"):
        yield payload


def best_youtube_complete_format(formats, *, max_height):
    candidates = []
    for item in formats:
        if not format_url_is_direct_http(item):
            continue
        if not ytdlp_format_has_explicit_complete_video(item):
            continue
        height = integer_value(item.get("height"))
        if max_height and height > max_height:
            continue
        candidates.append(item)
    if not candidates:
        return None
    return max(candidates, key=ytdlp_format_score)


def youtube_combined_size_within_limit(video, audio, max_file_size_bytes):
    if not max_file_size_bytes:
        return True
    video_size = format_size_bytes(video)
    audio_size = format_size_bytes(audio)
    if not video_size or not audio_size:
        return True
    return video_size + audio_size <= max_file_size_bytes


def best_youtube_video_stream(formats, *, max_height, audio=None, max_file_size_bytes=0):
    candidates = []
    for item in formats:
        if not format_url_is_direct_http(item):
            continue
        if not ytdlp_format_has_video(item) or ytdlp_format_has_audio(item):
            continue
        height = integer_value(item.get("height"))
        if max_height and height > max_height:
            continue
        if audio and not youtube_combined_size_within_limit(item, audio, max_file_size_bytes):
            continue
        candidates.append(item)
    if not candidates:
        return None
    return max(candidates, key=youtube_video_stream_score)


def best_youtube_audio_stream(formats):
    candidates = []
    for item in formats:
        if not format_url_is_direct_http(item):
            continue
        if not ytdlp_format_has_audio(item) or ytdlp_format_has_video(item):
            continue
        candidates.append(item)
    if not candidates:
        return None
    return max(candidates, key=youtube_audio_stream_score)


def build_youtube_merge_plan(
    payload,
    *,
    max_height=None,
    max_file_size_bytes=None,
):
    configured_max_height = int(
        max_height
        if max_height is not None
        else getattr(settings, "YOUTUBE_MERGE_MAX_HEIGHT", 4320) or 4320
    )
    configured_size_limit = int(
        max_file_size_bytes
        if max_file_size_bytes is not None
        else getattr(settings, "YOUTUBE_MERGE_MAX_FILE_SIZE_BYTES", 1024 * 1024 * 1024)
        or 0
    )
    formats = tuple(iter_youtube_formats(payload))
    audio = best_youtube_audio_stream(formats)
    complete = best_youtube_complete_format(formats, max_height=configured_max_height)
    unlimited_video = best_youtube_video_stream(formats, max_height=configured_max_height)
    video = best_youtube_video_stream(
        formats,
        max_height=configured_max_height,
        audio=audio,
        max_file_size_bytes=configured_size_limit,
    )

    complete_height = integer_value((complete or {}).get("height"))
    video_height = integer_value((video or {}).get("height"))
    oversized_video_candidate = bool(audio and unlimited_video and not video)
    estimated_video = video or (unlimited_video if oversized_video_candidate else None)
    estimated_size_bytes = format_size_bytes(estimated_video) + format_size_bytes(audio)
    estimated_size_known = bool(estimated_video and audio and estimated_size_bytes)
    estimated_size_within_limit = bool(
        not configured_size_limit
        or not estimated_size_known
        or estimated_size_bytes <= configured_size_limit
    )
    merge_video = video or (unlimited_video if oversized_video_candidate else None)
    merge_video_height = integer_value((merge_video or {}).get("height"))
    merge_candidate_found = bool(merge_video and audio)
    requires_merge = bool(
        merge_candidate_found
        and (not complete or merge_video_height > complete_height)
    )

    selected_max_height = video_height or configured_max_height
    if oversized_video_candidate:
        reason = "estimated_size_exceeds_limit"
    elif not merge_candidate_found:
        reason = "no_merge_candidate"
    elif not requires_merge:
        reason = "single_file_is_sufficient"
    else:
        reason = "merge_candidate_available"

    return {
        "available": bool(merge_candidate_found and estimated_size_within_limit),
        "requires_merge": requires_merge,
        "reason": reason,
        "implemented": True,
        "title": str((payload or {}).get("title") or ""),
        "id": str((payload or {}).get("id") or ""),
        "webpage_url": str((payload or {}).get("webpage_url") or ""),
        "format_count": len(formats),
        "requested_max_height": configured_max_height,
        "max_height": selected_max_height,
        "max_file_size_bytes": configured_size_limit,
        "estimated_size_bytes": estimated_size_bytes,
        "estimated_size_known": estimated_size_known,
        "estimated_size_within_limit": estimated_size_within_limit,
        "merge_format_selector": MERGE_FORMAT_SELECTOR_TEMPLATE.format(
            height=selected_max_height
        ),
        "complete_format": summarize_ytdlp_format(complete),
        "video_format": summarize_ytdlp_format(video),
        "audio_format": summarize_ytdlp_format(audio),
    }


def pick_youtube_cover_url(payload):
    if not isinstance(payload, dict):
        return ""
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
        if cover_url_is_allowed(url):
            return str(url)
    return ""


def build_youtube_merge_parse_result(source_url, payload):
    if not source_url_is_allowed(source_url):
        raise XhsDownloadError("YouTube source URL is not allowed")
    plan = build_youtube_merge_plan(payload)
    if not plan["available"] or not plan["requires_merge"]:
        raise XhsDownloadError("YouTube merge candidate is not available")

    title = clean_filename(plan["title"] or "youtube")
    cover_url = pick_youtube_cover_url(payload)
    source_key = youtube_merge_source_key(source_url)
    asset_fields = build_youtube_merge_asset_fields(
        source_url,
        media_type="video",
        index=1,
        url=source_url,
        max_height=int(plan.get("max_height") or getattr(settings, "YOUTUBE_MERGE_MAX_HEIGHT", 4320) or 4320),
    )
    video_height = int((plan.get("video_format") or {}).get("height") or 0)
    quality_label = f"{video_height}p 高清合并" if video_height else "高清合并"
    asset = MediaAsset(
        media_type="video",
        url=source_url,
        platform="third_party",
        cover_url=cover_url,
        cover_preview_url=cover_url,
        quality_label=quality_label,
        size_bytes=int(plan.get("estimated_size_bytes") or 0),
        **asset_fields,
    )
    metadata = {
        "source_platform": "youtube",
        "source_platform_name": "YouTube",
        "source_provider": PROVIDER_ID,
        "provider_label": PROVIDER_LABEL,
        "selected_provider": PROVIDER_ID,
        "candidate_count": int(plan.get("format_count") or 0),
        "canonical_source_url": source_url,
        "youtube_merge_plan": {
            key: value
            for key, value in plan.items()
            if key not in {"video_format", "audio_format", "complete_format"}
        },
        "provider_resource_id": source_key,
    }
    return ParseResult(
        platform="third_party",
        post_type="video",
        base_filename=title or "youtube",
        assets=(asset,),
        metadata=metadata,
    )


def build_youtube_merge_command(source_url, output_template, *, max_height=None):
    ytdlp_command = split_external_command(
        getattr(settings, "YTDLP_BINARY", "") or "",
        "yt-dlp",
    )
    max_height = int(max_height or getattr(settings, "YOUTUBE_MERGE_MAX_HEIGHT", 4320) or 4320)
    selector = MERGE_FORMAT_SELECTOR_TEMPLATE.format(height=max_height)
    args = [
        *ytdlp_command,
        "--ignore-config",
        "--no-warnings",
        "--no-playlist",
        "--no-part",
        "--force-overwrites",
        "-f",
        selector,
        "--merge-output-format",
        "mp4",
        "-o",
        str(output_template),
    ]
    ffmpeg_binary = str(getattr(settings, "YOUTUBE_MERGE_FFMPEG_BINARY", "") or "").strip()
    if ffmpeg_binary:
        ffmpeg_command = split_external_command(ffmpeg_binary, "ffmpeg")
        args.extend(["--ffmpeg-location", ffmpeg_command[0]])
    args.append(source_url)
    return args


def download_youtube_merge_to_temp_path(item, progress_callback=None):
    if not provider_manifest_allows_youtube_merge(item):
        raise XhsDownloadError("YouTube 合并资源签名未通过校验")
    source_url = str(item.get("url") or "")
    if not source_url_is_allowed(source_url):
        raise XhsDownloadError("YouTube 源链接不允许请求")
    parsed_asset = parse_youtube_merge_asset_id(item.get("asset_id"))
    if not parsed_asset:
        raise XhsDownloadError("YouTube 合并资源 ID 无效")

    timeout_seconds = float(
        getattr(settings, "YOUTUBE_MERGE_TIMEOUT_SECONDS", 300.0) or 300.0
    )
    max_bytes = int(
        getattr(settings, "YOUTUBE_MERGE_MAX_FILE_SIZE_BYTES", 1024 * 1024 * 1024)
        or 0
    )
    work_dir = tempfile.mkdtemp(prefix="youtube_merge_")
    output_template = Path(work_dir) / "merged.%(ext)s"
    try:
        estimated_size = int(item.get("size_bytes") or 0)
        if progress_callback and estimated_size:
            progress_callback(max(1, int(estimated_size * 0.02)), estimated_size)
        completed = run_external_command(
            build_youtube_merge_command(
                source_url,
                output_template,
                max_height=parsed_asset["max_height"],
            ),
            timeout_seconds=timeout_seconds,
            progress_callback=progress_callback,
            progress_path=work_dir,
            estimated_size=estimated_size,
        )
        if completed.returncode != 0:
            message = (completed.stderr or completed.stdout or "yt-dlp merge failed").strip()
            raise XhsDownloadError(f"YouTube 合并失败：{message[-500:]}")

        candidates = sorted(Path(work_dir).glob("merged.*"))
        output_path = next(
            (path for path in candidates if path.suffix.lower() == ".mp4"),
            candidates[0] if candidates else None,
        )
        if not output_path or not output_path.exists():
            raise XhsDownloadError("YouTube 合并未生成文件")
        output_size = output_path.stat().st_size
        if max_bytes and output_size > max_bytes:
            raise XhsDownloadError("YouTube 合并文件超过允许大小限制")
        if progress_callback and estimated_size:
            progress_callback(max(output_size, int(estimated_size * 0.98)), estimated_size)

        target = tempfile.NamedTemporaryFile(
            prefix="youtube_merge_result_",
            suffix=".mp4",
            delete=False,
        )
        target_path = target.name
        target.close()
        shutil.move(str(output_path), target_path)
        return {
            "path": target_path,
            "content_type": "video/mp4",
            "final_url": source_url,
            "size": Path(target_path).stat().st_size,
        }
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
