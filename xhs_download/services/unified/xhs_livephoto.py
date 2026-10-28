"""Xiaohongshu live photos on the unified pipeline: the admin's save mode.

Native pairs a photo with its motion when both come from the same imageList
item (step 2). The route's ``live_photo_mode`` decides what the page offers:

- 实况照片 (``livephoto``): the result takes the shape of the existing LivePhoto
  feature, so its cards, preview, pair download and browser ZIP serve the
  pairs unchanged. Nothing is probed: the still is the original JPEG the
  result already shows, and the motion is Native's h264 stream on a host the
  LivePhoto feature verified for browsers.
- 照片和视频分开 (``separate``): photo cards, and each motion as a video card.
- 只保存照片 (``still_only``): the photos only.

A pair the LivePhoto shape cannot serve leaves the whole note separate, so
every file stays downloadable.
"""
from dataclasses import replace
import hashlib
import logging

from ..assets import MediaAsset, ParseResult
from ..common import clean_filename
from ..pipeline import Kind
from ..provider_manifest import build_provider_manifest_token
from ..rich_media import (
    RICH_MEDIA_SCHEMA_VERSION,
    RichMediaAssetDescriptorV1,
    RichMediaContractV1,
    RichMediaGroupMemberV1,
    RichMediaGroupV1,
)

logger = logging.getLogger(__name__)

LIVEPHOTO, SEPARATE, STILL_ONLY = "livephoto", "separate", "still_only"
MODES = frozenset({LIVEPHOTO, SEPARATE, STILL_ONLY})
# The codec the LivePhoto feature reviewed for browser playback (xhs_livephoto_candidate_locator).
REVIEWED_MOTION_SOURCE = ".stream.h264["
MOTION_HOST_CLASSES = frozenset({"motion_versioned", "motion_backup"})


def live_photo_mode(route):
    mode = getattr(route, "live_photo_mode", LIVEPHOTO)
    return mode if mode in MODES else LIVEPHOTO


def _pairs(work):
    """(still item, motion item) for each live photo group, in still order."""
    items = {item.item_id: item for item in work.items}
    pairs = []
    for group in work.groups:
        members = [items[item_id] for item_id in group.item_ids if item_id in items]
        still = next((item for item in members if item.kind is Kind.IMAGE), None)
        motion = next((item for item in members if item.kind is Kind.VIDEO), None)
        if group.kind == "live_photo" and still is not None and motion is not None:
            pairs.append((still, motion))
    return sorted(pairs, key=lambda pair: pair[0].index)


def without_motions(work):
    """只保存照片: the live photos' motions leave; every photo stays."""
    motions = {motion.item_id for _still, motion in _pairs(work)}
    if not motions:
        return work
    return replace(work, items=tuple(item for item in work.items if item.item_id not in motions), groups=())


def reviewed_motion(work, item):
    """The motion's first h264 MP4 on a browser-verified host (master before backups)."""
    from ..platforms.xhs_livephoto_browser_admission import (
        XhsLivePhotoBrowserAdmissionError,
        validate_xhs_livephoto_browser_admitted_url,
    )

    for variant in item.variants:
        source = work.field_sources.get(f"items.{item.item_id}.variants.{variant.variant_id}.url", "")
        if REVIEWED_MOTION_SOURCE not in source or variant.container != "mp4":
            continue
        try:
            host_class = validate_xhs_livephoto_browser_admitted_url(variant.url, member_role="motion")
        except XhsLivePhotoBrowserAdmissionError:
            continue
        if host_class in MOTION_HOST_CLASSES:
            return variant
    return None


def _not_after(*variants):
    expiries = [variant.expires_at.timestamp() for variant in variants if variant and variant.expires_at]
    return min(expiries) if expiries else None


def _asset(media_type, url, asset_id, ordinal, **fields):
    from ..platforms.xhs_livephoto_builder import XHS_LIVEPHOTO_PROVIDER

    cache_key = f"xiaohongshu:livephoto:{ordinal}"
    token = build_provider_manifest_token(
        asset_id=asset_id, provider=XHS_LIVEPHOTO_PROVIDER, provider_resource_id=asset_id,
        provider_cache_key=cache_key, url=url, media_type=media_type, source_platform="xiaohongshu")
    return MediaAsset(media_type=media_type, url=url, platform="xiaohongshu", headers={}, asset_id=asset_id,
                      provider=XHS_LIVEPHOTO_PROVIDER, provider_resource_id=asset_id, provider_cache_key=cache_key,
                      provider_manifest_token=token, source_platform="xiaohongshu", **fields)


def as_live_photos(work, result):
    """``result`` (the separate shape) in the LivePhoto shape, or None to keep it separate."""
    from ..platforms.xhs_livephoto_asset_spec_mapper import _asset_id, _group_id, _regular_role, _scope_key

    stills = sorted((item for item in work.items if item.kind is Kind.IMAGE), key=lambda item: item.index)
    shown = result.image_assets
    pairs = _pairs(work)
    if not pairs or len(stills) != len(shown):
        return None
    motions = {}
    for still, motion in pairs:
        variant = reviewed_motion(work, motion)
        if variant is None:
            logger.info("unified_xhs_livephoto_separate reason=motion_not_reviewed")
            return None
        motions[still.item_id] = variant

    scope = _scope_key(work.work_id)
    post_type = "live_photo" if len(motions) == len(stills) else "multimedia"
    assets, descriptors, groups, records = [], [], [], []
    for ordinal, (item, still) in enumerate(zip(stills, shown)):
        motion = motions.get(item.item_id)
        label = clean_filename(f"{result.base_filename}_{ordinal + 1:02d}")
        still_id = _asset_id(scope, ordinal, "still")
        assets.append(_asset(
            "image", still.url, still_id, ordinal, preview_url=still.preview_url or still.url,
            filename=f"{label}_still.{still.ext}", ext=still.ext,
            quality_label="实况照片" if motion else "原始图片", media_host_suffixes=still.media_host_suffixes))
        descriptors.append(RichMediaAssetDescriptorV1(
            asset_id=still_id, media_type="image", extension=still.ext,
            role="still" if motion else _regular_role(post_type=post_type)))
        record = {"source_item_ordinal": ordinal, "kind": "regular_image", "still_asset_id": still_id,
                  "motion_asset_id": "", "group_id": "", "motion_source_kind": "",
                  "media_not_after": _not_after(item.default_variant)}
        status = "not_applicable"
        if motion:
            motion_id, group_id = _asset_id(scope, ordinal, "motion"), _group_id(scope, ordinal)
            assets.append(_asset(
                "video", motion.url, motion_id, ordinal, filename=f"{label}_motion.mp4", ext="mp4",
                cover_url=still.url, cover_preview_url=still.preview_url or still.url, quality_label="实况视频",
                size_bytes=int(motion.size_bytes or 0), media_host_suffixes=("xhscdn.com",)))
            descriptors.append(RichMediaAssetDescriptorV1(
                asset_id=motion_id, media_type="video", extension="mp4", role="motion"))
            groups.append(RichMediaGroupV1(
                group_id=group_id, relationship_type="live_photo",
                members=(RichMediaGroupMemberV1(asset_id=still_id, role="still"),
                         RichMediaGroupMemberV1(asset_id=motion_id, role="motion"))))
            record.update(kind="live_photo", motion_asset_id=motion_id, group_id=group_id,
                          motion_source_kind="unified_native",
                          media_not_after=_not_after(item.default_variant, motion))
            status = "ready"
        for prefix in ("delivery", "playback", "pair_download"):
            record[f"{prefix}_status"], record[f"{prefix}_error_code"] = status, ""
        records.append(record)

    metadata = dict(result.metadata or {})
    metadata.update({
        "xhs_live_photo_mode": LIVEPHOTO,
        "xhs_livephoto_preview_status": "enhanced",
        "xhs_livephoto_source_item_count": len(stills),
        "xhs_livephoto_livephoto_count": len(motions),
        "xhs_livephoto_regular_count": len(stills) - len(motions),
        "xhs_livephoto_bounded_probe_bytes": 0,
        "xhs_livephoto_full_media_bytes": 0,
        "xhs_livephoto_source_identity": hashlib.sha256(work.work_id.encode("utf-8")).hexdigest(),
        "xhs_livephoto_delivery_ready_count": len(motions),
        "xhs_livephoto_delivery_unavailable_count": 0,
        "xhs_livephoto_items": records,
    })
    rich_media = RichMediaContractV1(
        schema_version=RICH_MEDIA_SCHEMA_VERSION, post_type=post_type, assets=tuple(descriptors),
        groups=tuple(groups), content_blocks=())
    return ParseResult(platform="xiaohongshu", post_type=post_type, base_filename=result.base_filename,
                       assets=tuple(assets), metadata=metadata, rich_media=rich_media)
