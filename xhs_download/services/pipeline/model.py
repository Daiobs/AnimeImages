"""Unified result model (docs/architecture/unified-pipeline.md §1).

Presentation, delivery and filenames read only these objects. Only Provider
adapters read raw upstream output.
"""
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from types import MappingProxyType
from typing import Mapping


class Kind(str, Enum):
    IMAGE = "image"
    VIDEO = "video"
    AUDIO = "audio"


class Tracks(str, Enum):
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


@dataclass(frozen=True)
class Variant:
    variant_id: str
    url: str
    container: str = ""
    tracks: Tracks = Tracks.AV
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
    expires_at: datetime | None = None
    host_policy: tuple[str, ...] = ()

    @property
    def display_label(self):
        if self.quality_label:
            return self.quality_label
        if self.height:
            return f"{self.height}p"
        return DEFAULT_QUALITY_LABEL

    def rank_key(self):
        """Higher is better: original, then height, bitrate, size (INV-03)."""
        return (
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
        return max(keys, default=(0, 0, 0, 0))


def _is_placeholder(value, placeholders):
    return value.strip().casefold() in placeholders


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
