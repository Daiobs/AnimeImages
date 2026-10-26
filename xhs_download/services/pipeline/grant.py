"""Short-lived resource grants, contract section 5; not wired to requests yet.

These checks bind identity and redirect scope, not a delivery method. Each hop
must ALSO pass the existing outbound checks (public addresses, ports, DNS pinning
and request context). No network lookup, settings or key storage belongs here.
"""
import base64
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import ipaddress
import json
import re
from urllib.parse import urlsplit

from .model import Work, validate_work


MAX_TTL_SECONDS = 300
MAX_TOKEN_LENGTH = 65536
_DOMAIN = b"pipeline-download-grant:v1\0"
_VERSION = 1
_FIELDS = frozenset({
    "version", "url", "host_policy", "platform", "work_id", "variant_id",
    "required_headers", "issued_at", "expires_at",
})
_HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
_HOST_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
# Numeric forms also interpreted as IPv4 by URL/network stacks; no DNS needed.
_NUMERIC_HOST = re.compile(r"(?:0x[0-9a-f]+|[0-9]+)(?:\.(?:0x[0-9a-f]+|[0-9]+)){0,3}")
_BASE64 = re.compile(r"[A-Za-z0-9_-]+")
_HEX_SIGNATURE = re.compile(r"[0-9a-f]{64}")


class GrantError(ValueError):
    """Only a safe code: never include a URL, token, key or header value."""


@dataclass(frozen=True)
class Grant:
    url: str
    host_policy: tuple[str, ...]
    platform: str
    work_id: str
    variant_id: str
    required_headers: tuple[str, ...]
    issued_at: datetime
    expires_at: datetime


def _key(key):
    if not isinstance(key, bytes) or not key:
        raise GrantError("INVALID_KEY")
    return key


def _time(value):
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise GrantError("INVALID_TIME")
    return value.astimezone(timezone.utc)


def _time_text(value):
    return _time(value).isoformat(timespec="microseconds")


def _read_time(value):
    if not isinstance(value, str):
        raise GrantError("INVALID_TIME")
    try:
        parsed = _time(datetime.fromisoformat(value))
    except (ValueError, OverflowError):
        raise GrantError("INVALID_TIME") from None
    if _time_text(parsed) != value:
        raise GrantError("INVALID_TIME")
    return parsed


def _identity(value):
    if not isinstance(value, str) or not value.strip() or re.search(r"[\x00-\x1f\x7f]", value):
        raise GrantError("INVALID_IDENTITY")
    return value


def _host(value):
    if not isinstance(value, str) or not value:
        raise GrantError("INVALID_HOST")
    try:
        host = value.removesuffix(".").encode("idna").decode("ascii").lower()
    except UnicodeError:
        raise GrantError("INVALID_HOST") from None
    if _NUMERIC_HOST.fullmatch(host):
        raise GrantError("IP_LITERAL")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise GrantError("IP_LITERAL")
    if len(host) > 253 or not all(_HOST_LABEL.fullmatch(label) for label in host.split(".")):
        raise GrantError("INVALID_HOST")
    return host


def _url(value):
    if not isinstance(value, str) or re.search(r"[\x00-\x20\x7f]", value):
        raise GrantError("INVALID_URL")
    try:
        parts = urlsplit(value)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            raise GrantError("INVALID_URL")
        if parts.username is not None or parts.password is not None:
            raise GrantError("INVALID_URL")
        parts.port
        host = _host(parts.hostname)
    except ValueError as error:
        if isinstance(error, GrantError):
            raise
        raise GrantError("INVALID_URL") from None
    return parts.scheme, host


def _policy(values):
    if not isinstance(values, (tuple, list)):
        raise GrantError("INVALID_HOST_POLICY")
    for value in values:
        _host(value)
    return tuple(values)


def _headers(values):
    if not isinstance(values, (tuple, list)) or any(
        not isinstance(name, str) or not _HEADER_NAME.fullmatch(name) for name in values
    ):
        raise GrantError("INVALID_HEADER_NAMES")
    return tuple(values)


def _check_target(grant, target_url, is_redirect):
    if type(is_redirect) is not bool:
        raise GrantError("INVALID_HOP")
    if not is_redirect and target_url != grant.url:
        raise GrantError("INITIAL_URL_MISMATCH")
    scheme, host = _url(target_url)
    if grant.host_policy:
        if not any(host == _host(suffix) or host.endswith("." + _host(suffix))
                   for suffix in grant.host_policy):
            raise GrantError("HOST_NOT_ALLOWED")
    elif is_redirect and scheme != "https":
        raise GrantError("REDIRECT_REQUIRES_HTTPS")


def _payload(grant):
    return {
        "version": _VERSION, "url": grant.url, "host_policy": grant.host_policy,
        "platform": grant.platform, "work_id": grant.work_id,
        "variant_id": grant.variant_id, "required_headers": grant.required_headers,
        "issued_at": _time_text(grant.issued_at), "expires_at": _time_text(grant.expires_at),
    }


def _json(payload):
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False).encode("ascii")


def issue_grant(work, variant_id, *, key, now, ttl_seconds=MAX_TTL_SECONDS):
    """Bind a selected, rule-0-eligible variant, with no header values.

    The caller still chooses a permitted single/merge/archive delivery using the
    existing rule table; this resource grant cannot override that decision.
    """
    _key(key)
    now = _time(now)
    if type(ttl_seconds) is not int or not 0 < ttl_seconds <= MAX_TTL_SECONDS:
        raise GrantError("INVALID_TTL")
    if not isinstance(work, Work) or validate_work(work):
        raise GrantError("INVALID_WORK")
    _item, variant = work.find_variant(variant_id)
    if variant is None:
        raise GrantError("UNKNOWN_VARIANT")
    if not variant.deliverable:
        raise GrantError("NOT_DOWNLOADABLE")
    expires_at = now + timedelta(seconds=ttl_seconds)
    if variant.expires_at is not None:
        expires_at = min(expires_at, _time(variant.expires_at))
    if expires_at <= now:
        raise GrantError("EXPIRED")
    grant = Grant(
        url=variant.url, host_policy=_policy(variant.host_policy),
        platform=_identity(work.platform), work_id=_identity(work.work_id),
        variant_id=_identity(variant.variant_id), required_headers=_headers(variant.required_headers),
        issued_at=now, expires_at=expires_at,
    )
    _check_target(grant, grant.url, False)
    encoded = base64.urlsafe_b64encode(_json(_payload(grant))).rstrip(b"=").decode("ascii")
    signature = hmac.new(key, _DOMAIN + encoded.encode("ascii"), hashlib.sha256).hexdigest()
    token = encoded + "." + signature
    if len(token) > MAX_TOKEN_LENGTH:
        raise GrantError("TOKEN_TOO_LARGE")
    return token


def verify_grant(token, *, key, now):
    """Authenticate and return the claims; use these, not parallel client fields."""
    _key(key)
    now = _time(now)
    if not isinstance(token, str) or len(token) > MAX_TOKEN_LENGTH or token.count(".") != 1:
        raise GrantError("INVALID_TOKEN")
    encoded, signature = token.split(".")
    if not _BASE64.fullmatch(encoded) or not _HEX_SIGNATURE.fullmatch(signature):
        raise GrantError("INVALID_TOKEN")
    expected = hmac.new(key, _DOMAIN + encoded.encode("ascii"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise GrantError("BAD_SIGNATURE")
    try:
        data = base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True)
        payload = json.loads(data)
        if (not isinstance(payload, dict) or set(payload) != _FIELDS
                or type(payload["version"]) is not int or payload["version"] != _VERSION
                or _json(payload) != data
                or base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii") != encoded):
            raise GrantError("INVALID_TOKEN")
        grant = Grant(
            url=payload["url"], host_policy=_policy(payload["host_policy"]),
            platform=_identity(payload["platform"]), work_id=_identity(payload["work_id"]),
            variant_id=_identity(payload["variant_id"]), required_headers=_headers(payload["required_headers"]),
            issued_at=_read_time(payload["issued_at"]), expires_at=_read_time(payload["expires_at"]),
        )
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        raise GrantError("INVALID_TOKEN") from None
    if not grant.issued_at <= now < grant.expires_at:
        raise GrantError("EXPIRED_OR_NOT_YET_VALID")
    if not timedelta(0) < grant.expires_at - grant.issued_at <= timedelta(seconds=MAX_TTL_SECONDS):
        raise GrantError("INVALID_TTL")
    _check_target(grant, grant.url, False)
    return grant


def verify_hop(token, target_url, *, key, now, is_redirect=False):
    """Check EVERY hop before I/O, with redirect state from the trusted caller.

    First call: is_redirect=False and the exact authorized URL. Later calls:
    is_redirect=True and the resolved absolute Location. A successful return is
    not public-address/DNS approval; the outbound layer must still perform it.
    """
    grant = verify_grant(token, key=key, now=now)
    _check_target(grant, target_url, is_redirect)
    return grant
