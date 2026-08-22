"""Typed, network-free source URL lifecycle primitives."""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


SOURCE_URL_MAX_CHARS = 4096
SOURCE_QUERY_MAX_FIELDS = 64
SOURCE_PARAMETER_NAME_MAX_CHARS = 64
SOURCE_RESOLUTION_MAX_DURATION_MS = 300_000

_PLATFORM_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_PARAMETER_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")
_CANONICAL_IDENTITY_RE = re.compile(
    r"^[a-z][a-z0-9_]{0,31}:[A-Za-z0-9][A-Za-z0-9._:-]{0,126}$"
)
_SAFE_KIND_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}\.?$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.?$",
    re.IGNORECASE,
)
_HTTP_URL_RE = re.compile(
    r"https?://[^\s\"'<>\uFF0C\u3002\uFF1B\uFF01\uFF1F\u201D\u2019\u3011\u300B]+",
    re.IGNORECASE,
)
_TRAILING_URL_CHARS = "\uFF0C,\u3002\uFF1B;\uFF01!\uFF1F?\uFF09)]\u3011\u300B"


class SourceLifecycleError(ValueError):
    """A fixed-code error that never includes rejected source values."""

    def __init__(self, code: str, path: str):
        self.code = code
        self.path = path
        super().__init__(f"{code} path={path}")


def _reject(code: str, path: str) -> None:
    raise SourceLifecycleError(code, path)


class SourceParameterAction(str, Enum):
    RETAIN_FOR_PARSE = "retain_for_parse"
    RETAIN_FOR_IDENTITY = "retain_for_identity"
    REMOVE_AFTER_PARSE = "remove_after_parse"
    ALWAYS_DROP = "always_drop"


class SourceInputKind(str, Enum):
    URL = "url"
    SHARE_TEXT = "share_text"
    SHORT_URL = "short_url"
    MODAL_URL = "modal_url"
    VIDEO_PAGE = "video_page"
    NOTE_PAGE = "note_page"
    DYNAMIC_PAGE = "dynamic_page"
    OPUS_PAGE = "opus_page"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class SourceParameterApplication:
    """Internal URL result; callers must not serialize ``url`` as diagnostics."""

    url: str
    retained_parameter_names: tuple[str, ...]
    removed_parameter_names: tuple[str, ...]


def _normalize_parameter_names(value: Any, path: str) -> tuple[str, ...]:
    if type(value) is not tuple:
        _reject("SOURCE_PARAMETER_COLLECTION_INVALID", path)
    normalized = []
    seen = set()
    for index, item in enumerate(value):
        if (
            not isinstance(item, str)
            or len(item) > SOURCE_PARAMETER_NAME_MAX_CHARS
            or not _PARAMETER_NAME_RE.fullmatch(item)
        ):
            _reject("SOURCE_PARAMETER_NAME_INVALID", f"{path}[{index}]")
        name = item.lower()
        if name in seen:
            _reject("SOURCE_PARAMETER_NAME_DUPLICATE", f"{path}[{index}]")
        seen.add(name)
        normalized.append(name)
    return tuple(normalized)


@dataclass(frozen=True)
class SourceParameterPolicy:
    """Closed parameter policy applied without resolving or requesting a URL.

    ``retain_for_parse`` survives parse cleanup but is not declared identity
    material. ``retain_for_identity`` is the only class retained by
    :meth:`for_identity`. ``remove_after_parse`` is available only to the
    parser, and every unknown parameter follows the fail-closed drop policy.
    """

    retain_for_parse: tuple[str, ...] = ()
    retain_for_identity: tuple[str, ...] = ()
    remove_after_parse: tuple[str, ...] = ()
    always_drop: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        groups = {}
        for field_name in (
            "retain_for_parse",
            "retain_for_identity",
            "remove_after_parse",
            "always_drop",
        ):
            normalized = _normalize_parameter_names(
                getattr(self, field_name),
                f"source_parameter_policy.{field_name}",
            )
            object.__setattr__(self, field_name, normalized)
            for name in normalized:
                if name in groups:
                    _reject(
                        "SOURCE_PARAMETER_POLICY_OVERLAP",
                        f"source_parameter_policy.{field_name}",
                    )
                groups[name] = field_name

    def action_for(self, parameter_name: str) -> SourceParameterAction:
        name = str(parameter_name or "").lower()
        if name in self.retain_for_parse:
            return SourceParameterAction.RETAIN_FOR_PARSE
        if name in self.retain_for_identity:
            return SourceParameterAction.RETAIN_FOR_IDENTITY
        if name in self.remove_after_parse:
            return SourceParameterAction.REMOVE_AFTER_PARSE
        return SourceParameterAction.ALWAYS_DROP

    def for_parse(self, url: str) -> SourceParameterApplication:
        return self._apply(
            url,
            allowed_actions={
                SourceParameterAction.RETAIN_FOR_PARSE,
                SourceParameterAction.RETAIN_FOR_IDENTITY,
                SourceParameterAction.REMOVE_AFTER_PARSE,
            },
        )

    def after_parse(self, url: str) -> SourceParameterApplication:
        return self._apply(
            url,
            allowed_actions={
                SourceParameterAction.RETAIN_FOR_PARSE,
                SourceParameterAction.RETAIN_FOR_IDENTITY,
            },
        )

    def for_identity(self, url: str) -> SourceParameterApplication:
        return self._apply(
            url,
            allowed_actions={SourceParameterAction.RETAIN_FOR_IDENTITY},
        )

    def _apply(
        self,
        url: str,
        *,
        allowed_actions: set[SourceParameterAction],
    ) -> SourceParameterApplication:
        parsed = _parse_public_source_url(url, "source_url")
        try:
            query = parse_qsl(
                parsed.query,
                keep_blank_values=True,
                max_num_fields=SOURCE_QUERY_MAX_FIELDS,
            )
        except ValueError:
            _reject("SOURCE_QUERY_INVALID", "source_url.query")

        retained_pairs = []
        retained_names = []
        removed_names = []
        for name, value in query:
            normalized_name = name.lower()
            action = self.action_for(normalized_name)
            target = retained_names if action in allowed_actions else removed_names
            if normalized_name not in target:
                target.append(normalized_name)
            if action in allowed_actions:
                retained_pairs.append((name, value))

        normalized_url = urlunsplit(
            (
                parsed.scheme.lower(),
                parsed.netloc,
                parsed.path or "/",
                urlencode(retained_pairs, doseq=True),
                "",
            )
        )
        return SourceParameterApplication(
            url=normalized_url,
            retained_parameter_names=tuple(retained_names),
            removed_parameter_names=tuple(removed_names),
        )


@dataclass(frozen=True)
class SafeSourceDiagnostic:
    platform_id: str
    input_kind: str
    hostname: str
    path_class: str
    canonical_identity_kind: str
    resolution_duration_ms: int
    retained_parameter_names: tuple[str, ...]
    removed_parameter_names: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.platform_id, str) or not _PLATFORM_ID_RE.fullmatch(
            self.platform_id
        ):
            _reject("SOURCE_DIAGNOSTIC_PLATFORM_INVALID", "safe_source.platform_id")
        if self.input_kind not in {item.value for item in SourceInputKind}:
            _reject("SOURCE_DIAGNOSTIC_INPUT_KIND_INVALID", "safe_source.input_kind")
        hostname = str(self.hostname or "").rstrip(".").lower()
        if not _HOSTNAME_RE.fullmatch(hostname):
            _reject("SOURCE_DIAGNOSTIC_HOST_INVALID", "safe_source.hostname")
        if self.path_class not in {
            "root",
            "short_link",
            "modal",
            "video",
            "note",
            "dynamic",
            "opus",
            "other",
        }:
            _reject("SOURCE_DIAGNOSTIC_PATH_CLASS_INVALID", "safe_source.path_class")
        identity_kind_is_valid = isinstance(
            self.canonical_identity_kind,
            str,
        ) and _SAFE_KIND_RE.fullmatch(self.canonical_identity_kind)
        if not identity_kind_is_valid:
            _reject(
                "SOURCE_DIAGNOSTIC_IDENTITY_KIND_INVALID",
                "safe_source.canonical_identity_kind",
            )
        if (
            not isinstance(self.resolution_duration_ms, int)
            or isinstance(self.resolution_duration_ms, bool)
            or not 0
            <= self.resolution_duration_ms
            <= SOURCE_RESOLUTION_MAX_DURATION_MS
        ):
            _reject(
                "SOURCE_DIAGNOSTIC_DURATION_INVALID",
                "safe_source.resolution_duration_ms",
            )
        retained = _normalize_name_tuple(
            self.retained_parameter_names,
            "safe_source.retained_parameter_names",
        )
        removed = _normalize_name_tuple(
            self.removed_parameter_names,
            "safe_source.removed_parameter_names",
        )
        if set(retained) & set(removed):
            _reject(
                "SOURCE_DIAGNOSTIC_PARAMETER_OVERLAP",
                "safe_source.removed_parameter_names",
            )
        object.__setattr__(self, "hostname", hostname)
        object.__setattr__(self, "retained_parameter_names", retained)
        object.__setattr__(self, "removed_parameter_names", removed)

    def to_structural_dict(self) -> dict[str, Any]:
        return {
            "platform_id": self.platform_id,
            "input_kind": self.input_kind,
            "hostname": self.hostname,
            "path_class": self.path_class,
            "canonical_identity_kind": self.canonical_identity_kind,
            "resolution_duration_ms": self.resolution_duration_ms,
            "retained_parameter_names": list(self.retained_parameter_names),
            "removed_parameter_names": list(self.removed_parameter_names),
        }


def _normalize_name_tuple(value: Any, path: str) -> tuple[str, ...]:
    if type(value) is not tuple:
        _reject("SOURCE_DIAGNOSTIC_PARAMETER_COLLECTION_INVALID", path)
    names = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not _PARAMETER_NAME_RE.fullmatch(item):
            _reject("SOURCE_DIAGNOSTIC_PARAMETER_NAME_INVALID", f"{path}[{index}]")
        normalized = item.lower()
        if normalized not in names:
            names.append(normalized)
    return tuple(names)


@dataclass(frozen=True)
class SourceResolutionV1:
    platform_id: str
    input_kind: SourceInputKind
    input_url: str
    parse_url: str
    resolved_url: str
    canonical_identity: str
    retained_parameter_names: tuple[str, ...]
    removed_parameter_names: tuple[str, ...]
    resolution_duration_ms: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.platform_id, str) or not _PLATFORM_ID_RE.fullmatch(
            self.platform_id
        ):
            _reject("SOURCE_PLATFORM_ID_INVALID", "source_resolution.platform_id")
        if type(self.input_kind) is not SourceInputKind:
            _reject("SOURCE_INPUT_KIND_INVALID", "source_resolution.input_kind")
        for field_name in ("input_url", "parse_url", "resolved_url"):
            _parse_public_source_url(
                getattr(self, field_name),
                f"source_resolution.{field_name}",
            )
        if (
            not isinstance(self.canonical_identity, str)
            or not _CANONICAL_IDENTITY_RE.fullmatch(self.canonical_identity)
        ):
            _reject(
                "SOURCE_CANONICAL_IDENTITY_INVALID",
                "source_resolution.canonical_identity",
            )
        retained = _normalize_name_tuple(
            self.retained_parameter_names,
            "source_resolution.retained_parameter_names",
        )
        removed = _normalize_name_tuple(
            self.removed_parameter_names,
            "source_resolution.removed_parameter_names",
        )
        if set(retained) & set(removed):
            _reject(
                "SOURCE_DIAGNOSTIC_PARAMETER_OVERLAP",
                "source_resolution.removed_parameter_names",
            )
        object.__setattr__(self, "retained_parameter_names", retained)
        object.__setattr__(self, "removed_parameter_names", removed)
        if (
            not isinstance(self.resolution_duration_ms, int)
            or isinstance(self.resolution_duration_ms, bool)
            or not 0
            <= self.resolution_duration_ms
            <= SOURCE_RESOLUTION_MAX_DURATION_MS
        ):
            _reject(
                "SOURCE_RESOLUTION_DURATION_INVALID",
                "source_resolution.resolution_duration_ms",
            )

    @property
    def canonical_identity_kind(self) -> str:
        return self.canonical_identity.partition(":")[0]

    def to_safe_diagnostic(self) -> SafeSourceDiagnostic:
        parsed = _parse_public_source_url(
            self.resolved_url,
            "source_resolution.resolved_url",
        )
        return SafeSourceDiagnostic(
            platform_id=self.platform_id,
            input_kind=self.input_kind.value,
            hostname=(parsed.hostname or "").lower(),
            path_class=_path_class(self.input_kind, parsed.path),
            canonical_identity_kind=self.canonical_identity_kind,
            resolution_duration_ms=self.resolution_duration_ms,
            retained_parameter_names=self.retained_parameter_names,
            removed_parameter_names=self.removed_parameter_names,
        )


def extract_source_url(value: str) -> str:
    """Extract one HTTP URL without retaining the surrounding share text."""

    text = str(value or "").strip()
    match = _HTTP_URL_RE.search(text)
    candidate = (
        match.group(0).rstrip(_TRAILING_URL_CHARS)
        if match
        else text.rstrip(_TRAILING_URL_CHARS)
    )
    _parse_public_source_url(candidate, "source_input")
    return candidate


def build_source_resolution(
    *,
    platform_id: str,
    input_kind: SourceInputKind,
    input_value: str,
    canonical_identity: str,
    parameter_policy: SourceParameterPolicy,
    resolved_url: str = "",
    resolution_duration_ms: int = 0,
) -> SourceResolutionV1:
    """Build lifecycle state from caller-supplied identity and optional resolution.

    This function never resolves a redirect and never derives a platform
    identity from URL bytes. ``resolved_url`` must be supplied by an existing
    platform resolver when one has run.
    """

    if type(parameter_policy) is not SourceParameterPolicy:
        _reject("SOURCE_PARAMETER_POLICY_INVALID", "source_parameter_policy")
    input_url = extract_source_url(input_value)
    parse_application = parameter_policy.for_parse(input_url)
    resolved_input = resolved_url or parse_application.url
    final_application = parameter_policy.after_parse(resolved_input)
    removed_names = list(parse_application.removed_parameter_names)
    for name in final_application.removed_parameter_names:
        if name not in removed_names:
            removed_names.append(name)
    return SourceResolutionV1(
        platform_id=platform_id,
        input_kind=input_kind,
        input_url=input_url,
        parse_url=parse_application.url,
        resolved_url=final_application.url,
        canonical_identity=canonical_identity,
        retained_parameter_names=final_application.retained_parameter_names,
        removed_parameter_names=tuple(removed_names),
        resolution_duration_ms=resolution_duration_ms,
    )


def _parse_public_source_url(value: Any, path: str):
    if not isinstance(value, str) or not value or len(value) > SOURCE_URL_MAX_CHARS:
        _reject("SOURCE_URL_INVALID", path)
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        _reject("SOURCE_URL_INVALID", path)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        _reject("SOURCE_URL_INVALID", path)
    if parsed.username is not None or parsed.password is not None:
        _reject("SOURCE_URL_USERINFO_REJECTED", path)
    if port is not None and port not in {80, 443}:
        _reject("SOURCE_URL_PORT_REJECTED", path)
    hostname = parsed.hostname.rstrip(".").lower()
    if not _HOSTNAME_RE.fullmatch(hostname):
        _reject("SOURCE_HOST_INVALID", path)
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        if hostname == "localhost" or hostname.endswith(".localhost"):
            _reject("SOURCE_HOST_PRIVATE", path)
    else:
        if (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_multicast
            or address.is_reserved
            or address.is_unspecified
        ):
            _reject("SOURCE_HOST_PRIVATE", path)
    return parsed


def _path_class(input_kind: SourceInputKind, path: str) -> str:
    kind_classes = {
        SourceInputKind.SHORT_URL: "short_link",
        SourceInputKind.MODAL_URL: "modal",
        SourceInputKind.VIDEO_PAGE: "video",
        SourceInputKind.NOTE_PAGE: "note",
        SourceInputKind.DYNAMIC_PAGE: "dynamic",
        SourceInputKind.OPUS_PAGE: "opus",
    }
    if input_kind in kind_classes:
        return kind_classes[input_kind]
    first_segment = next((item for item in str(path or "").split("/") if item), "")
    if not first_segment:
        return "root"
    if first_segment.lower() in {"video", "explore", "discovery", "opus"}:
        return {
            "video": "video",
            "explore": "note",
            "discovery": "note",
            "opus": "opus",
        }[first_segment.lower()]
    return "other"
