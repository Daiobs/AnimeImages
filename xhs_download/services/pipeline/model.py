"""Unified result model (docs/architecture/unified-pipeline.md §1).

Presentation, delivery and filenames read only these objects. Only Provider
adapters read raw upstream output.
"""
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
import re
from types import MappingProxyType
from typing import Mapping
from urllib.parse import urlsplit


class Kind(str, Enum):
    IMAGE = "image"
    VIDEO = "video"
    AUDIO = "audio"


class Tracks(str, Enum):
    # Not stated by the upstream or the platform: delivered as a complete file,
    # never merged. Images keep it; tracks only matter for video and audio.
    UNKNOWN = "unknown"
    AV = "av"
    VIDEO_ONLY = "video_only"
    AUDIO_ONLY = "audio_only"


class Fetchable(str, Enum):
    TRUE = "true"
    FALSE = "false"
    UNKNOWN = "unknown"


class Completeness(str, Enum):
    FINAL = "final"
    PROVISIONAL = "provisional"


DEFAULT_QUALITY_LABEL = "默认清晰度"

# Values that name a media type or nothing at all, not a quality or a title.
PLACEHOLDER_LABELS = frozenset({
    "video", "image", "audio", "media", "file", "default", "unknown",
    "none", "null", "n/a", "视频", "图片", "音频", "原始文件",
})
PLACEHOLDER_TITLES = PLACEHOLDER_LABELS | frozenset({"untitled", "无标题", "作品"})

# Manifests and segments; a variant at such a path must say it is segmented.
SEGMENTED_PATH_SUFFIXES = (".m3u8", ".mpd", ".m4s")

# A Provider's own resolution label such as 1080P, 2160P or 1080P+.
_RESOLUTION_LABEL = re.compile(r"\s*(\d{3,4})[pP](?![A-Za-z])")


def height_from_label(label):
    """Height a Provider declared in its label; None for 原画, 高清, 4K and the like."""
    match = _RESOLUTION_LABEL.match(label or "")
    if not match:
        return None
    height = int(match.group(1))
    return height if 144 <= height <= 4320 else None


@dataclass(frozen=True)
class Variant:
    variant_id: str
    url: str
    container: str = ""
    tracks: Tracks = Tracks.UNKNOWN
    height: int | None = None
    width: int | None = None
    fps: float | None = None
    bitrate: int | None = None
    quality_label: str = ""
    is_original: bool = False
    size_bytes: int | None = None
    size_exact: bool = False
    merge_with: str = ""
    required_headers: tuple[str, ...] = ()
    browser_fetchable: Fetchable = Fetchable.UNKNOWN
    drm: bool = False
    segmented: bool = False
    expires_at: datetime | None = None
    host_policy: tuple[str, ...] = ()

    @property
    def display_label(self):
        if self.quality_label:
            return self.quality_label
        if self.height:
            return f"{self.height}p"
        return DEFAULT_QUALITY_LABEL

    @property
    def deliverable(self):
        """DRM and segmented streams are listed but never downloaded (D-02)."""
        return not (self.drm or self.segmented)

    def rank_key(self):
        """Higher is better: deliverable, original, height, bitrate, size (INV-03)."""
        return (
            1 if self.deliverable else 0,
            1 if self.is_original else 0,
            self.height or 0,
            self.bitrate or 0,
            self.size_bytes or 0,
        )


@dataclass(frozen=True)
class Item:
    item_id: str
    kind: Kind
    index: int
    variants: tuple[Variant, ...]

    def default_candidates(self):
        return tuple(
            variant for variant in self.variants
            if not (variant.tracks is Tracks.VIDEO_ONLY and not variant.merge_with)
            and not (self.kind is Kind.VIDEO and variant.tracks is Tracks.AUDIO_ONLY)
        )

    @property
    def default_variant(self):
        candidates = self.default_candidates()
        if not candidates:
            return None
        # max() keeps the first of equal keys, so ties follow adapter order.
        return max(candidates, key=Variant.rank_key)

    def variant(self, variant_id):
        for variant in self.variants:
            if variant.variant_id == variant_id:
                return variant
        return None


@dataclass(frozen=True)
class Group:
    kind: str
    item_ids: tuple[str, ...]


@dataclass(frozen=True)
class Work:
    source_url: str
    work_id: str
    items: tuple[Item, ...]
    platform: str = ""
    title: str = ""
    description: str = ""
    topics: tuple[str, ...] = ()
    author: str = ""
    cover_url: str = ""
    duration_seconds: float | None = None
    groups: tuple[Group, ...] = ()
    provider: str = ""
    completeness: Completeness = Completeness.FINAL
    field_sources: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))

    def item(self, item_id):
        for item in self.items:
            if item.item_id == item_id:
                return item
        return None

    def find_variant(self, variant_id):
        for item in self.items:
            variant = item.variant(variant_id)
            if variant is not None:
                return item, variant
        return None, None

    def rank_key(self):
        """Quality of the best default variant; used to decide upgrades."""
        keys = [item.default_variant.rank_key() for item in self.items if item.default_variant]
        return max(keys, default=(0, 0, 0, 0, 0))


def _is_placeholder(value, placeholders):
    return value.strip().casefold() in placeholders


def _segmented_path(url):
    try:
        path = urlsplit(url or "").path
    except ValueError:
        return False
    return path.lower().endswith(SEGMENTED_PATH_SUFFIXES)


def validate_work(work, *, platform_names=()):
    """Return the §1.4 violations as safe codes; an empty tuple means valid."""
    violations = []
    if not work.work_id:
        violations.append("missing_work_id")
    if not work.items:
        violations.append("no_items")
    title_placeholders = PLACEHOLDER_TITLES | {
        name.casefold() for name in (work.platform, *platform_names) if name
    }
    if work.title and _is_placeholder(work.title, title_placeholders):
        violations.append("placeholder_title")

    item_ids = set()
    variant_tracks = {}
    for item in work.items:
        if item.item_id in item_ids:
            violations.append("duplicate_item_id")
        item_ids.add(item.item_id)
        if item.index < 1:
            violations.append("item_index_below_one")
        if not item.variants:
            violations.append("item_without_variants")
        elif item.default_variant is None:
            violations.append("item_without_default")
        for variant in item.variants:
            if variant.variant_id in variant_tracks:
                violations.append("duplicate_variant_id")
            variant_tracks[variant.variant_id] = variant.tracks
            if not variant.url:
                violations.append("variant_without_url")
            if variant.quality_label and _is_placeholder(variant.quality_label, PLACEHOLDER_LABELS):
                violations.append("placeholder_quality_label")
            if variant.size_bytes is not None and variant.size_bytes <= 0:
                violations.append("placeholder_size")
            if not variant.segmented and _segmented_path(variant.url):
                violations.append("segmented_url_not_marked")

    for item in work.items:
        for variant in item.variants:
            if variant.tracks is not Tracks.VIDEO_ONLY or not variant.merge_with:
                continue
            if variant_tracks.get(variant.merge_with) is not Tracks.AUDIO_ONLY:
                violations.append("merge_partner_not_audio")

    grouped = set()
    for group in work.groups:
        for item_id in group.item_ids:
            if item_id not in item_ids:
                violations.append("group_unknown_item")
            if item_id in grouped:
                violations.append("item_in_two_groups")
            grouped.add(item_id)
    return tuple(dict.fromkeys(violations))


def fact_gaps(work):
    """Safe codes for facts the result does not state; health tests count them."""
    gaps = []
    if any(
        item.kind is not Kind.IMAGE and item.default_variant is not None
        and item.default_variant.tracks is Tracks.UNKNOWN
        for item in work.items
    ):
        gaps.append("TRACKS_UNKNOWN")
    return tuple(gaps)


COVERAGE_FIELDS = ("title", "cover", "quality", "size")


def field_coverage(work):
    """Who supplied title/cover/quality/size for the default result (A2)."""
    first = next((item.default_variant for item in work.items if item.default_variant), None)
    present = {
        "title": bool(work.title),
        "cover": bool(work.cover_url),
        "quality": bool(first and (first.quality_label or first.height)),
        "size": bool(first and first.size_bytes),
    }
    return {
        name: (work.field_sources.get(name, "provider") if present[name] else None)
        for name in COVERAGE_FIELDS
    }
