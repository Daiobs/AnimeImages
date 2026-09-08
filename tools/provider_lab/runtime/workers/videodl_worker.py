#!/usr/bin/env python3
from __future__ import annotations

import argparse
from importlib import metadata
import json
from pathlib import Path
import re

try:
    from .worker_common import (
        WorkerError,
        asset,
        emit_check,
        emit_error,
        emit_message,
        parse_request,
        raw_shape,
        safe_text,
        validated_url,
    )
except ImportError:  # Direct execution inside the isolated venv.
    from worker_common import (
        WorkerError,
        asset,
        emit_check,
        emit_error,
        emit_message,
        parse_request,
        raw_shape,
        safe_text,
        validated_url,
    )


VIDEODL_PIN = "6cd1de0fe1209380b183b654c65e98a629637873"
PROVIDER_ID = "videodl_repo"
PROVIDER_VERSION = f"videodl@{VIDEODL_PIN}"
OPTIONAL_REQUEST_FIELDS = frozenset(
    {"operation", "backend_id", "backend_category"}
)
BACKEND_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,79}$")
BACKEND_CATEGORIES = frozenset({"dedicated", "common"})
OPERATIONS = frozenset({"inventory", "match_dedicated", "backend", "legacy_auto"})


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


def backend_inventory() -> list[dict]:
    from videodl.modules import CommonVideoClientBuilder, VideoClientBuilder

    inventory = []
    for category, registry in (
        ("dedicated", VideoClientBuilder.REGISTERED_MODULES),
        ("common", CommonVideoClientBuilder.REGISTERED_MODULES),
    ):
        for backend_id, backend_class in registry.items():
            if not BACKEND_ID_RE.fullmatch(str(backend_id or "")):
                continue
            inventory.append(
                {
                    "backend_id": backend_id,
                    "backend_category": category,
                    "registry_available": callable(backend_class),
                    "display_label": safe_text(backend_id, 80),
                }
            )
    return inventory


def validate_operation_request(request: dict) -> str:
    allowed_fields = {
        "request_id",
        "source",
        "max_assets",
        "max_output_bytes",
    } | OPTIONAL_REQUEST_FIELDS
    if not set(request).issubset(allowed_fields):
        raise WorkerError("PROVIDER_INVALID_JSON")
    present_optional = OPTIONAL_REQUEST_FIELDS.intersection(request)
    operation = request.get("operation")
    if operation is None:
        if present_optional:
            raise WorkerError("PROVIDER_INVALID_JSON")
        return "legacy_auto"
    if operation not in OPERATIONS:
        raise WorkerError("PROVIDER_INVALID_JSON")
    if operation in {"inventory", "match_dedicated", "legacy_auto"}:
        if present_optional != {"operation"}:
            raise WorkerError("PROVIDER_INVALID_JSON")
        if operation == "inventory" and request["source"]:
            raise WorkerError("PROVIDER_INVALID_JSON")
        if operation != "inventory" and not request["source"]:
            raise WorkerError("PROVIDER_INVALID_JSON")
        return operation
    if present_optional != OPTIONAL_REQUEST_FIELDS or not request["source"]:
        raise WorkerError("PROVIDER_INVALID_JSON")
    if (
        request.get("backend_category") not in BACKEND_CATEGORIES
        or not BACKEND_ID_RE.fullmatch(str(request.get("backend_id") or ""))
    ):
        raise WorkerError("PROVIDER_INVALID_JSON")
    return operation


def matching_dedicated_backends(source: str) -> list[str]:
    from videodl.modules import VideoClientBuilder

    matches = []
    for backend_id, backend_class in VideoClientBuilder.REGISTERED_MODULES.items():
        try:
            matched = backend_class.belongto(source)
        except Exception:
            matched = False
        if matched:
            matches.append(backend_id)
    return matches[:16]


def backend_config(backend_id: str, work_dir: str) -> dict:
    return {
        "type": backend_id,
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


def run_exact_backend(source: str, backend_id: str, backend_category: str, work_dir: str):
    from videodl.modules import (
        BuildCommonVideoClient,
        BuildVideoClient,
        CommonVideoClientBuilder,
        VideoClientBuilder,
    )

    registry = (
        VideoClientBuilder.REGISTERED_MODULES
        if backend_category == "dedicated"
        else CommonVideoClientBuilder.REGISTERED_MODULES
    )
    if backend_id not in registry:
        raise WorkerError("PROVIDER_SCHEMA_INVALID")
    if backend_category == "dedicated":
        try:
            matched = bool(registry[backend_id].belongto(source))
        except Exception as error:
            raise WorkerError("PROVIDER_PROCESS_FAILED") from error
        if not matched:
            raise WorkerError("PROVIDER_UNSUPPORTED")
    builder = BuildVideoClient if backend_category == "dedicated" else BuildCommonVideoClient
    try:
        client = builder(backend_config(backend_id, work_dir))
        infos = client.parsefromurl(source, request_overrides={})
    except Exception as error:
        raise WorkerError("PROVIDER_PROCESS_FAILED") from error
    valid_infos = [
        info
        for info in list(infos or ())
        if bool(getattr(info, "with_valid_download_url", False))
        or bool(getattr(info, "with_valid_audio_download_url", False))
    ]
    if not valid_infos:
        raise WorkerError("PROVIDER_RESULT_EMPTY")
    return valid_infos


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
        requires_merge = None
        audio_asset_present = None
        if role == "primary" and media_type == "video":
            paired = False
            try:
                video_url, _ = validated_url(getattr(info, "download_url", ""))
                audio_url, _ = validated_url(getattr(info, "audio_download_url", ""))
                paired = video_url != audio_url
            except WorkerError:
                pass
            explicit_merge = getattr(info, "requires_merge", None)
            explicit_audio = getattr(info, "audio_asset_present", None)
            requires_merge = explicit_merge if type(explicit_merge) is bool else True if paired else None
            audio_asset_present = explicit_audio if type(explicit_audio) is bool else True if paired else None
        public, private = asset(
            asset_id=f"videodl-{len(public_assets) + 1}-{role}",
            url=url,
            media_type=media_type,
            role=role,
            declared_extension=(
                getattr(info, "audio_ext", "") if role == "audio" else getattr(info, "ext", "")
            ),
            requires_merge=requires_merge,
            requires_cookie=bool(cookies),
            headers_present=bool(headers),
            audio_asset_present=audio_asset_present,
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
        request = parse_request(
            optional_fields=OPTIONAL_REQUEST_FIELDS,
            allow_empty_source=True,
        )
        operation = validate_operation_request(request)
        if operation == "inventory":
            emit_message(
                {"operation": operation, "inventory": backend_inventory()},
                max_output_bytes=request["max_output_bytes"],
            )
            return 0
        if operation == "match_dedicated":
            emit_message(
                {
                    "operation": operation,
                    "backend_ids": matching_dedicated_backends(request["source"]),
                },
                max_output_bytes=request["max_output_bytes"],
            )
            return 0
        if operation == "backend":
            video_infos = run_exact_backend(
                request["source"],
                request["backend_id"],
                request["backend_category"],
                str(Path.cwd() / "provider-work"),
            )
        else:
            client = build_client(str(Path.cwd() / "provider-work"))
            video_infos = client.parsefromurl(request["source"])
        result, resources = normalize_result(video_infos, request)
        emit_message(
            {
                "result": result,
                "resources": resources,
                "metrics": {"candidate_count": len(video_infos)},
            },
            max_output_bytes=request["max_output_bytes"],
        )
        return 0
    except WorkerError as error:
        emit_error(error.code)
        return 2
    except Exception:
        emit_error("PROVIDER_PROCESS_FAILED")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
