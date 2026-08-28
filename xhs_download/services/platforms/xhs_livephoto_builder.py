"""Public-safe XHS LivePhoto runtime builder with bounded format evidence."""

from __future__ import annotations

import hashlib
import time
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass, field, replace
from threading import BoundedSemaphore
from typing import Callable
from urllib.parse import urljoin

import requests

from ..assets import MediaAsset, ParseResult
from ..common import HEADERS, clean_filename
from ..provider_manifest import build_provider_manifest_token
from ..rich_media import (
    RICH_MEDIA_SCHEMA_VERSION,
    RichMediaAssetDescriptorV1,
    RichMediaContractV1,
    RichMediaGroupMemberV1,
    RichMediaGroupV1,
)
from .xhs_livephoto_asset_spec_mapper import (
    XhsLivePhotoAssetSpecMappingError,
    XhsLivePhotoAssetSpecMapV1,
    build_xhs_livephoto_asset_specs,
    build_xhs_livephoto_member_proof,
    build_xhs_livephoto_private_format_proof,
    validate_xhs_livephoto_asset_spec_limits,
)
from .xhs_livephoto_browser_admission import (
    XhsLivePhotoBrowserAdmissionError,
    XhsPrivateHttpsMediaCandidateV1,
    normalize_xhs_livephoto_candidate_https,
)
from .xhs_livephoto_candidate_locator import (
    XhsLivePhotoCandidateMapV1,
    XhsLivePhotoMemberLocatorV1,
    XhsPrivateMediaCandidateV1,
    list_xhs_livephoto_motion_candidate_locators,
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

# A running Python thread cannot be force-killed safely. Keep uncooperative
# upstream reads fail-closed and globally bounded instead of letting each
# request create another set of blocked network probes.
_XHS_LIVEPHOTO_PROBE_NETWORK_SLOTS = BoundedSemaphore(
    XHS_LIVEPHOTO_MAX_PROBE_WORKERS
)

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

_PAIR_AUTHORITY_ERROR_CODES = frozenset(
    {
        "XHS_LIVEPHOTO_PAIR_SOURCE_REJECTED",
        "XHS_LIVEPHOTO_PAIR_MANIFEST_REJECTED",
        "XHS_LIVEPHOTO_PAIR_STILL_REJECTED",
        "XHS_LIVEPHOTO_PAIR_MOTION_REJECTED",
        "XHS_LIVEPHOTO_PAIR_RELATION_REJECTED",
        "XHS_LIVEPHOTO_PAIR_SIZE_REJECTED",
        "XHS_LIVEPHOTO_PAIR_TOO_LARGE",
        "XHS_LIVEPHOTO_PAIR_RECORD_TOO_LARGE",
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


def _candidate_probe_is_delivery_ready(
    record: _CandidateRecord,
    evidence: XhsLivePhotoProbeEvidenceV1 | None,
) -> bool:
    if evidence is None:
        return False
    try:
        format_proof = build_xhs_livephoto_private_format_proof(
            private_https_candidate=record.https_candidate,
            prefix=evidence.prefix,
            content_type=evidence.content_type,
        )
        build_xhs_livephoto_member_proof(
            locator=record.locator,
            private_https_candidate=record.https_candidate,
            private_format_proof=format_proof,
        )
    except XhsLivePhotoAssetSpecMappingError:
        return False
    return True


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
    if not _XHS_LIVEPHOTO_PROBE_NETWORK_SLOTS.acquire(blocking=False):
        raise XhsLivePhotoPreviewError(
            "XHS_LIVEPHOTO_STAFF_PREVIEW_PROBE_FAILED"
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
        _XHS_LIVEPHOTO_PROBE_NETWORK_SLOTS.release()


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
        ): (key, record)
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
        return {
            futures[future][0]: future.result()
            for future in completed
        }
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def _probe_xhs_livephoto_candidates_partial(
    records: tuple[_CandidateRecord, ...],
    *,
    deadline_monotonic: float,
) -> dict[str, XhsLivePhotoProbeEvidenceV1]:
    """Collect bounded evidence without promoting one delivery miss to structure."""

    unique_records: dict[str, _CandidateRecord] = {}
    for record in records:
        unique_records.setdefault(_probe_key(record), record)
    if not unique_records:
        return {}
    executor = ThreadPoolExecutor(
        max_workers=min(XHS_LIVEPHOTO_MAX_PROBE_WORKERS, len(unique_records)),
        thread_name_prefix="xhs-livephoto-probe",
    )
    futures = {
        executor.submit(
            _probe_exact_candidate,
            record,
            overall_deadline_monotonic=deadline_monotonic,
        ): (key, record)
        for key, record in unique_records.items()
    }
    evidence = {}
    try:
        completed, unresolved = wait(
            tuple(futures),
            timeout=_remaining_seconds(deadline_monotonic),
        )
        for future in unresolved:
            future.cancel()
        if any(
            futures[future][1].locator.member_role == "still"
            for future in unresolved
        ):
            raise XhsLivePhotoPreviewError(
                "XHS_LIVEPHOTO_STAFF_PREVIEW_DEADLINE"
            )
        for future in completed:
            try:
                evidence[futures[future][0]] = future.result()
            except Exception:
                continue
        return evidence
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


def _candidate_delivery_plan(
    *,
    image_list,
    candidate_map: XhsLivePhotoCandidateMapV1,
) -> tuple[
    tuple[_CandidateRecord, ...],
    dict[int, tuple[_CandidateRecord, ...]],
]:
    still_records = []
    motion_records_by_ordinal = {}
    for item in candidate_map.items:
        private_still = read_candidate_from_locator(
            image_list=image_list,
            locator=item.still_locator,
        )
        still_records.append(
            _CandidateRecord(
                locator=item.still_locator,
                private_candidate=private_still,
                https_candidate=normalize_xhs_livephoto_candidate_https(
                    private_still,
                    member_role="still",
                ),
            )
        )
        if item.item_kind != "live_photo":
            continue
        options = []
        for locator in list_xhs_livephoto_motion_candidate_locators(
            image_list=image_list,
            source_item_ordinal=item.source_item_ordinal,
        ):
            private_candidate = read_candidate_from_locator(
                image_list=image_list,
                locator=locator,
            )
            try:
                https_candidate = normalize_xhs_livephoto_candidate_https(
                    private_candidate,
                    member_role="motion",
                )
            except XhsLivePhotoBrowserAdmissionError:
                continue
            options.append(
                _CandidateRecord(
                    locator=locator,
                    private_candidate=private_candidate,
                    https_candidate=https_candidate,
                )
            )
        motion_records_by_ordinal[item.source_item_ordinal] = tuple(options)
    return tuple(still_records), motion_records_by_ordinal


def _probe_ordered_delivery_candidates(
    *,
    still_records: tuple[_CandidateRecord, ...],
    motion_records_by_ordinal: dict[int, tuple[_CandidateRecord, ...]],
    deadline_monotonic: float,
) -> dict[str, XhsLivePhotoProbeEvidenceV1]:
    """Probe one same-item preference wave at a time, parallel across items."""

    first_wave = still_records + tuple(
        records[0]
        for records in motion_records_by_ordinal.values()
        if records
    )
    evidence = _probe_xhs_livephoto_candidates_partial(
        first_wave,
        deadline_monotonic=deadline_monotonic,
    )
    max_options = max(
        (len(records) for records in motion_records_by_ordinal.values()),
        default=0,
    )
    for option_index in range(1, max_options):
        if _remaining_seconds(deadline_monotonic) <= 0:
            break
        wave = tuple(
            records[option_index]
            for records in motion_records_by_ordinal.values()
            if len(records) > option_index
            and not any(
                _candidate_probe_is_delivery_ready(
                    record,
                    evidence.get(_probe_key(record)),
                )
                for record in records[:option_index]
            )
        )
        if not wave:
            continue
        evidence.update(
            _probe_xhs_livephoto_candidates_partial(
                wave,
                deadline_monotonic=deadline_monotonic,
            )
        )
    return evidence


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
            provider = selected.provider or XHS_LIVEPHOTO_PROVIDER
            provider_resource_id = (
                selected.provider_resource_id or binding.asset_spec.asset_id
            )
            provider_cache_key = (
                selected.provider_cache_key
                or f"xiaohongshu:livephoto:{binding.source_item_ordinal}"
            )
            provider_manifest_token = build_provider_manifest_token(
                asset_id=binding.asset_spec.asset_id,
                provider=provider,
                provider_resource_id=provider_resource_id,
                provider_cache_key=provider_cache_key,
                url=selected.url,
                media_type="image",
                source_platform="xiaohongshu",
            )
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
                    provider=provider,
                    provider_resource_id=provider_resource_id,
                    provider_cache_key=provider_cache_key,
                    provider_manifest_token=provider_manifest_token,
                    source_platform="xiaohongshu",
                )
            )
        else:
            provider_resource_id = binding.asset_spec.asset_id
            provider_cache_key = (
                f"xiaohongshu:livephoto:{binding.source_item_ordinal}"
            )
            provider_manifest_token = build_provider_manifest_token(
                asset_id=binding.asset_spec.asset_id,
                provider=XHS_LIVEPHOTO_PROVIDER,
                provider_resource_id=provider_resource_id,
                provider_cache_key=provider_cache_key,
                url=record.https_candidate.value,
                media_type="video",
                source_platform="xiaohongshu",
            )
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
                    provider_resource_id=provider_resource_id,
                    provider_cache_key=provider_cache_key,
                    provider_manifest_token=provider_manifest_token,
                    source_platform="xiaohongshu",
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
    still_records, motion_records_by_ordinal = _candidate_delivery_plan(
        image_list=snapshot.image_list,
        candidate_map=candidate_map,
    )
    records = still_records + tuple(
        record
        for item_records in motion_records_by_ordinal.values()
        for record in item_records
    )
    phase_timings_ms["candidate"] = round(
        (time.monotonic() - phase_started) * 1000
    )

    phase_started = time.monotonic()
    if probe_candidates is None:
        probe_evidence = _probe_ordered_delivery_candidates(
            still_records=still_records,
            motion_records_by_ordinal=motion_records_by_ordinal,
            deadline_monotonic=enhancement_deadline,
        )
    else:
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
    for record in still_records:
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

    selected_motion_records = {}
    for item in candidate_map.items:
        if item.item_kind != "live_photo":
            continue
        for record in motion_records_by_ordinal.get(item.source_item_ordinal, ()):
            evidence = probe_evidence.get(_probe_key(record))
            if evidence is None:
                continue
            try:
                format_proof = build_xhs_livephoto_private_format_proof(
                    private_https_candidate=record.https_candidate,
                    prefix=evidence.prefix,
                    content_type=evidence.content_type,
                )
                member_proof = build_xhs_livephoto_member_proof(
                    locator=record.locator,
                    private_https_candidate=record.https_candidate,
                    private_format_proof=format_proof,
                )
            except XhsLivePhotoAssetSpecMappingError:
                continue
            selected_motion_records[item.source_item_ordinal] = record
            member_proofs.append(member_proof)
            records_by_locator[record.locator] = record
            break

    selected_candidate_items = tuple(
        replace(
            item,
            motion_locator=(
                selected_motion_records[item.source_item_ordinal].locator
                if item.source_item_ordinal in selected_motion_records
                else item.motion_locator
            ),
        )
        for item in candidate_map.items
    )
    selected_candidate_map = XhsLivePhotoCandidateMapV1(
        schema_version=candidate_map.schema_version,
        relationship_schema_version=candidate_map.relationship_schema_version,
        items=selected_candidate_items,
    )
    delivery_ready_ordinals = frozenset(selected_motion_records)

    phase_started = time.monotonic()
    asset_spec_map = build_xhs_livephoto_asset_specs(
        requested_note_id=snapshot.note_id,
        payload_note_id=snapshot.note_id,
        relationship_map=relationship_map,
        candidate_map=selected_candidate_map,
        image_list=snapshot.image_list,
        member_proofs=tuple(member_proofs),
        delivery_ready_ordinals=delivery_ready_ordinals,
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
            "xhs_livephoto_source_identity": hashlib.sha256(
                snapshot.note_id.encode("utf-8")
            ).hexdigest(),
            "xhs_livephoto_delivery_ready_count": len(delivery_ready_ordinals),
            "xhs_livephoto_delivery_unavailable_count": (
                relationship_map.livephoto_count - len(delivery_ready_ordinals)
            ),
        }
    )
    group_by_ordinal = {
        group.source_item_ordinal: group for group in asset_spec_map.groups
    }
    still_by_ordinal = {
        binding.source_item_ordinal: binding
        for binding in asset_spec_map.assets
        if binding.locator.member_role == "still"
    }
    motion_by_ordinal = {
        binding.source_item_ordinal: binding
        for binding in asset_spec_map.assets
        if binding.locator.member_role == "motion"
    }
    metadata["xhs_livephoto_items"] = [
        {
            "source_item_ordinal": item.source_item_ordinal,
            "kind": item.item_kind,
            "still_asset_id": still_by_ordinal[item.source_item_ordinal].asset_spec.asset_id,
            "motion_asset_id": (
                motion_by_ordinal[item.source_item_ordinal].asset_spec.asset_id
                if item.source_item_ordinal in motion_by_ordinal
                else ""
            ),
            "group_id": (
                group_by_ordinal[item.source_item_ordinal].group_id
                if item.source_item_ordinal in group_by_ordinal
                else ""
            ),
            "delivery_status": (
                "ready"
                if item.source_item_ordinal in delivery_ready_ordinals
                else "unavailable"
                if item.item_kind == "live_photo"
                else "not_applicable"
            ),
            "delivery_error_code": (
                "XHS_LIVEPHOTO_MOTION_CANDIDATE_UNAVAILABLE"
                if item.item_kind == "live_photo"
                and item.source_item_ordinal not in delivery_ready_ordinals
                else ""
            ),
            "motion_source_kind": (
                selected_motion_records[item.source_item_ordinal].locator.source_kind
                if item.source_item_ordinal in selected_motion_records
                else ""
            ),
        }
        for item in relationship_map.items
    ]
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
            provider=selected.provider or XHS_LIVEPHOTO_PROVIDER,
            provider_resource_id=(
                selected.provider_resource_id or asset.asset_id
            ),
            provider_cache_key=(
                selected.provider_cache_key
                or f"xiaohongshu:livephoto:{asset.asset_id}"
            ),
            source_platform="xiaohongshu",
        )
        rebound = replace(
            rebound,
            provider_manifest_token=build_provider_manifest_token(
                asset_id=rebound.asset_id,
                provider=rebound.provider,
                provider_resource_id=rebound.provider_resource_id,
                provider_cache_key=rebound.provider_cache_key,
                url=rebound.url,
                media_type="image",
                source_platform="xiaohongshu",
            ),
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


def build_xhs_livephoto_preview_context(
    result: ParseResult,
    *,
    client_identity: str = "",
    issue_pair_token=None,
) -> dict:
    if (
        type(result) is not ParseResult
        or result.platform != "xiaohongshu"
        or result.rich_media is None
    ):
        return {}

    assets_by_id = {asset.asset_id: asset for asset in result.assets}
    metadata = result.metadata or {}
    item_records = metadata.get("xhs_livephoto_items")
    if not isinstance(item_records, list):
        raise XhsLivePhotoPreviewError(
            "XHS_LIVEPHOTO_STAFF_PREVIEW_RESULT_INVALID"
        )
    if issue_pair_token is None:
        from ..xhs_livephoto_pair_handoff import issue_xhs_livephoto_pair_token

        issue_pair_token = issue_xhs_livephoto_pair_token

    items = []
    for expected_ordinal, item_record in enumerate(item_records):
        if (
            not isinstance(item_record, dict)
            or item_record.get("source_item_ordinal") != expected_ordinal
            or item_record.get("kind") not in {"regular_image", "live_photo"}
        ):
            raise XhsLivePhotoPreviewError(
                "XHS_LIVEPHOTO_STAFF_PREVIEW_RESULT_INVALID"
            )
        still_asset = assets_by_id.get(str(item_record.get("still_asset_id") or ""))
        if still_asset is None or still_asset.media_type != "image":
            raise XhsLivePhotoPreviewError(
                "XHS_LIVEPHOTO_STAFF_PREVIEW_RESULT_INVALID"
            )
        item = {
            "source_item_ordinal": expected_ordinal,
            "display_ordinal": expected_ordinal + 1,
            "kind": item_record["kind"],
            "still_url": still_asset.preview_url or still_asset.url,
            "still_original_url": still_asset.url,
            "still_filename": still_asset.filename,
            "still_extension": still_asset.ext or "jpg",
            "delivery_status": str(
                item_record.get("delivery_status") or "not_applicable"
            ),
            "delivery_error_code": str(
                item_record.get("delivery_error_code") or ""
            )[:96],
            "pair_token": "",
        }
        if item_record["kind"] == "live_photo" and item["delivery_status"] == "ready":
            motion_asset = assets_by_id.get(
                str(item_record.get("motion_asset_id") or "")
            )
            group_id = str(item_record.get("group_id") or "")
            if motion_asset is None or motion_asset.media_type != "video" or not group_id:
                raise XhsLivePhotoPreviewError(
                    "XHS_LIVEPHOTO_STAFF_PREVIEW_RESULT_INVALID"
                )
            if client_identity:
                try:
                    item["pair_token"] = issue_pair_token(
                        source_identity=str(
                            metadata.get("xhs_livephoto_source_identity") or ""
                        ),
                        group_id=group_id,
                        source_item_ordinal=expected_ordinal,
                        still_asset=still_asset,
                        motion_asset=motion_asset,
                        base_filename=result.base_filename,
                        client_identity=client_identity,
                    ).token
                except Exception as error:
                    error_code = str(getattr(error, "error_code", ""))
                    if error_code not in _PAIR_AUTHORITY_ERROR_CODES:
                        error_code = "XHS_LIVEPHOTO_PAIR_AUTHORITY_UNAVAILABLE"
                    item["delivery_status"] = "unavailable"
                    item["delivery_error_code"] = error_code
            else:
                item["delivery_status"] = "unavailable"
                item["delivery_error_code"] = (
                    "XHS_LIVEPHOTO_PAIR_AUTHORITY_UNAVAILABLE"
                )
        items.append(item)

    return {
        "items": items,
        "source_item_count": len(items),
        "livephoto_count": sum(item["kind"] == "live_photo" for item in items),
        "regular_count": sum(item["kind"] == "regular_image" for item in items),
        "delivery_ready_count": sum(
            item["kind"] == "live_photo"
            and item["delivery_status"] == "ready"
            and bool(item["pair_token"])
            for item in items
        ),
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
