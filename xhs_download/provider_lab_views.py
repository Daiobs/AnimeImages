from __future__ import annotations

from hashlib import sha256
import json
import ipaddress
import re

from django.contrib import admin
from django.core.cache import cache
from django.http import Http404, JsonResponse
from django.shortcuts import render
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_GET, require_POST, require_http_methods

from xhs_download.services.env_config import runtime_bool
from xhs_download.services.provider_lab.contracts import ProviderLabError, safe_text
from xhs_download.services.provider_lab.providers import (
    PROVIDER_REGISTRY,
    list_provider_descriptors,
    list_videodl_backend_inventory,
    normalized_source,
    run_provider,
    validate_confirmations,
)
from xhs_download.services.provider_lab.videodl_control import parse_videodl_options
from xhs_download.services.provider_lab.resources import (
    consume_resource_token,
    public_resource_descriptor,
)
from xhs_download.services.rate_limits import consume_fixed_window
from xhs_download.models import EvaluationBatch, EvaluationExecution
from xhs_download.services.provider_lab import history


PROVIDER_EXECUTIONS_PER_MINUTE = 20
SINGLE_FLIGHT_SECONDS = 90
HOST_HISTORY_MAX_ROWS = 200
HOST_HISTORY_MAX_BYTES = 2 * 1024 * 1024


def host_history_payload(value):
    # Accept canonical public DNS hostnames only; no DNS or provider calls.
    if (not isinstance(value, str) or len(value) > 253
            or not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", value)
            or "." not in value
            or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                   for label in value.split("."))
            or value.split(".")[-1] in {"localhost", "local", "internal", "test", "invalid", "example", "onion", "home", "lan"}):
        raise history.HistoryError("HISTORY_INPUT_INVALID")
    try:
        ipaddress.ip_address(value)
    except ValueError:
        pass
    else:
        raise history.HistoryError("HISTORY_INPUT_INVALID")
    try:
        if value.encode("ascii").decode("idna").encode("idna").decode("ascii") != value:
            raise history.HistoryError("HISTORY_INPUT_INVALID")
    except UnicodeError:
        raise history.HistoryError("HISTORY_INPUT_INVALID") from None
    _, canonical, _ = history.source_info("https://" + value + "/")
    if canonical != value or not re.search(r"[a-z]", value.split(".")[-1]):
        raise history.HistoryError("HISTORY_INPUT_INVALID")
    records = EvaluationExecution.objects.filter(summary__source_host=value)
    total = records.count()
    rows = []
    # Reserve space for the fixed envelope; count the same ASCII JSON encoding as JsonResponse.
    size = 1024
    for record in records.select_related("batch").order_by("-pk")[:HOST_HISTORY_MAX_ROWS]:
        batch = record.batch
        metadata = batch.metadata if isinstance(batch.metadata, dict) else {}
        row = {**history.execution_dict(record), "batch": {
            "id": batch.pk, "batch_key": batch.batch_key, "label": batch.label,
            "origin": batch.origin, "created_at": batch.created_at.isoformat(),
            "metadata": {key: metadata[key] for key in ("version", "evidence_kind") if key in metadata},
        }}
        row_size = len(json.dumps(row, ensure_ascii=True).encode("utf-8")) + 2
        if size + row_size > HOST_HISTORY_MAX_BYTES:
            break
        rows.append(row)
        size += row_size
    return {"ok": True, "source_host": value, "executions": rows,
            "total": total, "returned": len(rows), "truncated": len(rows) < total}


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
        "provider_lab_max_providers": len(PROVIDER_REGISTRY),
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
        if not {'source','provider_id'} <= payload_fields or payload_fields - {'source','provider_id','options','confirmations','history_execution_id'}:
            raise ProviderLabError("PROVIDER_SCHEMA_INVALID")
        provider_id = safe_text(payload.get("provider_id"), 80)
        if provider_id not in PROVIDER_REGISTRY:
            raise ProviderLabError("PROVIDER_NOT_SELECTED")
        confirmations = validate_confirmations(payload.get('confirmations'))
        options = None
        if "options" in payload:
            if provider_id != "videodl_repo":
                raise ProviderLabError("PROVIDER_SCHEMA_INVALID")
            options = parse_videodl_options(payload.get("options"))
        source = normalized_source(payload.get("source"))
        parent = None
        if payload.get("history_execution_id") is not None:
            if type(payload["history_execution_id"]) is not int:
                raise ProviderLabError("PROVIDER_SCHEMA_INVALID")
            parent = EvaluationExecution.objects.filter(pk=payload["history_execution_id"]).first()
            if (not parent or not history.retest_allowed(parent)
                    or parent.summary["source_url"] != source or parent.summary["provider_id"] != provider_id):
                raise ProviderLabError("PROVIDER_POLICY_REJECTED")
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
        record = None
        try:
            record = history.begin_execution(source, provider_id, payload.get("options"), parent.pk if parent else None)
        except Exception:
            pass  # A persistence failure must never cause a second parse.
        try:
            result = run_provider(
                provider_id=provider_id,
                source=source,
                user_id=request.user.pk,
                options=options,
                confirmations=confirmations,
            )
        except ProviderLabError as error:
            if record:
                try:
                    history.finish_execution(record, {"status": "failed", "error_code": error.code,
                                                       "error_detail": error.detail})
                except Exception:
                    pass
            raise
        finally:
            identity = execution_identity(request.user.pk, provider_id, source)
            cache.delete(f"provider-lab:single-flight:v1:{identity}")
        receipt = {"saved": False, "error_code": "HISTORY_SAVE_FAILED"}
        if record:
            try:
                history.finish_execution(record, result)
                receipt = {"saved": True, "id": record.pk}
            except Exception:
                pass
        return secure_json({**result, "history": receipt})
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


@never_cache
@require_http_methods(["GET", "POST"])
@csrf_protect
def provider_lab_history_view(request):
    require_provider_lab_access(request)
    try:
        if request.method == "POST":
            if len(request.body) > history.MAX_IMPORT_BYTES:
                raise history.HistoryError("HISTORY_INPUT_TOO_LARGE", 413)
            return secure_json(history.import_batch(parse_json_body(request)))
        batch_id = request.GET.get("batch_id")
        if "source_host" in request.GET:
            if "batch_id" in request.GET or len(request.GET.getlist("source_host")) != 1:
                raise history.HistoryError("HISTORY_INPUT_INVALID")
            return secure_json(host_history_payload(request.GET["source_host"]))
        if batch_id:
            batch = EvaluationBatch.objects.filter(pk=int(batch_id)).first()
            if not batch:
                raise history.HistoryError("HISTORY_NOT_FOUND", 404)
            return secure_json({"ok": True, "batch": history.batch_dict(batch),
                                "executions": [history.execution_dict(r) for r in batch.executions.order_by("pk")]})
        return secure_json({"ok": True, "batches": [history.batch_dict(b) for b in
                            EvaluationBatch.objects.order_by("-created_at")[:100]]})
    except history.HistoryError as error:
        return secure_json({"ok": False, "error_code": error.code}, status=error.status)
    except (ValueError, TypeError, ProviderLabError):
        return secure_json({"ok": False, "error_code": "HISTORY_INPUT_INVALID"}, status=400)
