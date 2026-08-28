"""Fail-closed XHS preview candidates shared by Single and Batch surfaces."""

from __future__ import annotations

import hashlib
import re
import secrets
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from django.core.cache import cache

from ..assets import MediaAsset
from ..common import normalize_text


XHS_PREVIEW_DESCRIPTOR_SCHEMA_VERSION = 1
XHS_PREVIEW_DESCRIPTOR_TTL_SECONDS = 10 * 60
XHS_PREVIEW_MAX_CANDIDATES = 3
XHS_PREVIEW_TOTAL_TIMEOUT_SECONDS = 8.0

_PREVIEW_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{32,64}$")
_NON_NOTE_PATH_RE = re.compile(
    r"(?:^|[/_.-])(?:avatar|favicon|logo|icon|sprite|emoji|qrcode)(?:[/_.-]|$)",
    re.IGNORECASE,
)
_BROWSER_RASTER_EXTENSIONS = frozenset(
    {"avif", "gif", "jpeg", "jpg", "png", "webp"}
)
_HEIC_EXTENSIONS = frozenset({"heic", "heif"})
_FIXED_NATIVE_PREVIEW_HOSTS = frozenset(
    {
        "ci.xiaohongshu.com",
        "sns-img.xhscdn.com",
        "sns-img-hw.xhscdn.com",
        "sns-img-qc.xhscdn.com",
        "sns-webpic-hw.xhscdn.com",
        "sns-webpic-qc.xhscdn.com",
    }
)
_PARSEHUB_RAW_HOST = "sns-img-hw.xhscdn.com"
_PARSEHUB_PROVIDER = "parsehub_xhs_images"
_ALLOWED_SURFACES = frozenset({"single", "staff_single", "batch"})
_ALLOWED_CANDIDATE_CLASSES = frozenset(
    {
        "existing_compatible_preview",
        "item_local_preview",
        "parsehub_raw_raster",
        "reviewed_derived_preview",
        "native_raster_fallback",
    }
)


@dataclass(frozen=True)
class XhsPreviewSourceV1:
    value: str
    source_kind: str
    authority_class: str


@dataclass(frozen=True)
class XhsPreviewCandidateV1:
    value: str
    candidate_class: str
    authority_class: str


@dataclass(frozen=True)
class XhsPreviewDescriptorV1:
    surface: str
    source_ordinal: int
    candidates: tuple[XhsPreviewCandidateV1, ...]


class XhsBoundPreviewUrl(str):
    """String-compatible preview URL carrying request-local same-item evidence."""

    def __new__(cls, value: str, item_sources: Iterable[XhsPreviewSourceV1]):
        instance = super().__new__(cls, value)
        instance.item_sources = tuple(item_sources)
        return instance


def _normalized_https_url(value: Any) -> str:
    normalized = normalize_text(value).strip()
    if normalized.startswith("//"):
        normalized = f"https:{normalized}"
    try:
        parsed = urlsplit(normalized)
        port = parsed.port
    except (TypeError, ValueError):
        return ""
    hostname = str(parsed.hostname or "").lower()
    if (
        parsed.scheme not in {"http", "https"}
        or hostname not in _FIXED_NATIVE_PREVIEW_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or parsed.fragment
        or not parsed.path.strip("/")
        or _NON_NOTE_PATH_RE.search(parsed.path)
    ):
        return ""
    path_parts = tuple(part for part in parsed.path.split("/") if part)
    if any(part in {".", ".."} for part in path_parts):
        return ""
    return parsed._replace(scheme="https", netloc=hostname).geturl()


def _known_resource_path(value: str) -> str:
    parsed = urlsplit(value)
    path_parts = tuple(part for part in parsed.path.split("/") if part)
    for index, part in enumerate(path_parts):
        if part in {"notes_pre_post", "spectrum"} and index + 1 < len(path_parts):
            return "/".join(path_parts[index:])
    return ""


def _path_extension(value: str) -> str:
    path = urlsplit(value).path.rsplit("/", 1)[-1]
    if "." not in path:
        return ""
    extension = path.rsplit(".", 1)[-1].lower()
    return "jpg" if extension == "jpeg" else extension


def classify_xhs_preview_url(value: Any) -> tuple[str, str] | None:
    """Return a normalized URL and a fixed reviewed authority class."""

    normalized = _normalized_https_url(value)
    if not normalized:
        return None
    parsed = urlsplit(normalized)
    hostname = str(parsed.hostname or "").lower()
    extension = _path_extension(normalized)
    known_resource = _known_resource_path(normalized)
    has_transform = "imageview" in parsed.query.lower()

    if hostname == "ci.xiaohongshu.com":
        if known_resource:
            authority = "reviewed_ci_preview" if has_transform else "reviewed_ci_resource"
        elif extension in _BROWSER_RASTER_EXTENSIONS:
            authority = "reviewed_ci_raster"
        else:
            # A bare CI resource plus a synthesized imageView query caused the
            # partial-preview regression. It is not an admitted preview class.
            return None
    elif hostname == _PARSEHUB_RAW_HOST and not parsed.query:
        if extension not in _BROWSER_RASTER_EXTENSIONS | _HEIC_EXTENSIONS:
            return None
        authority = "parsehub_raw"
    elif hostname.startswith(("sns-webpic-", "sns-img-")) or hostname == "sns-img.xhscdn.com":
        path_parts = tuple(part for part in parsed.path.split("/") if part)
        if has_transform:
            authority = "native_explicit_preview"
        elif known_resource:
            authority = "native_known_resource"
        elif extension in _BROWSER_RASTER_EXTENSIONS | _HEIC_EXTENSIONS:
            authority = "native_raster"
        elif len(path_parts) >= 2 and len(path_parts[-1]) >= 8:
            authority = "native_item_resource"
        else:
            return None
    else:
        return None
    return normalized, authority


def _candidate_class_matches_authority(
    candidate_class: str,
    authority_class: str,
) -> bool:
    if candidate_class == "parsehub_raw_raster":
        return authority_class == "parsehub_raw"
    if authority_class == "parsehub_raw":
        return False
    if candidate_class == "reviewed_derived_preview":
        return authority_class == "reviewed_ci_preview"
    if candidate_class == "native_raster_fallback":
        return authority_class in {
            "native_raster",
            "reviewed_ci_raster",
            "reviewed_ci_preview",
        }
    return candidate_class in {
        "existing_compatible_preview",
        "item_local_preview",
    }


def _iter_candidate_urls(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
        return
    if isinstance(value, Mapping):
        for key in ("url", "urlDefault", "url_default"):
            yield from _iter_candidate_urls(value.get(key))
        return
    if isinstance(value, (list, tuple)):
        for child in value:
            yield from _iter_candidate_urls(child)


def build_xhs_item_preview_sources(item: Mapping[str, Any] | Any) -> tuple[XhsPreviewSourceV1, ...]:
    """Extract only direct, same-item URL evidence from one selected-note item."""

    if not isinstance(item, Mapping):
        return ()
    raw_sources: list[tuple[str, Any]] = [("url_default", item.get("urlDefault"))]
    info_list = item.get("infoList")
    if isinstance(info_list, (list, tuple)):
        for info in info_list:
            if not isinstance(info, Mapping):
                continue
            scene = str(info.get("imageScene") or "unknown").strip().lower()[:24]
            raw_sources.append((f"info_{scene}", info.get("url")))

    sources = []
    seen = set()
    for source_kind, raw_value in raw_sources:
        for value in _iter_candidate_urls(raw_value):
            classified = classify_xhs_preview_url(value)
            if classified is None:
                continue
            normalized, authority_class = classified
            if normalized in seen:
                continue
            seen.add(normalized)
            sources.append(
                XhsPreviewSourceV1(
                    value=normalized,
                    source_kind=source_kind,
                    authority_class=authority_class,
                )
            )
    return tuple(sources[:XHS_PREVIEW_MAX_CANDIDATES])


def build_xhs_preview_source_sets(
    image_items: Iterable[Mapping[str, Any]],
) -> tuple[tuple[XhsPreviewSourceV1, ...], ...]:
    return tuple(build_xhs_item_preview_sources(item) for item in image_items)


def bind_xhs_preview_sources(
    value: str,
    item_sources: Iterable[XhsPreviewSourceV1],
) -> str:
    item_sources = tuple(item_sources)
    return XhsBoundPreviewUrl(value, item_sources) if value and item_sources else value


def _derived_preview(value: Any, *, width: int) -> XhsPreviewCandidateV1 | None:
    classified = classify_xhs_preview_url(value)
    if classified is None:
        return None
    normalized, _authority = classified
    resource_path = _known_resource_path(normalized)
    if not resource_path:
        return None
    width = max(320, min(int(width), 2400))
    derived = (
        f"https://ci.xiaohongshu.com/{resource_path}"
        f"?imageView2/2/w/{width}/format/webp"
    )
    return XhsPreviewCandidateV1(
        value=derived,
        candidate_class="reviewed_derived_preview",
        authority_class="reviewed_ci_preview",
    )


def _safe_original_candidate(asset: MediaAsset) -> XhsPreviewCandidateV1 | None:
    classified = classify_xhs_preview_url(asset.url)
    if classified is None:
        return None
    normalized, authority_class = classified
    extension = str(asset.ext or _path_extension(normalized)).lower().lstrip(".")
    extension = "jpg" if extension == "jpeg" else extension
    if extension not in _BROWSER_RASTER_EXTENSIONS:
        return None
    if asset.provider == _PARSEHUB_PROVIDER:
        if authority_class != "parsehub_raw":
            return None
        candidate_class = "parsehub_raw_raster"
    elif authority_class in {
        "native_raster",
        "reviewed_ci_raster",
        "reviewed_ci_preview",
    }:
        candidate_class = "native_raster_fallback"
    else:
        return None
    return XhsPreviewCandidateV1(
        value=normalized,
        candidate_class=candidate_class,
        authority_class=authority_class,
    )


def resolve_xhs_preview_candidates(
    asset: MediaAsset,
    *,
    item_sources: Iterable[XhsPreviewSourceV1] = (),
    width: int,
) -> tuple[XhsPreviewCandidateV1, ...]:
    """Resolve at most three same-item preview candidates without URL guessing."""

    item_sources = tuple(item_sources)
    if not item_sources:
        item_sources = tuple(getattr(asset.preview_url, "item_sources", ()))
    candidates: list[XhsPreviewCandidateV1] = []
    seen = set()

    def add(candidate: XhsPreviewCandidateV1 | None):
        if candidate is None or candidate.value in seen:
            return
        classified = classify_xhs_preview_url(candidate.value)
        if classified is None:
            return
        normalized, authority_class = classified
        if not _candidate_class_matches_authority(
            candidate.candidate_class,
            authority_class,
        ):
            return
        seen.add(normalized)
        candidates.append(
            XhsPreviewCandidateV1(
                value=normalized,
                candidate_class=candidate.candidate_class,
                authority_class=authority_class,
            )
        )

    existing = classify_xhs_preview_url(asset.preview_url)
    if existing is not None:
        add(
            XhsPreviewCandidateV1(
                value=existing[0],
                candidate_class="existing_compatible_preview",
                authority_class=existing[1],
            )
        )

    original = _safe_original_candidate(asset)
    if asset.provider == _PARSEHUB_PROVIDER:
        add(original)

    for source in item_sources:
        if not isinstance(source, XhsPreviewSourceV1):
            continue
        add(
            XhsPreviewCandidateV1(
                value=source.value,
                candidate_class="item_local_preview",
                authority_class=source.authority_class,
            )
        )
        if len(candidates) >= XHS_PREVIEW_MAX_CANDIDATES:
            break

    if not candidates:
        add(_derived_preview(asset.url, width=width))
    if asset.provider != _PARSEHUB_PROVIDER:
        add(original)

    return tuple(candidates[:XHS_PREVIEW_MAX_CANDIDATES])


def _descriptor_cache_key(token: str) -> str:
    digest = hashlib.sha256(token.encode("ascii")).hexdigest()
    return f"xhs-preview:v1:{digest}"


def issue_xhs_preview_descriptor(
    candidates: Iterable[XhsPreviewCandidateV1],
    *,
    surface: str,
    source_ordinal: int,
) -> str:
    candidates = tuple(candidates)
    if (
        surface not in _ALLOWED_SURFACES
        or type(source_ordinal) is not int
        or not 0 <= source_ordinal < 100
        or not 1 <= len(candidates) <= XHS_PREVIEW_MAX_CANDIDATES
    ):
        return ""
    serialized = []
    for candidate in candidates:
        classified = classify_xhs_preview_url(candidate.value)
        if (
            classified is None
            or candidate.candidate_class not in _ALLOWED_CANDIDATE_CLASSES
            or not _candidate_class_matches_authority(
                candidate.candidate_class,
                classified[1],
            )
        ):
            return ""
        serialized.append(
            {
                "url": classified[0],
                "candidate_class": candidate.candidate_class,
            }
        )
    token = secrets.token_urlsafe(32)
    payload = {
        "version": XHS_PREVIEW_DESCRIPTOR_SCHEMA_VERSION,
        "surface": surface,
        "source_ordinal": source_ordinal,
        "candidates": tuple(serialized),
    }
    try:
        stored = cache.set(
            _descriptor_cache_key(token),
            payload,
            timeout=XHS_PREVIEW_DESCRIPTOR_TTL_SECONDS,
        )
    except Exception:
        return ""
    if stored is False:
        return ""
    return token


def load_xhs_preview_descriptor(token: Any) -> XhsPreviewDescriptorV1 | None:
    token = str(token or "")
    if not _PREVIEW_TOKEN_RE.fullmatch(token):
        return None
    payload = cache.get(_descriptor_cache_key(token))
    if not isinstance(payload, Mapping):
        return None
    surface = str(payload.get("surface") or "")
    source_ordinal = payload.get("source_ordinal")
    raw_candidates = payload.get("candidates")
    if (
        payload.get("version") != XHS_PREVIEW_DESCRIPTOR_SCHEMA_VERSION
        or surface not in _ALLOWED_SURFACES
        or type(source_ordinal) is not int
        or not 0 <= source_ordinal < 100
        or not isinstance(raw_candidates, (list, tuple))
        or not 1 <= len(raw_candidates) <= XHS_PREVIEW_MAX_CANDIDATES
    ):
        return None
    candidates = []
    for raw_candidate in raw_candidates:
        if not isinstance(raw_candidate, Mapping):
            return None
        candidate_class = str(raw_candidate.get("candidate_class") or "")
        classified = classify_xhs_preview_url(raw_candidate.get("url"))
        if (
            candidate_class not in _ALLOWED_CANDIDATE_CLASSES
            or classified is None
            or not _candidate_class_matches_authority(
                candidate_class,
                classified[1],
            )
        ):
            return None
        candidates.append(
            XhsPreviewCandidateV1(
                value=classified[0],
                candidate_class=candidate_class,
                authority_class=classified[1],
            )
        )
    return XhsPreviewDescriptorV1(
        surface=surface,
        source_ordinal=source_ordinal,
        candidates=tuple(candidates),
    )


def xhs_preview_final_url_is_allowed(
    candidate: XhsPreviewCandidateV1,
    final_url: Any,
) -> bool:
    classified = classify_xhs_preview_url(final_url)
    if classified is None:
        return False
    normalized, authority_class = classified
    candidate_host = str(urlsplit(candidate.value).hostname or "").lower()
    final_host = str(urlsplit(normalized).hostname or "").lower()
    if not candidate_host or final_host != candidate_host:
        return False
    return _candidate_class_matches_authority(
        candidate.candidate_class,
        authority_class,
    )


__all__ = (
    "XHS_PREVIEW_DESCRIPTOR_TTL_SECONDS",
    "XHS_PREVIEW_MAX_CANDIDATES",
    "XHS_PREVIEW_TOTAL_TIMEOUT_SECONDS",
    "XhsPreviewCandidateV1",
    "XhsPreviewDescriptorV1",
    "XhsPreviewSourceV1",
    "bind_xhs_preview_sources",
    "build_xhs_item_preview_sources",
    "build_xhs_preview_source_sets",
    "classify_xhs_preview_url",
    "issue_xhs_preview_descriptor",
    "load_xhs_preview_descriptor",
    "resolve_xhs_preview_candidates",
    "xhs_preview_final_url_is_allowed",
)
