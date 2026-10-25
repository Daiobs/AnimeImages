"""Download observations only; these tokens never authorize media access."""

import logging
import re
from uuid import uuid4

from django.core import signing
from django.db.models import Case, IntegerField, Max, OuterRef, Subquery, Value, When
from django.urls import reverse
from django.utils.crypto import constant_time_compare

from ..models import (
    DownloadAuditEvent,
    DownloadAuditEvidence,
    DownloadAuditPhase,
    DownloadAuditTransport,
)
from .observability import record_download_request_log, request_privacy_fields


logger = logging.getLogger(__name__)
SCHEMA = "download_audit_v1"
TOKEN_SALT = "download-audit-v1"
TOKEN_MAX_AGE = 24 * 60 * 60
MAX_BYTES = (1 << 53) - 1
BROWSER_TRANSPORTS = frozenset((
    DownloadAuditTransport.BROWSER_NATIVE,
    DownloadAuditTransport.BROWSER_STREAMING,
    DownloadAuditTransport.BROWSER_BLOB,
))
IDENTITY_FIELDS = ("client_identity_hash", "ip_hash", "user_agent_hash", "is_staff")
PAYLOAD_FIELDS = frozenset(("schema", "token", "phase", "transport", "actual_bytes", "error_code"))
ERROR_CODE = re.compile(r"^[A-Za-z0-9_.:-]{0,80}$")
ATTEMPT_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _identity(request):
    privacy = request_privacy_fields(request)
    return {key: privacy[key] for key in IDENTITY_FIELDS}


def _bytes(value):
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_BYTES:
        raise ValueError("Invalid audit byte count")
    return value


def record_transfer_audit(
    request, *, phase, transport="", attempt_id="", evidence_source="server_observed",
    media_items=(), actual_bytes=0, error_code="", **fields,
):
    """Fail-open event writer. Callers retain the returned attempt_id for later phases."""
    try:
        attempt_id = attempt_id or uuid4().hex
        if not isinstance(attempt_id, str) or not ATTEMPT_ID.fullmatch(attempt_id):
            raise ValueError("Invalid audit attempt")
        if phase not in DownloadAuditPhase.values:
            raise ValueError("Invalid audit phase")
        unstarted = (
            phase in {"prepared", "failed"} and transport == "" and actual_bytes == 0
            and evidence_source == DownloadAuditEvidence.SERVER_OBSERVED
        )
        if transport not in DownloadAuditTransport.values and not unstarted:
            raise ValueError("Invalid audit transport")
        if evidence_source not in DownloadAuditEvidence.values:
            raise ValueError("Invalid audit evidence source")
        actual_bytes = _bytes(actual_bytes)
        if transport == DownloadAuditTransport.BROWSER_NATIVE and (
            phase in {"transfer_started", "transfer_completed"} or actual_bytes
        ):
            raise ValueError("Native transfer is not observable by this application")
        return record_download_request_log(
            request, **fields, phase=phase, transport=transport, attempt_id=attempt_id,
            evidence_source=evidence_source, media_items=media_items,
            actual_bytes=actual_bytes, error_code=error_code,
        )
    except Exception:
        logger.warning("download_transfer_audit_write_failed", exc_info=True)
        return None


def issue_browser_download_audit(request, item, *, prepared_log=None):
    """Attach an identity-bound receipt to an already authorized item, without I/O to media."""
    if (not isinstance(item, dict)
            or str(item.get("type") or item.get("media_type") or "").lower() not in {"video", "audio"}):
        return item
    try:
        endpoint = reverse("media_browser_download_telemetry")
        identity = _identity(request)
        # A missing identity must not produce a transferable anonymous receipt.
        if not identity["client_identity_hash"] or not identity["ip_hash"]:
            return item
        trusted_item = dict(item)
        trusted_item["provider"] = item.get("provider") or item.get("provider_id") or ""
        prepared = prepared_log
        if prepared is not None:
            if (not isinstance(prepared, DownloadAuditEvent) or not prepared.pk
                    or prepared.phase != DownloadAuditPhase.PREPARED or prepared.transport
                    or prepared.evidence_source != DownloadAuditEvidence.SERVER_OBSERVED
                    or any(getattr(prepared, key) != identity[key]
                           for key in ("client_identity_hash", "ip_hash", "is_staff"))):
                return item
        else:
            prepared = record_transfer_audit(
                request, phase=DownloadAuditPhase.PREPARED,
                media_items=(trusted_item,), download_type="browser",
            )
        if prepared is None:
            return item
        token = signing.dumps({
            "schema": SCHEMA, "attempt_id": prepared.attempt_id,
            "prepared_id": prepared.pk, "identity": identity,
        }, salt=TOKEN_SALT)
        return {**item, "download_audit": {
            "schema": SCHEMA, "token": token, "attempt_id": prepared.attempt_id,
            "endpoint": endpoint,
        }}
    except Exception:
        logger.warning("download_audit_issue_failed", exc_info=True)
        return item


def record_browser_download_audit(request, payload):
    """Validate a client observation. Invalid input raises ValueError; DB failures return False."""
    if not isinstance(payload, dict) or set(payload) - PAYLOAD_FIELDS or payload.get("schema") != SCHEMA:
        raise ValueError("Invalid download audit schema")
    token = payload.get("token")
    if not isinstance(token, str) or not 1 <= len(token) <= 4096:
        raise ValueError("Invalid download audit token")
    phase, transport = payload.get("phase"), payload.get("transport")
    if not isinstance(phase, str) or phase not in DownloadAuditPhase.values or phase == "prepared":
        raise ValueError("Invalid browser audit phase")
    if not isinstance(transport, str) or transport not in BROWSER_TRANSPORTS:
        raise ValueError("Invalid browser audit transport")
    actual_bytes = _bytes(payload.get("actual_bytes", 0))
    error_code = payload.get("error_code", "")
    if not isinstance(error_code, str) or not ERROR_CODE.fullmatch(error_code):
        raise ValueError("Invalid browser audit error code")
    if transport == DownloadAuditTransport.BROWSER_NATIVE and (
        phase in {"transfer_started", "transfer_completed"} or actual_bytes
    ):
        raise ValueError("Native transfer cannot be reported as observed")
    try:
        receipt = signing.loads(token, salt=TOKEN_SALT, max_age=TOKEN_MAX_AGE)
    except (signing.BadSignature, ValueError, TypeError) as error:
        raise ValueError("Invalid or expired download audit token") from error
    if not isinstance(receipt, dict) or receipt.get("schema") != SCHEMA:
        raise ValueError("Invalid download audit receipt")
    identity = receipt.get("identity")
    current_identity = _identity(request)
    if not isinstance(identity, dict) or any(
        not constant_time_compare(str(identity.get(key)), str(value))
        for key, value in current_identity.items()
    ):
        raise ValueError("Download audit identity mismatch")
    attempt_id, prepared_id = receipt.get("attempt_id"), receipt.get("prepared_id")
    if (not isinstance(attempt_id, str) or not ATTEMPT_ID.fullmatch(attempt_id)
            or isinstance(prepared_id, bool) or not isinstance(prepared_id, int)):
        raise ValueError("Invalid download audit receipt")
    try:
        prepared = DownloadAuditEvent.objects.filter(
            pk=prepared_id, attempt_id=attempt_id, phase=DownloadAuditPhase.PREPARED,
            transport="", evidence_source=DownloadAuditEvidence.SERVER_OBSERVED,
            client_identity_hash=identity["client_identity_hash"], ip_hash=identity["ip_hash"],
            is_staff=identity["is_staff"],
        ).first()
    except Exception:
        logger.warning("download_audit_receipt_read_failed", exc_info=True)
        return False
    if prepared is None:
        raise ValueError("Unknown download audit preparation")
    log = record_transfer_audit(
        request, phase=phase, transport=transport, attempt_id=attempt_id,
        evidence_source=DownloadAuditEvidence.CLIENT_OBSERVED,
        download_type=prepared.download_type, provider=prepared.provider,
        source_platform=prepared.source_platform, item_count=prepared.item_count,
        image_count=prepared.image_count, video_count=prepared.video_count,
        estimated_bytes=prepared.estimated_bytes, actual_bytes=actual_bytes,
        error_code=error_code,
    )
    return log is not None


def download_attempts(queryset):
    """Rank the full attempt first; the caller's window selects the outcome timestamp."""
    events = queryset.model._default_manager.using(queryset.db).exclude(attempt_id="").exclude(phase="")
    ranked = events.filter(attempt_id=OuterRef("attempt_id")).annotate(
        outcome_rank=Case(
            When(phase__in=("failed", "cancelled"), then=Value(5)),
            When(phase="transfer_completed", then=Value(3)),
            When(phase="unknown", then=Value(2)),
            When(phase="transfer_started", then=Value(1)),
            default=Value(0), output_field=IntegerField(),
        ),
    ).order_by("-outcome_rank", "-created_at", "-pk")
    received = events.filter(attempt_id=OuterRef("attempt_id")).order_by().values("attempt_id").annotate(
        total=Max("actual_bytes"),
    ).values("total")[:1]
    return queryset.filter(pk=Subquery(ranked.values("pk")[:1])).annotate(observed_bytes=Subquery(received))


def legacy_download_logs(queryset):
    """Unmigrated business rows, excluding audit-linked dispatch compatibility."""
    linked_logs = DownloadAuditEvent.objects.using(queryset.db).filter(
        legacy_log_id__isnull=False,
    ).values("legacy_log_id")
    return queryset.filter(phase="", attempt_id="").exclude(pk__in=Subquery(linked_logs))


def operational_download_logs(queryset):
    """Keep old business outcomes, never audit-linked dispatch or client feedback."""
    return legacy_download_logs(queryset).exclude(
        evidence_source=DownloadAuditEvidence.CLIENT_OBSERVED,
    ).exclude(
        status__in=("pending", "cancelled", "unknown"),
    )
