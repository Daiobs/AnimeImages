import json
import secrets
import threading
import time
from dataclasses import dataclass
from uuid import uuid4

from django.core import signing
from django.core.cache import cache

from .assets import MediaAsset, ParseResult
from .common import (
    XhsProviderTimeoutError,
    clean_filename,
    validate_external_url,
)
from .provider_manifest import provider_manifest_is_valid


DOUYIN_CONTINUATION_SALT = "xhs_download.douyin_continuation.v1"
DOUYIN_CONTINUATION_TTL_SECONDS = 15
DOUYIN_CONTINUATION_POLL_AFTER_MS = 1500
DOUYIN_CONTINUATION_MAX_POLLS = 5
DOUYIN_CONTINUATION_MAX_CACHE_BYTES = 32 * 1024
DOUYIN_CONTINUATION_ALLOWED_PROVIDERS = frozenset(
    {"kukutool", "tiantianjiexi"}
)
DOUYIN_CONTINUATION_METADATA_KEYS = frozenset(
    {
        "selected_provider",
        "source_provider",
        "provider_label",
        "fallback_reason",
        "ultra_race_winner",
        "ultra_race_provider_count",
        "kukutool_candidate_count",
        "tiantianjiexi_candidate_count",
    }
)


def douyin_media_host_suffixes():
    from .platforms.douyin import DOUYIN_MEDIA_HOST_SUFFIXES

    return DOUYIN_MEDIA_HOST_SUFFIXES


class DouyinContinuationPendingError(XhsProviderTimeoutError):
    def __init__(self, task_id):
        super().__init__("超高清资源仍在处理中，正在自动续查。")
        self.task_id = str(task_id or "")


class DouyinContinuationError(ValueError):
    def __init__(self, message, *, error_code, status_code):
        super().__init__(message)
        self.error_code = error_code
        self.status_code = status_code


@dataclass(frozen=True)
class DouyinContinuationOutcome:
    status: str
    result: ParseResult | None = None
    message: str = ""
    error_code: str = ""
    poll_after_ms: int = DOUYIN_CONTINUATION_POLL_AFTER_MS


def build_douyin_continuation_cache_key(task_id):
    return f"douyin-continuation:task:{task_id}"


def build_douyin_continuation_poll_key(task_id):
    return f"douyin-continuation:polls:{task_id}"


def safe_continuation_metadata(metadata):
    safe = {}
    for key in DOUYIN_CONTINUATION_METADATA_KEYS:
        value = (metadata or {}).get(key)
        if isinstance(value, bool):
            safe[key] = value
        elif isinstance(value, int):
            safe[key] = value
        elif isinstance(value, str):
            safe[key] = value[:120]
    return safe


def serialize_douyin_continuation_result(result):
    if (
        not isinstance(result, ParseResult)
        or result.platform != "douyin"
        or result.post_type != "video"
        or not result.video_assets
    ):
        raise ValueError("continuation result is not a Douyin video")

    asset = result.video_assets[0]
    provider = str(asset.provider or "").strip().lower()
    if provider not in DOUYIN_CONTINUATION_ALLOWED_PROVIDERS:
        raise ValueError("continuation provider is not allowed")
    item = asset.to_download_item()
    if not provider_manifest_is_valid(item):
        raise ValueError("continuation provider manifest is invalid")
    validate_external_url(
        asset.url,
        douyin_media_host_suffixes(),
        "续查资源 URL 不在允许的抖音媒体域名内",
    )
    for preview_url in (asset.cover_url, asset.cover_preview_url):
        if preview_url:
            validate_external_url(
                preview_url,
                douyin_media_host_suffixes(),
                "续查封面 URL 不在允许的抖音媒体域名内",
            )

    payload = {
        "platform": "douyin",
        "post_type": "video",
        "base_filename": clean_filename(result.base_filename)[:200],
        "asset": {
            "media_type": "video",
            "url": str(asset.url),
            "cover_url": str(asset.cover_url or ""),
            "cover_preview_url": str(asset.cover_preview_url or ""),
            "filename": str(asset.filename or "")[:200],
            "ext": str(asset.ext or "")[:16],
            "quality_label": str(asset.quality_label or "")[:80],
            "size_bytes": max(0, int(asset.size_bytes or 0)),
            "size_is_estimate": bool(asset.size_is_estimate),
            "asset_id": str(asset.asset_id or "")[:160],
            "provider": provider,
            "provider_resource_id": str(asset.provider_resource_id or "")[:160],
            "provider_cache_key": str(asset.provider_cache_key or "")[:240],
            "provider_manifest_token": str(
                asset.provider_manifest_token or ""
            ),
        },
        "metadata": safe_continuation_metadata(result.metadata),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if len(encoded) > DOUYIN_CONTINUATION_MAX_CACHE_BYTES:
        raise ValueError("continuation result exceeds cache limit")
    return payload


def deserialize_douyin_continuation_result(payload):
    if not isinstance(payload, dict):
        raise ValueError("continuation result is missing")
    asset_data = payload.get("asset")
    if (
        payload.get("platform") != "douyin"
        or payload.get("post_type") != "video"
        or not isinstance(asset_data, dict)
    ):
        raise ValueError("continuation result is invalid")

    provider = str(asset_data.get("provider") or "").strip().lower()
    if provider not in DOUYIN_CONTINUATION_ALLOWED_PROVIDERS:
        raise ValueError("continuation provider is invalid")
    item = {
        "type": "video",
        "url": str(asset_data.get("url") or ""),
        "asset_id": str(asset_data.get("asset_id") or ""),
        "provider": provider,
        "provider_resource_id": str(
            asset_data.get("provider_resource_id") or ""
        ),
        "provider_cache_key": str(asset_data.get("provider_cache_key") or ""),
        "provider_manifest_token": str(
            asset_data.get("provider_manifest_token") or ""
        ),
    }
    if not provider_manifest_is_valid(item):
        raise ValueError("continuation provider manifest is invalid")
    validate_external_url(
        item["url"],
        douyin_media_host_suffixes(),
        "续查资源 URL 不在允许的抖音媒体域名内",
    )
    for preview_url in (
        asset_data.get("cover_url"),
        asset_data.get("cover_preview_url"),
    ):
        if preview_url:
            validate_external_url(
                preview_url,
                douyin_media_host_suffixes(),
                "续查封面 URL 不在允许的抖音媒体域名内",
            )

    asset = MediaAsset(
        media_type="video",
        url=item["url"],
        platform="douyin",
        cover_url=str(asset_data.get("cover_url") or ""),
        cover_preview_url=str(asset_data.get("cover_preview_url") or ""),
        filename=str(asset_data.get("filename") or "")[:200],
        ext=str(asset_data.get("ext") or "")[:16],
        quality_label=str(asset_data.get("quality_label") or "")[:80],
        size_bytes=max(0, int(asset_data.get("size_bytes") or 0)),
        size_is_estimate=bool(asset_data.get("size_is_estimate")),
        media_host_suffixes=douyin_media_host_suffixes(),
        asset_id=item["asset_id"],
        provider=provider,
        provider_resource_id=item["provider_resource_id"],
        provider_cache_key=item["provider_cache_key"],
        provider_manifest_token=item["provider_manifest_token"],
    )
    return ParseResult(
        platform="douyin",
        post_type="video",
        base_filename=clean_filename(payload.get("base_filename"))[:200],
        assets=(asset,),
        metadata=safe_continuation_metadata(payload.get("metadata")),
    )


class DouyinContinuationCoordinator:
    def __init__(self, provider_count, *, settle_seconds=1.0):
        self.task_id = uuid4().hex
        self.provider_count = max(1, int(provider_count or 1))
        self.settle_seconds = max(0.0, float(settle_seconds or 0))
        self.created_at = time.time()
        self._lock = threading.Lock()
        self._active = True
        self._available = True
        self._unresolved_count = self.provider_count
        self._timeout_count = 0
        self._failure_count = 0
        self._best_result = None
        self._best_payload = None
        self._best_score = (0, 0)
        self._best_provider = ""
        self._candidate_ready_at = 0.0
        self._write_record()

    @property
    def available(self):
        with self._lock:
            return self._available

    def _record(self):
        status = "pending"
        if self._best_payload and (
            self._unresolved_count == 0
            or time.time() >= self._candidate_ready_at
        ):
            status = "success"
        elif self._unresolved_count == 0:
            status = "timeout" if self._timeout_count else "failed"
        record = {
            "status": status,
            "created_at": self.created_at,
            "unresolved_count": self._unresolved_count,
            "timeout_count": self._timeout_count,
            "failure_count": self._failure_count,
            "provider": self._best_provider,
            "candidate_ready_at": self._candidate_ready_at,
        }
        if self._best_payload:
            record["result"] = self._best_payload
        return record

    def _write_record(self):
        if not self._available or not self._active:
            return False
        record = self._record()
        encoded = json.dumps(
            record,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        if len(encoded) > DOUYIN_CONTINUATION_MAX_CACHE_BYTES:
            self._available = False
            return False
        try:
            cache.set(
                build_douyin_continuation_cache_key(self.task_id),
                record,
                timeout=DOUYIN_CONTINUATION_TTL_SECONDS,
            )
        except Exception:
            self._available = False
            return False
        return True

    def capture_future(
        self,
        future,
        *,
        provider_name,
        score_result,
        is_strong_result,
        prepare_result,
    ):
        with self._lock:
            if not self._active:
                return
            self._unresolved_count = max(0, self._unresolved_count - 1)
            try:
                result = future.result()
                if not result or not result.assets:
                    self._failure_count += 1
                else:
                    prepared_result = prepare_result(result)
                    payload = serialize_douyin_continuation_result(
                        prepared_result
                    )
                    score = tuple(score_result(prepared_result))
                    if self._best_result is None or score > self._best_score:
                        self._best_result = prepared_result
                        self._best_payload = payload
                        self._best_score = score
                        self._best_provider = str(provider_name or "")[:40]
                        self._candidate_ready_at = (
                            time.time() + self.settle_seconds
                        )
                    if is_strong_result(prepared_result):
                        self._candidate_ready_at = 0.0
            except XhsProviderTimeoutError:
                self._timeout_count += 1
            except Exception:
                self._failure_count += 1
            self._write_record()

    def get_best_result(self):
        with self._lock:
            return self._best_result

    def snapshot_result_and_pending(self):
        with self._lock:
            return (
                self._best_result,
                bool(
                    self._active
                    and self._available
                    and self._unresolved_count > 0
                ),
            )

    def has_pending_work(self):
        with self._lock:
            return bool(
                self._active
                and self._available
                and self._unresolved_count > 0
            )

    def abandon(self):
        with self._lock:
            self._active = False
            try:
                cache.delete(
                    build_douyin_continuation_cache_key(self.task_id)
                )
            except Exception:
                pass


def issue_douyin_continuation_token(
    *,
    task_id,
    client_identity,
    platform_key,
):
    return signing.dumps(
        {
            "task_id": str(task_id or ""),
            "client_identity": str(client_identity or ""),
            "platform_key": str(platform_key or "home")[:40],
        },
        salt=DOUYIN_CONTINUATION_SALT,
        compress=True,
    )


def load_douyin_continuation_token(token, *, client_identity):
    try:
        payload = signing.loads(
            str(token or ""),
            salt=DOUYIN_CONTINUATION_SALT,
            max_age=DOUYIN_CONTINUATION_TTL_SECONDS,
        )
    except signing.SignatureExpired as error:
        raise DouyinContinuationError(
            "解析续查已超时，请重新尝试。",
            error_code="DOUYIN_CONTINUATION_EXPIRED",
            status_code=410,
        ) from error
    except signing.BadSignature as error:
        raise DouyinContinuationError(
            "解析续查凭据无效，请重新尝试。",
            error_code="DOUYIN_CONTINUATION_INVALID",
            status_code=403,
        ) from error

    task_id = str(payload.get("task_id") or "")
    token_identity = str(payload.get("client_identity") or "")
    current_identity = str(client_identity or "")
    if (
        not task_id
        or not token_identity
        or not secrets.compare_digest(token_identity, current_identity)
    ):
        raise DouyinContinuationError(
            "解析续查与当前客户端不匹配，请重新尝试。",
            error_code="DOUYIN_CONTINUATION_CLIENT_MISMATCH",
            status_code=403,
        )
    return task_id, str(payload.get("platform_key") or "home")[:40]


def consume_douyin_continuation_poll(task_id):
    key = build_douyin_continuation_poll_key(task_id)
    try:
        cache.add(key, 0, timeout=DOUYIN_CONTINUATION_TTL_SECONDS)
        poll_count = cache.incr(key)
    except ValueError:
        cache.set(key, 1, timeout=DOUYIN_CONTINUATION_TTL_SECONDS)
        poll_count = 1
    except Exception as error:
        raise DouyinContinuationError(
            "解析续查服务暂时不可用，请重新尝试。",
            error_code="DOUYIN_CONTINUATION_CACHE_ERROR",
            status_code=503,
        ) from error
    return poll_count


def poll_douyin_continuation(token, *, client_identity):
    task_id, platform_key = load_douyin_continuation_token(
        token,
        client_identity=client_identity,
    )
    poll_count = consume_douyin_continuation_poll(task_id)
    if poll_count > DOUYIN_CONTINUATION_MAX_POLLS:
        return (
            DouyinContinuationOutcome(
                status="timeout",
                message="超高清解析续查已结束，请重新尝试。",
                error_code="DOUYIN_CONTINUATION_POLL_LIMIT",
            ),
            platform_key,
        )

    try:
        record = cache.get(build_douyin_continuation_cache_key(task_id))
    except Exception as error:
        raise DouyinContinuationError(
            "解析续查服务暂时不可用，请重新尝试。",
            error_code="DOUYIN_CONTINUATION_CACHE_ERROR",
            status_code=503,
        ) from error
    if not isinstance(record, dict):
        return (
            DouyinContinuationOutcome(
                status="timeout",
                message="解析续查已结束，请重新尝试。",
                error_code="DOUYIN_CONTINUATION_NOT_FOUND",
            ),
            platform_key,
        )

    status = str(record.get("status") or "pending")
    result_payload = record.get("result")
    if (
        status == "pending"
        and result_payload
        and time.time() >= float(record.get("candidate_ready_at") or 0)
    ):
        status = "success"
    if status == "success":
        try:
            result = deserialize_douyin_continuation_result(result_payload)
        except Exception:
            return (
                DouyinContinuationOutcome(
                    status="failed",
                    message="后台解析结果未通过安全校验，请重新尝试。",
                    error_code="DOUYIN_CONTINUATION_RESULT_REJECTED",
                ),
                platform_key,
            )
        return (
            DouyinContinuationOutcome(status="success", result=result),
            platform_key,
        )
    if status == "failed":
        return (
            DouyinContinuationOutcome(
                status="failed",
                message="超高清解析未能完成，请重新尝试。",
                error_code="DOUYIN_CONTINUATION_FAILED",
            ),
            platform_key,
        )
    if status == "timeout":
        return (
            DouyinContinuationOutcome(
                status="timeout",
                message="超高清解析超过续查时间限制，请重新尝试。",
                error_code="DOUYIN_CONTINUATION_TIMEOUT",
            ),
            platform_key,
        )
    return (
        DouyinContinuationOutcome(
            status="continuation_pending",
            message="超高清资源仍在处理中，正在自动续查。",
        ),
        platform_key,
    )
