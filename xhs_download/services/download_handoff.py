import json
import secrets
from dataclasses import dataclass
from uuid import uuid4

from django.core import signing
from django.core.cache import cache


DOWNLOAD_HANDOFF_SALT = "xhs_download.download_handoff.v1"
DOWNLOAD_HANDOFF_TTL_SECONDS = 60
DOWNLOAD_HANDOFF_MAX_USES = 1
DOWNLOAD_HANDOFF_MAX_CACHE_BYTES = 32 * 1024


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


def build_download_handoff_cache_key(ticket_id):
    return f"download-handoff:ticket:{ticket_id}"


def build_download_handoff_use_key(ticket_id):
    return f"download-handoff:uses:{ticket_id}"


def issue_download_handoff(
    *,
    item,
    base_filename,
    client_identity,
    recovered=False,
    recovery_used=False,
):
    ticket_id = uuid4().hex
    record = {
        "item": dict(item or {}),
        "base_filename": str(base_filename or "media")[:200],
        "client_identity": str(client_identity or ""),
        "recovered": bool(recovered),
        "recovery_used": bool(recovery_used),
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
    token = signing.dumps(
        {
            "ticket_id": ticket_id,
            "client_identity": str(client_identity or ""),
        },
        salt=DOWNLOAD_HANDOFF_SALT,
        compress=True,
    )
    return token


def load_download_handoff(token, *, client_identity, consume=True):
    try:
        payload = signing.loads(
            str(token or ""),
            salt=DOWNLOAD_HANDOFF_SALT,
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
    token_identity = str(payload.get("client_identity") or "")
    current_identity = str(client_identity or "")
    if (
        not ticket_id
        or not token_identity
        or not secrets.compare_digest(token_identity, current_identity)
    ):
        raise DownloadHandoffError(
            "下载链接与当前客户端不匹配，请重新点击下载。",
            error_code="DOWNLOAD_HANDOFF_CLIENT_MISMATCH",
            status_code=403,
        )

    record = cache.get(build_download_handoff_cache_key(ticket_id))
    if not isinstance(record, dict):
        raise DownloadHandoffError(
            "下载链接已失效，请重新点击下载。",
            error_code="DOWNLOAD_HANDOFF_NOT_FOUND",
            status_code=410,
        )
    if not secrets.compare_digest(
        str(record.get("client_identity") or ""),
        current_identity,
    ):
        raise DownloadHandoffError(
            "下载链接与当前客户端不匹配，请重新点击下载。",
            error_code="DOWNLOAD_HANDOFF_CLIENT_MISMATCH",
            status_code=403,
        )

    if consume:
        use_key = build_download_handoff_use_key(ticket_id)
        cache.add(use_key, 0, timeout=DOWNLOAD_HANDOFF_TTL_SECONDS)
        try:
            use_count = cache.incr(use_key)
        except ValueError:
            cache.set(use_key, 1, timeout=DOWNLOAD_HANDOFF_TTL_SECONDS)
            use_count = 1
        if use_count > DOWNLOAD_HANDOFF_MAX_USES:
            raise DownloadHandoffError(
                "下载链接使用次数已达上限，请重新点击下载。",
                error_code="DOWNLOAD_HANDOFF_USE_LIMIT",
                status_code=409,
            )

    item = record.get("item")
    if not isinstance(item, dict):
        raise DownloadHandoffError(
            "下载资源信息无效，请重新解析后再试。",
            error_code="DOWNLOAD_HANDOFF_INVALID",
            status_code=400,
        )
    return DownloadHandoff(
        ticket_id=ticket_id,
        item=dict(item),
        base_filename=str(record.get("base_filename") or "media"),
        recovered=bool(record.get("recovered")),
        recovery_used=bool(record.get("recovery_used")),
    )
