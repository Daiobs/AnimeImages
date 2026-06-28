import hashlib
import logging
from urllib.parse import urlparse

from django.conf import settings

from ..models import (
    DownloadRequestLog,
    ObservabilityStatus,
    ParseRequestLog,
    ProviderAdminAuditLog,
)
from .common import extract_url_from_text
from .rate_limits import get_client_identity, get_client_ip


logger = logging.getLogger(__name__)
MAX_ERROR_MESSAGE_LENGTH = 300
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
            "error_message_short": short_text(fields.get("error_message_short")),
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
            "item_count": safe_int(fields.get("item_count"), summary["item_count"]),
            "image_count": safe_int(fields.get("image_count"), summary["image_count"]),
            "video_count": safe_int(fields.get("video_count"), summary["video_count"]),
            "estimated_bytes": safe_int(fields.get("estimated_bytes"), summary["estimated_bytes"]),
            "actual_bytes": safe_int(fields.get("actual_bytes")),
            "provider": short_text(fields.get("provider") or summary["provider"], 80),
            "source_platform": short_text(fields.get("source_platform") or summary["source_platform"], 64),
            "error_code": short_text(fields.get("error_code"), 80),
            "error_type": short_text(fields.get("error_type"), 120),
            "error_message_short": short_text(fields.get("error_message_short")),
            "duration_ms": safe_int(fields.get("duration_ms")),
        }
        return DownloadRequestLog.objects.create(**payload)
    except Exception:
        logger.exception("download_request_log_write_failed")
        return None


def provider_runtime_snapshot(setting):
    return {field: getattr(setting, field) for field in PROVIDER_RUNTIME_AUDIT_FIELDS}


def changed_fields_between(before, after):
    return [field for field in PROVIDER_RUNTIME_AUDIT_FIELDS if before.get(field) != after.get(field)]


def record_provider_admin_audit_log(request, *, action, before_snapshot, after_snapshot):
    try:
        user = getattr(request, "user", None)
        changed_fields = changed_fields_between(before_snapshot, after_snapshot)
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
