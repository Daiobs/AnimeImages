"""Public-safe XHS LivePhoto runtime builder with bounded format evidence."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass, field, replace
from typing import Callable
from urllib.parse import urljoin

import requests

from ..assets import MediaAsset, ParseResult
from ..common import HEADERS, clean_filename
from ..rich_media import (
    RICH_MEDIA_SCHEMA_VERSION,
    RichMediaAssetDescriptorV1,
    RichMediaContractV1,
    RichMediaGroupMemberV1,
    RichMediaGroupV1,
)
from .xhs_livephoto_asset_spec_mapper import (
    XhsLivePhotoAssetSpecMapV1,
    build_xhs_livephoto_asset_specs,
    build_xhs_livephoto_member_proof,
    build_xhs_livephoto_private_format_proof,
    validate_xhs_livephoto_asset_spec_limits,
)
from .xhs_livephoto_browser_admission import (
    XhsPrivateHttpsMediaCandidateV1,
    normalize_xhs_livephoto_candidate_https,
)
from .xhs_livephoto_candidate_locator import (
    XhsLivePhotoMemberLocatorV1,
    XhsPrivateMediaCandidateV1,
    locate_xhs_livephoto_candidates,
    read_candidate_from_locator,
)
from .xhs_livephoto_relationship_mapper import map_xhs_livephoto_relationships
from .xhs_page_state import XhsPrivateSelectedNoteSnapshotV1


XHS_LIVEPHOTO_PROVIDER = "xhs_livephoto_preview_v1"
XHS_LIVEPHOTO_STAFF_PROVIDER = XHS_LIVEPHOTO_PROVIDER
XHS_LIVEPHOTO_MAX_PROBE_BYTES = 4096
XHS_LIVEPHOTO_MAX_PROBE_WORKERS = 6
XHS_LIVEPHOTO_CANDIDATE_TIMEOUT_SECONDS = 5.0
XHS_LIVEPHOTO_TOTAL_TIMEOUT_SECONDS = 20.0
XHS_LIVEPHOTO_MAX_REDIRECTS = 2

# Compatibility constants for the already-reviewed Staff-only vertical slice.
XHS_LIVEPHOTO_STAFF_MAX_PROBE_BYTES = XHS_LIVEPHOTO_MAX_PROBE_BYTES
XHS_LIVEPHOTO_STAFF_MAX_PROBE_WORKERS = XHS_LIVEPHOTO_MAX_PROBE_WORKERS
XHS_LIVEPHOTO_STAFF_CANDIDATE_TIMEOUT_SECONDS = (
    XHS_LIVEPHOTO_CANDIDATE_TIMEOUT_SECONDS
)
XHS_LIVEPHOTO_STAFF_TOTAL_TIMEOUT_SECONDS = XHS_LIVEPHOTO_TOTAL_TIMEOUT_SECONDS
XHS_LIVEPHOTO_STAFF_MAX_REDIRECTS = XHS_LIVEPHOTO_MAX_REDIRECTS

_REDIRECT_STATUS_CODES = frozenset({301, 302, 303, 307, 308})

_GENERIC_ERROR_CODE = "XHS_LIVEPHOTO_STAFF_PREVIEW_FAILED"
_ERROR_CODES = frozenset(
    {
        _GENERIC_ERROR_CODE,
        "XHS_LIVEPHOTO_STAFF_PREVIEW_DEADLINE",
        "XHS_LIVEPHOTO_STAFF_PREVIEW_PROBE_FAILED",
        "XHS_LIVEPHOTO_STAFF_PREVIEW_RESULT_INVALID",
    }
)


class XhsLivePhotoPreviewError(ValueError):
    def __init__(self, code: str):
        self.code = code if code in _ERROR_CODES else _GENERIC_ERROR_CODE
        super().__init__(self.code)


@dataclass(frozen=True)
class XhsLivePhotoProbeEvidenceV1:
    content_type: str
    bytes_read: int
    prefix: bytes = field(repr=False, compare=False)

    def __post_init__(self):
        if (
            type(self.content_type) is not str
            or type(self.prefix) is not bytes
            or type(self.bytes_read) is not int
            or self.bytes_read != len(self.prefix)
            or not 0 < self.bytes_read <= XHS_LIVEPHOTO_MAX_PROBE_BYTES
        ):
            raise XhsLivePhotoPreviewError(
                "XHS_LIVEPHOTO_STAFF_PREVIEW_PROBE_FAILED"
            )


@dataclass(frozen=True)
class _CandidateRecord:
    locator: XhsLivePhotoMemberLocatorV1
    private_candidate: XhsPrivateMediaCandidateV1 = field(
        repr=False,
        compare=False,
    )
    https_candidate: XhsPrivateHttpsMediaCandidateV1 = field(
        repr=False,
        compare=False,
    )


@dataclass(frozen=True)
class XhsLivePhotoBuildOutcome:
    status: str
    bounded_probe_bytes: int = 0
    phase_timings_ms: dict[str, int] = field(default_factory=dict)
    result: ParseResult | None = field(default=None, repr=False, compare=False)

    def __post_init__(self):
        if self.status not in {"enhanced", "not_applicable"}:
            raise XhsLivePhotoPreviewError(
                "XHS_LIVEPHOTO_STAFF_PREVIEW_RESULT_INVALID"
            )


def _remaining_seconds(deadline_monotonic: float) -> float:
    return max(0.0, deadline_monotonic - time.monotonic())


def _probe_key(record: _CandidateRecord) -> str:
    return record.https_candidate.value


def _probe_exact_candidate(
    record: _CandidateRecord,
    *,
    overall_deadline_monotonic: float,
) -> XhsLivePhotoProbeEvidenceV1:
    started_at = time.monotonic()
    candidate_deadline = min(
        overall_deadline_monotonic,
        started_at + XHS_LIVEPHOTO_CANDIDATE_TIMEOUT_SECONDS,
    )
    remaining = _remaining_seconds(candidate_deadline)
    if remaining <= 0:
        raise XhsLivePhotoPreviewError(
            "XHS_LIVEPHOTO_STAFF_PREVIEW_DEADLINE"
        )

    headers = {
        "User-Agent": HEADERS["User-Agent"],
        "Referer": HEADERS["Referer"],
        "Accept": "*/*",
        "Range": f"bytes=0-{XHS_LIVEPHOTO_MAX_PROBE_BYTES - 1}",
    }
    response = None
    request_candidate = record.https_candidate
    try:
        redirect_count = 0
        while True:
            remaining = _remaining_seconds(candidate_deadline)
            if remaining <= 0:
                raise XhsLivePhotoPreviewError(
                    "XHS_LIVEPHOTO_STAFF_PREVIEW_DEADLINE"
                )
            response = requests.get(
                request_candidate.value,
                headers=headers,
                stream=True,
                timeout=min(
                    XHS_LIVEPHOTO_CANDIDATE_TIMEOUT_SECONDS,
                    remaining,
                ),
                allow_redirects=False,
            )
            if response.status_code not in _REDIRECT_STATUS_CODES:
                break
            if redirect_count >= XHS_LIVEPHOTO_MAX_REDIRECTS:
                raise XhsLivePhotoPreviewError(
                    "XHS_LIVEPHOTO_STAFF_PREVIEW_PROBE_FAILED"
                )
            redirect_value = urljoin(
                request_candidate.value,
                str(response.headers.get("Location") or ""),
            )
            redirected_private_candidate = XhsPrivateMediaCandidateV1(
                value=redirect_value,
                locator=record.locator,
                member_role=record.locator.member_role,
            )
            redirected_https_candidate = normalize_xhs_livephoto_candidate_https(
                redirected_private_candidate,
                member_role=record.locator.member_role,
            )
            if (
                redirected_https_candidate.host_class
                != record.https_candidate.host_class
            ):
                raise XhsLivePhotoPreviewError(
                    "XHS_LIVEPHOTO_STAFF_PREVIEW_PROBE_FAILED"
                )
            response.close()
            response = None
            request_candidate = redirected_https_candidate
            redirect_count += 1

        if response.status_code not in {200, 206}:
            raise XhsLivePhotoPreviewError(
                "XHS_LIVEPHOTO_STAFF_PREVIEW_PROBE_FAILED"
            )

        final_private_candidate = XhsPrivateMediaCandidateV1(
            value=str(response.url or ""),
            locator=record.locator,
            member_role=record.locator.member_role,
        )
        final_https_candidate = normalize_xhs_livephoto_candidate_https(
            final_private_candidate,
            member_role=record.locator.member_role,
        )
        if final_https_candidate.host_class != record.https_candidate.host_class:
            raise XhsLivePhotoPreviewError(
                "XHS_LIVEPHOTO_STAFF_PREVIEW_PROBE_FAILED"
            )

        iterator = response.iter_content(
            chunk_size=XHS_LIVEPHOTO_MAX_PROBE_BYTES
        )
        prefix = bytes(next(iterator, b""))[:XHS_LIVEPHOTO_MAX_PROBE_BYTES]
        return XhsLivePhotoProbeEvidenceV1(
            content_type=str(response.headers.get("Content-Type") or ""),
            bytes_read=len(prefix),
            prefix=prefix,
        )
    except XhsLivePhotoPreviewError:
        raise
    except Exception:
        raise XhsLivePhotoPreviewError(
            "XHS_LIVEPHOTO_STAFF_PREVIEW_PROBE_FAILED"
        ) from None
    finally:
        if response is not None:
            response.close()


def probe_xhs_livephoto_candidates(
    records: tuple[_CandidateRecord, ...],
    *,
    deadline_monotonic: float,
) -> dict[str, XhsLivePhotoProbeEvidenceV1]:
    unique_records: dict[str, _CandidateRecord] = {}
    for record in records:
        unique_records.setdefault(_probe_key(record), record)
    if not unique_records:
        raise XhsLivePhotoPreviewError(
            "XHS_LIVEPHOTO_STAFF_PREVIEW_PROBE_FAILED"
        )

    executor = ThreadPoolExecutor(
        max_workers=min(
            XHS_LIVEPHOTO_MAX_PROBE_WORKERS,
            len(unique_records),
        ),
        thread_name_prefix="xhs-livephoto-probe",
    )
    futures = {
        executor.submit(
            _probe_exact_candidate,
            record,
            overall_deadline_monotonic=deadline_monotonic,
        ): key
        for key, record in unique_records.items()
    }
    try:
        completed, unresolved = wait(
            tuple(futures),
            timeout=_remaining_seconds(deadline_monotonic),
        )
        if unresolved:
            for future in unresolved:
                future.cancel()
            raise XhsLivePhotoPreviewError(
                "XHS_LIVEPHOTO_STAFF_PREVIEW_DEADLINE"
            )
        return {futures[future]: future.result() for future in completed}
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def _candidate_records(
    *,
    image_list,
    candidate_map,
) -> tuple[_CandidateRecord, ...]:
    records = []
    for item in candidate_map.items:
        locators = [item.still_locator]
        if item.motion_locator is not None:
            locators.append(item.motion_locator)
        for locator in locators:
            private_candidate = read_candidate_from_locator(
                image_list=image_list,
                locator=locator,
            )
            https_candidate = normalize_xhs_livephoto_candidate_https(
                private_candidate,
                member_role=locator.member_role,
            )
            records.append(
                _CandidateRecord(
                    locator=locator,
                    private_candidate=private_candidate,
                    https_candidate=https_candidate,
                )
            )
    return tuple(records)


def _build_rich_media_contract(
    asset_spec_map: XhsLivePhotoAssetSpecMapV1,
    assets: tuple[MediaAsset, ...],
) -> RichMediaContractV1:
    assets_by_id = {asset.asset_id: asset for asset in assets}
    return RichMediaContractV1(
        schema_version=RICH_MEDIA_SCHEMA_VERSION,
        post_type=asset_spec_map.post_type,
        assets=tuple(
            RichMediaAssetDescriptorV1(
                asset_id=binding.asset_spec.asset_id,
                media_type=binding.asset_spec.media_type,
                extension=assets_by_id[binding.asset_spec.asset_id].ext,
                role=binding.asset_spec.role,
            )
            for binding in asset_spec_map.assets
        ),
        groups=tuple(
            RichMediaGroupV1(
                group_id=group.group_id,
                relationship_type="live_photo",
                members=(
                    RichMediaGroupMemberV1(
                        asset_id=group.still_asset_id,
                        role="still",
                    ),
                    RichMediaGroupMemberV1(
                        asset_id=group.motion_asset_id,
                        role="motion",
                    ),
                ),
            )
            for group in asset_spec_map.groups
        ),
        content_blocks=(),
    )


def _build_media_assets(
    *,
    base_filename: str,
    asset_spec_map: XhsLivePhotoAssetSpecMapV1,
    records_by_locator: dict[XhsLivePhotoMemberLocatorV1, _CandidateRecord],
    selected_still_assets: tuple[MediaAsset, ...],
) -> tuple[MediaAsset, ...]:
    if len(selected_still_assets) != len({
        binding.source_item_ordinal for binding in asset_spec_map.assets
    }):
        raise XhsLivePhotoPreviewError(
            "XHS_LIVEPHOTO_STAFF_PREVIEW_RESULT_INVALID"
        )
    assets = []
    for binding in asset_spec_map.assets:
        record = records_by_locator[binding.locator]
        member_label = (
            "motion" if binding.locator.member_role == "motion" else "still"
        )
        filename = clean_filename(
            f"{base_filename}_{binding.source_item_ordinal + 1:02d}_{member_label}"
        )
        quality_label = (
            "实况视频"
            if binding.locator.member_role == "motion"
            else "实况照片"
            if binding.group_id is not None
            else "原始图片"
        )
        if binding.locator.member_role == "still":
            selected = selected_still_assets[binding.source_item_ordinal]
            selected_extension = selected.ext or binding.asset_spec.extension
            assets.append(
                MediaAsset(
                    media_type="image",
                    url=selected.url,
                    platform="xiaohongshu",
                    preview_url=selected.preview_url or selected.url,
                    filename=selected.filename or f"{filename}.{selected_extension}",
                    ext=selected_extension,
                    quality_label=quality_label,
                    headers={},
                    media_host_suffixes=selected.media_host_suffixes,
                    asset_id=binding.asset_spec.asset_id,
                    provider=XHS_LIVEPHOTO_PROVIDER,
                    provider_manifest_token="",
                )
            )
        else:
            assets.append(
                MediaAsset(
                    media_type="video",
                    url=record.https_candidate.value,
                    platform="xiaohongshu",
                    filename=f"{filename}.{binding.asset_spec.extension}",
                    ext=binding.asset_spec.extension,
                    quality_label=quality_label,
                    headers={},
                    media_host_suffixes=("xhscdn.com",),
                    asset_id=binding.asset_spec.asset_id,
                    provider=XHS_LIVEPHOTO_PROVIDER,
                    provider_manifest_token="",
                )
            )
    return tuple(assets)


def build_xhs_livephoto_result(
    *,
    flat_result: ParseResult,
    snapshot: XhsPrivateSelectedNoteSnapshotV1,
    deadline_monotonic: float = 0,
    probe_candidates: Callable | None = None,
) -> XhsLivePhotoBuildOutcome:
    if (
        type(flat_result) is not ParseResult
        or flat_result.platform != "xiaohongshu"
        or type(snapshot) is not XhsPrivateSelectedNoteSnapshotV1
    ):
        raise XhsLivePhotoPreviewError(
            "XHS_LIVEPHOTO_STAFF_PREVIEW_RESULT_INVALID"
        )

    started_at = time.monotonic()
    enhancement_deadline = started_at + XHS_LIVEPHOTO_TOTAL_TIMEOUT_SECONDS
    if deadline_monotonic:
        enhancement_deadline = min(enhancement_deadline, float(deadline_monotonic))
    if enhancement_deadline <= started_at:
        raise XhsLivePhotoPreviewError(
            "XHS_LIVEPHOTO_STAFF_PREVIEW_DEADLINE"
        )

    phase_timings_ms = {}
    probe_candidates = probe_candidates or probe_xhs_livephoto_candidates
    phase_started = time.monotonic()
    relationship_map = map_xhs_livephoto_relationships(
        requested_note_id=snapshot.note_id,
        payload_note_id=snapshot.note_id,
        image_list=snapshot.image_list,
        note_level_video_present=snapshot.note_level_video_present,
    )
    phase_timings_ms["relationship"] = round(
        (time.monotonic() - phase_started) * 1000
    )
    if relationship_map.livephoto_count == 0:
        return XhsLivePhotoBuildOutcome(
            status="not_applicable",
            phase_timings_ms=phase_timings_ms,
        )
    validate_xhs_livephoto_asset_spec_limits(relationship_map)

    phase_started = time.monotonic()
    candidate_map = locate_xhs_livephoto_candidates(
        relationship_map=relationship_map,
        image_list=snapshot.image_list,
    )
    records = _candidate_records(
        image_list=snapshot.image_list,
        candidate_map=candidate_map,
    )
    phase_timings_ms["candidate"] = round(
        (time.monotonic() - phase_started) * 1000
    )

    phase_started = time.monotonic()
    probe_evidence = probe_candidates(
        records,
        deadline_monotonic=enhancement_deadline,
    )
    phase_timings_ms["probe"] = round(
        (time.monotonic() - phase_started) * 1000
    )
    bounded_probe_bytes = sum(
        evidence.bytes_read for evidence in probe_evidence.values()
    )

    member_proofs = []
    records_by_locator = {}
    for record in records:
        evidence = probe_evidence.get(_probe_key(record))
        if evidence is None:
            raise XhsLivePhotoPreviewError(
                "XHS_LIVEPHOTO_STAFF_PREVIEW_PROBE_FAILED"
            )
        format_proof = build_xhs_livephoto_private_format_proof(
            private_https_candidate=record.https_candidate,
            prefix=evidence.prefix,
            content_type=evidence.content_type,
        )
        member_proofs.append(
            build_xhs_livephoto_member_proof(
                locator=record.locator,
                private_https_candidate=record.https_candidate,
                private_format_proof=format_proof,
            )
        )
        records_by_locator[record.locator] = record

    phase_started = time.monotonic()
    asset_spec_map = build_xhs_livephoto_asset_specs(
        requested_note_id=snapshot.note_id,
        payload_note_id=snapshot.note_id,
        relationship_map=relationship_map,
        candidate_map=candidate_map,
        image_list=snapshot.image_list,
        member_proofs=tuple(member_proofs),
    )
    phase_timings_ms["asset_spec"] = round(
        (time.monotonic() - phase_started) * 1000
    )

    phase_started = time.monotonic()
    assets = _build_media_assets(
        base_filename=flat_result.base_filename,
        asset_spec_map=asset_spec_map,
        records_by_locator=records_by_locator,
        selected_still_assets=flat_result.image_assets,
    )
    rich_media = _build_rich_media_contract(asset_spec_map, assets)
    metadata = dict(flat_result.metadata or {})
    metadata.update(
        {
            "xhs_livephoto_preview_status": "enhanced",
            "xhs_livephoto_source_item_count": len(relationship_map.items),
            "xhs_livephoto_livephoto_count": relationship_map.livephoto_count,
            "xhs_livephoto_regular_count": relationship_map.regular_image_count,
            "xhs_livephoto_bounded_probe_bytes": bounded_probe_bytes,
            "xhs_livephoto_full_media_bytes": 0,
        }
    )
    phase_timings_ms["result"] = round(
        (time.monotonic() - phase_started) * 1000
    )
    phase_timings_ms["total"] = round((time.monotonic() - started_at) * 1000)
    metadata["xhs_livephoto_phase_timings_ms"] = dict(phase_timings_ms)
    result = ParseResult(
        platform="xiaohongshu",
        post_type=asset_spec_map.post_type,
        base_filename=flat_result.base_filename,
        assets=assets,
        metadata=metadata,
        rich_media=rich_media,
    )
    return XhsLivePhotoBuildOutcome(
        status="enhanced",
        bounded_probe_bytes=bounded_probe_bytes,
        phase_timings_ms=phase_timings_ms,
        result=result,
    )


def bind_xhs_livephoto_still_result(
    result: ParseResult,
    still_result: ParseResult,
) -> ParseResult:
    """Bind admitted stills by source ordinal without changing motion authority."""

    if (
        type(result) is not ParseResult
        or type(still_result) is not ParseResult
        or result.platform != "xiaohongshu"
        or still_result.platform != "xiaohongshu"
        or result.rich_media is None
        or len(result.image_assets) != len(still_result.image_assets)
    ):
        raise XhsLivePhotoPreviewError(
            "XHS_LIVEPHOTO_STAFF_PREVIEW_RESULT_INVALID"
        )

    selected_stills = iter(still_result.image_assets)
    extensions_by_id = {}
    assets = []
    for asset in result.assets:
        if asset.media_type != "image":
            assets.append(asset)
            continue
        selected = next(selected_stills)
        extension = selected.ext or asset.ext
        filename = asset.filename
        if filename and extension:
            filename = f"{filename.rsplit('.', 1)[0]}.{extension}"
        rebound = replace(
            asset,
            url=selected.url,
            preview_url=selected.preview_url or selected.url,
            filename=filename,
            ext=extension,
            headers={},
            media_host_suffixes=selected.media_host_suffixes,
        )
        assets.append(rebound)
        extensions_by_id[rebound.asset_id] = extension

    rich_media = replace(
        result.rich_media,
        assets=tuple(
            replace(
                descriptor,
                extension=extensions_by_id.get(
                    descriptor.asset_id,
                    descriptor.extension,
                ),
            )
            for descriptor in result.rich_media.assets
        ),
    )
    metadata = dict(still_result.metadata or {})
    metadata.update(result.metadata or {})
    return ParseResult(
        platform=result.platform,
        post_type=result.post_type,
        base_filename=result.base_filename,
        assets=tuple(assets),
        metadata=metadata,
        rich_media=rich_media,
    )


def build_xhs_livephoto_preview_context(result: ParseResult) -> dict:
    if (
        type(result) is not ParseResult
        or result.platform != "xiaohongshu"
        or result.rich_media is None
    ):
        return {}

    assets_by_id = {asset.asset_id: asset for asset in result.assets}
    descriptor_by_id = {
        descriptor.asset_id: descriptor for descriptor in result.rich_media.assets
    }
    group_by_asset_id = {
        member.asset_id: group
        for group in result.rich_media.groups
        for member in group.members
    }
    consumed = set()
    items = []
    for asset in result.assets:
        if asset.asset_id in consumed:
            continue
        descriptor = descriptor_by_id[asset.asset_id]
        group = group_by_asset_id.get(asset.asset_id)
        if group is None:
            if descriptor.media_type != "image":
                raise XhsLivePhotoPreviewError(
                    "XHS_LIVEPHOTO_STAFF_PREVIEW_RESULT_INVALID"
                )
            consumed.add(asset.asset_id)
            items.append(
                {
                    "kind": "regular_image",
                    "still_url": asset.preview_url or asset.url,
                    "still_extension": descriptor.extension,
                }
            )
            continue

        members_by_role = {member.role: member for member in group.members}
        still_member = members_by_role.get("still")
        motion_member = members_by_role.get("motion")
        if still_member is None or motion_member is None:
            raise XhsLivePhotoPreviewError(
                "XHS_LIVEPHOTO_STAFF_PREVIEW_RESULT_INVALID"
            )
        still_asset = assets_by_id[still_member.asset_id]
        motion_asset = assets_by_id[motion_member.asset_id]
        if asset.asset_id != still_asset.asset_id:
            raise XhsLivePhotoPreviewError(
                "XHS_LIVEPHOTO_STAFF_PREVIEW_RESULT_INVALID"
            )
        consumed.update((still_asset.asset_id, motion_asset.asset_id))
        items.append(
            {
                "kind": "live_photo",
                "group_id": group.group_id,
                "still_url": still_asset.preview_url or still_asset.url,
                "motion_url": motion_asset.url,
                "still_extension": descriptor_by_id[still_asset.asset_id].extension,
                "motion_extension": descriptor_by_id[motion_asset.asset_id].extension,
            }
        )

    return {
        "items": items,
        "source_item_count": len(items),
        "livephoto_count": sum(item["kind"] == "live_photo" for item in items),
        "regular_count": sum(item["kind"] == "regular_image" for item in items),
    }


# Compatibility wrappers for the already-reviewed Staff-only vertical slice.
XhsLivePhotoStaffBuildOutcome = XhsLivePhotoBuildOutcome
XhsLivePhotoStaffPreviewError = XhsLivePhotoPreviewError


def build_xhs_livephoto_staff_result(**kwargs) -> XhsLivePhotoBuildOutcome:
    outcome = build_xhs_livephoto_result(**kwargs)
    if outcome.result is None:
        return outcome
    result = outcome.result
    metadata = dict(result.metadata or {})
    metadata.update(
        {
            "xhs_livephoto_staff_preview_status": metadata.get(
                "xhs_livephoto_preview_status", ""
            ),
            "xhs_livephoto_staff_source_item_count": metadata.get(
                "xhs_livephoto_source_item_count", 0
            ),
            "xhs_livephoto_staff_livephoto_count": metadata.get(
                "xhs_livephoto_livephoto_count", 0
            ),
            "xhs_livephoto_staff_regular_count": metadata.get(
                "xhs_livephoto_regular_count", 0
            ),
            "xhs_livephoto_staff_bounded_probe_bytes": metadata.get(
                "xhs_livephoto_bounded_probe_bytes", 0
            ),
            "xhs_livephoto_staff_full_media_bytes": metadata.get(
                "xhs_livephoto_full_media_bytes", 0
            ),
            "xhs_livephoto_staff_phase_timings_ms": dict(
                metadata.get("xhs_livephoto_phase_timings_ms") or {}
            ),
        }
    )
    staff_result = ParseResult(
        platform=result.platform,
        post_type=result.post_type,
        base_filename=result.base_filename,
        assets=result.assets,
        metadata=metadata,
        rich_media=result.rich_media,
    )
    return XhsLivePhotoBuildOutcome(
        status=outcome.status,
        bounded_probe_bytes=outcome.bounded_probe_bytes,
        phase_timings_ms=outcome.phase_timings_ms,
        result=staff_result,
    )


build_xhs_livephoto_staff_preview_context = build_xhs_livephoto_preview_context


__all__ = (
    "XHS_LIVEPHOTO_CANDIDATE_TIMEOUT_SECONDS",
    "XHS_LIVEPHOTO_MAX_REDIRECTS",
    "XHS_LIVEPHOTO_MAX_PROBE_BYTES",
    "XHS_LIVEPHOTO_MAX_PROBE_WORKERS",
    "XHS_LIVEPHOTO_STAFF_CANDIDATE_TIMEOUT_SECONDS",
    "XHS_LIVEPHOTO_STAFF_MAX_PROBE_BYTES",
    "XHS_LIVEPHOTO_STAFF_MAX_PROBE_WORKERS",
    "XHS_LIVEPHOTO_STAFF_MAX_REDIRECTS",
    "XHS_LIVEPHOTO_STAFF_PROVIDER",
    "XHS_LIVEPHOTO_PROVIDER",
    "XHS_LIVEPHOTO_STAFF_TOTAL_TIMEOUT_SECONDS",
    "XHS_LIVEPHOTO_TOTAL_TIMEOUT_SECONDS",
    "XhsLivePhotoProbeEvidenceV1",
    "XhsLivePhotoBuildOutcome",
    "XhsLivePhotoPreviewError",
    "XhsLivePhotoStaffBuildOutcome",
    "XhsLivePhotoStaffPreviewError",
    "build_xhs_livephoto_preview_context",
    "build_xhs_livephoto_result",
    "bind_xhs_livephoto_still_result",
    "build_xhs_livephoto_staff_preview_context",
    "build_xhs_livephoto_staff_result",
    "probe_xhs_livephoto_candidates",
)
