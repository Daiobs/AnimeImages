from __future__ import annotations

from hashlib import sha256
import json

from django.contrib import admin
from django.core.cache import cache
from django.http import Http404, JsonResponse
from django.shortcuts import render
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_GET, require_POST

from xhs_download.services.env_config import runtime_bool
from xhs_download.services.provider_lab.contracts import ProviderLabError, safe_text
from xhs_download.services.provider_lab.providers import (
    PROVIDER_REGISTRY,
    list_provider_descriptors,
    list_videodl_backend_inventory,
    normalized_source,
    run_provider,
)
from xhs_download.services.provider_lab.videodl_control import parse_videodl_options
from xhs_download.services.provider_lab.resources import (
    consume_resource_token,
    public_resource_descriptor,
)
from xhs_download.services.rate_limits import consume_fixed_window


PROVIDER_EXECUTIONS_PER_MINUTE = 20
SINGLE_FLIGHT_SECONDS = 30


def provider_lab_enabled() -> bool:
    return runtime_bool("UPSTREAM_PROVIDER_LAB_ENABLED", False)


def provider_lab_access_allowed(request) -> bool:
    user = getattr(request, "user", None)
    return bool(
        provider_lab_enabled()
        and user
        and getattr(user, "is_authenticated", False)
        and getattr(user, "is_active", False)
        and getattr(user, "is_staff", False)
    )


def require_provider_lab_access(request) -> None:
    if not provider_lab_access_allowed(request):
        raise Http404


def secure_json(payload, *, status=200):
    response = JsonResponse(payload, status=status)
    response["Cache-Control"] = "no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response


def parse_json_body(request) -> dict:
    try:
        payload = json.loads(request.body or b"{}")
    except (TypeError, ValueError, UnicodeDecodeError) as error:
        raise ProviderLabError("PROVIDER_INVALID_JSON") from error
    if not isinstance(payload, dict):
        raise ProviderLabError("PROVIDER_INVALID_JSON")
    return payload


def execution_identity(user_id: int, provider_id: str, source: str) -> str:
    digest = sha256(source.encode("utf-8")).hexdigest()[:32]
    return f"{int(user_id)}:{safe_text(provider_id, 80)}:{digest}"


def reserve_single_flight(user_id: int, provider_id: str, source: str) -> bool:
    identity = execution_identity(user_id, provider_id, source)
    return bool(
        cache.add(
            f"provider-lab:single-flight:v1:{identity}",
            1,
            timeout=SINGLE_FLIGHT_SECONDS,
        )
    )


@never_cache
@require_GET
def provider_lab_view(request):
    require_provider_lab_access(request)
    context = {
        **admin.site.each_context(request),
        "title": "上游 Provider 测试工作台",
        "provider_lab_providers": list_provider_descriptors(),
        "provider_lab_run_url": "/admin/provider-lab/run/",
        "provider_lab_resource_url": "/admin/provider-lab/resource/",
        "provider_lab_max_concurrency": 3,
        "provider_lab_max_providers": 5,
        "videodl_backend_inventory": list_videodl_backend_inventory(),
    }
    response = render(request, "admin/provider_lab.html", context)
    response["Cache-Control"] = "no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response


@never_cache
@require_POST
@csrf_protect
def provider_lab_run_view(request):
    require_provider_lab_access(request)
    try:
        payload = parse_json_body(request)
        payload_fields = set(payload)
        if payload_fields not in (
            {"source", "provider_id"},
            {"source", "provider_id", "options"},
        ):
            raise ProviderLabError("PROVIDER_SCHEMA_INVALID")
        provider_id = safe_text(payload.get("provider_id"), 80)
        if provider_id not in PROVIDER_REGISTRY:
            raise ProviderLabError("PROVIDER_NOT_SELECTED")
        options = None
        if "options" in payload:
            if provider_id != "videodl_repo":
                raise ProviderLabError("PROVIDER_SCHEMA_INVALID")
            options = parse_videodl_options(payload.get("options"))
        source = normalized_source(payload.get("source"))
        limit = consume_fixed_window(
            "provider-lab-execution",
            str(request.user.pk),
            PROVIDER_EXECUTIONS_PER_MINUTE,
            60,
        )
        if not limit.allowed:
            raise ProviderLabError("PROVIDER_RATE_LIMITED", http_status=429)
        if not reserve_single_flight(request.user.pk, provider_id, source):
            raise ProviderLabError("PROVIDER_RATE_LIMITED", http_status=429)
        result = run_provider(
            provider_id=provider_id,
            source=source,
            user_id=request.user.pk,
            options=options,
        )
        return secure_json(result)
    except ProviderLabError as error:
        return secure_json(
            {
                "schema_version": "1.0",
                "status": "failed",
                "error_code": error.code,
            },
            status=error.http_status,
        )


@never_cache
@require_POST
@csrf_protect
def provider_lab_resource_view(request):
    require_provider_lab_access(request)
    try:
        payload = parse_json_body(request)
        if set(payload) != {
            "resource_token",
            "request_id",
            "provider_id",
            "asset_id",
        }:
            raise ProviderLabError("PROVIDER_SCHEMA_INVALID")
        descriptor = consume_resource_token(
            token=payload.get("resource_token"),
            user_id=request.user.pk,
            request_id=payload.get("request_id"),
            provider_id=payload.get("provider_id"),
            asset_id=payload.get("asset_id"),
        )
        return secure_json(public_resource_descriptor(descriptor))
    except ProviderLabError as error:
        return secure_json(
            {
                "schema_version": "1.0",
                "status": "failed",
                "error_code": error.code,
            },
            status=error.http_status,
        )
