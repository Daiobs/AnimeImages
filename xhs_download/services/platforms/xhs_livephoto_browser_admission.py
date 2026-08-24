"""Dormant HTTPS admission for already-selected XHS LivePhoto candidates."""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from .xhs_livephoto_candidate_locator import XhsPrivateMediaCandidateV1


XHS_LIVEPHOTO_BROWSER_ADMISSION_SCHEMA_VERSION = "1.0"

_MEMBER_ROLES = frozenset({"still", "motion"})
_HOST_CLASSES = frozenset(
    {"still_exact", "motion_versioned", "motion_zl", "motion_backup"}
)
_MOTION_HOST_CLASSES = frozenset(
    {"motion_versioned", "motion_zl", "motion_backup"}
)
_STILL_FORMAT_CLASSES = frozenset({"jpeg", "webp"})
_MOTION_FORMAT_CLASSES = frozenset({"mp4"})
_STILL_HOST = "sns-webpic-qc.xhscdn.com"
_MOTION_VERSIONED_HOST_RE = re.compile(r"^sns-video-v[0-9]+\.xhscdn\.com$")
_MOTION_BACKUP_HOST_RE = re.compile(r"^sns-bak-v[0-9]+\.xhscdn\.com$")
_MOTION_ZL_HOST = "sns-video-zl.xhscdn.com"
_CONTROL_CHARACTER_RE = re.compile(r"[\x00-\x20\x7f]")

_GENERIC_ERROR_CODE = "XHS_LIVEPHOTO_BROWSER_ADMISSION_ERROR"
_ERROR_CODES = frozenset(
    {
        _GENERIC_ERROR_CODE,
        "XHS_LIVEPHOTO_BROWSER_ADMISSION_CANDIDATE_INVALID",
        "XHS_LIVEPHOTO_BROWSER_ADMISSION_HOST_REJECTED",
        "XHS_LIVEPHOTO_BROWSER_ADMISSION_PATH_INVALID",
        "XHS_LIVEPHOTO_BROWSER_ADMISSION_PORT_REJECTED",
        "XHS_LIVEPHOTO_BROWSER_ADMISSION_ROLE_MISMATCH",
        "XHS_LIVEPHOTO_BROWSER_ADMISSION_SCHEME_REJECTED",
        "XHS_LIVEPHOTO_BROWSER_ADMISSION_USERINFO_REJECTED",
    }
)


class XhsLivePhotoBrowserAdmissionError(ValueError):
    """Fixed-code admission failure that never renders a rejected URL."""

    def __init__(self, code: str):
        if type(code) is not str or code not in _ERROR_CODES:
            code = _GENERIC_ERROR_CODE
        self.code = code
        super().__init__(code)


def _reject(code: str) -> None:
    raise XhsLivePhotoBrowserAdmissionError(code)


def _reviewed_host_class(hostname: str, *, member_role: str) -> str:
    hostname = str(hostname or "").lower()
    if member_role == "still":
        return "still_exact" if hostname == _STILL_HOST else ""
    if member_role != "motion":
        return ""
    if _MOTION_VERSIONED_HOST_RE.fullmatch(hostname):
        return "motion_versioned"
    if hostname == _MOTION_ZL_HOST:
        return "motion_zl"
    if _MOTION_BACKUP_HOST_RE.fullmatch(hostname):
        return "motion_backup"
    return ""


def _is_ip_literal(hostname: str) -> bool:
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        return False
    return True


def _validated_candidate_parts(value: Any, *, member_role: str):
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or _CONTROL_CHARACTER_RE.search(value)
        or member_role not in _MEMBER_ROLES
        or value.startswith("//")
    ):
        _reject("XHS_LIVEPHOTO_BROWSER_ADMISSION_CANDIDATE_INVALID")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except (TypeError, ValueError):
        _reject("XHS_LIVEPHOTO_BROWSER_ADMISSION_CANDIDATE_INVALID")

    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        _reject("XHS_LIVEPHOTO_BROWSER_ADMISSION_SCHEME_REJECTED")
    if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
        _reject("XHS_LIVEPHOTO_BROWSER_ADMISSION_USERINFO_REJECTED")
    if (scheme == "http" and port not in (None, 80)) or (
        scheme == "https" and port not in (None, 443)
    ):
        _reject("XHS_LIVEPHOTO_BROWSER_ADMISSION_PORT_REJECTED")

    hostname = (parsed.hostname or "").lower()
    if (
        not hostname
        or hostname.endswith(".")
        or hostname == "localhost"
        or hostname.endswith(".localhost")
        or _is_ip_literal(hostname)
    ):
        _reject("XHS_LIVEPHOTO_BROWSER_ADMISSION_HOST_REJECTED")
    host_class = _reviewed_host_class(hostname, member_role=member_role)
    if not host_class:
        _reject("XHS_LIVEPHOTO_BROWSER_ADMISSION_HOST_REJECTED")
    if not parsed.path or not parsed.path.strip("/") or "\\" in parsed.path:
        _reject("XHS_LIVEPHOTO_BROWSER_ADMISSION_PATH_INVALID")
    return parsed, scheme, host_class


@dataclass(frozen=True)
class XhsPrivateHttpsMediaCandidateV1:
    value: str = field(repr=False, compare=False, hash=False)
    member_role: str
    host_class: str
    upgrade_applied: bool

    def __post_init__(self) -> None:
        _parsed, scheme, validated_host_class = _validated_candidate_parts(
            self.value,
            member_role=self.member_role,
        )
        if (
            scheme != "https"
            or self.host_class not in _HOST_CLASSES
            or validated_host_class != self.host_class
            or (
                self.member_role == "still" and self.host_class != "still_exact"
            )
            or (
                self.member_role == "motion"
                and self.host_class not in _MOTION_HOST_CLASSES
            )
            or type(self.upgrade_applied) is not bool
        ):
            _reject("XHS_LIVEPHOTO_BROWSER_ADMISSION_CANDIDATE_INVALID")

    def to_structural_dict(self) -> dict[str, Any]:
        return {
            "schema_version": XHS_LIVEPHOTO_BROWSER_ADMISSION_SCHEMA_VERSION,
            "member_role": self.member_role,
            "host_class": self.host_class,
            "upgrade_applied": self.upgrade_applied,
        }


@dataclass(frozen=True)
class XhsLivePhotoBrowserAdmissionEvidenceV1:
    member_role: str
    host_class: str
    format_class: str
    response_type: str
    body_readable: bool
    final_host_class: str
    range_result: str
    redirect_class: str
    upgrade_applied: bool

    def __post_init__(self) -> None:
        valid_host_class = (
            self.host_class == "still_exact"
            if self.member_role == "still"
            else self.host_class in _MOTION_HOST_CLASSES
        )
        valid_format = (
            self.format_class in _STILL_FORMAT_CLASSES
            if self.member_role == "still"
            else self.format_class in _MOTION_FORMAT_CLASSES
        )
        if (
            self.member_role not in _MEMBER_ROLES
            or self.host_class not in _HOST_CLASSES
            or not valid_host_class
            or self.final_host_class != self.host_class
            or not valid_format
            or self.response_type != "cors"
            or self.body_readable is not True
            or self.range_result not in {"partial_206", "ignored_bounded_200"}
            or self.redirect_class not in {"none", "reviewed_same_class"}
            or type(self.upgrade_applied) is not bool
        ):
            _reject("XHS_LIVEPHOTO_BROWSER_ADMISSION_CANDIDATE_INVALID")

    def to_structural_dict(self) -> dict[str, Any]:
        return {
            "schema_version": XHS_LIVEPHOTO_BROWSER_ADMISSION_SCHEMA_VERSION,
            "member_role": self.member_role,
            "host_class": self.host_class,
            "format_class": self.format_class,
            "response_type": self.response_type,
            "body_readable": self.body_readable,
            "final_host_class": self.final_host_class,
            "range_result": self.range_result,
            "redirect_class": self.redirect_class,
            "upgrade_applied": self.upgrade_applied,
        }


def normalize_xhs_livephoto_candidate_https(
    private_candidate: XhsPrivateMediaCandidateV1,
    *,
    member_role: str,
) -> XhsPrivateHttpsMediaCandidateV1:
    """Validate one private candidate and preserve its signed URL text."""

    if type(private_candidate) is not XhsPrivateMediaCandidateV1:
        _reject("XHS_LIVEPHOTO_BROWSER_ADMISSION_CANDIDATE_INVALID")
    if (
        member_role not in _MEMBER_ROLES
        or private_candidate.member_role != member_role
    ):
        _reject("XHS_LIVEPHOTO_BROWSER_ADMISSION_ROLE_MISMATCH")

    value = private_candidate.value
    parsed, scheme, host_class = _validated_candidate_parts(
        value,
        member_role=member_role,
    )

    # Replace only the scheme prefix so signed path/query bytes remain unchanged.
    fragmentless = value.split("#", 1)[0]
    separator_index = fragmentless.find("://")
    normalized_value = "https" + fragmentless[separator_index:]
    if scheme == "http" and parsed.port == 80:
        authority_and_suffix = normalized_value[len("https://"):]
        authority, suffix = authority_and_suffix.split("/", 1)
        normalized_value = f"https://{authority.rsplit(':', 1)[0]}/{suffix}"

    if not normalized_value.startswith("https://"):
        _reject("XHS_LIVEPHOTO_BROWSER_ADMISSION_SCHEME_REJECTED")
    if urlsplit(normalized_value).query != parsed.query:
        _reject("XHS_LIVEPHOTO_BROWSER_ADMISSION_CANDIDATE_INVALID")
    return XhsPrivateHttpsMediaCandidateV1(
        value=normalized_value,
        member_role=member_role,
        host_class=host_class,
        upgrade_applied=scheme == "http",
    )


__all__ = (
    "XHS_LIVEPHOTO_BROWSER_ADMISSION_SCHEMA_VERSION",
    "XhsLivePhotoBrowserAdmissionError",
    "XhsLivePhotoBrowserAdmissionEvidenceV1",
    "XhsPrivateHttpsMediaCandidateV1",
    "normalize_xhs_livephoto_candidate_https",
)
