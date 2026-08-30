from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import time
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit
from uuid import uuid4

from django.conf import settings

from xhs_download.services.assets import MediaAsset, ParseResult
from xhs_download.services.common import (
    XhsProviderTimeoutError,
    extract_url_from_text,
)
from xhs_download.services.env_config import runtime_bool, runtime_float, runtime_str
from xhs_download.services.platforms.tiantianjiexi_generic import (
    TianTianJieXiGenericProvider,
    provider_configured as tiantian_provider_configured,
)
from xhs_download.services.platforms.external_tools import external_command_health
from xhs_download.services.platforms.ytdlp_generic import YtDlpGenericProvider
from xhs_download.services.registry import parse_media_page

from .contracts import (
    LabAssetV1,
    LabProviderAttemptV1,
    LabProviderResultV1,
    LabRelationshipV1,
    MAX_ASSETS,
    MAX_RELATIONSHIPS,
    MAX_SOURCE_LENGTH,
    ProviderLabError,
    safe_float,
    safe_int,
    safe_text,
    sanitize_raw_shape,
    validated_resource_url,
)
from .resources import issue_resource_token
from .runtime import execute_provider_worker
from .videodl_control import (
    VideodlControlledError,
    VideodlExecutionOptions,
    execute_controlled_videodl,
    load_videodl_inventory,
    public_inventory,
)


PARSEHUB_PIN = "144260f4349c5bb57415f7555a572fcd48c3b8ba"
VIDEODL_PIN = "6cd1de0fe1209380b183b654c65e98a629637873"
WORKER_ROOT = Path(settings.BASE_DIR) / "tools" / "provider_lab" / "runtime" / "workers"


@dataclass(frozen=True)
class ProviderDescriptor:
    provider_id: str
    label: str
    provider_version: str
    provider_scope: str
    license_name: str
    license_scope: str
    default_selected: bool
    runner: Callable[..., Any]
    availability: Callable[[], bool]
    not_configured_hint: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "label": self.label,
            "provider_version": self.provider_version,
            "provider_scope": self.provider_scope,
            "license_name": self.license_name,
            "license_scope": self.license_scope,
            "default_selected": self.default_selected,
            "available": bool(self.availability()),
            "not_configured_hint": self.not_configured_hint,
        }


def normalized_source(value: Any) -> str:
    source = str(value or "").strip()
    if not source or len(source) > MAX_SOURCE_LENGTH:
        raise ProviderLabError("PROVIDER_SCHEMA_INVALID")
    candidate = extract_url_from_text(source)
    parsed = urlsplit(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ProviderLabError("PROVIDER_UNSUPPORTED")
    return candidate


def request_id_value() -> str:
    return uuid4().hex


def _result_kind(post_type: str, assets: list[LabAssetV1]) -> str:
    post_type = str(post_type or "").lower()
    if post_type in {"video", "image", "gallery", "livephoto", "multimedia", "article"}:
        return post_type
    types = {asset.media_type for asset in assets}
    if types == {"video"}:
        return "video"
    if types == {"image"}:
        return "gallery" if len(assets) > 1 else "image"
    if types:
        return "multimedia"
    return "unknown"


def _issue_asset(
    *,
    user_id: int,
    request_id: str,
    provider_id: str,
    asset_id: str,
    url: str,
    media_type: str,
    role: str,
    headers_present: bool = False,
    requires_cookie: bool = False,
    **kwargs,
) -> LabAssetV1:
    _url, hostname = validated_resource_url(url)
    token = issue_resource_token(
        user_id=user_id,
        request_id=request_id,
        provider_id=provider_id,
        asset_id=asset_id,
        url=url,
        media_type=media_type,
        headers_present=headers_present,
        requires_cookie=requires_cookie,
    )
    return LabAssetV1(
        asset_id=asset_id,
        media_type=media_type,
        role=role,
        resource_token=token,
        host_class=hostname,
        headers_present=headers_present,
        requires_cookie=requires_cookie,
        **kwargs,
    )


def _parse_result_metadata(result: ParseResult) -> dict[str, str]:
    metadata = dict(result.metadata or {})
    return {
        "title": safe_text(metadata.get("title") or result.base_filename, 300),
        "author": safe_text(
            metadata.get("author") or metadata.get("nickname") or metadata.get("uploader"),
            200,
        ),
        "content_summary": safe_text(
            metadata.get("description") or metadata.get("content") or metadata.get("desc"),
            512,
        ),
    }


def _media_asset_to_lab(
    asset: MediaAsset,
    *,
    index: int,
    total: int,
    user_id: int,
    request_id: str,
    provider_id: str,
) -> LabAssetV1:
    header_names = {str(key).strip().lower() for key in (asset.headers or {})}
    return _issue_asset(
        user_id=user_id,
        request_id=request_id,
        provider_id=provider_id,
        asset_id=f"{provider_id}-{index}",
        url=asset.url,
        media_type=asset.media_type if asset.media_type in {"image", "video", "audio"} else "unknown",
        role="primary" if total == 1 else "gallery",
        declared_extension=asset.ext,
        size_bytes=asset.size_bytes,
        quality_label=asset.quality_label,
        headers_present=bool(asset.headers),
        requires_cookie="cookie" in header_names,
    )


def run_animeimages_current(*, source: str, request_id: str, user_id: int):
    result = parse_media_page(
        source,
        prefer_ultra_hd=False,
        prefer_youtube_merge=False,
        timeout_seconds=runtime_float("PROVIDER_LAB_CURRENT_TIMEOUT_SECONDS", 20.0),
    )
    if not isinstance(result, ParseResult):
        raise ProviderLabError("PROVIDER_SCHEMA_INVALID")
    source_assets = list(result.assets or ())[:MAX_ASSETS]
    assets = [
        _media_asset_to_lab(
            item,
            index=index,
            total=len(source_assets),
            user_id=user_id,
            request_id=request_id,
            provider_id="animeimages_current",
        )
        for index, item in enumerate(source_assets, start=1)
    ]
    if not assets:
        raise ProviderLabError("PROVIDER_RESULT_EMPTY")
    metadata = dict(result.metadata or {})
    payload = LabProviderResultV1(
        request_id=request_id,
        provider_id="animeimages_current",
        provider_version="animeimages-current",
        provider_scope="runtime",
        status="success",
        claimed_platform=result.platform,
        selected_backend=safe_text(
            metadata.get("source_provider") or metadata.get("provider_label") or "platform_registry",
            160,
        ),
        result_kind=_result_kind(result.post_type, assets),
        metadata=_parse_result_metadata(result),
        assets=assets,
        raw_shape=sanitize_raw_shape(
            {
                "result_class": type(result).__name__,
                "platform": result.platform,
                "post_type": result.post_type,
                "metadata": metadata,
                "asset_count": len(result.assets or ()),
                "rich_media": result.rich_media,
            }
        ),
        license_name="AnimeImages",
        license_scope="runtime",
    )
    return payload.as_dict(), []


def _ytdlp_assets(payload, *, user_id, request_id):
    candidates = []
    formats = payload.get("formats")
    if isinstance(formats, list):
        candidates.extend(item for item in formats if isinstance(item, dict))
    if payload.get("url"):
        candidates.insert(0, payload)
    assets = []
    seen = set()
    for item in candidates:
        url = str(item.get("url") or "").strip()
        if not url or url in seen:
            continue
        try:
            validated_resource_url(url)
        except ProviderLabError:
            continue
        seen.add(url)
        vcodec = safe_text(item.get("vcodec"), 80)
        acodec = safe_text(item.get("acodec"), 80)
        has_video = bool(vcodec and vcodec != "none")
        has_audio = bool(acodec and acodec != "none")
        media_type = "video" if has_video or not item.get("vcodec") else "audio"
        assets.append(
            _issue_asset(
                user_id=user_id,
                request_id=request_id,
                provider_id="yt_dlp_existing",
                asset_id=f"yt-dlp-{len(assets) + 1}",
                url=url,
                media_type=media_type,
                role="primary" if not assets else ("audio" if media_type == "audio" else "gallery"),
                declared_extension=item.get("ext"),
                width=safe_int(item.get("width")),
                height=safe_int(item.get("height")),
                duration_seconds=safe_float(item.get("duration") or payload.get("duration")),
                size_bytes=safe_int(item.get("filesize") or item.get("filesize_approx")),
                quality_label=safe_text(
                    item.get("format_note") or item.get("resolution") or item.get("format_id"),
                    120,
                ),
                codec=vcodec or acodec,
                requires_merge=bool(has_video and not has_audio),
                audio_asset_present=bool(has_audio),
            )
        )
        if len(assets) >= MAX_ASSETS:
            break
    return assets


def run_ytdlp_existing(*, source: str, request_id: str, user_id: int):
    provider = YtDlpGenericProvider()
    health = provider.health(force_version_probe=False)
    if not health.get("ready"):
        raise ProviderLabError("PROVIDER_NOT_CONFIGURED")
    payload = provider.parse(
        source,
        timeout_seconds=runtime_float("PROVIDER_LAB_YTDLP_TIMEOUT_SECONDS", 20.0),
    )
    if not isinstance(payload, dict):
        raise ProviderLabError("PROVIDER_SCHEMA_INVALID")
    assets = _ytdlp_assets(payload, user_id=user_id, request_id=request_id)
    if not assets:
        raise ProviderLabError("PROVIDER_RESULT_EMPTY")
    result = LabProviderResultV1(
        request_id=request_id,
        provider_id="yt_dlp_existing",
        provider_version=safe_text(health.get("version_output") or "yt-dlp-existing", 160),
        provider_scope="runtime",
        status="success",
        claimed_platform=safe_text(payload.get("extractor_key") or payload.get("extractor"), 80),
        selected_backend=safe_text(payload.get("extractor_key") or payload.get("extractor"), 160),
        result_kind="video",
        metadata={
            "title": payload.get("title"),
            "author": payload.get("uploader") or payload.get("channel"),
            "content_summary": payload.get("description"),
        },
        assets=assets,
        raw_shape=sanitize_raw_shape(payload),
        license_name="yt-dlp",
        license_scope="runtime_metadata_only",
    )
    return result.as_dict(), []


def run_tiantian_existing(*, source: str, request_id: str, user_id: int):
    provider = TianTianJieXiGenericProvider()
    if not tiantian_provider_configured():
        raise ProviderLabError("PROVIDER_NOT_CONFIGURED")
    payload = provider.parse(
        source,
        timeout_seconds=runtime_float("PROVIDER_LAB_TIANTIAN_TIMEOUT_SECONDS", 20.0),
    )
    if not isinstance(payload, dict):
        raise ProviderLabError("PROVIDER_SCHEMA_INVALID")
    assets = []
    videos = payload.get("videos") or ()
    for item in videos:
        if not isinstance(item, dict) or not item.get("url"):
            continue
        assets.append(
            _issue_asset(
                user_id=user_id,
                request_id=request_id,
                provider_id="tiantian_existing",
                asset_id=f"tiantian-video-{len(assets) + 1}",
                url=item.get("url"),
                media_type="video",
                role="primary" if not assets else "gallery",
                size_bytes=item.get("size"),
                quality_label=item.get("quality"),
            )
        )
        if len(assets) >= MAX_ASSETS:
            break
    for image_url in payload.get("images") or ():
        if len(assets) >= MAX_ASSETS:
            break
        assets.append(
            _issue_asset(
                user_id=user_id,
                request_id=request_id,
                provider_id="tiantian_existing",
                asset_id=f"tiantian-image-{len(assets) + 1}",
                url=image_url,
                media_type="image",
                role="gallery",
            )
        )
    if not assets:
        raise ProviderLabError("PROVIDER_RESULT_EMPTY")
    result = LabProviderResultV1(
        request_id=request_id,
        provider_id="tiantian_existing",
        provider_version="tiantian-existing",
        provider_scope="runtime",
        status="success",
        claimed_platform="",
        selected_backend="tiantianjiexi",
        result_kind=_result_kind("", assets),
        metadata={
            "title": payload.get("title"),
            "author": payload.get("nickname"),
            "content_summary": payload.get("description"),
        },
        assets=assets,
        raw_shape=sanitize_raw_shape(payload),
        license_name="Configured upstream",
        license_scope="runtime_parse_only",
    )
    return result.as_dict(), []


def _external_python(provider: str) -> str:
    env_name = f"{provider.upper()}_PROVIDER_LAB_PYTHON"
    default = f"/tmp/animeimages-provider-lab/{provider}/bin/python"
    return runtime_str(env_name, default)


def _run_external(provider: str, *, source: str, request_id: str, user_id: int):
    del user_id
    payload = execute_provider_worker(
        python=_external_python(provider),
        worker=WORKER_ROOT / f"{provider}_worker.py",
        request_id=request_id,
        source=source,
        timeout_seconds=runtime_float("PROVIDER_LAB_EXTERNAL_TIMEOUT_SECONDS", 25.0),
    )
    return payload["result"], payload["resources"]


def run_parsehub_repo(**kwargs):
    return _run_external("parsehub", **kwargs)


def run_videodl_repo(
    *,
    source: str,
    request_id: str,
    user_id: int,
    options: VideodlExecutionOptions | None = None,
):
    del user_id
    if options is None:
        result, resources = _run_external(
            "videodl",
            source=source,
            request_id=request_id,
            user_id=0,
        )
        return result, resources, ()
    controlled = execute_controlled_videodl(
        python=_external_python("videodl"),
        worker=WORKER_ROOT / "videodl_worker.py",
        request_id=request_id,
        source=source,
        options=options,
        total_timeout_seconds=runtime_float(
            "PROVIDER_LAB_EXTERNAL_TIMEOUT_SECONDS", 25.0
        ),
    )
    return controlled.result, controlled.resources, controlled.attempts


def list_videodl_backend_inventory() -> dict[str, Any]:
    if not runtime_bool("VIDEODL_PROVIDER_LAB_ENABLED", False):
        return {"status": "disabled", "items": []}
    python = _external_python("videodl")
    if not Path(python).is_file():
        return {"status": "not_configured", "items": []}
    try:
        items = load_videodl_inventory(
            python=python,
            worker=WORKER_ROOT / "videodl_worker.py",
            request_id=request_id_value(),
        )
    except ProviderLabError:
        return {"status": "unavailable", "items": []}
    return {"status": "ready", "items": public_inventory(items)}


def _always_available() -> bool:
    return True


def _external_available(provider: str) -> bool:
    return Path(_external_python(provider)).is_file()


def _ytdlp_available() -> bool:
    if not bool(getattr(settings, "YTDLP_PROVIDER_ENABLED", False)):
        return False
    binary = str(getattr(settings, "YTDLP_BINARY", "") or "").strip()
    return bool(external_command_health(binary, "yt-dlp").get("executable_found"))


PROVIDER_REGISTRY: Mapping[str, ProviderDescriptor] = {
    "animeimages_current": ProviderDescriptor(
        provider_id="animeimages_current",
        label="AnimeImages 当前结果",
        provider_version="animeimages-current",
        provider_scope="runtime",
        license_name="AnimeImages",
        license_scope="runtime",
        default_selected=True,
        runner=run_animeimages_current,
        availability=_always_available,
    ),
    "parsehub_repo": ProviderDescriptor(
        provider_id="parsehub_repo",
        label="ParseHub",
        provider_version=f"parsehub@{PARSEHUB_PIN}",
        provider_scope="lab_only",
        license_name="MIT",
        license_scope="lab_only_production_candidate",
        default_selected=True,
        runner=run_parsehub_repo,
        availability=lambda: _external_available("parsehub"),
        not_configured_hint="请先初始化 ParseHub 独立环境。",
    ),
    "videodl_repo": ProviderDescriptor(
        provider_id="videodl_repo",
        label="videodl",
        provider_version=f"videodl@{VIDEODL_PIN}",
        provider_scope="lab_only_noncommercial",
        license_name="PolyForm Noncommercial 1.0.0",
        license_scope="noncommercial_lab_only",
        default_selected=True,
        runner=run_videodl_repo,
        availability=lambda: runtime_bool("VIDEODL_PROVIDER_LAB_ENABLED", False)
        and _external_available("videodl"),
        not_configured_hint="仅限非商业本地研究测试；需显式启用并初始化独立环境。",
    ),
    "yt_dlp_existing": ProviderDescriptor(
        provider_id="yt_dlp_existing",
        label="现有 yt-dlp",
        provider_version="yt-dlp-existing",
        provider_scope="runtime",
        license_name="yt-dlp",
        license_scope="runtime_metadata_only",
        default_selected=False,
        runner=run_ytdlp_existing,
        availability=_ytdlp_available,
    ),
    "tiantian_existing": ProviderDescriptor(
        provider_id="tiantian_existing",
        label="现有天天解析",
        provider_version="tiantian-existing",
        provider_scope="runtime",
        license_name="Configured upstream",
        license_scope="runtime_parse_only",
        default_selected=False,
        runner=run_tiantian_existing,
        availability=tiantian_provider_configured,
    ),
}


def list_provider_descriptors() -> list[dict[str, Any]]:
    return [descriptor.as_dict() for descriptor in PROVIDER_REGISTRY.values()]


def _worker_result_to_contract(
    result_data: dict[str, Any],
    resources: list[dict[str, Any]],
    *,
    descriptor: ProviderDescriptor,
    user_id: int,
    request_id: str,
    elapsed_ms: int,
    attempts: tuple[LabProviderAttemptV1, ...] = (),
) -> LabProviderResultV1:
    if (
        result_data.get("provider_id") != descriptor.provider_id
        or result_data.get("provider_version") != descriptor.provider_version
        or result_data.get("request_id") != request_id
        or result_data.get("status") != "success"
    ):
        raise ProviderLabError("PROVIDER_SCHEMA_INVALID")
    resources_by_asset = {}
    for resource in resources:
        if not isinstance(resource, dict):
            raise ProviderLabError("PROVIDER_SCHEMA_INVALID")
        asset_id = safe_text(resource.get("asset_id"), 120)
        if not asset_id or asset_id in resources_by_asset:
            raise ProviderLabError("PROVIDER_SCHEMA_INVALID")
        resources_by_asset[asset_id] = resource

    assets = []
    asset_ids = set()
    for item in list(result_data.get("assets") or ())[:MAX_ASSETS]:
        if not isinstance(item, dict):
            raise ProviderLabError("PROVIDER_SCHEMA_INVALID")
        asset_id = safe_text(item.get("asset_id"), 120)
        resource = resources_by_asset.get(asset_id)
        if not asset_id or asset_id in asset_ids or not resource:
            raise ProviderLabError("PROVIDER_SCHEMA_INVALID")
        asset_ids.add(asset_id)
        exact_url, hostname = validated_resource_url(resource.get("url"))
        if hostname != safe_text(item.get("host_class"), 253).lower():
            raise ProviderLabError("PROVIDER_SCHEMA_INVALID")
        token = issue_resource_token(
            user_id=user_id,
            request_id=request_id,
            provider_id=descriptor.provider_id,
            asset_id=asset_id,
            url=exact_url,
            media_type=item.get("media_type"),
            headers_present=bool(resource.get("headers_present")),
            requires_cookie=bool(resource.get("requires_cookie")),
        )
        assets.append(
            LabAssetV1(
                asset_id=asset_id,
                media_type=item.get("media_type", "unknown"),
                role=item.get("role", "unknown"),
                resource_token=token,
                host_class=hostname,
                declared_extension=item.get("declared_extension", ""),
                width=safe_int(item.get("width")),
                height=safe_int(item.get("height")),
                duration_seconds=safe_float(item.get("duration_seconds")),
                size_bytes=safe_int(item.get("size_bytes")),
                quality_label=item.get("quality_label", ""),
                codec=item.get("codec", ""),
                requires_merge=bool(item.get("requires_merge")),
                requires_cookie=bool(resource.get("requires_cookie")),
                headers_present=bool(resource.get("headers_present")),
                audio_asset_present=bool(item.get("audio_asset_present")),
            )
        )
    if not assets:
        raise ProviderLabError("PROVIDER_RESULT_EMPTY")
    relationships = []
    for item in list(result_data.get("relationships") or ())[:MAX_RELATIONSHIPS]:
        if not isinstance(item, dict):
            raise ProviderLabError("PROVIDER_SCHEMA_INVALID")
        relationship = LabRelationshipV1(
            relationship_type=item.get("relationship_type", ""),
            source_asset_id=item.get("source_asset_id", ""),
            target_asset_id=item.get("target_asset_id", ""),
        )
        public_relationship = relationship.as_dict()
        if (
            public_relationship["source_asset_id"] not in asset_ids
            or public_relationship["target_asset_id"] not in asset_ids
        ):
            raise ProviderLabError("PROVIDER_SCHEMA_INVALID")
        relationships.append(relationship)
    return LabProviderResultV1(
        request_id=request_id,
        provider_id=descriptor.provider_id,
        provider_version=descriptor.provider_version,
        provider_scope=descriptor.provider_scope,
        status="success",
        elapsed_ms=elapsed_ms,
        claimed_platform=result_data.get("claimed_platform", ""),
        selected_backend=result_data.get("selected_backend", ""),
        result_kind=result_data.get("result_kind", "unknown"),
        metadata=result_data.get("metadata") or {},
        assets=assets,
        relationships=relationships,
        attempts=attempts,
        warnings=result_data.get("warnings") or (),
        raw_shape=sanitize_raw_shape(result_data.get("raw_shape") or {}),
        license_name=descriptor.license_name,
        license_scope=descriptor.license_scope,
    )


def _failure_result(
    *,
    descriptor: ProviderDescriptor,
    request_id: str,
    elapsed_ms: int,
    error: ProviderLabError,
    attempts: tuple[LabProviderAttemptV1, ...] = (),
) -> dict[str, Any]:
    status = {
        "PROVIDER_NOT_CONFIGURED": "not_configured",
        "PROVIDER_UNSUPPORTED": "unsupported",
        "PROVIDER_TIMEOUT": "timeout",
    }.get(error.code, "failed")
    return LabProviderResultV1(
        request_id=request_id,
        provider_id=descriptor.provider_id,
        provider_version=descriptor.provider_version,
        provider_scope=descriptor.provider_scope,
        status=status,
        elapsed_ms=elapsed_ms,
        attempts=attempts,
        error_code=error.code,
        license_name=descriptor.license_name,
        license_scope=descriptor.license_scope,
    ).as_dict()


def run_provider(
    *,
    provider_id: str,
    source: str,
    user_id: int,
    options: VideodlExecutionOptions | None = None,
) -> dict[str, Any]:
    descriptor = PROVIDER_REGISTRY.get(str(provider_id or ""))
    if descriptor is None:
        raise ProviderLabError("PROVIDER_NOT_SELECTED")
    request_id = request_id_value()
    started_at = time.monotonic()
    attempts: tuple[LabProviderAttemptV1, ...] = ()
    try:
        normalized = normalized_source(source)
        if options is not None and (
            provider_id != "videodl_repo"
            or not isinstance(options, VideodlExecutionOptions)
        ):
            raise ProviderLabError("PROVIDER_SCHEMA_INVALID")
        if provider_id == "videodl_repo" and not runtime_bool(
            "VIDEODL_PROVIDER_LAB_ENABLED", False
        ):
            raise ProviderLabError("PROVIDER_LICENSE_RESTRICTED")
        if provider_id == "videodl_repo":
            result_data, resources, attempts = descriptor.runner(
                source=normalized,
                request_id=request_id,
                user_id=user_id,
                options=options,
            )
        else:
            result_data, resources = descriptor.runner(
                source=normalized,
                request_id=request_id,
                user_id=user_id,
            )
        elapsed_ms = int((time.monotonic() - started_at) * 1000)
        if provider_id in {"parsehub_repo", "videodl_repo"}:
            contract = _worker_result_to_contract(
                result_data,
                resources,
                descriptor=descriptor,
                user_id=user_id,
                request_id=request_id,
                elapsed_ms=elapsed_ms,
                attempts=attempts,
            )
            return contract.as_dict()
        result_data["elapsed_ms"] = elapsed_ms
        return result_data
    except XhsProviderTimeoutError:
        lab_error = ProviderLabError("PROVIDER_TIMEOUT")
        return _failure_result(
            descriptor=descriptor,
            request_id=request_id,
            elapsed_ms=int((time.monotonic() - started_at) * 1000),
            error=lab_error,
            attempts=attempts,
        )
    except VideodlControlledError as error:
        return _failure_result(
            descriptor=descriptor,
            request_id=request_id,
            elapsed_ms=int((time.monotonic() - started_at) * 1000),
            error=error,
            attempts=error.attempts,
        )
    except ProviderLabError as error:
        return _failure_result(
            descriptor=descriptor,
            request_id=request_id,
            elapsed_ms=int((time.monotonic() - started_at) * 1000),
            error=error,
            attempts=attempts,
        )
    except Exception:
        error = ProviderLabError("PROVIDER_PROCESS_FAILED")
        return _failure_result(
            descriptor=descriptor,
            request_id=request_id,
            elapsed_ms=int((time.monotonic() - started_at) * 1000),
            error=error,
            attempts=attempts,
        )
