import json
import logging
import subprocess
from concurrent.futures import TimeoutError
from urllib.parse import urlparse

from django.conf import settings
from ..pipeline_shadow_capture import capture_enabled, capture_raw

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
PROVIDER_ID = "gallery_dl_generic"
PROVIDER_LABEL = "gallery-dl"
IMAGE_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".gif",
    ".bmp",
    ".tif",
    ".tiff",
    ".avif",
}
VIDEO_EXTENSIONS = {".mp4", ".m4v", ".mov", ".webm"}
IMAGE_KEYWORDS = (
    "image",
    "img",
    "photo",
    "picture",
    "pic",
    "thumbnail",
    "original",
    "file",
    "download",
    "src",
    "url",
)
LOW_QUALITY_FIELD_KEYWORDS = (
    "avatar",
    "icon",
    "preview",
    "small",
    "thumb",
    "thumbnail",
)
GALLERYDL_GROUP_METADATA_KEY = "__gallerydl_group_key"


def run_gallerydl_json(url, *, binary="", timeout_seconds=15.0, max_items=30):
    try:
        command = split_external_command(binary, "gallery-dl")
    except ValueError as error:
        raise RuntimeError(f"gallery-dl command is invalid: {error}") from error
    args = [
        *command,
        "--config-ignore",
        "--no-input",
        "--no-download",
        "--range",
        f"1-{max(1, int(max_items or 30))}",
        "-J",
        url,
    ]
    try:
        completed = run_external_command(
            args,
            timeout_seconds=max(0.1, float(timeout_seconds or 15.0)),
        )
    except subprocess.TimeoutExpired as error:
        raise XhsProviderTimeoutError("gallery-dl 解析超时，请稍后再试") from error
    except FileNotFoundError as error:
        raise RuntimeError("gallery-dl executable was not found") from error

    if completed.returncode != 0:
        message = (completed.stderr or completed.stdout or "gallery-dl failed").strip()
        raise RuntimeError(message[-500:])

    stdout = (completed.stdout or "").strip()
    if not stdout:
        raise RuntimeError("gallery-dl returned empty output")
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        payload = []
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                payload.append(json.loads(line))
            except json.JSONDecodeError as error:
                raise RuntimeError("gallery-dl returned invalid JSON") from error
    if capture_enabled():
        capture_raw(PROVIDER_ID, payload, url)
    return payload


def integer_value(value):
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def extension_from_url(url):
    path = (urlparse(str(url or "")).path or "").lower()
    for extension in (*IMAGE_EXTENSIONS, *VIDEO_EXTENSIONS):
        if path.endswith(extension):
            return extension.lstrip(".")
    return ""


def media_type_from_metadata(metadata, url=""):
    url_extension = extension_from_url(url)
    if url_extension and f".{url_extension}" in IMAGE_EXTENSIONS:
        return "image"
    if url_extension and f".{url_extension}" in VIDEO_EXTENSIONS:
        return "video"

    if not isinstance(metadata, dict):
        return ""
    extension = str(metadata.get("extension") or metadata.get("ext") or "").lower().lstrip(".")
    if extension and f".{extension}" in IMAGE_EXTENSIONS:
        return "image"
    if extension and f".{extension}" in VIDEO_EXTENSIONS:
        return "video"
    mime_type = str(
        metadata.get("mime")
        or metadata.get("mime_type")
        or metadata.get("content_type")
        or ""
    ).lower()
    if mime_type.startswith("image/"):
        return "image"
    if mime_type.startswith("video/"):
        return "video"
    return ""


def metadata_title(metadata, fallback=""):
    if not isinstance(metadata, dict):
        return fallback
    for key in ("title", "description", "caption", "filename", "id"):
        value = metadata.get(key)
        if value:
            return str(value)
    return fallback


def field_name_is_low_quality(field_name):
    field_name = str(field_name or "").lower()
    return any(keyword in field_name for keyword in LOW_QUALITY_FIELD_KEYWORDS)


def merge_gallerydl_metadata(metadata, parent_metadata=None, group_key=""):
    merged = {}
    if isinstance(parent_metadata, dict):
        merged.update(parent_metadata)
    if isinstance(metadata, dict):
        merged.update(metadata)
    if group_key:
        merged[GALLERYDL_GROUP_METADATA_KEY] = group_key
    return merged


def iter_gallerydl_candidates(value, field_name="", parent_metadata=None, path="root", group_key="root"):
    if isinstance(value, dict):
        direct_url = value.get("url") or value.get("file_url") or value.get("download_url") or value.get("src")
        own_metadata = merge_gallerydl_metadata(value, None, group_key)
        inherited_metadata = merge_gallerydl_metadata(value, parent_metadata, group_key)
        if isinstance(direct_url, str):
            media_type = media_type_from_metadata(own_metadata, direct_url)
            if media_type:
                yield direct_url, inherited_metadata, field_name or "url", media_type
        for key, child in value.items():
            key_lower = str(key).lower()
            if any(keyword in key_lower for keyword in IMAGE_KEYWORDS):
                yield from iter_gallerydl_candidates(
                    child,
                    key_lower,
                    inherited_metadata,
                    f"{path}.{key_lower}",
                    group_key,
                )
            elif isinstance(child, (dict, list, tuple)):
                yield from iter_gallerydl_candidates(
                    child,
                    key_lower,
                    inherited_metadata,
                    f"{path}.{key_lower}",
                    group_key,
                )
        return

    if isinstance(value, (list, tuple)):
        if len(value) >= 3 and isinstance(value[1], str) and isinstance(value[2], dict):
            url = value[1]
            metadata = merge_gallerydl_metadata(value[2], parent_metadata, group_key)
            media_type = media_type_from_metadata(metadata, url)
            if media_type:
                yield url, metadata, field_name or "message", media_type
        for index, item in enumerate(value):
            item_group_key = f"{path}[{index}]"
            yield from iter_gallerydl_candidates(
                item,
                field_name,
                parent_metadata,
                item_group_key,
                item_group_key,
            )


def gallerydl_candidate_group_key(candidate):
    metadata = candidate.get("metadata")
    if not isinstance(metadata, dict):
        return ""
    return str(metadata.get(GALLERYDL_GROUP_METADATA_KEY) or "")


class GalleryDlGenericProvider:
    provider_id = PROVIDER_ID
    label = PROVIDER_LABEL

    def __init__(self, runner=None):
        self.runner = runner or run_gallerydl_json

    def run(self, url, timeout_seconds=None):
        timeout_seconds = effective_external_timeout_seconds(
            timeout_seconds
            if timeout_seconds is not None
            else getattr(settings, "GALLERYDL_TIMEOUT_SECONDS", 15.0)
        )
        return self.runner(
            url,
            binary=getattr(settings, "GALLERYDL_BINARY", "") or "",
            timeout_seconds=timeout_seconds,
            max_items=int(getattr(settings, "GALLERYDL_MAX_ITEMS", 30) or 30),
        )

    def parse(self, url, timeout_seconds=None):
        source_key = source_key_for_url(url)
        cached = get_cached_external_payload(PROVIDER_ID, source_key)
        if cached is not None:
            if capture_enabled():
                capture_raw(PROVIDER_ID, cached, url)
            return cached
        failure = get_cached_external_failure(PROVIDER_ID, source_key)
        if failure:
            raise XhsProviderTimeoutError("gallery-dl 最近解析失败，请稍后再试")

        timeout_seconds = effective_external_timeout_seconds(
            timeout_seconds
            if timeout_seconds is not None
            else getattr(settings, "GALLERYDL_TIMEOUT_SECONDS", 15.0)
        )
        future = submit_provider_task(
            "third-party-gallerydl",
            getattr(settings, "GALLERYDL_EXECUTOR_WORKERS", 1),
            getattr(settings, "GALLERYDL_MAX_PENDING_TASKS", 2),
            self.run,
            url,
            timeout_seconds,
        )
        if future is None:
            raise XhsProviderTimeoutError("gallery-dl 解析队列已满，请稍后再试")
        try:
            payload = future.result(timeout=timeout_seconds)
            cache_external_payload(
                PROVIDER_ID,
                source_key,
                payload,
                getattr(settings, "GALLERYDL_PAYLOAD_CACHE_TTL_SECONDS", 300),
            )
            clear_cached_external_failure(PROVIDER_ID, source_key)
            return payload
        except TimeoutError as error:
            cache_external_failure(
                PROVIDER_ID,
                source_key,
                type(error).__name__,
                getattr(settings, "GALLERYDL_FAILURE_CACHE_TTL_SECONDS", 30),
            )
            raise XhsProviderTimeoutError("gallery-dl 解析超时，请稍后再试") from error
        except Exception as error:
            cache_external_failure(
                PROVIDER_ID,
                source_key,
                type(error).__name__,
                getattr(settings, "GALLERYDL_FAILURE_CACHE_TTL_SECONDS", 30),
            )
            raise

    def health(self, version_cache_seconds=60, force_version_probe=False):
        binary = str(getattr(settings, "GALLERYDL_BINARY", "") or "").strip()
        command_health = external_command_health(binary, "gallery-dl")
        enabled = bool(getattr(settings, "GALLERYDL_PROVIDER_ENABLED", False))
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
            "timeout_seconds": float(getattr(settings, "GALLERYDL_TIMEOUT_SECONDS", 15.0) or 15.0),
            "output_max_bytes": external_command_output_max_bytes(),
            "executor_workers": int(getattr(settings, "GALLERYDL_EXECUTOR_WORKERS", 1) or 1),
            "max_pending_tasks": int(getattr(settings, "GALLERYDL_MAX_PENDING_TASKS", 2) or 2),
            "max_items": int(getattr(settings, "GALLERYDL_MAX_ITEMS", 30) or 30),
            "payload_cache_ttl_seconds": int(getattr(settings, "GALLERYDL_PAYLOAD_CACHE_TTL_SECONDS", 300) or 0),
            "failure_cache_ttl_seconds": int(getattr(settings, "GALLERYDL_FAILURE_CACHE_TTL_SECONDS", 30) or 0),
        }


class GalleryDlResultNormalizer:
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

    def normalize(self, *, rule, platform_name, source_url, payload):
        allowed_suffixes = getattr(rule, "media_host_suffixes", ())
        platform_id = getattr(rule, "platform_id", "")
        max_items = max(1, int(getattr(settings, "GALLERYDL_MAX_ITEMS", 30) or 30))
        allowed_media_types = set(getattr(rule, "media_types", ()) or ("image",))
        seen = set()
        assets = []
        title = platform_name
        candidates = []

        for raw_url, metadata, field_name, media_type in iter_gallerydl_candidates(payload):
            if media_type not in allowed_media_types:
                continue
            media_url = public_url_or_empty(
                raw_url,
                allowed_suffixes,
                platform_id,
                field_name,
            )
            if not media_url or media_url in seen:
                continue
            candidates.append({
                "url": media_url,
                "metadata": metadata,
                "field_name": field_name,
                "media_type": media_type,
                "low_quality_field": field_name_is_low_quality(field_name),
            })

        high_quality_group_keys = {
            gallerydl_candidate_group_key(candidate)
            for candidate in candidates
            if not candidate["low_quality_field"]
        }
        high_quality_group_keys.discard("")
        if high_quality_group_keys:
            candidates = [
                candidate
                for candidate in candidates
                if not (
                    candidate["low_quality_field"]
                    and gallerydl_candidate_group_key(candidate) in high_quality_group_keys
                )
            ]

        for candidate in candidates:
            media_url = candidate["url"]
            metadata = candidate["metadata"]
            media_type = candidate["media_type"]
            if media_url in seen:
                continue
            seen.add(media_url)
            title = metadata_title(metadata, title)
            index = len(assets) + 1
            assets.append(
                MediaAsset(
                    media_type=media_type,
                    url=media_url,
                    platform=self.platform_id,
                    preview_url=media_url if media_type == "image" else "",
                    filename=clean_filename(str(metadata.get("filename") or "")) if isinstance(metadata, dict) else "",
                    ext=str(metadata.get("extension") or extension_from_url(media_url)) if isinstance(metadata, dict) else extension_from_url(media_url),
                    size_bytes=integer_value(metadata.get("filesize") or metadata.get("size")) if isinstance(metadata, dict) else 0,
                    **self.build_provider_asset_fields(
                        rule,
                        source_url,
                        media_type,
                        index,
                        media_url,
                    ),
                )
            )
            if len(assets) >= max_items:
                break

        if not assets:
            raise ValueError("gallery-dl result does not contain direct media assets")
        post_type = "video" if any(asset.media_type == "video" for asset in assets) else "image"

        return ParseResult(
            platform=self.platform_id,
            post_type=post_type,
            base_filename=clean_filename(title) or "third_party",
            assets=tuple(assets),
            metadata={
                "source_platform": rule.platform_id,
                "source_platform_name": platform_name,
                "source_provider": PROVIDER_ID,
                "provider_label": PROVIDER_LABEL,
                "candidate_count": len(assets),
                "canonical_source_url": source_url,
            },
        )
