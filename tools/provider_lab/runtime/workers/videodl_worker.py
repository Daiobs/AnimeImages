#!/usr/bin/env python3
from __future__ import annotations

import argparse
from importlib import metadata
import json
from pathlib import Path

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


VIDEODL_PIN = "6cd1de0fe1209380b183b654c65e98a629637873"
PROVIDER_ID = "videodl_repo"
PROVIDER_VERSION = f"videodl@{VIDEODL_PIN}"


def installed_commit() -> str:
    try:
        direct_url = metadata.distribution("videofetch").read_text("direct_url.json")
        payload = json.loads(direct_url or "{}")
    except (metadata.PackageNotFoundError, TypeError, ValueError, json.JSONDecodeError):
        return ""
    return str((payload.get("vcs_info") or {}).get("commit_id") or "").lower()


def verify_install() -> None:
    try:
        installed_version = metadata.version("videofetch")
        import videodl
        from videodl.videodl import VideoClient  # noqa: F401
    except Exception as error:
        raise WorkerError("PROVIDER_NOT_CONFIGURED") from error
    if (
        installed_version != "0.9.1"
        or getattr(videodl, "__license__", "") != "PolyForm-Noncommercial-1.0.0"
        or installed_commit() != VIDEODL_PIN
    ):
        raise WorkerError("PROVIDER_NOT_CONFIGURED")


def build_client(work_dir: str):
    from videodl.modules import CommonVideoClientBuilder, VideoClientBuilder
    from videodl.videodl import VideoClient

    names = list(VideoClientBuilder.REGISTERED_MODULES) + list(CommonVideoClientBuilder.REGISTERED_MODULES)
    names = list(dict.fromkeys(names))
    safe_config = {
        name: {
            "auto_set_proxies": False,
            "random_update_ua": False,
            "max_retries": 1,
            "maintain_session": False,
            "disable_print": True,
            "work_dir": work_dir,
            "freeproxy_settings": None,
            "default_search_cookies": None,
            "default_download_cookies": None,
            "default_parse_cookies": None,
        }
        for name in [*names, "WebMediaGrabber"]
    }
    return VideoClient(
        allowed_video_sources=names,
        init_video_clients_cfg=safe_config,
        clients_threadings={},
        requests_overrides={},
        apply_common_video_clients_only=False,
    )


def normalize_result(video_infos: object, request: dict) -> tuple[dict, list[dict]]:
    infos = list(video_infos or ()) if isinstance(video_infos, (list, tuple)) else []
    public_assets = []
    private_resources = []
    warnings = []
    backends = []
    raw_items = []

    def add_resource(info, url, media_type, role, suffix):
        if not url or len(public_assets) >= request["max_assets"]:
            return
        headers = getattr(info, f"default_{suffix}_headers", None)
        cookies = getattr(info, f"default_{suffix}_cookies", None)
        has_audio = bool(getattr(info, "audio_download_url", ""))
        public, private = asset(
            asset_id=f"videodl-{len(public_assets) + 1}-{role}",
            url=url,
            media_type=media_type,
            role=role,
            declared_extension=(
                getattr(info, "audio_ext", "") if role == "audio" else getattr(info, "ext", "")
            ),
            requires_merge=bool(
                getattr(info, "download_with_ffmpeg", False) or has_audio
            ),
            requires_cookie=bool(cookies),
            headers_present=bool(headers),
            audio_asset_present=has_audio,
        )
        public_assets.append(public)
        private_resources.append(private)

    for info in infos[: request["max_assets"]]:
        backend = safe_text(getattr(info, "source", ""), 120)
        if backend and backend not in backends:
            backends.append(backend)
        cover_url = getattr(info, "cover_url", "")
        if cover_url:
            add_resource(info, cover_url, "image", "cover", "download")
        add_resource(info, getattr(info, "download_url", ""), "video", "primary", "download")
        add_resource(info, getattr(info, "audio_download_url", ""), "audio", "audio", "audio_download")
        if getattr(info, "err_msg", ""):
            warnings.append("provider_item_error_present")
        raw_items.append(
            {
                "result_class": type(info).__name__,
                "source": backend,
                "title": safe_text(getattr(info, "title", ""), 300),
                "raw_data": getattr(info, "raw_data", {}),
                "download_url": getattr(info, "download_url", ""),
                "default_download_headers": getattr(info, "default_download_headers", None),
                "default_download_cookies": getattr(info, "default_download_cookies", None),
                "audio_download_url": getattr(info, "audio_download_url", ""),
                "default_audio_download_headers": getattr(info, "default_audio_download_headers", None),
                "default_audio_download_cookies": getattr(info, "default_audio_download_cookies", None),
                "ext": safe_text(getattr(info, "ext", ""), 16),
                "identifier": {"type": type(getattr(info, "identifier", None)).__name__},
                "download_with_ffmpeg": bool(getattr(info, "download_with_ffmpeg", False)),
                "audio_asset_present": bool(getattr(info, "audio_download_url", "")),
            }
        )
    if not public_assets:
        raise WorkerError("PROVIDER_RESULT_EMPTY")
    first = infos[0]
    result = {
        "schema_version": "1.0",
        "request_id": request["request_id"],
        "provider_id": PROVIDER_ID,
        "provider_version": PROVIDER_VERSION,
        "provider_scope": "lab_only_noncommercial",
        "status": "success",
        "elapsed_ms": 0,
        "claimed_platform": "",
        "selected_backend": ", ".join(backends[:4]),
        "result_kind": "video",
        "metadata": {
            "title": safe_text(getattr(first, "title", ""), 300),
            "author": "",
            "content_summary": "",
        },
        "assets": public_assets,
        "relationships": [],
        "warnings": list(dict.fromkeys(warnings)),
        "error_code": "",
        "raw_shape": raw_shape(raw_items),
        "license_name": "PolyForm Noncommercial 1.0.0",
        "license_scope": "noncommercial_lab_only",
    }
    return result, private_resources


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
        client = build_client(str(Path.cwd() / "provider-work"))
        result, resources = normalize_result(client.parsefromurl(request["source"]), request)
        emit(result, resources, max_output_bytes=request["max_output_bytes"])
        return 0
    except WorkerError as error:
        emit_error(error.code)
        return 2
    except Exception:
        emit_error("PROVIDER_PROCESS_FAILED")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
