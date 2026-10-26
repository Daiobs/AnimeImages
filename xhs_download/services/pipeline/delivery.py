"""Delivery rule table (docs/architecture/unified-pipeline.md §3).

Only variant facts are inputs. Platform, Provider and resource IDs are not
parameters, so they cannot change the outcome.
"""
from dataclasses import dataclass
from enum import Enum

from .model import Fetchable, Kind, Tracks


class Method(str, Enum):
    SERVER_MERGE = "server_merge"
    SERVER_ATTACHMENT = "server_attachment"
    BROWSER_ARCHIVE = "browser_archive"
    BROWSER_STREAMING = "browser_streaming"
    BROWSER_BLOB = "browser_blob"
    BROWSER_DIRECT = "browser_direct"
    NONE = "none"


SERVER_METHODS = frozenset({Method.SERVER_MERGE, Method.SERVER_ATTACHMENT})

# Same cap as MAX_BROWSER_BLOB_BYTES in static/js/ordinary_media_download.js.
BLOB_LIMIT_BYTES = 1024 * 1024 * 1024


@dataclass(frozen=True)
class Delivery:
    rule: int
    method: Method
    fallback: Method | None = None
    archive: bool = False
    reason: str = ""

    @property
    def counts_against_quota(self):
        return self.method in SERVER_METHODS

    @property
    def fallback_counts_against_quota(self):
        return self.fallback in SERVER_METHODS


def _undeliverable_reason(variant):
    if variant.drm:
        return "DRM_PROTECTED"
    if variant.segmented:
        return "SEGMENTED_STREAM"
    if variant.request_method != "GET":
        return "UNSUPPORTED_REQUEST"
    return "NEEDS_AUDIO_MERGE"


def _needs_merge(variant):
    return variant.tracks is Tracks.VIDEO_ONLY and bool(variant.merge_with)


def decide_delivery(kind, variant, *, allow_server_fallback=True,
                    blob_limit_bytes=BLOB_LIMIT_BYTES):
    """Deliver one selected variant; the first matching rule wins."""
    if not variant.deliverable:
        return Delivery(0, Method.NONE, reason=_undeliverable_reason(variant))
    if _needs_merge(variant):
        return Delivery(1, Method.SERVER_MERGE)
    if variant.required_headers:
        return Delivery(2, Method.SERVER_ATTACHMENT)
    if kind in (Kind.VIDEO, Kind.AUDIO) and variant.browser_fetchable is Fetchable.TRUE:
        blob = variant.size_bytes is None or variant.size_bytes <= blob_limit_bytes
        return Delivery(4, Method.BROWSER_STREAMING, Method.BROWSER_BLOB if blob else None)
    if kind is Kind.IMAGE:
        return Delivery(5, Method.BROWSER_DIRECT)
    if variant.browser_fetchable is Fetchable.UNKNOWN:
        fallback = Method.SERVER_ATTACHMENT if allow_server_fallback else None
        return Delivery(6, Method.BROWSER_STREAMING, fallback)
    return Delivery(7, Method.NONE, reason="BROWSER_CANNOT_READ")


def decide_archive(entries, *, allow_server_fallback=True):
    """Save several items as one ZIP: (kind, variant) for each selected item.

    The browser packs it; the server packs only what the browser cannot read.
    """
    if len(entries) < 2:
        raise ValueError("an archive needs at least two items")
    variants = [variant for _kind, variant in entries]
    blocked = next((variant for variant in variants if not variant.deliverable), None)
    if blocked is not None:
        return Delivery(0, Method.NONE, reason=_undeliverable_reason(blocked))
    if any(_needs_merge(variant) for variant in variants):
        return Delivery(1, Method.NONE, reason="ARCHIVE_NEEDS_MERGE")
    if any(variant.required_headers for variant in variants):
        return Delivery(2, Method.SERVER_ATTACHMENT, archive=True)
    if any(variant.browser_fetchable is Fetchable.FALSE for variant in variants):
        return Delivery(3, Method.SERVER_ATTACHMENT, archive=True)
    if any(variant.browser_fetchable is Fetchable.UNKNOWN for variant in variants):
        fallback = Method.SERVER_ATTACHMENT if allow_server_fallback else None
        return Delivery(3, Method.BROWSER_ARCHIVE, fallback, archive=True)
    return Delivery(3, Method.BROWSER_ARCHIVE, archive=True)
