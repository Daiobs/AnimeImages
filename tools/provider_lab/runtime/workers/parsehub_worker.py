#!/usr/bin/env python3
from __future__ import annotations

import argparse
from importlib import metadata
import json

try:
    from .worker_common import (
        WorkerError,
        asset,
        emit,
        emit_check,
        emit_error,
        parse_request,
        raw_shape,
        safe_text,
    )
except ImportError:  # Direct execution inside the isolated venv.
    from worker_common import (
        WorkerError,
        asset,
        emit,
        emit_check,
        emit_error,
        parse_request,
        raw_shape,
        safe_text,
    )


PARSEHUB_PIN = "144260f4349c5bb57415f7555a572fcd48c3b8ba"
PROVIDER_ID = "parsehub_repo"
PROVIDER_VERSION = f"parsehub@{PARSEHUB_PIN}"


def installed_commit() -> str:
    try:
        direct_url = metadata.distribution("parsehub").read_text("direct_url.json")
        payload = json.loads(direct_url or "{}")
    except (metadata.PackageNotFoundError, TypeError, ValueError, json.JSONDecodeError):
        return ""
    return str((payload.get("vcs_info") or {}).get("commit_id") or "").lower()


def verify_install() -> None:
    try:
        installed_version = metadata.version("parsehub")
        from parsehub import ParseHub  # noqa: F401
    except Exception as error:
        raise WorkerError("PROVIDER_NOT_CONFIGURED") from error
    if installed_version != "2.2.0" or installed_commit() != PARSEHUB_PIN:
        raise WorkerError("PROVIDER_NOT_CONFIGURED")


def result_kind(result: object, media: list[object]) -> str:
    class_name = type(result).__name__
    if class_name == "VideoParseResult":
        return "video"
    if class_name == "ImageParseResult":
        return "livephoto" if any(type(item).__name__ == "LivePhotoRef" for item in media) else (
            "gallery" if len(media) > 1 else "image"
        )
    if class_name == "MultimediaParseResult":
        return "multimedia"
    if class_name == "RichTextParseResult":
        return "article"
    return "unknown"


def normalize_result(result: object, request: dict) -> tuple[dict, list[dict]]:
    raw_media = getattr(result, "media", None)
    if raw_media is None:
        media = []
    elif isinstance(raw_media, (list, tuple)):
        media = list(raw_media)
    else:
        media = [raw_media]
    public_assets = []
    private_resources = []
    relationships = []

    def add_media(url, media_type, role, index, **kwargs):
        if len(public_assets) >= request["max_assets"]:
            return ""
        asset_id = f"parsehub-{index}-{role}"
        public, private = asset(
            asset_id=asset_id,
            url=url,
            media_type=media_type,
            role=role,
            **kwargs,
        )
        public_assets.append(public)
        private_resources.append(private)
        return asset_id

    for index, item in enumerate(media, start=1):
        class_name = type(item).__name__
        if class_name == "LivePhotoRef":
            still_id = add_media(
                getattr(item, "url", ""),
                "image",
                "still",
                index,
                declared_extension=getattr(item, "ext", "jpg"),
                width=getattr(item, "width", 0),
                height=getattr(item, "height", 0),
                duration_seconds=getattr(item, "duration", 0),
            )
            motion_url = getattr(item, "video_url", "")
            if motion_url and still_id:
                motion_id = add_media(
                    motion_url,
                    "video",
                    "motion",
                    index,
                    declared_extension=getattr(item, "video_ext", "mp4"),
                    width=getattr(item, "width", 0),
                    height=getattr(item, "height", 0),
                    duration_seconds=getattr(item, "duration", 0),
                )
                if motion_id:
                    relationships.append(
                        {
                            "relationship_type": "livephoto_motion",
                            "source_asset_id": still_id,
                            "target_asset_id": motion_id,
                        }
                    )
            continue
        media_type = {
            "ImageRef": "image",
            "AniRef": "image",
            "VideoRef": "video",
        }.get(class_name, "unknown")
        role = "primary" if len(media) == 1 else "gallery"
        add_media(
            getattr(item, "url", ""),
            media_type,
            role,
            index,
            declared_extension=getattr(item, "ext", ""),
            width=getattr(item, "width", 0),
            height=getattr(item, "height", 0),
            duration_seconds=getattr(item, "duration", 0),
        )

    if not public_assets:
        raise WorkerError("PROVIDER_RESULT_EMPTY")
    platform = getattr(result, "platform", None)
    platform_id = safe_text(getattr(platform, "id", ""), 80)
    result_payload = {
        "schema_version": "1.0",
        "request_id": request["request_id"],
        "provider_id": PROVIDER_ID,
        "provider_version": PROVIDER_VERSION,
        "provider_scope": "lab_only",
        "status": "success",
        "elapsed_ms": 0,
        "claimed_platform": platform_id,
        "selected_backend": platform_id or type(result).__name__,
        "result_kind": result_kind(result, media),
        "metadata": {
            "title": safe_text(getattr(result, "title", ""), 300),
            "author": "",
            "content_summary": safe_text(getattr(result, "content", ""), 512),
        },
        "assets": public_assets,
        "relationships": relationships,
        "warnings": [],
        "error_code": "",
        "raw_shape": raw_shape(result),
        "license_name": "MIT",
        "license_scope": "lab_only_production_candidate",
    }
    return result_payload, private_resources


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    try:
        verify_install()
        if args.check_only:
            emit_check(PROVIDER_ID, PROVIDER_VERSION)
            return 0
        request = parse_request()
        from parsehub import ParseHub

        result = ParseHub().parse_sync(request["source"], cookie=None, proxy=None)
        normalized, resources = normalize_result(result, request)
        emit(normalized, resources, max_output_bytes=request["max_output_bytes"])
        return 0
    except WorkerError as error:
        emit_error(error.code)
        return 2
    except Exception as error:
        code = "PROVIDER_UNSUPPORTED" if type(error).__name__ == "UnknownPlatform" else "PROVIDER_PROCESS_FAILED"
        emit_error(code)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
