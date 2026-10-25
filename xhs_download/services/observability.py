import hashlib
import logging
import re
from urllib.parse import urlparse

from django.conf import settings
from django.db import transaction

from ..models import (
    DownloadAuditEvent,
    DownloadAuditPhase,
    DownloadRequestLog,
    ObservabilityStatus,
    ParseRequestLog,
    ProviderAdminAuditLog,
    RoutingDecisionLog,
)
from .common import extract_url_from_text
from .rate_limits import get_client_identity, get_client_ip


logger = logging.getLogger(__name__)
MAX_ERROR_MESSAGE_LENGTH = 300
URL_PATTERN = re.compile(r"https?://[^\s<>'\"\]\)]+", re.IGNORECASE)
PROVIDER_MANIFEST_TOKEN_PATTERN = re.compile(
    r"(?i)(provider_manifest_token=)[^&\s]+"
)
CONNECTION_HOST_PATTERN = re.compile(
    r"(?i)(host=['\"])[^'\"]+(['\"])",
)
SENSITIVE_QUERY_VALUE_PATTERN = re.compile(
    r"(?i)(^|[?&;\s])"
    r"((?:token|sign|signature|auth_key|x-signature|x-bogus|msToken|verifyFp)=)"
    r"[^&\s;]+"
)
PROVIDER_RUNTIME_AUDIT_FIELDS = (
    "douyin_ultra_mode",
    "youtube_merge_mode",
    "third_party_mode",
    "douyin_ultra_rollout_percent",
    "youtube_merge_rollout_percent",
    "third_party_rollout_percent",
    "douyin_ultra_disabled_reason",
    "youtube_merge_disabled_reason",
    "third_party_disabled_reason",
    "third_party_allowed_public_hosts",
    "third_party_blocked_hosts",
    "third_party_allowed_public_platforms",
    "third_party_blocked_platforms",
    "kukutool_disabled",
    "douyin_ultra_total_timeout_seconds",
    "douyin_ultra_kukutool_grace_seconds",
    "kukutool_download_timeout_seconds",
    "kukutool_download_delivery_mode",
    "youtube_frontend_timeout_seconds",
)


def safe_int(value, default=0):
    try:
        if value is None or value == "":
            return default
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def short_text(value, limit=MAX_ERROR_MESSAGE_LENGTH):
    return str(value or "").strip()[:limit]


def sanitize_observability_text(value, limit=MAX_ERROR_MESSAGE_LENGTH):
    text = str(value or "").strip()
    if not text:
        return ""
    text = URL_PATTERN.sub("[url-redacted]", text)
    text = PROVIDER_MANIFEST_TOKEN_PATTERN.sub(
        r"\1[redacted]",
        text,
    )
    text = CONNECTION_HOST_PATTERN.sub(
        r"\1[host-redacted]\2",
        text,
    )
    text = SENSITIVE_QUERY_VALUE_PATTERN.sub(
        lambda match: f"{match.group(1)}{match.group(2)}[redacted]",
        text,
    )
    return text[: max(0, int(limit or 0))]


def sanitize_observability_value(value, limit=MAX_ERROR_MESSAGE_LENGTH):
    if isinstance(value, str):
        return sanitize_observability_text(value, limit=limit)
    if isinstance(value, dict):
        return {
            str(key): sanitize_observability_value(item, limit=limit)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [sanitize_observability_value(item, limit=limit) for item in value]
    return value


def salted_hash(value):
    value = str(value or "").strip()
    if not value:
        return ""
    salt = str(getattr(settings, "SECRET_KEY", ""))
    return hashlib.sha256(f"{salt}:{value}".encode("utf-8")).hexdigest()


def get_request_id(request):
    return str(getattr(request, "request_id", "") or "")


def request_user_is_staff(request):
    user = getattr(request, "user", None)
    return bool(getattr(user, "is_active", False) and getattr(user, "is_staff", False))


def request_privacy_fields(request):
    try:
        ip = get_client_ip(request)
    except Exception:
        ip = ""
    try:
        identity = get_client_identity(request)
    except Exception:
        identity = ""
    user_agent = request.META.get("HTTP_USER_AGENT", "") if request else ""
    return {
        "request_id": get_request_id(request),
        "client_identity_hash": salted_hash(identity),
        "ip_hash": salted_hash(ip),
        "user_agent_hash": salted_hash(user_agent),
        "is_staff": request_user_is_staff(request),
    }


def query_host(value):
    try:
        url = extract_url_from_text(value)
        return (urlparse(url).hostname or "").lower()
    except Exception:
        return ""


def media_item_summary(media_items):
    media_items = tuple(item for item in (media_items or ()) if isinstance(item, dict))
    image_count = sum(1 for item in media_items if item.get("type") == "image")
    video_count = sum(1 for item in media_items if item.get("type") == "video")
    providers = [str(item.get("provider") or "") for item in media_items if item.get("provider")]
    platforms = [
        str(item.get("source_platform") or item.get("platform") or "")
        for item in media_items
        if item.get("source_platform") or item.get("platform")
    ]
    estimated_bytes = sum(safe_int(item.get("size_bytes")) for item in media_items)
    return {
        "item_count": len(media_items),
        "image_count": image_count,
        "video_count": video_count,
        "estimated_bytes": estimated_bytes,
        "provider": providers[0] if providers else "",
        "source_platform": platforms[0] if platforms else "",
    }


def record_parse_request_log(request, **fields):
    try:
        payload = {
            **request_privacy_fields(request),
            "page_platform": short_text(fields.get("page_platform"), 32),
            "query_host": short_text(fields.get("query_host") or query_host(fields.get("query")), 255),
            "source_platform": short_text(fields.get("source_platform"), 64),
            "source_platform_name": short_text(fields.get("source_platform_name"), 120),
            "result_platform": short_text(fields.get("result_platform"), 64),
            "post_type": short_text(fields.get("post_type"), 32),
            "selected_provider": short_text(fields.get("selected_provider"), 80),
            "provider_label": short_text(fields.get("provider_label"), 120),
            "requested_ultra_hd": bool(fields.get("requested_ultra_hd")),
            "prefer_ultra_hd": bool(fields.get("prefer_ultra_hd")),
            "requested_youtube_merge": bool(fields.get("requested_youtube_merge")),
            "prefer_youtube_merge": bool(fields.get("prefer_youtube_merge")),
            "status": fields.get("status") or ObservabilityStatus.SUCCESS,
            "error_code": short_text(fields.get("error_code"), 80),
            "error_type": short_text(fields.get("error_type"), 120),
            "error_message_short": sanitize_observability_text(
                fields.get("error_message_short")
            ),
            "duration_ms": safe_int(fields.get("duration_ms")),
            "selected_provider_duration_ms": safe_int(fields.get("selected_provider_duration_ms")),
            "provider_total_duration_ms": safe_int(fields.get("provider_total_duration_ms")),
            "provider_fallback_count": safe_int(fields.get("provider_fallback_count")),
            "image_count": safe_int(fields.get("image_count")),
            "video_count": safe_int(fields.get("video_count")),
            "runtime_mode": short_text(fields.get("runtime_mode"), 32),
            "rollout_percent": safe_int(fields.get("rollout_percent")),
            "rollout_bucket": safe_int(fields.get("rollout_bucket")),
        }
        return ParseRequestLog.objects.create(**payload)
    except Exception:
        logger.exception("parse_request_log_write_failed")
        return None


def record_download_request_log(request, **fields):
    try:
        summary = media_item_summary(fields.get("media_items") or ())
        privacy = request_privacy_fields(request)
        payload = {
            "request_id": privacy["request_id"],
            "client_identity_hash": privacy["client_identity_hash"],
            "ip_hash": privacy["ip_hash"],
            "is_staff": privacy["is_staff"],
            "status": fields.get("status") or ObservabilityStatus.SUCCESS,
            "download_type": short_text(fields.get("download_type"), 32),
            "attempt_id": short_text(fields.get("attempt_id"), 64),
            "phase": short_text(fields.get("phase"), 24),
            "transport": short_text(fields.get("transport"), 32),
            "evidence_source": short_text(fields.get("evidence_source"), 24),
            "item_count": safe_int(fields.get("item_count"), summary["item_count"]),
            "image_count": safe_int(fields.get("image_count"), summary["image_count"]),
            "video_count": safe_int(fields.get("video_count"), summary["video_count"]),
            "estimated_bytes": safe_int(fields.get("estimated_bytes"), summary["estimated_bytes"]),
            "actual_bytes": safe_int(fields.get("actual_bytes")),
            "route_type": short_text(fields.get("route_type"), 32),
            "route_reason": short_text(
                sanitize_observability_text(fields.get("route_reason"), limit=200),
                200,
            ),
            "server_egress_estimated_bytes": safe_int(
                fields.get("server_egress_estimated_bytes")
            ),
            "browser_egress_estimated_bytes": safe_int(
                fields.get("browser_egress_estimated_bytes")
            ),
            "provider": short_text(fields.get("provider") or summary["provider"], 80),
            "source_platform": short_text(fields.get("source_platform") or summary["source_platform"], 64),
            "error_code": short_text(fields.get("error_code"), 80),
            "error_type": short_text(fields.get("error_type"), 120),
            "error_message_short": sanitize_observability_text(
                fields.get("error_message_short")
            ),
            "duration_ms": safe_int(fields.get("duration_ms")),
            "policy_cost_score": float(fields.get("policy_cost_score") or 0),
            "policy_provider_score": float(fields.get("policy_provider_score") or 0),
            "policy_routing_score": float(fields.get("policy_routing_score") or 0),
            "final_policy_route": short_text(fields.get("final_policy_route"), 32),
            "policy_confidence": float(fields.get("policy_confidence") or 0),
            "policy_active": bool(fields.get("policy_active")),
            "policy_decision_source": short_text(fields.get("policy_decision_source"), 80),
            "policy_estimated_savings_bytes": safe_int(
                fields.get("policy_estimated_savings_bytes")
            ),
            "recovery_count": safe_int(fields.get("recovery_count")),
            "fallback_count": safe_int(fields.get("fallback_count")),
            "recovery_action": short_text(fields.get("recovery_action"), 80),
            "recovery_reason": short_text(
                sanitize_observability_text(fields.get("recovery_reason"), limit=200),
                200,
            ),
            "fallback_from_route": short_text(fields.get("fallback_from_route"), 32),
            "fallback_to_route": short_text(fields.get("fallback_to_route"), 32),
            "failure_type": short_text(fields.get("failure_type"), 64),
            "auto_recovery_count": safe_int(fields.get("auto_recovery_count")),
            "provider_auto_switch_count": safe_int(fields.get("provider_auto_switch_count")),
            "failure_to_recovery_ms": safe_int(fields.get("failure_to_recovery_ms")),
            "healing_action": short_text(fields.get("healing_action"), 80),
            "healing_risk": short_text(fields.get("healing_risk"), 16),
            "healing_confidence": float(fields.get("healing_confidence") or 0),
        }
        if payload["phase"]:
            if not payload["attempt_id"] or payload["phase"] not in DownloadAuditPhase.values:
                raise ValueError("Invalid download audit event")
            business_status = payload["status"]
            payload["status"] = {
                DownloadAuditPhase.TRANSFER_COMPLETED: ObservabilityStatus.SUCCESS,
                DownloadAuditPhase.FAILED: ObservabilityStatus.FAILED,
                DownloadAuditPhase.UNKNOWN: "unknown",
                DownloadAuditPhase.CANCELLED: "cancelled",
            }.get(payload["phase"], "pending")
            business_log = None
            dispatch = (
                fields.get("dispatch_feedback") is True
                and payload["phase"] in {"prepared", "handed_off"}
            )
            terminal = (
                fields.get("legacy_log") is True
                and payload["phase"] in {"transfer_completed", "failed", "cancelled"}
            )
            # Event idempotency also protects the original business record. Browser
            # observations and additional phases never enter the old business table.
            with transaction.atomic():
                log, created = DownloadAuditEvent.objects.get_or_create(
                    attempt_id=payload["attempt_id"],
                    phase=payload["phase"],
                    transport=payload["transport"],
                    defaults=payload,
                )
                if (created and payload["evidence_source"] == "server_observed"
                        and (dispatch or terminal)):
                    business_payload = {
                        **payload, "status": business_status,
                        "attempt_id": "", "phase": "", "transport": "", "evidence_source": "",
                    }
                    business_log = DownloadRequestLog.objects.create(**business_payload)
                    log.legacy_log_id = business_log.pk
                    log.save(update_fields=("legacy_log_id",))
            if business_log is not None and dispatch and payload["route_type"]:
                record_download_business_feedback(business_log, business_payload)
            return log
        log = DownloadRequestLog.objects.create(**payload)
        record_download_business_feedback(log, payload)
        return log
    except Exception:
        logger.exception("download_request_log_write_failed")
        return None


def record_download_business_feedback(log, payload):
    record_routing_decision_log_from_download(payload)
    try:
        from .control_execution_tracker import track_control_execution_from_download_log

        track_control_execution_from_download_log(log)
    except Exception:
        logger.exception("policy_control_execution_track_failed")


def record_routing_decision_log_from_download(payload):
    route_type = str(payload.get("route_type") or "")
    if not route_type:
        return None
    try:
        return RoutingDecisionLog.objects.create(
            request_id=short_text(payload.get("request_id"), 128),
            route_type=short_text(route_type, 32),
            provider=short_text(payload.get("provider"), 80),
            estimated_server_bytes=safe_int(
                payload.get("server_egress_estimated_bytes")
            ),
            estimated_browser_bytes=safe_int(
                payload.get("browser_egress_estimated_bytes")
            ),
            actual_server_bytes=safe_int(payload.get("actual_bytes")),
            success=payload.get("status") == ObservabilityStatus.SUCCESS,
            error_code=short_text(payload.get("error_code"), 80),
            duration_ms=safe_int(payload.get("duration_ms")),
        )
    except Exception:
        logger.exception("routing_decision_log_write_failed")
        return None


def provider_runtime_snapshot(setting):
    return {
        field: sanitize_observability_value(getattr(setting, field))
        for field in PROVIDER_RUNTIME_AUDIT_FIELDS
    }


def changed_fields_between(before, after):
    fields = tuple(
        dict.fromkeys(
            list(PROVIDER_RUNTIME_AUDIT_FIELDS)
            + list((before or {}).keys())
            + list((after or {}).keys())
        )
    )
    return [field for field in fields if before.get(field) != after.get(field)]


def record_provider_admin_audit_log(request, *, action, before_snapshot, after_snapshot):
    try:
        user = getattr(request, "user", None)
        changed_fields = changed_fields_between(before_snapshot, after_snapshot)
        before_snapshot = sanitize_observability_value(before_snapshot)
        after_snapshot = sanitize_observability_value(after_snapshot)
        privacy = request_privacy_fields(request)
        ProviderAdminAuditLog.objects.create(
            request_id=privacy["request_id"],
            ip_hash=privacy["ip_hash"],
            user_agent_hash=privacy["user_agent_hash"],
            actor_id=getattr(user, "pk", None),
            actor_username=short_text(getattr(user, "get_username", lambda: "")(), 150),
            actor_is_superuser=bool(getattr(user, "is_superuser", False)),
            action=short_text(action, 80),
            changed_fields=changed_fields,
            before_snapshot=before_snapshot,
            after_snapshot=after_snapshot,
        )
    except Exception:
        logger.exception("provider_admin_audit_log_write_failed")
