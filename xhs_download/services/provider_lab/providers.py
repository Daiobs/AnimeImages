from __future__ import annotations

import copy
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
import threading
import time
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit
from uuid import uuid4

from django.conf import settings
from django.core.cache import cache

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
    MAX_RETAINED_CANDIDATES,
    MAX_RELATIONSHIPS,
    MAX_SOURCE_LENGTH,
    ProviderLabError,
    exact_public_identity,
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
from . import videodl_policy
from .videodl_policy import VIDEODL_POLICY_PIN, VIDEODL_POLICY_REVISION


PARSEHUB_PIN = "144260f4349c5bb57415f7555a572fcd48c3b8ba"
VIDEODL_PIN = VIDEODL_POLICY_PIN
WORKER_ROOT = Path(settings.BASE_DIR) / "tools" / "provider_lab" / "runtime" / "workers"
VIDEODL_INVENTORY_SUCCESS_TTL_SECONDS = 600
VIDEODL_INVENTORY_FAILURE_TTL_SECONDS = 30
VIDEODL_INVENTORY_SINGLE_FLIGHT_SECONDS = 10
_VIDEODL_INVENTORY_LOCK = threading.Lock()


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
    execution_provider_id: str = ""
    enabled_setting: str = ""
    requires_confirmation: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        from ..quality_execution import PROVIDER_ALIASES
        execution_id = self.execution_provider_id or PROVIDER_ALIASES.get(self.provider_id, '')
        baseline = self.provider_id == 'animeimages_current'
        enabled = not self.enabled_setting or runtime_bool(self.enabled_setting, False)
        try:
            available = enabled and bool(self.availability())
        except Exception:
            available = False
        reason = '' if available else 'PROVIDER_DISABLED' if not enabled else 'PROVIDER_NOT_CONFIGURED'
        route_reason = 'COMPOSITE_BASELINE' if baseline else (
            '' if execution_id in PROVIDER_ALIASES.values() else 'NO_REPEATABLE_EXECUTOR')
        return {
            "provider_id": self.provider_id,
            "label": self.label,
            "provider_version": self.provider_version,
            "provider_scope": self.provider_scope,
            "license_name": self.license_name,
            "license_scope": self.license_scope,
            "default_selected": self.default_selected,
            "available": available,
            "unexecuted_reason": reason,
            "not_configured_hint": self.not_configured_hint,
            "execution_provider_id": execution_id,
            "baseline": baseline,
            "independent_upstream": not baseline,
            "route_selectable": not bool(route_reason),
            "route_unavailable_reason": route_reason,
            "requires_confirmation": list(self.requires_confirmation),
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
    resource_bundle: dict | None = None,
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
        resource_bundle=resource_bundle,
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
            metadata.get("author") or metadata.get("author_name") or metadata.get("nickname") or metadata.get("uploader"),
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
    resource_bundle: dict | None = None,
) -> LabAssetV1:
    header_names = {str(key).strip().lower() for key in (asset.headers or {})}
    return _issue_asset(
        user_id=user_id,
        request_id=request_id,
        provider_id=provider_id,
        asset_id=f"{provider_id}-{index}",
        resource_bundle=resource_bundle,
        url=asset.url,
        media_type=asset.media_type if asset.media_type in {"image", "video", "audio"} else "unknown",
        role="primary" if total == 1 else "gallery",
        declared_extension=asset.ext,
        size_bytes=asset.size_bytes,
        quality_label=asset.quality_label,
        size_is_estimate=asset.size_is_estimate,
        headers_present=bool(asset.headers),
        requires_cookie="cookie" in header_names,
    )


def _track_state(codec):
    value = str(codec or '').strip().lower()
    if not value or value == 'unknown':
        return 'unknown'
    return 'absent' if value == 'none' else 'present'


def _candidate_relationships(candidates):
    retained = candidates[:MAX_RETAINED_CANDIDATES]
    by_work = {}
    for candidate in retained:
        work = exact_public_identity(candidate.get('work_id'))
        upstream_id = exact_public_identity(candidate.get('upstream_asset_id'))
        if work and upstream_id:
            key = (work, upstream_id)
            # Ambiguous upstream IDs must not create an accidental pairing.
            by_work[key] = candidate['candidate_id'] if key not in by_work else None
    relationships = []
    seen = set()
    for candidate in retained:
        work = exact_public_identity(candidate.get('work_id'))
        for relation in candidate.get('relationships') or ():
            source = by_work.get((work, exact_public_identity(relation.get('source_asset_id'))))
            target = by_work.get((work, exact_public_identity(relation.get('target_asset_id'))))
            identity = (relation.get('relationship_type'), source, target)
            if source and target and source != target and identity not in seen:
                seen.add(identity)
                relationships.append(LabRelationshipV1(*identity))
                if len(relationships) >= MAX_RETAINED_CANDIDATES:
                    return relationships
    return relationships


def parse_result_to_lab(result, *, provider_id, user_id, request_id, provider_version=None):
    """Present the product's retained candidates, not its downloadable subset."""
    if not isinstance(result, ParseResult):
        raise ProviderLabError('PROVIDER_SCHEMA_INVALID')
    descriptor = PROVIDER_REGISTRY[provider_id]
    metadata = dict(result.metadata or {})
    from .resources import create_resource_bundle, store_resource_bundle
    resource_bundle = create_resource_bundle()
    candidates = metadata.get('upstream_candidates')
    assets = []
    if candidates is not None:
        from ..upstream_candidates import resolve_candidate_record
        if not isinstance(candidates, (list, tuple)):
            raise ProviderLabError('PROVIDER_SCHEMA_INVALID')
        for candidate in candidates[:MAX_RETAINED_CANDIDATES]:
            if not isinstance(candidate, dict):
                raise ProviderLabError('PROVIDER_SCHEMA_INVALID')
            candidate_id = candidate.get('candidate_id')
            record = resolve_candidate_record(candidate.get('resource_token'), candidate_id)
            if not isinstance(record, dict) or not record.get('url'):
                raise ProviderLabError('PROVIDER_SCHEMA_INVALID')
            audio = candidate.get('audio_tracks', _track_state(candidate.get('acodec')))
            video = candidate.get('video_tracks', _track_state(candidate.get('vcodec')))
            headers = record.get('headers') or {}
            header_names = {str(key).lower() for key in headers}
            assets.append(_issue_asset(
                user_id=user_id, request_id=request_id, provider_id=provider_id,
                asset_id=candidate_id, url=record['url'],
                resource_bundle=resource_bundle,
                media_type=candidate.get('media_type', 'unknown'),
                role=candidate.get('role', 'unknown'),
                upstream_role=candidate.get('upstream_role', ''),
                declared_extension=candidate.get('extension') or record.get('ext', ''),
                width=safe_int(candidate.get('width')), height=safe_int(candidate.get('height')),
                duration_seconds=safe_float(candidate.get('duration')),
                size_bytes=safe_int(candidate.get('size_bytes')),
                size_is_estimate=candidate.get('size_is_estimate') is True,
                quality_label=candidate.get('format_label', ''),
                format_id=candidate.get('format_id', ''), fps=candidate.get('fps'),
                work_id=candidate.get('work_id', ''), upstream_asset_id=candidate.get('upstream_asset_id', ''),
                work_label=candidate.get('work_label', ''),
                vcodec=candidate.get('vcodec', ''), acodec=candidate.get('acodec', ''),
                codec=candidate.get('vcodec') or candidate.get('acodec') or '',
                bitrate=candidate.get('bitrate'), audio_tracks=audio, video_tracks=video,
                protocol=candidate.get('protocol', ''),
                delivery_state=candidate.get('delivery_state', 'unknown'),
                metadata_source=candidate.get('metadata_source', 'unknown'),
                ordinal=candidate.get('ordinal', len(assets) + 1),
                requires_merge=candidate.get('requires_merge'),
                audio_asset_present=candidate.get('audio_asset_present'),
                headers_present=bool(headers) or record.get('headers_present') is True
                                or candidate.get('headers_present') is True
                                or candidate.get('requires_context') is True,
                requires_cookie='cookie' in header_names or record.get('requires_cookie') is True
                                or candidate.get('requires_cookie') is True,
            ))
    else:
        assets = [_media_asset_to_lab(asset, index=index, total=len(result.assets), resource_bundle=resource_bundle,
                    user_id=user_id, request_id=request_id, provider_id=provider_id)
                  for index, asset in enumerate(result.assets[:MAX_RETAINED_CANDIDATES], 1)]
    if not assets:
        raise ProviderLabError('PROVIDER_RESULT_EMPTY')
    store_resource_bundle(resource_bundle)
    observed = len(candidates) if candidates is not None else len(result.assets)
    public_metadata = _parse_result_metadata(result)
    truncated = safe_int(metadata.get('candidates_truncated', metadata.get('truncated_count')))
    public_metadata.update(candidate_count=metadata.get('candidate_count', observed + truncated),
                           returned_count=metadata.get('returned_count', observed),
                           truncated_count=truncated + max(0, observed - MAX_RETAINED_CANDIDATES),
                           truncated=bool(truncated or metadata.get('truncated') or observed > MAX_RETAINED_CANDIDATES),
                           unexpanded_count=metadata.get('unexpanded_count'),
                           unexpanded_scope=metadata.get('unexpanded_scope', ''),
                           baseline=provider_id == 'animeimages_current')
    relationships = _candidate_relationships(candidates or ())
    payload = LabProviderResultV1(
        request_id=request_id, provider_id=provider_id,
        provider_version=provider_version or metadata.get('upstream_version') or descriptor.provider_version,
        provider_scope=descriptor.provider_scope, status='success',
        claimed_platform=result.platform,
        selected_backend=safe_text(metadata.get('upstream_extractor') or metadata.get('source_provider'), 160),
        result_kind=_result_kind(result.post_type, assets), metadata=public_metadata,
        assets=assets, relationships=relationships, warnings=metadata.get('warnings') or (),
        raw_shape=sanitize_raw_shape({'result_class': type(result).__name__, 'candidate_count': observed}),
        license_name=descriptor.license_name, license_scope=descriptor.license_scope,
    )
    return payload.as_dict(), []


def _run_shared(provider_id, *, source, request_id, user_id, service_authorized=False):
    from ..quality_execution import execute_provider
    authorization = {'service_authorized': True} if service_authorized is True else {}
    result = execute_provider(provider_id, source, timeout_seconds=runtime_float(
        'PROVIDER_LAB_EXTERNAL_TIMEOUT_SECONDS', 25.0), **authorization)
    return parse_result_to_lab(result, provider_id=provider_id, user_id=user_id, request_id=request_id)


def run_animeimages_current(*, source: str, request_id: str, user_id: int):
    result = parse_media_page(
        source,
        prefer_ultra_hd=False,
        prefer_youtube_merge=False,
        timeout_seconds=runtime_float("PROVIDER_LAB_CURRENT_TIMEOUT_SECONDS", 20.0),
    )
    return parse_result_to_lab(result, provider_id='animeimages_current', user_id=user_id, request_id=request_id)


def run_ytdlp_existing(*, source: str, request_id: str, user_id: int):
    return _run_shared('yt_dlp_existing', source=source, request_id=request_id, user_id=user_id)


def run_gallerydl_existing(*, source: str, request_id: str, user_id: int):
    return _run_shared('gallery_dl_existing', source=source, request_id=request_id, user_id=user_id)


def run_tiantian_shared(*, source, request_id, user_id, service_authorized=False):
    return _run_shared('tiantian_existing', source=source, request_id=request_id, user_id=user_id,
                       service_authorized=service_authorized)


def run_kukutool_existing(*, source, request_id, user_id, service_authorized=False):
    return _run_shared('kukutool_existing', source=source, request_id=request_id, user_id=user_id,
                       service_authorized=service_authorized)


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
    return _run_shared('parsehub_repo', **kwargs)


def run_videodl_repo(
    *,
    source: str,
    request_id: str,
    user_id: int,
    options: VideodlExecutionOptions | None = None,
    timeout_seconds: float | None = None,
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
        total_timeout_seconds=min(30.0, max(.1, float(timeout_seconds))) if timeout_seconds is not None else runtime_float(
            "PROVIDER_LAB_EXTERNAL_TIMEOUT_SECONDS", 25.0
        ),
    )
    return controlled.result, controlled.resources, controlled.attempts


def _runtime_file_identity(path: str | Path) -> str:
    candidate = Path(path).resolve()
    try:
        stat = candidate.stat()
    except OSError:
        return f"{candidate}:missing"
    return f"{candidate}:{stat.st_size}:{stat.st_mtime_ns}"


def videodl_inventory_cache_key(*, python: str, worker: str | Path) -> str:
    identity = "|".join(
        (
            VIDEODL_PIN,
            VIDEODL_POLICY_REVISION,
            _runtime_file_identity(videodl_policy.__file__),
            _runtime_file_identity(python),
            _runtime_file_identity(worker),
        )
    )
    digest = sha256(identity.encode("utf-8")).hexdigest()
    return f"provider-lab:videodl-inventory:v3:{digest}"


def list_videodl_backend_inventory() -> dict[str, Any]:
    if not runtime_bool("VIDEODL_PROVIDER_LAB_ENABLED", False):
        return {"status": "disabled", "items": []}
    python = _external_python("videodl")
    if not Path(python).is_file():
        return {"status": "not_configured", "items": []}
    worker = WORKER_ROOT / "videodl_worker.py"
    cache_key = videodl_inventory_cache_key(python=python, worker=worker)
    cached = cache.get(cache_key)
    if isinstance(cached, dict):
        return copy.deepcopy(cached)
    with _VIDEODL_INVENTORY_LOCK:
        cached = cache.get(cache_key)
        if isinstance(cached, dict):
            return copy.deepcopy(cached)
        lock_key = f"{cache_key}:load"
        if not cache.add(
            lock_key,
            1,
            timeout=VIDEODL_INVENTORY_SINGLE_FLIGHT_SECONDS,
        ):
            cached = cache.get(cache_key)
            return (
                copy.deepcopy(cached)
                if isinstance(cached, dict)
                else {"status": "loading", "items": []}
            )
        try:
            try:
                items = load_videodl_inventory(
                    python=python,
                    worker=worker,
                    request_id=request_id_value(),
                )
                payload = {"status": "ready", "items": public_inventory(items)}
                timeout = VIDEODL_INVENTORY_SUCCESS_TTL_SECONDS
            except ProviderLabError:
                payload = {"status": "unavailable", "items": []}
                timeout = VIDEODL_INVENTORY_FAILURE_TTL_SECONDS
            cache.set(cache_key, payload, timeout=timeout)
            return copy.deepcopy(payload)
        finally:
            cache.delete(lock_key)


def _always_available() -> bool:
    return True


def _external_available(provider: str) -> bool:
    return Path(_external_python(provider)).is_file()


def _ytdlp_available() -> bool:
    if not bool(getattr(settings, "YTDLP_PROVIDER_ENABLED", False)):
        return False
    binary = str(getattr(settings, "YTDLP_BINARY", "") or "").strip()
    return bool(external_command_health(binary, "yt-dlp").get("executable_found"))


def _shared_available(provider_id):
    from ..quality_execution import provider_available
    return provider_available(provider_id)


PROVIDER_REGISTRY: Mapping[str, ProviderDescriptor] = {
    "animeimages_current": ProviderDescriptor(
        provider_id="animeimages_current",
        label="AnimeImages 当前路线基线",
        provider_version="animeimages-current",
        provider_scope="runtime",
        license_name="AnimeImages",
        license_scope="runtime",
        default_selected=False,
        runner=run_animeimages_current,
        availability=_always_available,
        requires_confirmation=('paid_service', 'external_sharing'),
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
        availability=lambda: _shared_available('parsehub_repo'),
        enabled_setting='PARSEHUB_UPSTREAM_ENABLED',
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
        availability=lambda: _shared_available('videodl_repo'),
        enabled_setting='VIDEODL_PROVIDER_LAB_ENABLED',
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
        availability=lambda: _shared_available('yt_dlp_existing'),
        enabled_setting='YTDLP_PROVIDER_ENABLED',
    ),
    "gallery_dl_existing": ProviderDescriptor(
        provider_id="gallery_dl_existing", label="gallery-dl",
        provider_version="gallery-dl-runtime", provider_scope="runtime",
        license_name="GPL-2.0", license_scope="runtime_metadata_only",
        default_selected=False, runner=run_gallerydl_existing,
        availability=lambda: _shared_available('gallery_dl_existing'),
        enabled_setting='GALLERYDL_PROVIDER_ENABLED',
        not_configured_hint="请配置 gallery-dl 独立执行环境。",
    ),
    "tiantian_existing": ProviderDescriptor(
        provider_id="tiantian_existing",
        label="现有天天解析",
        provider_version="tiantian-existing",
        provider_scope="runtime",
        license_name="Configured upstream",
        license_scope="runtime_parse_only",
        default_selected=False,
        runner=run_tiantian_shared,
        availability=tiantian_provider_configured,
        execution_provider_id='tiantianjiexi_generic',
        enabled_setting='TIANTIANJIEXI_PROVIDER_ENABLED',
        requires_confirmation=('paid_service','external_sharing'),
    ),
    "kukutool_existing": ProviderDescriptor(
        provider_id='kukutool_existing', label='Kukutool', provider_version='kukutool-existing',
        provider_scope='runtime', license_name='Configured upstream', license_scope='runtime_parse_only',
        default_selected=False, runner=run_kukutool_existing,
        availability=lambda: _shared_available('kukutool_existing'),
        execution_provider_id='kukutool_generic', enabled_setting='THIRD_PARTY_KUKUTOOL_PROVIDER_ENABLED',
        requires_confirmation=('external_sharing',),
    ),
}


def list_provider_descriptors() -> list[dict[str, Any]]:
    return [descriptor.as_dict() for descriptor in PROVIDER_REGISTRY.values()]


def route_provider_ids() -> tuple[str, ...]:
    return tuple(item['execution_provider_id'] for item in list_provider_descriptors() if item['route_selectable'])


def validate_confirmations(value):
    if value is None:
        return {}
    if (not isinstance(value, dict) or set(value) - {'paid_service', 'external_sharing'}
            or any(type(flag) is not bool for flag in value.values())):
        raise ProviderLabError('PROVIDER_SCHEMA_INVALID')
    return value


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
                upstream_role=item.get("upstream_role", item.get("role", "")),
                resource_token=token,
                host_class=hostname,
                declared_extension=item.get("declared_extension", ""),
                width=safe_int(item.get("width")),
                height=safe_int(item.get("height")),
                duration_seconds=safe_float(item.get("duration_seconds")),
                size_bytes=safe_int(item.get("size_bytes")),
                quality_label=item.get("quality_label", ""),
                codec=item.get("codec", ""),
                requires_merge=item.get("requires_merge"),
                requires_cookie=bool(resource.get("requires_cookie")),
                headers_present=bool(resource.get("headers_present")),
                audio_asset_present=item.get("audio_asset_present"),
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
    executed: bool = False,
) -> dict[str, Any]:
    status = {
        "PROVIDER_NOT_CONFIGURED": "not_configured",
        "PROVIDER_UNSUPPORTED": "unsupported",
        "PROVIDER_TIMEOUT": "timeout",
    }.get(error.code, "failed")
    if error.code == "PROVIDER_NOT_CONFIGURED":
        executed = False
    result = LabProviderResultV1(
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
    result.update(error_detail=error.detail, executed=executed, unexecuted_reason='' if executed else error.code,
                  baseline=descriptor.provider_id == 'animeimages_current',
                  independent_upstream=descriptor.provider_id != 'animeimages_current')
    return result


def run_provider(
    *,
    provider_id: str,
    source: str,
    user_id: int,
    options: VideodlExecutionOptions | None = None,
    confirmations: dict | None = None,
) -> dict[str, Any]:
    descriptor = PROVIDER_REGISTRY.get(str(provider_id or ""))
    if descriptor is None:
        raise ProviderLabError("PROVIDER_NOT_SELECTED")
    confirmations = validate_confirmations(confirmations)
    request_id = request_id_value()
    started_at = time.monotonic()
    attempts: tuple[LabProviderAttemptV1, ...] = ()
    execution_started = False
    try:
        normalized = normalized_source(source)
        state = descriptor.as_dict()
        missing = [name for name in descriptor.requires_confirmation if confirmations.get(name) is not True]
        if not state['available'] or missing:
            reason = state['unexecuted_reason'] or 'CONFIRMATION_REQUIRED'
            result = _failure_result(descriptor=descriptor, request_id=request_id, elapsed_ms=0,
                error=ProviderLabError('PROVIDER_NOT_CONFIGURED' if not state['available'] else 'PROVIDER_POLICY_REJECTED'))
            result.update(executed=False, unexecuted_reason=reason, requires_confirmation=missing,
                          baseline=state['baseline'], independent_upstream=state['independent_upstream'])
            return result
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
            from ..quality_execution import execute_provider
            execution_started = True
            result = execute_provider(provider_id, normalized, options=options,
                timeout_seconds=runtime_float('PROVIDER_LAB_EXTERNAL_TIMEOUT_SECONDS', 25.0))
            result_data, resources = parse_result_to_lab(result, provider_id=provider_id,
                request_id=request_id, user_id=user_id)
            result_data['attempts'] = list(result.metadata.get('attempts') or ())
        else:
            from ..quality_execution import SERVICE_PROVIDER_IDS
            authorization = ({'service_authorized': True}
                             if descriptor.execution_provider_id in SERVICE_PROVIDER_IDS else {})
            execution_started = True
            result_data, resources = descriptor.runner(
                source=normalized,
                request_id=request_id,
                user_id=user_id,
                **authorization,
            )
        elapsed_ms = int((time.monotonic() - started_at) * 1000)
        result_data["elapsed_ms"] = elapsed_ms
        result_data.update(executed=True, unexecuted_reason='', baseline=state['baseline'],
                           independent_upstream=state['independent_upstream'])
        return result_data
    except XhsProviderTimeoutError:
        lab_error = ProviderLabError("PROVIDER_TIMEOUT")
        return _failure_result(
            descriptor=descriptor,
            request_id=request_id,
            elapsed_ms=int((time.monotonic() - started_at) * 1000),
            error=lab_error,
            attempts=attempts,
            executed=execution_started,
        )
    except VideodlControlledError as error:
        return _failure_result(
            descriptor=descriptor,
            request_id=request_id,
            elapsed_ms=int((time.monotonic() - started_at) * 1000),
            error=error,
            attempts=error.attempts,
            executed=execution_started,
        )
    except ProviderLabError as error:
        return _failure_result(
            descriptor=descriptor,
            request_id=request_id,
            elapsed_ms=int((time.monotonic() - started_at) * 1000),
            error=error,
            attempts=attempts,
            executed=execution_started,
        )
    except Exception:
        error = ProviderLabError("PROVIDER_PROCESS_FAILED")
        return _failure_result(
            descriptor=descriptor,
            request_id=request_id,
            elapsed_ms=int((time.monotonic() - started_at) * 1000),
            error=error,
            attempts=attempts,
            executed=execution_started,
        )
