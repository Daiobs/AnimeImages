import json
import secrets
import time
from dataclasses import dataclass
from uuid import uuid4

from django.conf import settings
from django.core import signing
from django.core.cache import cache

from .common import validate_media_url
from .downloads import provider_manifest_allows_public_download


DOWNLOAD_HANDOFF_SALT = "xhs_download.download_handoff.stream.v2"
DOWNLOAD_STATUS_SALT = "xhs_download.download_handoff.status.v2"
DOWNLOAD_STREAM_START_TIMEOUT_SECONDS = max(
    1.0,
    min(
        55.0,
        float(
            getattr(
                settings,
                "DOWNLOAD_STREAM_START_TIMEOUT_SECONDS",
                50.0,
            )
            or 50.0
        ),
    ),
)
DOWNLOAD_HANDOFF_TTL_SECONDS = max(
    60,
    int(DOWNLOAD_STREAM_START_TIMEOUT_SECONDS) + 10,
)
DOWNLOAD_HANDOFF_MAX_USES = 1
DOWNLOAD_HANDOFF_MAX_CACHE_BYTES = 32 * 1024
DOWNLOAD_HANDOFF_ITEM_STRING_LIMITS = {
    "type": 16,
    "url": 8192,
    "filename": 240,
    "provider": 64,
    "provider_resource_id": 1024,
    "provider_cache_key": 1024,
    "provider_manifest_token": 16384,
    "asset_id": 1024,
}
DOWNLOAD_HANDOFF_PROVIDER_FIELDS = frozenset(
    {
        "provider",
        "provider_resource_id",
        "provider_cache_key",
        "provider_manifest_token",
        "asset_id",
    }
)
DOWNLOAD_HANDOFF_MAX_SIZE_BYTES = (1 << 63) - 1
DOWNLOAD_STATUS_STATES = frozenset(
    {
        "issued",
        "connecting",
        "refreshing",
        "streaming",
        "completed",
        "failed",
        "expired",
    }
)
DOWNLOAD_STATUS_ERROR_MESSAGES = {
    "DOWNLOAD_CONCURRENCY_LIMIT": "当前已有下载任务正在处理，请等待完成后再试。",
    "DOWNLOAD_DAILY_BYTES_LIMIT_GLOBAL": "今日全站下载流量已达到限制，请稍后再试。",
    "DOWNLOAD_DAILY_BYTES_LIMIT_IP": "今日下载流量已达到限制，请明天再试。",
    "DOWNLOAD_HANDOFF_EXPIRED": "下载链接已过期，请重新点击下载。",
    "DOWNLOAD_STREAM_START_TIMEOUT": "资源连接超时，请重新点击下载或重新解析后再试。",
    "PROVIDER_MANIFEST_REJECTED": "Provider 签名资源未通过校验。",
    "PROXY_FAILED_RECOVERY_BLOCKED": "资源连接失败，自动恢复未成功，请重新解析后再试。",
    "SINGLE_ARCHIVE_SIZE_LIMIT": "本次下载资源超过当前大小限制。",
    "SINGLE_VIDEO_SIZE_LIMIT": "视频资源超过当前大小限制。",
    "RATE_LIMITED": "请求过于频繁，请稍后再试。",
    "TIANTIANJIEXI_REFRESH_FAILED": "资源已失效，自动刷新失败，请重新解析。",
    "TIANTIANJIEXI_REFRESH_TIMEOUT": "资源刷新超时，请重新解析后再试。",
    "TIANTIANJIEXI_RESOURCE_EXPIRED": "资源已失效，请重新解析后再试。",
    "UPSTREAM_CONNECTION_ERROR": "上游资源暂时无法连接，请稍后重试。",
    "UPSTREAM_SSL_ERROR": "上游资源连接异常，请稍后重试。",
    "VISITOR_VIDEO_DOWNLOAD_DISABLED": "当前环境暂不允许访客下载视频资源。",
}
DOWNLOAD_STATUS_DEFAULT_ERROR_CODE = "PROXY_FAILED_RECOVERY_BLOCKED"


class DownloadHandoffError(ValueError):
    def __init__(self, message, *, error_code, status_code):
        super().__init__(message)
        self.error_code = error_code
        self.status_code = status_code


@dataclass(frozen=True)
class DownloadHandoff:
    ticket_id: str
    item: dict
    base_filename: str
    recovered: bool
    recovery_used: bool
    expires_at: float


@dataclass(frozen=True)
class DownloadHandoffTokens:
    stream_token: str
    status_token: str
    expires_in_seconds: int


@dataclass(frozen=True)
class DownloadStatus:
    ticket_id: str
    status: str
    error_code: str
    message: str
    recovered: bool
    stream_started: bool
    upstream_attempt_count: int
    updated_at: float
    expires_at: float

    def as_public_dict(self):
        payload = {
            "ok": self.status not in {"failed", "expired"},
            "status": self.status,
            "recovered": self.recovered,
            "stream_started": self.stream_started,
            "upstream_attempt_count": self.upstream_attempt_count,
        }
        if self.error_code:
            payload["error_code"] = self.error_code
        if self.message:
            payload["message"] = self.message
        return payload


def build_download_handoff_cache_key(ticket_id):
    return f"download-handoff:ticket:{ticket_id}"


def build_download_handoff_use_key(ticket_id):
    return f"download-handoff:uses:{ticket_id}"


def build_download_status_cache_key(ticket_id):
    return f"download-handoff:status:{ticket_id}"


def _remaining_ttl(expires_at):
    remaining = float(expires_at or 0) - time.time()
    if remaining <= 0:
        return 0
    return max(1, int(remaining + 0.999))


def _bounded_attempt_count(value):
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError):
        parsed = 0
    return max(0, min(2, parsed))


def _build_signed_token(ticket_id, *, salt):
    return signing.dumps(
        {"ticket_id": ticket_id},
        salt=salt,
        compress=True,
    )


def _load_signed_ticket(token, *, salt):
    try:
        payload = signing.loads(
            str(token or ""),
            salt=salt,
            max_age=DOWNLOAD_HANDOFF_TTL_SECONDS,
        )
    except signing.SignatureExpired as error:
        raise DownloadHandoffError(
            "下载链接已过期，请重新点击下载。",
            error_code="DOWNLOAD_HANDOFF_EXPIRED",
            status_code=410,
        ) from error
    except signing.BadSignature as error:
        raise DownloadHandoffError(
            "下载链接无效，请重新点击下载。",
            error_code="DOWNLOAD_HANDOFF_INVALID",
            status_code=403,
        ) from error

    ticket_id = str(payload.get("ticket_id") or "")
    if not ticket_id:
        raise DownloadHandoffError(
            "下载链接无效，请重新点击下载。",
            error_code="DOWNLOAD_HANDOFF_INVALID",
            status_code=403,
        )
    return ticket_id


def _validate_client_identity(record, client_identity):
    record_identity = str(record.get("client_identity") or "")
    current_identity = str(client_identity or "")
    if (
        not record_identity
        or not current_identity
        or not secrets.compare_digest(record_identity, current_identity)
    ):
        raise DownloadHandoffError(
            "下载链接与当前客户端不匹配，请重新点击下载。",
            error_code="DOWNLOAD_HANDOFF_CLIENT_MISMATCH",
            status_code=403,
        )


def _expired_handoff_error():
    return DownloadHandoffError(
        "下载链接已过期，请重新点击下载。",
        error_code="DOWNLOAD_HANDOFF_EXPIRED",
        status_code=410,
    )


def _invalid_handoff_item(message="下载资源信息无效，请重新解析后再试。"):
    return DownloadHandoffError(
        message,
        error_code="DOWNLOAD_HANDOFF_INVALID",
        status_code=400,
    )


def _bounded_handoff_text(item, key, *, required=False):
    value = item.get(key)
    if value in (None, ""):
        if required:
            raise _invalid_handoff_item()
        return ""
    if not isinstance(value, str):
        raise _invalid_handoff_item()
    value = value.strip()
    if required and not value:
        raise _invalid_handoff_item()
    if len(value) > DOWNLOAD_HANDOFF_ITEM_STRING_LIMITS[key]:
        raise _invalid_handoff_item("下载资源信息过长，请重新解析后再试。")
    return value


def _bounded_handoff_record_text(value, *, default, max_length):
    if value in (None, ""):
        return default
    if not isinstance(value, str):
        raise _invalid_handoff_item()
    value = value.strip() or default
    if len(value) > max_length:
        raise _invalid_handoff_item("下载资源信息过长，请重新解析后再试。")
    return value


def sanitize_download_handoff_item(item):
    if not isinstance(item, dict):
        raise _invalid_handoff_item()

    media_type = _bounded_handoff_text(item, "type", required=True).lower()
    if media_type not in {"image", "video"}:
        raise _invalid_handoff_item()
    sanitized = {
        "type": media_type,
        "url": _bounded_handoff_text(item, "url", required=True),
    }

    for key in (
        "filename",
        "provider",
        "provider_resource_id",
        "provider_cache_key",
        "provider_manifest_token",
        "asset_id",
    ):
        value = _bounded_handoff_text(item, key)
        if value:
            sanitized[key] = value

    if "size_bytes" in item:
        size_bytes = item.get("size_bytes")
        if isinstance(size_bytes, bool) or not isinstance(size_bytes, int):
            raise _invalid_handoff_item()
        if not 0 <= size_bytes <= DOWNLOAD_HANDOFF_MAX_SIZE_BYTES:
            raise _invalid_handoff_item()
        sanitized["size_bytes"] = size_bytes

    if sanitized.get("provider"):
        if not provider_manifest_allows_public_download(sanitized):
            raise DownloadHandoffError(
                "Provider 签名资源未通过校验。",
                error_code="PROVIDER_MANIFEST_REJECTED",
                status_code=400,
            )
    else:
        for key in DOWNLOAD_HANDOFF_PROVIDER_FIELDS:
            sanitized.pop(key, None)
        try:
            validate_media_url(sanitized["url"])
        except (TypeError, ValueError) as error:
            raise _invalid_handoff_item() from error

    return sanitized


def issue_download_handoff_tokens(
    *,
    item,
    base_filename,
    client_identity,
    recovered=False,
    recovery_used=False,
):
    ticket_id = uuid4().hex
    issued_at = time.time()
    expires_at = issued_at + DOWNLOAD_HANDOFF_TTL_SECONDS
    record = {
        "item": sanitize_download_handoff_item(item),
        "base_filename": _bounded_handoff_record_text(
            base_filename,
            default="media",
            max_length=200,
        ),
        "client_identity": _bounded_handoff_record_text(
            client_identity,
            default="",
            max_length=256,
        ),
        "recovered": bool(recovered),
        "recovery_used": bool(recovery_used),
        "expires_at": expires_at,
    }
    encoded = json.dumps(
        record,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if len(encoded) > DOWNLOAD_HANDOFF_MAX_CACHE_BYTES:
        raise DownloadHandoffError(
            "下载资源信息过大，请重新解析后再试。",
            error_code="DOWNLOAD_PREPARE_PAYLOAD_TOO_LARGE",
            status_code=400,
        )

    cache.set(
        build_download_handoff_cache_key(ticket_id),
        record,
        timeout=DOWNLOAD_HANDOFF_TTL_SECONDS,
    )
    cache.set(
        build_download_status_cache_key(ticket_id),
        {
            "status": "issued",
            "error_code": "",
            "message": "",
            "recovered": bool(recovered),
            "stream_started": False,
            "upstream_attempt_count": 0,
            "updated_at": issued_at,
            "expires_at": expires_at,
        },
        timeout=DOWNLOAD_HANDOFF_TTL_SECONDS,
    )
    return DownloadHandoffTokens(
        stream_token=_build_signed_token(
            ticket_id,
            salt=DOWNLOAD_HANDOFF_SALT,
        ),
        status_token=_build_signed_token(
            ticket_id,
            salt=DOWNLOAD_STATUS_SALT,
        ),
        expires_in_seconds=DOWNLOAD_HANDOFF_TTL_SECONDS,
    )


def issue_download_handoff(
    *,
    item,
    base_filename,
    client_identity,
    recovered=False,
    recovery_used=False,
):
    return issue_download_handoff_tokens(
        item=item,
        base_filename=base_filename,
        client_identity=client_identity,
        recovered=recovered,
        recovery_used=recovery_used,
    ).stream_token


def load_download_handoff(token, *, client_identity, consume=True):
    ticket_id = _load_signed_ticket(token, salt=DOWNLOAD_HANDOFF_SALT)
    record = cache.get(build_download_handoff_cache_key(ticket_id))
    if not isinstance(record, dict):
        raise _expired_handoff_error()
    _validate_client_identity(record, client_identity)

    remaining_ttl = _remaining_ttl(record.get("expires_at"))
    if not remaining_ttl:
        raise _expired_handoff_error()

    if consume:
        claimed = cache.add(
            build_download_handoff_use_key(ticket_id),
            DOWNLOAD_HANDOFF_MAX_USES,
            timeout=remaining_ttl,
        )
        if not claimed:
            raise DownloadHandoffError(
                "下载链接已使用，请重新点击下载。",
                error_code="DOWNLOAD_HANDOFF_ALREADY_USED",
                status_code=409,
            )

    item = sanitize_download_handoff_item(record.get("item"))
    return DownloadHandoff(
        ticket_id=ticket_id,
        item=dict(item),
        base_filename=str(record.get("base_filename") or "media"),
        recovered=bool(record.get("recovered")),
        recovery_used=bool(record.get("recovery_used")),
        expires_at=float(record.get("expires_at") or 0),
    )


def load_download_status(token, *, client_identity):
    ticket_id = _load_signed_ticket(token, salt=DOWNLOAD_STATUS_SALT)
    handoff_record = cache.get(build_download_handoff_cache_key(ticket_id))
    if not isinstance(handoff_record, dict):
        raise _expired_handoff_error()
    _validate_client_identity(handoff_record, client_identity)
    if not _remaining_ttl(handoff_record.get("expires_at")):
        raise _expired_handoff_error()

    record = cache.get(build_download_status_cache_key(ticket_id))
    if not isinstance(record, dict):
        raise _expired_handoff_error()
    if not _remaining_ttl(record.get("expires_at")):
        raise _expired_handoff_error()

    status = str(record.get("status") or "")
    if status not in DOWNLOAD_STATUS_STATES:
        status = "failed"
    error_code = ""
    message = ""
    if status == "failed":
        error_code = str(record.get("error_code") or "").upper()
        if error_code not in DOWNLOAD_STATUS_ERROR_MESSAGES:
            error_code = DOWNLOAD_STATUS_DEFAULT_ERROR_CODE
        message = DOWNLOAD_STATUS_ERROR_MESSAGES[error_code]
    elif status == "expired":
        error_code = "DOWNLOAD_HANDOFF_EXPIRED"
        message = DOWNLOAD_STATUS_ERROR_MESSAGES[error_code]
    return DownloadStatus(
        ticket_id=ticket_id,
        status=status,
        error_code=error_code,
        message=message,
        recovered=bool(record.get("recovered")),
        stream_started=bool(record.get("stream_started")),
        upstream_attempt_count=_bounded_attempt_count(
            record.get("upstream_attempt_count")
        ),
        updated_at=float(record.get("updated_at") or 0),
        expires_at=float(record.get("expires_at") or 0),
    )


def update_download_status(
    ticket_id,
    status,
    *,
    error_code="",
    recovered=None,
    upstream_attempt_count=None,
):
    status = str(status or "")
    if status not in DOWNLOAD_STATUS_STATES:
        raise ValueError("Unsupported download status")

    key = build_download_status_cache_key(str(ticket_id or ""))
    record = cache.get(key)
    if not isinstance(record, dict):
        return False
    remaining_ttl = _remaining_ttl(record.get("expires_at"))
    if not remaining_ttl:
        return False

    normalized_error_code = ""
    message = ""
    if status == "failed":
        normalized_error_code = str(error_code or "").upper()
        if normalized_error_code not in DOWNLOAD_STATUS_ERROR_MESSAGES:
            normalized_error_code = DOWNLOAD_STATUS_DEFAULT_ERROR_CODE
        message = DOWNLOAD_STATUS_ERROR_MESSAGES[normalized_error_code]
    elif status == "expired":
        normalized_error_code = "DOWNLOAD_HANDOFF_EXPIRED"
        message = DOWNLOAD_STATUS_ERROR_MESSAGES[normalized_error_code]

    updated = {
        **record,
        "status": status,
        "error_code": normalized_error_code,
        "message": message,
        "stream_started": bool(
            record.get("stream_started")
            or status in {"streaming", "completed"}
        ),
        "updated_at": time.time(),
    }
    if recovered is not None:
        updated["recovered"] = bool(recovered)
    if upstream_attempt_count is not None:
        updated["upstream_attempt_count"] = _bounded_attempt_count(
            upstream_attempt_count
        )
    cache.set(key, updated, timeout=remaining_ttl)
    return True
