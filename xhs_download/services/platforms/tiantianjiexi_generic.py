import logging
import time
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import requests
from django.conf import settings
from django.core.cache import cache

from ..assets import MediaAsset, ParseResult
from ..common import XhsProviderTimeoutError, clean_filename
from ..provider_manifest import build_provider_manifest_token
from ..rate_limits import acquire_concurrency, consume_fixed_window, release_concurrency
from .kukutool_generic import (
    canonicalize_source_url,
    integer_value,
    public_url_or_empty,
    source_key_for_url,
)


logger = logging.getLogger(__name__)

PROVIDER_ID = "tiantianjiexi_generic"
PROVIDER_LABEL = "天天解析"
ACCOUNT_STATUS_CACHE_KEY = "third-party:tiantianjiexi:account-status"
FAILURE_HIT_COUNT_KEY = "third-party:tiantianjiexi:failure-hit-count"


class TianTianJieXiApiError(RuntimeError):
    pass


class TianTianJieXiAuthError(TianTianJieXiApiError):
    pass


class TianTianJieXiRateLimitError(TianTianJieXiApiError):
    pass


def build_payload_cache_key(source_key):
    return f"third-party:tiantianjiexi:payload:{source_key}"


def build_failure_cache_key(source_key):
    return f"third-party:tiantianjiexi:failure:{source_key}"


def build_single_flight_key(source_key):
    return f"third-party:tiantianjiexi:single-flight:{source_key}"


def build_circuit_open_key():
    return "third-party:tiantianjiexi:circuit:open"


def build_circuit_failure_key(window):
    return f"third-party:tiantianjiexi:circuit:failures:{window}"


def provider_configured():
    return bool(
        getattr(settings, "THIRD_PARTY_PROVIDER_ENABLED", False)
        and getattr(settings, "TIANTIANJIEXI_PROVIDER_ENABLED", False)
        and str(getattr(settings, "TIANTIANJIEXI_API_BASE_URL", "") or "").strip()
        and str(getattr(settings, "TIANTIANJIEXI_PHONE", "") or "").strip()
        and str(getattr(settings, "TIANTIANJIEXI_PASSWORD", "") or "").strip()
    )


def api_configured():
    return bool(
        str(getattr(settings, "TIANTIANJIEXI_API_BASE_URL", "") or "").strip()
        and str(getattr(settings, "TIANTIANJIEXI_PHONE", "") or "").strip()
        and str(getattr(settings, "TIANTIANJIEXI_PASSWORD", "") or "").strip()
    )


def message_indicates_auth_error(message):
    text = str(message or "").lower()
    return any(marker in text for marker in ("password", "phone", "auth", "login", "unauthorized", "账号", "密码", "登录"))


def message_indicates_rate_limit(message):
    text = str(message or "").lower()
    return any(marker in text for marker in ("rate", "limit", "quota", "frequency", "too many", "限制", "频繁", "额度", "次数"))


def response_message(payload):
    if not isinstance(payload, dict):
        return ""
    for key in ("msg", "message", "error", "err_msg", "desc"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def payload_has_media(payload):
    if not isinstance(payload, dict):
        return False
    keys = {
        "videos",
        "video",
        "video_url",
        "videoUrl",
        "url",
        "download_url",
        "downloadUrl",
        "play_url",
        "playUrl",
        "images",
        "image_urls",
        "pics",
        "photos",
    }
    return any(payload.get(key) for key in keys)


def unwrap_api_payload(payload):
    if not isinstance(payload, dict):
        raise TianTianJieXiApiError("天天解析返回格式不是 JSON 对象")
    for key in ("data", "result", "detail", "info"):
        nested = payload.get(key)
        if isinstance(nested, dict) and payload_has_media(nested):
            merged = dict(nested)
            for meta_key in ("title", "desc", "description", "cover", "nickname"):
                if meta_key not in merged and payload.get(meta_key):
                    merged[meta_key] = payload.get(meta_key)
            return merged
    return payload


def ensure_api_success(payload):
    if payload_has_media(unwrap_api_payload(payload)):
        return
    status_value = payload.get("status") or payload.get("code") or payload.get("success")
    message = response_message(payload)
    if status_value in {True, "true", "success", "ok", 0, "0", 1, "1", 200, "200"}:
        return
    if message_indicates_auth_error(message):
        raise TianTianJieXiAuthError("天天解析认证失败")
    if message_indicates_rate_limit(message):
        raise TianTianJieXiRateLimitError("天天解析调用额度或频率受限")
    raise TianTianJieXiApiError(message or "天天解析未返回可用媒体资源")


def normalize_list(value):
    if value is None or value == "":
        return ()
    if isinstance(value, (list, tuple)):
        return tuple(value)
    return (value,)


def first_text(payload, keys):
    if not isinstance(payload, dict):
        return ""
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def field_value(raw, keys):
    if not isinstance(raw, dict):
        return None
    for key in keys:
        value = raw.get(key)
        if value:
            return value
    return None


def size_value(value):
    if isinstance(value, (int, float)):
        return max(0, int(value))
    text = str(value or "").strip().lower()
    if not text:
        return 0
    try:
        return max(0, int(float(text)))
    except ValueError:
        pass
    units = (("gb", 1024 ** 3), ("g", 1024 ** 3), ("mb", 1024 ** 2), ("m", 1024 ** 2), ("kb", 1024), ("k", 1024))
    for suffix, multiplier in units:
        if text.endswith(suffix):
            try:
                return max(0, int(float(text[: -len(suffix)].strip()) * multiplier))
            except ValueError:
                return 0
    return integer_value(value)


def quality_rank(label):
    text = str(label or "").lower()
    if any(marker in text for marker in ("原画", "无压缩", "source", "original", "ultra", "超高清", "超清")):
        return 700
    if "4k" in text or "2160" in text:
        return 600
    if "2k" in text or "1440" in text:
        return 500
    if "1080" in text:
        return 400
    if "720" in text:
        return 300
    if "480" in text:
        return 200
    return 100


def infer_quality_from_url(url):
    parsed = urlsplit(str(url or ""))
    params = parse_qs(parsed.query)

    def int_param(name):
        try:
            return int((params.get(name) or ["0"])[0] or 0)
        except (TypeError, ValueError):
            return 0

    br = max(int_param("br"), int_param("bt"))
    qs = str((params.get("qs") or [""])[0] or "")
    if qs == "13" or br >= 20000:
        return "原画"
    if br >= 10000:
        return "2K"
    if br >= 2500:
        return "1080P"
    if br >= 1500:
        return "720P"
    return ""


def safe_metadata_value(value):
    if isinstance(value, dict):
        sanitized = {}
        for key, item in value.items():
            if str(key).lower() in {
                "password",
                "phone",
                "token",
                "sign",
                "signature",
                "auth_key",
                "x-signature",
                "x_bogus",
                "x-bogus",
                "mstoken",
                "msToken".lower(),
                "verifyfp",
                "verifyFp".lower(),
                "cookie",
                "authorization",
            }:
                continue
            sanitized[key] = safe_metadata_value(item)
        return sanitized
    if isinstance(value, list):
        return [safe_metadata_value(item) for item in value]
    return value


class TianTianJieXiApiClient:
    def __init__(self, session=None):
        self.session = session or requests

    @property
    def base_url(self):
        return str(getattr(settings, "TIANTIANJIEXI_API_BASE_URL", "") or "").strip()

    @property
    def phone(self):
        return str(getattr(settings, "TIANTIANJIEXI_PHONE", "") or "").strip()

    @property
    def password(self):
        return str(getattr(settings, "TIANTIANJIEXI_PASSWORD", "") or "").strip()

    def parse_url(self, url, timeout_seconds=None):
        if not api_configured():
            raise TianTianJieXiAuthError("天天解析账号未配置")
        timeout = float(timeout_seconds if timeout_seconds is not None else getattr(settings, "TIANTIANJIEXI_TIMEOUT_SECONDS", 15) or 15)
        try:
            response = self.session.post(
                self.base_url,
                data={
                    "phone": self.phone,
                    "password": self.password,
                    "link": url,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=max(0.1, timeout),
            )
            response.raise_for_status()
            payload = response.json()
        except requests.Timeout as error:
            raise XhsProviderTimeoutError("天天解析接口超时") from error
        except requests.RequestException as error:
            raise TianTianJieXiApiError("天天解析接口请求失败") from error
        except ValueError as error:
            raise TianTianJieXiApiError("天天解析接口返回非 JSON") from error
        ensure_api_success(payload)
        return TianTianJieXiResultNormalizer.normalize_payload(unwrap_api_payload(payload))

    def query_account_status(self):
        if not api_configured():
            return {
                "available": False,
                "error_type": "TianTianJieXiAuthError",
                "error": "api_not_configured",
            }
        ttl_seconds = int(getattr(settings, "TIANTIANJIEXI_ACCOUNT_STATUS_CACHE_TTL_SECONDS", 300) or 0)
        if ttl_seconds > 0:
            cached = cache.get(ACCOUNT_STATUS_CACHE_KEY)
            if isinstance(cached, dict):
                return {**cached, "cached": True}
        timeout = float(getattr(settings, "TIANTIANJIEXI_TIMEOUT_SECONDS", 15) or 15)
        try:
            response = self.session.get(
                self.base_url,
                params={
                    "action": "query",
                    "phone": self.phone,
                    "password": self.password,
                },
                timeout=max(0.1, min(timeout, 5.0)),
            )
            response.raise_for_status()
            payload = response.json()
            status = {
                "available": True,
                "cached": False,
                "metadata": safe_metadata_value(payload),
            }
        except Exception as error:
            status = {
                "available": False,
                "cached": False,
                "error_type": type(error).__name__,
                "error": "account_status_unavailable",
            }
        if ttl_seconds > 0:
            cache.set(ACCOUNT_STATUS_CACHE_KEY, status, timeout=ttl_seconds)
        return status


class TianTianJieXiGenericProvider:
    provider_id = PROVIDER_ID
    label = PROVIDER_LABEL

    def __init__(self, client=None):
        self.client = client or TianTianJieXiApiClient()

    def get_cached_payload(self, source_key):
        try:
            payload = cache.get(build_payload_cache_key(source_key))
        except Exception:
            logger.exception("tiantianjiexi_payload_cache_read_failed", extra={"provider": PROVIDER_ID})
            return None
        return payload if isinstance(payload, dict) else None

    def cache_payload(self, source_key, payload):
        ttl_seconds = int(getattr(settings, "TIANTIANJIEXI_PAYLOAD_CACHE_TTL_SECONDS", 120) or 0)
        if ttl_seconds <= 0 or not isinstance(payload, dict):
            return
        try:
            cache.set(build_payload_cache_key(source_key), payload, timeout=ttl_seconds)
        except Exception:
            logger.exception("tiantianjiexi_payload_cache_write_failed", extra={"provider": PROVIDER_ID})

    def failure_cache_hit(self, source_key):
        try:
            reason = cache.get(build_failure_cache_key(source_key))
        except Exception:
            logger.exception("tiantianjiexi_failure_cache_read_failed", extra={"provider": PROVIDER_ID})
            return True
        if reason:
            try:
                cache.add(FAILURE_HIT_COUNT_KEY, 0, timeout=24 * 60 * 60)
                cache.incr(FAILURE_HIT_COUNT_KEY)
            except Exception:
                logger.exception("tiantianjiexi_failure_hit_count_failed", extra={"provider": PROVIDER_ID})
        return bool(reason)

    def cache_failure(self, source_key, reason):
        ttl_seconds = int(getattr(settings, "TIANTIANJIEXI_FAILURE_CACHE_TTL_SECONDS", 30) or 0)
        if ttl_seconds <= 0:
            return
        try:
            cache.set(build_failure_cache_key(source_key), str(reason or "failed")[:120], timeout=ttl_seconds)
        except Exception:
            logger.exception("tiantianjiexi_failure_cache_write_failed", extra={"provider": PROVIDER_ID})

    def clear_failure(self, source_key):
        try:
            cache.delete(build_failure_cache_key(source_key))
        except Exception:
            logger.exception("tiantianjiexi_failure_cache_delete_failed", extra={"provider": PROVIDER_ID})

    def circuit_is_open(self):
        now = int(time.time())
        try:
            open_until = int(cache.get(build_circuit_open_key(), 0) or 0)
            if open_until and open_until <= now:
                cache.delete(build_circuit_open_key())
                return False
            return open_until > now
        except Exception:
            logger.exception("tiantianjiexi_circuit_read_failed", extra={"provider": PROVIDER_ID})
            return True

    def record_success(self):
        now = int(time.time())
        window_seconds = max(1, int(getattr(settings, "TIANTIANJIEXI_CIRCUIT_WINDOW_SECONDS", 60) or 60))
        window = now // window_seconds
        try:
            cache.delete_many((build_circuit_open_key(), build_circuit_failure_key(window)))
        except Exception:
            logger.exception("tiantianjiexi_circuit_success_reset_failed", extra={"provider": PROVIDER_ID})

    def record_failure(self):
        now = int(time.time())
        window_seconds = max(1, int(getattr(settings, "TIANTIANJIEXI_CIRCUIT_WINDOW_SECONDS", 60) or 60))
        threshold = max(1, int(getattr(settings, "TIANTIANJIEXI_CIRCUIT_FAILURE_THRESHOLD", 5) or 5))
        open_seconds = max(1, int(getattr(settings, "TIANTIANJIEXI_CIRCUIT_OPEN_SECONDS", 120) or 120))
        window = now // window_seconds
        try:
            key = build_circuit_failure_key(window)
            cache.add(key, 0, timeout=window_seconds + 5)
            count = cache.incr(key)
            if count >= threshold:
                cache.set(build_circuit_open_key(), now + open_seconds, timeout=open_seconds)
        except Exception:
            logger.exception("tiantianjiexi_circuit_failure_record_failed", extra={"provider": PROVIDER_ID})

    def wait_for_cached_payload(self, source_key, timeout_seconds):
        deadline = time.monotonic() + max(0.0, float(timeout_seconds or 0.0))
        while time.monotonic() < deadline:
            payload = self.get_cached_payload(source_key)
            if payload:
                return payload
            time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
        return self.get_cached_payload(source_key)

    def release_single_flight(self, lock_key, lock_token):
        try:
            if lock_key and cache.get(lock_key) == lock_token:
                cache.delete(lock_key)
        except Exception:
            logger.exception("tiantianjiexi_single_flight_release_failed", extra={"provider": PROVIDER_ID})

    def parse(self, url, timeout_seconds=None):
        if not provider_configured():
            raise TianTianJieXiAuthError("天天解析 Provider 未启用或账号未配置")
        source_key = source_key_for_url(url)
        cached = self.get_cached_payload(source_key)
        if cached:
            return cached
        if self.circuit_is_open():
            raise XhsProviderTimeoutError("天天解析暂时不可用，请稍后再试")
        if self.failure_cache_hit(source_key):
            raise XhsProviderTimeoutError("天天解析刚刚失败，请稍后再试")

        timeout_seconds = float(timeout_seconds if timeout_seconds is not None else getattr(settings, "THIRD_PARTY_FRONTEND_TIMEOUT_SECONDS", 3.0) or 3.0)
        lock_key = build_single_flight_key(source_key)
        lock_token = uuid4().hex
        lock_ttl = max(
            int(timeout_seconds) + 5,
            int(getattr(settings, "TIANTIANJIEXI_TIMEOUT_SECONDS", 15) or 15) + 5,
        )
        try:
            lock_acquired = cache.add(lock_key, lock_token, timeout=lock_ttl)
        except Exception:
            logger.exception("tiantianjiexi_single_flight_acquire_failed", extra={"provider": PROVIDER_ID})
            lock_acquired = True
        if not lock_acquired:
            cached = self.wait_for_cached_payload(source_key, timeout_seconds)
            if cached:
                return cached
            raise XhsProviderTimeoutError("天天解析正在处理中，请稍后重试同一链接。")

        concurrency_acquired = False
        try:
            request_limit = consume_fixed_window(
                "third-party-tiantianjiexi",
                "all",
                getattr(settings, "TIANTIANJIEXI_REQUEST_LIMIT_PER_MINUTE", 20),
                60,
            )
            if not request_limit.allowed:
                raise TianTianJieXiRateLimitError("天天解析请求频率受限")
            concurrency = acquire_concurrency(
                "third-party-tiantianjiexi-global",
                "all",
                getattr(settings, "TIANTIANJIEXI_GLOBAL_MAX_CONCURRENCY", 2),
            )
            if not concurrency.allowed:
                raise TianTianJieXiRateLimitError("天天解析并发受限")
            concurrency_acquired = True

            attempts = max(1, int(getattr(settings, "TIANTIANJIEXI_RETRIES", 2) or 1))
            deadline = time.monotonic() + max(0.1, timeout_seconds)
            last_error = None
            for attempt in range(attempts):
                try:
                    remaining_timeout = deadline - time.monotonic()
                    if remaining_timeout <= 0:
                        raise XhsProviderTimeoutError("天天解析接口超时")
                    payload = self.client.parse_url(
                        url,
                        timeout_seconds=min(
                            max(0.1, remaining_timeout),
                            float(getattr(settings, "TIANTIANJIEXI_TIMEOUT_SECONDS", 15) or 15),
                        ),
                    )
                    if not isinstance(payload, dict):
                        raise TianTianJieXiApiError("天天解析返回格式无效")
                    self.cache_payload(source_key, payload)
                    self.clear_failure(source_key)
                    self.record_success()
                    return payload
                except (TianTianJieXiAuthError, TianTianJieXiRateLimitError):
                    raise
                except Exception as error:
                    last_error = error
                    if attempt >= attempts - 1:
                        raise
            raise last_error or TianTianJieXiApiError("天天解析失败")
        except Exception as error:
            self.cache_failure(source_key, type(error).__name__)
            self.record_failure()
            raise
        finally:
            if concurrency_acquired:
                release_concurrency("third-party-tiantianjiexi-global", "all")
            self.release_single_flight(lock_key, lock_token)

    def query_account_status(self):
        return self.client.query_account_status()

    def health(self, source_key=""):
        now = int(time.time())
        window_seconds = max(1, int(getattr(settings, "TIANTIANJIEXI_CIRCUIT_WINDOW_SECONDS", 60) or 60))
        window = now // window_seconds
        circuit_open_until = 0
        circuit_failure_count = 0
        failure_cache_reason = ""
        failure_hit_count = 0
        single_flight_active = False
        cache_error = ""
        account_status = {}
        account_status_cached = False
        try:
            circuit_open_until = int(cache.get(build_circuit_open_key(), 0) or 0)
            circuit_failure_count = int(cache.get(build_circuit_failure_key(window), 0) or 0)
            failure_hit_count = int(cache.get(FAILURE_HIT_COUNT_KEY, 0) or 0)
            account_status_cached = bool(cache.get(ACCOUNT_STATUS_CACHE_KEY))
            if source_key:
                failure_cache_reason = str(cache.get(build_failure_cache_key(source_key)) or "")
                single_flight_active = bool(cache.get(build_single_flight_key(source_key)))
        except Exception as error:
            cache_error = type(error).__name__
            logger.debug("tiantianjiexi_health_cache_read_failed", extra={"provider": PROVIDER_ID}, exc_info=True)
        if (
            bool(getattr(settings, "TIANTIANJIEXI_PROVIDER_ENABLED", False))
            and bool(getattr(settings, "TIANTIANJIEXI_HEALTH_QUERY_ACCOUNT_STATUS_ENABLED", True))
            and api_configured()
        ):
            try:
                account_status = self.query_account_status()
                account_status_cached = account_status_cached or bool(account_status.get("cached"))
            except Exception as error:
                account_status = {"available": False, "error_type": type(error).__name__}
        return {
            "provider": PROVIDER_ID,
            "label": PROVIDER_LABEL,
            "enabled": provider_configured(),
            "provider_enabled": bool(getattr(settings, "TIANTIANJIEXI_PROVIDER_ENABLED", False)),
            "api_configured": api_configured(),
            "api_base_url_configured": bool(str(getattr(settings, "TIANTIANJIEXI_API_BASE_URL", "") or "").strip()),
            "account_status_cached": account_status_cached,
            "account_status_available": bool(account_status.get("available")),
            "account_status": account_status,
            "capabilities": {
                "official_api": True,
                "supports_video": True,
                "supports_image": True,
                "supports_audio": True,
                "supports_multi_quality": True,
            },
            "circuit": {
                "open": circuit_open_until > now,
                "open_until": circuit_open_until,
                "failure_count": circuit_failure_count,
            },
            "failure_cache": {
                "source_key": source_key,
                "hit": bool(failure_cache_reason),
                "reason": failure_cache_reason,
                "hit_count": failure_hit_count,
            },
            "single_flight": {
                "source_key": source_key,
                "active": single_flight_active,
            },
            "cache_error": cache_error,
        }


class TianTianJieXiResultNormalizer:
    def __init__(self, platform_id="third_party"):
        self.platform_id = platform_id

    @classmethod
    def normalize_payload(cls, payload):
        if not isinstance(payload, dict):
            raise TianTianJieXiApiError("天天解析返回格式无效")
        title = first_text(payload, ("title", "name"))
        nickname = first_text(payload, ("nickname", "author", "user"))
        description = first_text(payload, ("desc", "description"))
        if nickname and title and nickname not in title:
            title = f"{nickname}_{title}"
        cover = first_text(payload, ("cover", "cover_url", "pic", "image", "thumbnail"))
        audio_url = first_text(payload, ("audio", "audio_url", "music", "music_url"))
        return {
            "title": title,
            "description": description,
            "cover": cover,
            "videos": cls.collect_video_resources(payload),
            "images": cls.collect_image_urls(payload),
            "audio_url": audio_url,
        }

    @classmethod
    def collect_video_resources(cls, payload):
        resources = []

        def add(raw):
            if isinstance(raw, str):
                resources.append({"url": raw, "quality": "video", "size": 0})
                return
            if not isinstance(raw, dict):
                return
            url = field_value(
                raw,
                ("url", "video_url", "videoUrl", "download_url", "downloadUrl", "play_url", "playUrl"),
            )
            if not url:
                return
            quality = field_value(
                raw,
                (
                    "quality",
                    "quality_label",
                    "qualityLabel",
                    "type",
                    "name",
                    "resolution",
                    "clarity",
                ),
            ) or infer_quality_from_url(url) or "video"
            size = field_value(
                raw,
                (
                    "size",
                    "file_size",
                    "fileSize",
                    "filesize",
                    "size_in_bytes",
                    "sizeInBytes",
                    "bytes",
                ),
            )
            resources.append({"url": url, "quality": str(quality), "size": size_value(size)})
            for nested_key in (
                "streams",
                "stream",
                "video_list",
                "videoList",
                "download_url_list",
                "downloadUrlList",
                "url_list",
                "urlList",
                "play_addr",
                "playAddr",
            ):
                for nested_item in normalize_list(raw.get(nested_key)):
                    add(nested_item)

        for key in ("videos", "video", "video_url", "videoUrl", "url", "download_url", "downloadUrl", "play_url", "playUrl"):
            value = payload.get(key)
            for item in normalize_list(value):
                add(item)
        add(payload)
        deduped = {}
        for resource in resources:
            current = deduped.get(resource["url"])
            score = (quality_rank(resource["quality"]), resource["size"])
            if current is None or score > (quality_rank(current["quality"]), current["size"]):
                deduped[resource["url"]] = resource
        return tuple(
            sorted(
                deduped.values(),
                key=lambda item: (quality_rank(item["quality"]), item["size"]),
                reverse=True,
            )
        )

    @classmethod
    def collect_image_urls(cls, payload):
        seen = set()
        results = []
        for key in ("images", "image_urls", "pics", "photos"):
            for item in normalize_list(payload.get(key)):
                candidates = (item,)
                if isinstance(item, dict):
                    candidates = (
                        field_value(item, ("url", "src", "image", "pic", "download_url", "downloadUrl")),
                    )
                for candidate in candidates:
                    if isinstance(candidate, str) and candidate.strip() and candidate not in seen:
                        seen.add(candidate)
                        results.append(candidate)
        return tuple(results)

    def build_provider_asset_fields(self, rule, source_url, media_type, index, url):
        source_key = source_key_for_url(source_url)
        asset_id = f"third_party:{rule.platform_id}:{source_key}:{media_type}:{index}"
        provider_cache_key = f"third_party:{PROVIDER_ID}:{source_key}"
        return {
            "asset_id": asset_id,
            "provider": PROVIDER_ID,
            "provider_resource_id": source_key,
            "provider_cache_key": provider_cache_key,
            "provider_manifest_token": build_provider_manifest_token(
                asset_id=asset_id,
                provider=PROVIDER_ID,
                provider_resource_id=source_key,
                provider_cache_key=provider_cache_key,
                url=url,
                media_type=media_type,
            ),
        }

    def build_metadata(self, rule, platform_name, candidate_count, source_url, payload):
        metadata = {
            "source_platform": rule.platform_id,
            "source_platform_name": platform_name,
            "source_provider": PROVIDER_ID,
            "provider_label": PROVIDER_LABEL,
            "candidate_count": candidate_count,
            "canonical_source_url": canonicalize_source_url(source_url),
        }
        if payload.get("audio_url"):
            metadata["audio_url"] = payload["audio_url"]
        if payload.get("description"):
            metadata["description"] = payload["description"]
        return metadata

    def normalize(self, *, rule, platform_name, source_url, payload):
        if not isinstance(payload, dict):
            raise ValueError("天天解析结果格式无效")
        title = clean_filename(str(payload.get("title") or platform_name))
        platform_id = getattr(rule, "platform_id", "")
        cover_url = public_url_or_empty(
            payload.get("cover"),
            getattr(rule, "image_media_host_suffixes", ()) or getattr(rule, "media_host_suffixes", ()),
            platform_id,
            "cover",
        )
        video_suffixes = getattr(rule, "video_media_host_suffixes", ()) or getattr(rule, "media_host_suffixes", ())
        valid_videos = []
        for resource in payload.get("videos") or ():
            if not isinstance(resource, dict):
                continue
            url = public_url_or_empty(resource.get("url"), video_suffixes, platform_id, "videos")
            if not url:
                continue
            valid_videos.append({
                **resource,
                "url": url,
                "quality": str(resource.get("quality") or resource.get("quality_label") or "video"),
                "size": size_value(resource.get("size")),
            })
        valid_videos = sorted(
            valid_videos,
            key=lambda item: (quality_rank(item["quality"]), item["size"]),
            reverse=True,
        )
        if valid_videos:
            assets = tuple(
                MediaAsset(
                    media_type="video",
                    url=resource["url"],
                    platform=self.platform_id,
                    cover_url=cover_url,
                    cover_preview_url=cover_url,
                    quality_label=resource["quality"],
                    size_bytes=resource["size"],
                    **self.build_provider_asset_fields(
                        rule,
                        source_url,
                        "video",
                        index,
                        resource["url"],
                    ),
                )
                for index, resource in enumerate(valid_videos[:1], start=1)
            )
            return ParseResult(
                platform=self.platform_id,
                post_type="video",
                base_filename=title or "third_party",
                assets=assets,
                metadata=self.build_metadata(rule, platform_name, len(valid_videos), source_url, payload),
            )

        image_suffixes = getattr(rule, "image_media_host_suffixes", ()) or getattr(rule, "media_host_suffixes", ())
        image_urls = []
        for image_url in payload.get("images") or ():
            url = public_url_or_empty(image_url, image_suffixes, platform_id, "images")
            if url:
                image_urls.append(url)
        if image_urls:
            assets = tuple(
                MediaAsset(
                    media_type="image",
                    url=image_url,
                    platform=self.platform_id,
                    preview_url=image_url,
                    **self.build_provider_asset_fields(
                        rule,
                        source_url,
                        "image",
                        index,
                        image_url,
                    ),
                )
                for index, image_url in enumerate(image_urls, start=1)
            )
            return ParseResult(
                platform=self.platform_id,
                post_type="image",
                base_filename=title or "third_party",
                assets=assets,
                metadata=self.build_metadata(rule, platform_name, len(image_urls), source_url, payload),
            )
        raise ValueError("天天解析结果不包含允许的媒体资源")
