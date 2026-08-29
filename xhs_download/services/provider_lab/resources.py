from __future__ import annotations

from dataclasses import dataclass
import secrets
import time
from typing import Any

from django.core.cache import cache

from .contracts import ProviderLabError, safe_text, validated_resource_url


RESOURCE_TTL_SECONDS = 300
RESOURCE_CACHE_MAX_SECONDS = 600
RESOURCE_MAX_USES = 10
RESOURCE_CACHE_PREFIX = "provider-lab:resource:v1"
RESOURCE_USAGE_CACHE_PREFIX = "provider-lab:resource-uses:v1"


@dataclass(frozen=True)
class LabResourceDescriptor:
    token: str
    user_id: int
    request_id: str
    provider_id: str
    asset_id: str
    url: str
    media_type: str
    headers_present: bool
    requires_cookie: bool
    uses: int = 0


def resource_cache_key(token: str) -> str:
    return f"{RESOURCE_CACHE_PREFIX}:{safe_text(token, 160)}"


def resource_usage_cache_key(token: str) -> str:
    return f"{RESOURCE_USAGE_CACHE_PREFIX}:{safe_text(token, 160)}"


def issue_resource_token(
    *,
    user_id: int,
    request_id: str,
    provider_id: str,
    asset_id: str,
    url: str,
    media_type: str,
    headers_present: bool = False,
    requires_cookie: bool = False,
) -> str:
    exact_url, _hostname = validated_resource_url(url)
    token = secrets.token_urlsafe(32)
    expires_at = time.time() + RESOURCE_TTL_SECONDS
    payload = {
        "user_id": int(user_id),
        "request_id": safe_text(request_id, 80),
        "provider_id": safe_text(provider_id, 80),
        "asset_id": safe_text(asset_id, 120),
        "url": exact_url,
        "media_type": safe_text(media_type, 20),
        "headers_present": bool(headers_present),
        "requires_cookie": bool(requires_cookie),
        "expires_at": expires_at,
    }
    cache.set(resource_cache_key(token), payload, timeout=RESOURCE_TTL_SECONDS)
    cache.set(resource_usage_cache_key(token), 0, timeout=RESOURCE_TTL_SECONDS)
    return token


def consume_resource_token(
    *,
    token: str,
    user_id: int,
    request_id: str,
    provider_id: str,
    asset_id: str,
) -> LabResourceDescriptor:
    token = safe_text(token, 160)
    payload = cache.get(resource_cache_key(token))
    if not isinstance(payload, dict):
        raise ProviderLabError("PROVIDER_SCHEMA_INVALID", http_status=404)
    expected = {
        "user_id": int(user_id),
        "request_id": safe_text(request_id, 80),
        "provider_id": safe_text(provider_id, 80),
        "asset_id": safe_text(asset_id, 120),
    }
    if any(payload.get(key) != value for key, value in expected.items()):
        raise ProviderLabError("PROVIDER_SCHEMA_INVALID", http_status=404)
    if float(payload.get("expires_at") or 0) <= time.time():
        cache.delete(resource_cache_key(token))
        cache.delete(resource_usage_cache_key(token))
        raise ProviderLabError("PROVIDER_SCHEMA_INVALID", http_status=404)
    try:
        uses = int(cache.incr(resource_usage_cache_key(token)))
    except (TypeError, ValueError):
        cache.delete(resource_cache_key(token))
        raise ProviderLabError("PROVIDER_SCHEMA_INVALID", http_status=404)
    if uses > RESOURCE_MAX_USES:
        cache.delete(resource_cache_key(token))
        cache.delete(resource_usage_cache_key(token))
        raise ProviderLabError("PROVIDER_RATE_LIMITED", http_status=429)
    exact_url, _hostname = validated_resource_url(payload.get("url"))
    return LabResourceDescriptor(
        token=token,
        user_id=int(payload["user_id"]),
        request_id=str(payload["request_id"]),
        provider_id=str(payload["provider_id"]),
        asset_id=str(payload["asset_id"]),
        url=exact_url,
        media_type=safe_text(payload.get("media_type"), 20),
        headers_present=bool(payload.get("headers_present")),
        requires_cookie=bool(payload.get("requires_cookie")),
        uses=uses,
    )


def public_resource_descriptor(descriptor: LabResourceDescriptor) -> dict[str, Any]:
    inspectable = not descriptor.headers_present and not descriptor.requires_cookie
    payload = {
        "schema_version": "1.0",
        "request_id": descriptor.request_id,
        "provider_id": descriptor.provider_id,
        "asset_id": descriptor.asset_id,
        "media_type": descriptor.media_type,
        "inspectable": inspectable,
        "credentials": "omit",
        "max_probe_bytes": 4096,
    }
    if inspectable:
        payload["url"] = descriptor.url
    else:
        payload["error_code"] = "RESOURCE_REQUIRES_REQUEST_CONTEXT"
    return payload
