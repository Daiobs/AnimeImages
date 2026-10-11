from __future__ import annotations

from dataclasses import dataclass
import json
import math
import re
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
RESOURCE_BUNDLE_CACHE_PREFIX = "provider-lab:resource-bundle:v1"
RESOURCE_BUNDLE_MAX_ITEMS = 1000
RESOURCE_BUNDLE_MAX_BYTES = 2 * 1024 * 1024


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


def create_resource_bundle() -> dict[str, Any]:
    return {"id": "g" + secrets.token_urlsafe(18), "resources": {},
            "expires_at": time.time() + RESOURCE_TTL_SECONDS}


def store_resource_bundle(bundle: dict[str, Any]) -> None:
    if (len(bundle["resources"]) > RESOURCE_BUNDLE_MAX_ITEMS
            or len(json.dumps(bundle).encode()) > RESOURCE_BUNDLE_MAX_BYTES):
        raise ProviderLabError("PROVIDER_OUTPUT_TOO_LARGE")
    cache.set(f"{RESOURCE_BUNDLE_CACHE_PREFIX}:{bundle['id']}", bundle,
              timeout=max(1, math.ceil(bundle["expires_at"] - time.time())))


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
    resource_bundle: dict[str, Any] | None = None,
) -> str:
    exact_url, _hostname = validated_resource_url(url)
    token = secrets.token_urlsafe(32)
    expires_at = time.time() + RESOURCE_TTL_SECONDS
    if resource_bundle is not None:
        if len(resource_bundle["resources"]) >= RESOURCE_BUNDLE_MAX_ITEMS:
            raise ProviderLabError("PROVIDER_OUTPUT_TOO_LARGE")
        token = resource_bundle["id"] + "." + token
        expires_at = resource_bundle["expires_at"]
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
    if resource_bundle is not None:
        resource_bundle["resources"][token] = {**payload, "uses": 0}
    else:
        cache.set(resource_cache_key(token), payload, timeout=RESOURCE_TTL_SECONDS)
        cache.set(resource_usage_cache_key(token), 0, timeout=RESOURCE_TTL_SECONDS)
    return token


def _consume_bundle(token: str, expected: dict[str, Any]) -> dict[str, Any]:
    if not re.fullmatch(r"g[A-Za-z0-9_-]{24}\.[A-Za-z0-9_-]{43}", token):
        raise ProviderLabError("PROVIDER_SCHEMA_INVALID", http_status=404)
    key = f"{RESOURCE_BUNDLE_CACHE_PREFIX}:{token.split('.', 1)[0]}"
    lock_key, owner = key + ":lock", secrets.token_hex(16)
    # The lease cannot expire while any descriptor in this bundle is still valid.
    if not cache.add(lock_key, owner, timeout=RESOURCE_TTL_SECONDS):
        raise ProviderLabError("PROVIDER_RATE_LIMITED", http_status=429)
    try:
        bundle = cache.get(key)
        payload = bundle.get("resources", {}).get(token) if isinstance(bundle, dict) else None
        if (not isinstance(payload, dict)
                or any(payload.get(name) != value for name, value in expected.items())
                or float(payload.get("expires_at") or 0) <= time.time()):
            raise ProviderLabError("PROVIDER_SCHEMA_INVALID", http_status=404)
        uses = int(payload.get("uses", 0)) + 1
        if uses > RESOURCE_MAX_USES:
            raise ProviderLabError("PROVIDER_RATE_LIMITED", http_status=429)
        payload["uses"] = uses
        if cache.get(lock_key) != owner or bundle["expires_at"] <= time.time():
            raise ProviderLabError("PROVIDER_SCHEMA_INVALID", http_status=404)
        cache.set(key, bundle, timeout=max(1, math.ceil(bundle["expires_at"] - time.time())))
        if cache.get(lock_key) != owner or bundle["expires_at"] <= time.time():
            raise ProviderLabError("PROVIDER_SCHEMA_INVALID", http_status=404)
        return payload
    finally:
        if cache.get(lock_key) == owner:
            cache.delete(lock_key)


def consume_resource_token(
    *,
    token: str,
    user_id: int,
    request_id: str,
    provider_id: str,
    asset_id: str,
) -> LabResourceDescriptor:
    token = safe_text(token, 160)
    expected = {
        "user_id": int(user_id), "request_id": safe_text(request_id, 80),
        "provider_id": safe_text(provider_id, 80), "asset_id": safe_text(asset_id, 120),
    }
    if "." in token:
        payload = _consume_bundle(token, expected)
        exact_url, _hostname = validated_resource_url(payload.get("url"))
        return LabResourceDescriptor(
            token=token, **expected, url=exact_url,
            media_type=safe_text(payload.get("media_type"), 20),
            headers_present=bool(payload.get("headers_present")),
            requires_cookie=bool(payload.get("requires_cookie")), uses=payload["uses"],
        )
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
