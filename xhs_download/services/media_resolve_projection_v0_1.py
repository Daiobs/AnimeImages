"""Pure AnimeImages ParseResult -> Media Resolve v0.1 projection.

``SourceIdentity`` is an already-verified Adapter-boundary input.  This module
does not verify identity from locators, invoke a Provider, or create a Delivery
handoff.  Product locator and Provider fields are inspected only where needed
to decide whether a current ``MediaAsset`` represents a usable local-domain
candidate; none is copied to the Core surface or used to derive an identifier.
"""

from __future__ import annotations

from typing import Iterable

from .assets import MediaAsset, ParseResult
from .media_resolve_v0_1 import (
    CandidateCompleteness,
    CandidateProjection,
    DesiredMediaCapability,
    EvidenceState,
    FailureKind,
    Fallback,
    MediaCandidate,
    MediaItem,
    MediaItemsCompleteness,
    MediaResolveValidationError,
    MediaType,
    QualityPreference,
    ResolveRequest,
    ResolveResult,
    Retryability,
    SelectionOwner,
    SourceIdentity,
    UserAction,
    WarningCode,
)


_PRODUCT_MEDIA_TYPES = {
    "video": MediaType.VIDEO,
    "image": MediaType.IMAGE,
    "audio": MediaType.AUDIO,
}


def _reject(code: str, path: str) -> None:
    raise MediaResolveValidationError(code, path)


def _map_media_type(value: object) -> MediaType:
    if type(value) is not str:
        return MediaType.UNKNOWN
    return _PRODUCT_MEDIA_TYPES.get(value.strip().lower(), MediaType.UNKNOWN)


def _matches_request(
    media_type: MediaType,
    desired: DesiredMediaCapability,
) -> bool:
    if desired is DesiredMediaCapability.ANY_RESOLVABLE:
        return True
    return media_type.value == desired.value


def _has_usable_product_representation(asset: MediaAsset) -> bool:
    # Presence is a current-domain usability signal only.  No locator is parsed,
    # reached, copied, admitted, or treated as source-identity evidence.
    return type(asset.url) is str and bool(asset.url.strip())


def _project_size(asset: MediaAsset, path: str):
    if type(asset.size_bytes) is not int or asset.size_bytes < 0:
        _reject("PROJECTED_ASSET_SIZE_INVALID", f"{path}.size_bytes")
    if type(asset.size_is_estimate) is not bool:
        _reject("PROJECTED_ASSET_SIZE_ESTIMATE_FLAG_INVALID", f"{path}.size_is_estimate")
    # AnimeImages uses zero as the default when upstream supplied no total.  The
    # projection omits it rather than inventing an exact zero-byte fact.
    if asset.size_bytes == 0:
        return None
    value_key = "estimatedBytes" if asset.size_is_estimate else "bytes"
    return {value_key: asset.size_bytes, "evidence": EvidenceState.ASSERTED}


def _merge_warnings(
    explicit: Iterable[WarningCode],
    automatic: Iterable[WarningCode],
) -> tuple[WarningCode, ...]:
    return tuple(explicit) + tuple(automatic)


def project_parse_result(
    parse_result: ParseResult,
    *,
    request: ResolveRequest,
    source_identity: SourceIdentity,
    result_id: str,
    media_items_completeness: MediaItemsCompleteness,
    candidate_completeness: CandidateCompleteness,
    selection_owner: SelectionOwner,
    warnings: Iterable[WarningCode] = (),
) -> ResolveResult:
    """Project one current-tree AnimeImages ParseResult into the v0.1 Core.

    Each current ``MediaAsset`` is one logical physical item with one redacted
    candidate.  Position-scoped identifiers are deterministic and independent
    of locators, headers, Provider fields, quality labels, and raw metadata.
    """

    if type(parse_result) is not ParseResult:
        _reject("PROJECTED_PARSE_RESULT_INVALID", "parseResult")
    if type(request) is not ResolveRequest:
        _reject("PROJECTED_REQUEST_INVALID", "request")
    if type(source_identity) is not SourceIdentity:
        _reject("PROJECTED_VERIFIED_SOURCE_IDENTITY_REQUIRED", "sourceIdentity")
    if type(media_items_completeness) is not MediaItemsCompleteness:
        _reject("PROJECTED_ITEM_COMPLETENESS_INVALID", "mediaItemsCompleteness")
    if type(candidate_completeness) is not CandidateCompleteness:
        _reject("PROJECTED_CANDIDATE_COMPLETENESS_INVALID", "candidateCompleteness")
    if type(selection_owner) is not SelectionOwner:
        _reject("PROJECTED_SELECTION_OWNER_INVALID", "selectionOwner")
    if type(parse_result.assets) is not tuple:
        _reject("PROJECTED_ASSET_COLLECTION_INVALID", "parseResult.assets")

    media_items: list[MediaItem] = []
    ordinary_omissions = 0
    for asset_index, asset in enumerate(parse_result.assets):
        path = f"parseResult.assets[{asset_index}]"
        if type(asset) is not MediaAsset:
            _reject("PROJECTED_MEDIA_ASSET_INVALID", path)
        media_type = _map_media_type(asset.media_type)
        if not _matches_request(media_type, request.desired_media_capability):
            continue
        if not _has_usable_product_representation(asset):
            ordinary_omissions += 1
            continue

        projected_index = len(media_items) + 1
        media_item_id = f"item-{projected_index:04d}"
        candidate_id = f"candidate-{projected_index:04d}-0001"
        candidate = MediaCandidate(
            candidate_id=candidate_id,
            size=_project_size(asset, path),
        )
        selected_candidate_id = (
            None
            if selection_owner is SelectionOwner.CONSUMER
            else candidate.candidate_id
        )
        media_items.append(
            MediaItem(
                media_item_id=media_item_id,
                media_type=media_type,
                candidate_projection=CandidateProjection(
                    completeness=candidate_completeness,
                    selection_owner=selection_owner,
                    candidates=(candidate,),
                    selected_candidate_id=selected_candidate_id,
                ),
            )
        )

    if not media_items:
        return ResolveResult.failed(
            kind=FailureKind.NO_USABLE_MEDIA,
            retryability=Retryability.NOT_RETRYABLE,
            fallback=Fallback.UNKNOWN,
            user_action=UserAction.CHANGE_INPUT,
        )

    automatic_warnings: list[WarningCode] = []
    effective_item_completeness = media_items_completeness
    if ordinary_omissions:
        effective_item_completeness = MediaItemsCompleteness.BOUNDED
        automatic_warnings.append(WarningCode.MEDIA_ITEMS_OMITTED)
    if (
        request.quality_preference is QualityPreference.BEST_AVAILABLE
        and selection_owner is SelectionOwner.CONSUMER
    ):
        automatic_warnings.append(WarningCode.PREFERENCE_NOT_APPLIED)

    return ResolveResult.resolved(
        result_id=result_id,
        source_identity=source_identity,
        media_items_completeness=effective_item_completeness,
        media_items=media_items,
        warnings=_merge_warnings(warnings, automatic_warnings),
    )


__all__ = ("project_parse_result",)
