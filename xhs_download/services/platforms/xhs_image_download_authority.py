"""Shared fail-closed authority for downloadable XHS image assets."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from ..common import host_matches, normalize_text
from ..provider_manifest import provider_manifest_is_valid
from .xhs_parsehub_images import (
    PARSEHUB_XHS_IMAGE_EXTENSIONS,
    PARSEHUB_XHS_PROVIDER_ID,
    XhsParseHubImageError,
    validate_xhs_parsehub_raw_url,
)


XHS_IMAGE_MEDIA_HOST_SUFFIXES = ("xiaohongshu.com", "xhscdn.com")
XHS_DOWNLOADABLE_IMAGE_EXTENSIONS = PARSEHUB_XHS_IMAGE_EXTENSIONS
XHS_IMAGE_EMPTY_RESOURCE_VALUES = {
    "",
    ".",
    "..",
    "null",
    "none",
    "undefined",
}
XHS_IMAGE_NON_NOTE_PATH_RE = re.compile(
    r"(?:^|[/_.-])(?:avatar|favicon|logo|icon|sprite|emoji|qrcode)(?:[/_.-]|$)",
    re.IGNORECASE,
)
XHS_LIVEPHOTO_NATIVE_IMAGE_PROVIDER = "xhs_livephoto_preview_v1"


class XhsDownloadableImageAuthorityError(ValueError):
    def __init__(self, code: str):
        self.code = str(code or "XHS_IMAGE_AUTHORITY_REJECTED")[:64]
        super().__init__(self.code)


@dataclass(frozen=True)
class XhsImageCandidateAuthorityV1:
    value: str
    candidate_class: str
    scheme: str


@dataclass(frozen=True)
class XhsDownloadableImageAuthorityV1:
    provider_class: str
    candidate_class: str
    extension: str
    browser_magic_required: bool = True


def classify_xhs_image_candidate(value: Any) -> XhsImageCandidateAuthorityV1 | None:
    """Classify the existing ordinary-image candidate policy without broadening it."""

    normalized = normalize_text(value).strip()
    if normalized.lower() in XHS_IMAGE_EMPTY_RESOURCE_VALUES:
        return None
    if normalized.startswith("//"):
        normalized = f"https:{normalized}"
    try:
        parsed = urlsplit(normalized)
        port = parsed.port
    except (TypeError, ValueError):
        return None
    hostname = str(parsed.hostname or "").lower()
    if (
        parsed.scheme not in {"http", "https"}
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 80, 443}
        or not host_matches(hostname, XHS_IMAGE_MEDIA_HOST_SUFFIXES)
    ):
        return None

    path_parts = tuple(part.strip() for part in parsed.path.split("/") if part.strip())
    if not path_parts:
        return None
    path_lower = parsed.path.lower()
    if XHS_IMAGE_NON_NOTE_PATH_RE.search(path_lower):
        return None
    if path_lower.endswith((".js", ".css", ".zip")):
        return None
    resource_key = path_parts[-1]
    if resource_key.lower() in XHS_IMAGE_EMPTY_RESOURCE_VALUES | {
        "notes_pre_post",
        "spectrum",
    }:
        return None

    has_known_resource_path = any(
        part in {"notes_pre_post", "spectrum"} and index + 1 < len(path_parts)
        for index, part in enumerate(path_parts)
    )
    if hostname == "ci.xiaohongshu.com":
        candidate_class = "native_ci_exact"
    elif hostname.startswith("sns-webpic"):
        candidate_class = "native_sns_webpic"
    elif hostname.startswith("sns-img"):
        candidate_class = "native_sns_img"
    elif has_known_resource_path:
        candidate_class = "native_known_resource_path"
    else:
        return None
    return XhsImageCandidateAuthorityV1(
        value=normalized,
        candidate_class=candidate_class,
        scheme=parsed.scheme,
    )


def validate_xhs_downloadable_image_asset(item: Any) -> XhsDownloadableImageAuthorityV1:
    """Validate one exact manifest-bound image for ordinary or pair download."""

    if not isinstance(item, dict):
        raise XhsDownloadableImageAuthorityError("XHS_IMAGE_ITEM_REJECTED")
    if (
        str(item.get("type") or "").lower() != "image"
        or str(item.get("source_platform") or "").lower() != "xiaohongshu"
        or not provider_manifest_is_valid(item)
    ):
        raise XhsDownloadableImageAuthorityError("XHS_IMAGE_MANIFEST_REJECTED")

    extension = str(item.get("extension") or "").lower().lstrip(".")
    if extension == "jpeg":
        extension = "jpg"
    if extension not in XHS_DOWNLOADABLE_IMAGE_EXTENSIONS:
        raise XhsDownloadableImageAuthorityError("XHS_IMAGE_FORMAT_REJECTED")

    provider = str(item.get("provider") or "")
    url = str(item.get("url") or "")
    if provider == PARSEHUB_XHS_PROVIDER_ID:
        try:
            validate_xhs_parsehub_raw_url(url)
        except XhsParseHubImageError as error:
            raise XhsDownloadableImageAuthorityError(
                "XHS_IMAGE_PARSEHUB_URL_REJECTED"
            ) from error
        provider_class = "parsehub_raw"
        candidate_class = "parsehub_raw_exact"
    elif provider == XHS_LIVEPHOTO_NATIVE_IMAGE_PROVIDER:
        candidate = classify_xhs_image_candidate(url)
        if candidate is None or candidate.scheme != "https":
            raise XhsDownloadableImageAuthorityError(
                "XHS_IMAGE_NATIVE_URL_REJECTED"
            )
        provider_class = "native"
        candidate_class = candidate.candidate_class
    else:
        raise XhsDownloadableImageAuthorityError("XHS_IMAGE_PROVIDER_REJECTED")

    return XhsDownloadableImageAuthorityV1(
        provider_class=provider_class,
        candidate_class=candidate_class,
        extension=extension,
    )


__all__ = (
    "XHS_DOWNLOADABLE_IMAGE_EXTENSIONS",
    "XHS_IMAGE_EMPTY_RESOURCE_VALUES",
    "XHS_IMAGE_MEDIA_HOST_SUFFIXES",
    "XHS_IMAGE_NON_NOTE_PATH_RE",
    "XHS_LIVEPHOTO_NATIVE_IMAGE_PROVIDER",
    "XhsDownloadableImageAuthorityError",
    "XhsDownloadableImageAuthorityV1",
    "XhsImageCandidateAuthorityV1",
    "classify_xhs_image_candidate",
    "validate_xhs_downloadable_image_asset",
)
