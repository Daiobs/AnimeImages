from hashlib import sha256
import shutil
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from django.conf import settings

from ..assets import MediaAsset, ParseResult
from ..common import XhsDownloadError, clean_filename, host_matches, is_blocked_hostname
from ..provider_manifest import build_provider_manifest_token, provider_manifest_is_valid
from .external_tools import run_external_command, split_external_command
from .ytdlp_generic import (
    PROVIDER_LABEL as YTDLP_PROVIDER_LABEL,
    float_value,
    format_url_is_direct_http,
    integer_value,
    ytdlp_format_has_audio,
    ytdlp_format_has_explicit_complete_video,
    ytdlp_format_has_video,
    ytdlp_format_score,
)


PROVIDER_ID = "yt_dlp_merge"
PROVIDER_LABEL = "yt-dlp 高清合并"
MERGE_FORMAT_SELECTOR_TEMPLATE = "bv*[height<={height}]+ba/b[height<={height}]/b"


def default_max_height():
    return int(getattr(settings, "YTDLP_MERGE_DEFAULT_MAX_HEIGHT", 1080) or 1080)


def high_quality_max_height():
    return int(
        getattr(
            settings,
            "YTDLP_MERGE_HIGH_QUALITY_MAX_HEIGHT",
            getattr(settings, "YTDLP_MERGE_MAX_HEIGHT", 4320),
        )
        or 4320
    )


def source_key_for_url(source_url):
    return sha256(str(source_url or "").strip().encode("utf-8")).hexdigest()[:24]


def format_size_bytes(item):
    if not isinstance(item, dict):
        return 0
    return integer_value(item.get("filesize") or item.get("filesize_approx"))


def video_stream_score(item):
    ext = str(item.get("ext") or "").lower()
    vcodec = str(item.get("vcodec") or "").lower()
    if vcodec.startswith("av01"):
        codec_score = 3
    elif vcodec.startswith("vp9"):
        codec_score = 2
    elif vcodec.startswith(("avc", "h264")):
        codec_score = 1
    else:
        codec_score = 0
    return (
        integer_value(item.get("height")),
        integer_value(item.get("width")),
        integer_value(item.get("fps")),
        codec_score,
        1 if ext == "mp4" else 0,
        float_value(item.get("tbr")),
        format_size_bytes(item),
    )


def audio_stream_score(item):
    acodec = str(item.get("acodec") or "").lower()
    if acodec.startswith("opus"):
        codec_score = 2
    elif acodec.startswith(("mp4a", "aac")):
        codec_score = 1
    else:
        codec_score = 0
    return (
        codec_score,
        float_value(item.get("abr") or item.get("tbr")),
        format_size_bytes(item),
    )


def iter_formats(payload):
    formats = payload.get("formats") if isinstance(payload, dict) else None
    if isinstance(formats, (list, tuple)):
        for item in formats:
            if isinstance(item, dict):
                yield item
    if isinstance(payload, dict) and payload.get("url"):
        yield payload


def best_complete_format(formats, *, max_height):
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
    return max(candidates, key=ytdlp_format_score) if candidates else None


def combined_size_within_limit(video, audio, max_file_size_bytes):
    if not max_file_size_bytes:
        return True
    video_size = format_size_bytes(video)
    audio_size = format_size_bytes(audio)
    if not video_size or not audio_size:
        return True
    return video_size + audio_size <= max_file_size_bytes


def best_video_stream(formats, *, max_height, audio=None, max_file_size_bytes=0):
    candidates = []
    for item in formats:
        if not format_url_is_direct_http(item):
            continue
        if not ytdlp_format_has_video(item) or ytdlp_format_has_audio(item):
            continue
        height = integer_value(item.get("height"))
        if max_height and height > max_height:
            continue
        if audio and not combined_size_within_limit(item, audio, max_file_size_bytes):
            continue
        candidates.append(item)
    return max(candidates, key=video_stream_score) if candidates else None


def best_audio_stream(formats):
    candidates = []
    for item in formats:
        if not format_url_is_direct_http(item):
            continue
        if not ytdlp_format_has_audio(item) or ytdlp_format_has_video(item):
            continue
        candidates.append(item)
    return max(candidates, key=audio_stream_score) if candidates else None


def summarize_format(item):
    if not isinstance(item, dict):
        return {}
    return {
        "format_id": str(item.get("format_id") or ""),
        "format_note": str(item.get("format_note") or ""),
        "ext": str(item.get("ext") or ""),
        "height": integer_value(item.get("height")),
        "width": integer_value(item.get("width")),
        "fps": integer_value(item.get("fps")),
        "tbr": float_value(item.get("tbr")),
        "abr": float_value(item.get("abr")),
        "vcodec": str(item.get("vcodec") or ""),
        "acodec": str(item.get("acodec") or ""),
        "size_bytes": format_size_bytes(item),
    }


def build_ytdlp_merge_plan(payload, *, max_height=None, max_file_size_bytes=None):
    max_height = int(
        max_height
        if max_height is not None
        else default_max_height()
    )
    max_file_size_bytes = int(
        max_file_size_bytes
        if max_file_size_bytes is not None
        else getattr(settings, "YTDLP_MERGE_MAX_FILE_SIZE_BYTES", 1024 * 1024 * 1024)
        or 0
    )
    formats = tuple(iter_formats(payload))
    audio = best_audio_stream(formats)
    complete = best_complete_format(formats, max_height=max_height)
    unlimited_video = best_video_stream(formats, max_height=max_height)
    video = best_video_stream(
        formats,
        max_height=max_height,
        audio=audio,
        max_file_size_bytes=max_file_size_bytes,
    )
    complete_height = integer_value((complete or {}).get("height"))
    video_height = integer_value((video or {}).get("height"))
    oversized_video_candidate = bool(audio and unlimited_video and not video)
    estimated_video = video or (unlimited_video if oversized_video_candidate else None)
    estimated_size_bytes = format_size_bytes(estimated_video) + format_size_bytes(audio)
    estimated_size_known = bool(estimated_video and audio and estimated_size_bytes)
    estimated_size_within_limit = bool(
        not max_file_size_bytes
        or not estimated_size_known
        or estimated_size_bytes <= max_file_size_bytes
    )
    merge_video = video or (unlimited_video if oversized_video_candidate else None)
    merge_video_height = integer_value((merge_video or {}).get("height"))
    merge_candidate_found = bool(merge_video and audio)
    requires_merge = bool(
        merge_candidate_found and (not complete or merge_video_height > complete_height)
    )
    selected_max_height = video_height or max_height
    if oversized_video_candidate:
        reason = "estimated_size_exceeds_limit"
    elif not video or not audio:
        reason = "no_merge_candidate"
    elif not requires_merge:
        reason = "single_file_is_sufficient"
    else:
        reason = "merge_candidate_available"
    return {
        "available": bool(video and audio and estimated_size_within_limit),
        "requires_merge": requires_merge,
        "reason": reason,
        "implemented": True,
        "title": str((payload or {}).get("title") or ""),
        "format_count": len(formats),
        "requested_max_height": max_height,
        "max_height": selected_max_height,
        "max_file_size_bytes": max_file_size_bytes,
        "estimated_size_bytes": estimated_size_bytes,
        "estimated_size_known": estimated_size_known,
        "estimated_size_within_limit": estimated_size_within_limit,
        "merge_format_selector": MERGE_FORMAT_SELECTOR_TEMPLATE.format(height=max_height),
        "complete_format": summarize_format(complete),
        "video_format": summarize_format(video),
        "audio_format": summarize_format(audio),
    }


def source_url_is_allowed(rule, source_url):
    parsed = urlparse(str(source_url or ""))
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    if is_blocked_hostname(parsed.hostname):
        return False
    return host_matches(parsed.hostname, getattr(rule, "host_suffixes", ()))


def pick_cover_url(rule, payload):
    from .third_party import third_party_media_url_is_allowed

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
        if third_party_media_url_is_allowed(
            getattr(rule, "platform_id", ""),
            url,
            media_type="image",
            is_cover=True,
        ):
            return str(url)
    return ""


def build_asset_fields(rule, source_url, *, media_type="video", index=1, url="", max_height=None):
    source_key = source_key_for_url(source_url)
    max_height = int(max_height or default_max_height())
    asset_id = f"{PROVIDER_ID}:{rule.platform_id}:{source_key}:{media_type}:{index}:h{max_height}"
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


def build_parse_result(rule, platform_name, source_url, payload, *, max_height=None):
    if not source_url_is_allowed(rule, source_url):
        raise XhsDownloadError("yt-dlp merge source URL is not allowed")
    plan = build_ytdlp_merge_plan(payload, max_height=max_height)
    if not plan["available"] or not plan["requires_merge"]:
        raise XhsDownloadError("yt-dlp merge candidate is not available")
    title = clean_filename(plan["title"] or platform_name or "yt_dlp")
    video_height = int((plan.get("video_format") or {}).get("height") or 0)
    video_fps = int((plan.get("video_format") or {}).get("fps") or 0)
    if video_height and video_fps:
        quality_label = f"{video_height}p{video_fps} 高清合并"
    elif video_height:
        quality_label = f"{video_height}p 高清合并"
    else:
        quality_label = "高清合并"
    cover_url = pick_cover_url(rule, payload)
    asset = MediaAsset(
        media_type="video",
        url=source_url,
        platform="third_party",
        cover_url=cover_url,
        cover_preview_url=cover_url,
        quality_label=quality_label,
        size_bytes=int(plan.get("estimated_size_bytes") or 0),
        **build_asset_fields(
            rule,
            source_url,
            max_height=int(plan.get("max_height") or default_max_height()),
        ),
    )
    return ParseResult(
        platform="third_party",
        post_type="video",
        base_filename=title or "yt_dlp",
        assets=(asset,),
        metadata={
            "source_platform": rule.platform_id,
            "source_platform_name": platform_name,
            "source_provider": PROVIDER_ID,
            "provider_label": PROVIDER_LABEL,
            "selected_provider": PROVIDER_ID,
            "selected_provider_base": YTDLP_PROVIDER_LABEL,
            "candidate_count": int(plan.get("format_count") or 0),
            "canonical_source_url": source_url,
            "yt_dlp_merge_plan": {
                key: value
                for key, value in plan.items()
                if key not in {"video_format", "audio_format", "complete_format"}
            },
        },
    )


def parse_asset_id(asset_id):
    parts = str(asset_id or "").split(":")
    if len(parts) < 5 or parts[0] != PROVIDER_ID:
        return None
    extra_parts = list(parts[5:])
    is_cover = False
    if extra_parts and extra_parts[-1] == "cover":
        is_cover = True
        extra_parts = extra_parts[:-1]
    max_height = default_max_height()
    if extra_parts:
        if len(extra_parts) != 1 or not extra_parts[0].startswith("h"):
            return None
        value = extra_parts[0][1:]
        if not value.isdigit():
            return None
        max_height = int(value)
    if extra_parts and max_height <= 0:
        return None
    platform_id, source_key, media_type, index = parts[1:5]
    if not source_key or len(source_key) > 64:
        return None
    if media_type != "video" or not index.isdigit():
        return None
    return {
        "platform_id": platform_id,
        "source_key": source_key,
        "media_type": media_type,
        "index": index,
        "max_height": max_height,
        "is_cover": is_cover,
    }


def manifest_item_is_allowed(item):
    from .third_party import get_third_party_rule, third_party_media_url_is_allowed

    if not isinstance(item, dict):
        return False
    parsed_asset = parse_asset_id(item.get("asset_id"))
    if not parsed_asset:
        return False
    if item.get("provider") != PROVIDER_ID:
        return False
    if item.get("provider_resource_id") != parsed_asset["source_key"]:
        return False
    if item.get("provider_cache_key") != (
        f"{PROVIDER_ID}:{parsed_asset['source_key']}:h{parsed_asset['max_height']}"
    ):
        return False
    item_type = str(item.get("type") or "").lower()
    if parsed_asset["is_cover"]:
        if item_type and item_type != "image":
            return False
        return third_party_media_url_is_allowed(
            parsed_asset["platform_id"],
            item.get("url"),
            media_type="image",
            is_cover=True,
        )
    if item_type and item_type != "video":
        return False
    rule = get_third_party_rule(parsed_asset["platform_id"])
    return bool(rule and source_url_is_allowed(rule, item.get("url")))


def provider_manifest_allows_download(item):
    return (
        isinstance(item, dict)
        and item.get("provider") == PROVIDER_ID
        and provider_manifest_is_valid(item)
        and manifest_item_is_allowed(item)
    )


def build_command(source_url, output_template, *, max_height=None):
    command = split_external_command(getattr(settings, "YTDLP_BINARY", "") or "", "yt-dlp")
    max_height = int(max_height or default_max_height())
    args = [
        *command,
        "--ignore-config",
        "--no-warnings",
        "--no-playlist",
        "--no-part",
        "--force-overwrites",
        "-f",
        MERGE_FORMAT_SELECTOR_TEMPLATE.format(height=max_height),
        "--merge-output-format",
        "mp4",
        "-o",
        str(output_template),
    ]
    ffmpeg_binary = (
        str(getattr(settings, "YTDLP_MERGE_FFMPEG_BINARY", "") or "").strip()
        or str(getattr(settings, "YOUTUBE_MERGE_FFMPEG_BINARY", "") or "").strip()
    )
    if ffmpeg_binary:
        args.extend(["--ffmpeg-location", split_external_command(ffmpeg_binary, "ffmpeg")[0]])
    args.append(source_url)
    return args


def download_to_temp_path(item, progress_callback=None):
    if not provider_manifest_allows_download(item):
        raise XhsDownloadError("yt-dlp 合并资源签名未通过校验")
    parsed_asset = parse_asset_id(item.get("asset_id"))
    if not parsed_asset:
        raise XhsDownloadError("yt-dlp 合并资源 ID 无效")
    source_url = str(item.get("url") or "")
    timeout_seconds = float(getattr(settings, "YTDLP_MERGE_TIMEOUT_SECONDS", 300.0) or 300.0)
    max_bytes = int(
        getattr(settings, "YTDLP_MERGE_MAX_FILE_SIZE_BYTES", 1024 * 1024 * 1024)
        or 0
    )
    work_dir = tempfile.mkdtemp(prefix="ytdlp_merge_")
    output_template = Path(work_dir) / "merged.%(ext)s"
    try:
        estimated_size = int(item.get("size_bytes") or 0)
        if progress_callback and estimated_size:
            progress_callback(max(1, int(estimated_size * 0.02)), estimated_size)
        completed = run_external_command(
            build_command(
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
            raise XhsDownloadError(f"yt-dlp 合并失败：{message[-500:]}")
        candidates = sorted(Path(work_dir).glob("merged.*"))
        output_path = next(
            (path for path in candidates if path.suffix.lower() == ".mp4"),
            candidates[0] if candidates else None,
        )
        if not output_path or not output_path.exists():
            raise XhsDownloadError("yt-dlp 合并未生成文件")
        output_size = output_path.stat().st_size
        if max_bytes and output_size > max_bytes:
            raise XhsDownloadError("yt-dlp 合并文件超过允许大小限制")
        if progress_callback and estimated_size:
            progress_callback(max(output_size, int(estimated_size * 0.98)), estimated_size)
        target = tempfile.NamedTemporaryFile(
            prefix="ytdlp_merge_result_",
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
