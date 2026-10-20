import logging
import math
import re
import time
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import requests
from django.core.cache import cache

from ..assets import MediaAsset, ParseResult
from ..common import XhsProviderTimeoutError, clean_filename
from ..douyin_ultra_attempts import (
    DouyinUltraAttemptAccountingError,
    DouyinUltraAttemptRateLimitedError,
    DouyinUltraAttemptSupersededError,
)
from ..env_config import (
    runtime_bool,
    runtime_float,
    runtime_int,
    runtime_str,
)
from ..provider_manifest import build_provider_manifest_token
from ..provider_reliability import (
    ProviderFailureClass,
    ProviderOutcome,
    ProviderResultInvalidError,
    classify_provider_failure,
    normalize_provider_failure_class,
    record_provider_attempt,
)
from ..rate_limits import acquire_concurrency, release_concurrency, reserve_fixed_window
from .kukutool_generic import (
    canonicalize_source_url,
    collect_service_video_resources,
    filter_service_format_rows,
    ServiceFormatPayload,
    service_context,
    mixed_image_asset_allowed,
    integer_value,
    public_url_or_empty,
    source_key_for_url,
)
from .tiantian_serial import serialized, TianTianAdmissionDeferred


logger = logging.getLogger(__name__)

PROVIDER_ID = "tiantianjiexi_generic"
PROVIDER_LABEL = "天天解析"
ACCOUNT_STATUS_CACHE_KEY = "third-party:tiantianjiexi:account-status"
FAILURE_HIT_COUNT_KEY = "third-party:tiantianjiexi:failure-hit-count"
XHS_NOTE_ID_RE = re.compile(r"(?<![0-9a-f])([0-9a-f]{24})(?![0-9a-f])", re.IGNORECASE)
BILIBILI_VIDEO_ID_RE = re.compile(r"(?:BV[0-9A-Za-z]{10}|av[1-9][0-9]*)")
SERVICE_FACTS_VERSION = 1


def bilibili_identity_facts(payload):
    """Retain comparable IDs/pages, never source URLs or the native response."""
    facts = []

    def add(identity="", page=""):
        fact = {}
        if BILIBILI_VIDEO_ID_RE.fullmatch(identity):
            fact["video_id"] = identity
        if page:
            fact["p"] = page
        if fact and fact not in facts:
            facts.append(fact)

    def visit(value, depth=0, work_context=True):
        if depth > 10:
            if {"incomplete": True} not in facts:
                facts.append({"incomplete": True})
            return
        if isinstance(value, (list, tuple)):
            for child in value:
                visit(child, depth + 1, False)
        elif isinstance(value, dict):
            for key, child in value.items():
                if key == "_bilibili_identity_facts":
                    # Also survives a cache serializer's conversion to plain dict.
                    if isinstance(child, (list, tuple)):
                        for fact in child:
                            if isinstance(fact, dict):
                                if fact.get("incomplete") is True and {"incomplete": True} not in facts:
                                    facts.append({"incomplete": True})
                                page = str(fact.get("p", ""))
                                add(str(fact.get("video_id") or ""),
                                    page if page.isdigit() else "")
                    continue
                if key in {"bvid", "bv", "avid", "av", "aid", "video_id", "videoId"}:
                    text = str(child or "")
                    if key in {"aid", "avid", "av"} and text.isdigit():
                        text = "av" + text
                    add(identity=text)
                if work_context and key == "p" and str(child).isdigit():
                    add(page=str(child))
                if key in {"source_url", "sourceUrl", "canonical_url", "canonicalUrl",
                           "share_url", "shareUrl", "link", "url"} and isinstance(child, str):
                    try:
                        parsed = urlsplit(child)
                    except ValueError:
                        continue
                    host = (parsed.hostname or "").lower()
                    if host == "bilibili.com" or host.endswith(".bilibili.com"):
                        match = re.fullmatch(r"/video/([^/]+)/?", parsed.path)
                        if match:
                            pages = parse_qs(parsed.query).get("p", ["1"])
                            # Non-P1 URLs are a conflict, not a missing identity.
                            page = pages[0] if len(pages) == 1 and pages[0].isdigit() else "0"
                            add(match[1], page)
                if isinstance(child, (dict, list, tuple)):
                    visit(child, depth + 1, work_context and key in {
                        "data", "result", "detail", "info"})

    visit(payload)
    return tuple(facts)


class TianTianJieXiApiError(RuntimeError):
    def __init__(self, message, *, diagnostic_detail="SERVICE_RESPONSE_REJECTED"):
        super().__init__(message)
        self.diagnostic_detail = diagnostic_detail


class TianTianJieXiAuthError(TianTianJieXiApiError):
    pass


class TianTianJieXiUncertainError(TianTianJieXiApiError):
    """A sent transport attempt must not enter the retry loop."""


class TianTianJieXiUnsupportedError(TianTianJieXiApiError):
    """The service explicitly declined this source, not the account."""


class TianTianJieXiRateLimitError(TianTianJieXiApiError):
    provider_outcome = ProviderOutcome.FAILURE_RATE_LIMIT
    provider_failure_class = ProviderFailureClass.RATE_LIMIT


class TianTianJieXiFailureCooldownError(XhsProviderTimeoutError):
    def __init__(self, retry_after_seconds=0):
        super().__init__("天天解析刚刚失败，请稍后再试")
        self.retry_after_seconds = max(0, int(retry_after_seconds or 0))


def build_payload_cache_key(source_key):
    return f"third-party:tiantianjiexi:payload:{source_key}"


def build_payload_cache_metadata_key(source_key):
    return f"third-party:tiantianjiexi:payload-meta:{source_key}"


def build_failure_cache_key(source_key):
    return f"third-party:tiantianjiexi:failure:{source_key}"


def build_single_flight_key(source_key):
    return f"third-party:tiantianjiexi:single-flight:{source_key}"


def build_circuit_open_key():
    return "third-party:tiantianjiexi:circuit:open"


def build_circuit_failure_key(window):
    return f"third-party:tiantianjiexi:circuit:failures:{window}"


def source_key_for_identity(source_identity):
    """Build a bounded work key from an adapter-issued logical identity.

    The Douyin namespace deliberately retains the existing canonical /video URL
    hash so live payload and recovery cache keys stay compatible during rollout.
    Other callers continue to use URL identity unless they explicitly opt in.
    """
    source_identity = str(source_identity or "").strip()
    if (
        not source_identity
        or len(source_identity) > 256
        or any(ord(character) < 32 for character in source_identity)
    ):
        raise ValueError("invalid logical source identity")
    namespace, separator, resource_id = source_identity.partition(":")
    if (
        separator
        and namespace == "douyin"
        and resource_id.isdigit()
        and 15 <= len(resource_id) <= 25
    ):
        return source_key_for_url(
            f"https://www.douyin.com/video/{resource_id}"
        )
    return source_key_for_url(f"logical-source:{source_identity}")


def source_key_for_request(url, source_identity=None):
    if source_identity:
        return source_key_for_identity(source_identity)
    return source_key_for_url(url)


def provider_configured():
    return bool(
        runtime_bool("THIRD_PARTY_PROVIDER_ENABLED", False)
        and runtime_bool("TIANTIANJIEXI_PROVIDER_ENABLED", False)
        and runtime_str("TIANTIANJIEXI_API_BASE_URL", "https://tiantianjiexi.com/ajax/detail.php")
        and runtime_str("TIANTIANJIEXI_PHONE")
        and runtime_str("TIANTIANJIEXI_PASSWORD")
    )


def api_configured():
    return bool(
        runtime_str("TIANTIANJIEXI_API_BASE_URL", "https://tiantianjiexi.com/ajax/detail.php")
        and runtime_str("TIANTIANJIEXI_PHONE")
        and runtime_str("TIANTIANJIEXI_PASSWORD")
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
        "audio",
        "audio_url",
        "music",
        "music_url",
    }
    return any(payload.get(key) for key in keys)


def unwrap_api_payload(payload):
    if not isinstance(payload, dict):
        raise TianTianJieXiApiError("天天解析返回格式不是 JSON 对象")
    for key in ("data", "result", "detail", "info"):
        nested = payload.get(key)
        if isinstance(nested, dict) and payload_has_media(nested):
            merged = dict(nested)
            merged.update(service_context(nested, service_context(payload)))
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
    if str(status_value) == "451" and "暂不支持该链接" in message:
        raise TianTianJieXiUnsupportedError(
            "天天解析暂不支持该链接", diagnostic_detail="SERVICE_BUSINESS_CODE_451"
        )
    if status_value in {True, "true", "success", "ok", 0, "0", 1, "1", 200, "200"}:
        return
    if message_indicates_auth_error(message):
        raise TianTianJieXiAuthError("天天解析认证失败")
    if message_indicates_rate_limit(message):
        raise TianTianJieXiRateLimitError("天天解析调用额度或频率受限")
    code = str(status_value) if type(status_value) in (int, str) else ""
    detail = (f"SERVICE_BUSINESS_CODE_{int(code)}"
              if re.fullmatch(r"-?[0-9]{1,3}", code)
              else "SERVICE_BUSINESS_CODE_UNKNOWN")
    raise TianTianJieXiApiError(message or "天天解析未返回可用媒体资源", diagnostic_detail=detail)


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
    def __init__(self, session=None, observe=None):
        self.session = session or requests
        self.observe = observe

    @property
    def base_url(self):
        return runtime_str("TIANTIANJIEXI_API_BASE_URL", "https://tiantianjiexi.com/ajax/detail.php")

    @property
    def phone(self):
        return runtime_str("TIANTIANJIEXI_PHONE")

    @property
    def password(self):
        return runtime_str("TIANTIANJIEXI_PASSWORD")

    @serialized
    def parse_url(self, url, timeout_seconds=None):
        if not api_configured():
            raise TianTianJieXiAuthError("天天解析账号未配置")
        timeout = float(timeout_seconds if timeout_seconds is not None else runtime_float("TIANTIANJIEXI_TIMEOUT_SECONDS", 15) or 15)
        endpoint = self.base_url
        fields = {"phone": self.phone, "password": self.password, "link": url}
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        try:
            if urlsplit(endpoint).scheme.lower() not in {"http", "https"}:
                raise requests.exceptions.InvalidSchema("unsupported service scheme")
            requests.Request("POST", endpoint, data=fields, headers=headers).prepare()
        except (requests.exceptions.InvalidURL, requests.exceptions.MissingSchema,
                requests.exceptions.InvalidSchema) as error:
            raise TianTianJieXiApiError("天天解析请求配置无效", diagnostic_detail="SERVICE_REQUEST_NOT_SENT") from error
        try:
            response = self.session.post(
                endpoint,
                data=fields,
                headers=headers,
                timeout=max(0.1, timeout),
            )
            response.raise_for_status()
            payload = response.json()
        except requests.Timeout as error:
            raise XhsProviderTimeoutError("天天解析接口超时") from error
        except requests.exceptions.JSONDecodeError as error:
            raise TianTianJieXiApiError("天天解析接口返回非 JSON", diagnostic_detail="SERVICE_INVALID_JSON") from error
        except requests.exceptions.SSLError as error:
            raise TianTianJieXiUncertainError("天天解析 TLS 失败", diagnostic_detail="SERVICE_TLS_ERROR") from error
        except requests.HTTPError as error:
            status = error.response.status_code if error.response is not None else 0
            detail = f"SERVICE_HTTP_{status}" if 400 <= status <= 599 else "SERVICE_RESPONSE_REJECTED"
            raise TianTianJieXiApiError("天天解析 HTTP 失败", diagnostic_detail=detail) from error
        except requests.RequestException as error:
            raise TianTianJieXiUncertainError("天天解析接口请求失败", diagnostic_detail="SERVICE_NETWORK_ERROR") from error
        except ValueError as error:
            raise TianTianJieXiApiError("天天解析接口返回非 JSON", diagnostic_detail="SERVICE_INVALID_JSON") from error
        if self.observe is not None:
            self.observe(payload)
        ensure_api_success(payload)
        normalized = TianTianJieXiResultNormalizer.normalize_payload(unwrap_api_payload(payload))
        # Envelope identity can sit outside data; capture it before unwrapping.
        facts = bilibili_identity_facts(payload)
        if facts:
            normalized["_bilibili_identity_facts"] = facts
        return normalized

    def query_account_status(self):
        if not api_configured():
            return {
                "available": False,
                "error_type": "TianTianJieXiAuthError",
                "error": "api_not_configured",
            }
        ttl_seconds = runtime_int("TIANTIANJIEXI_ACCOUNT_STATUS_CACHE_TTL_SECONDS", 300)
        if ttl_seconds > 0:
            cached = cache.get(ACCOUNT_STATUS_CACHE_KEY)
            if isinstance(cached, dict):
                return {**cached, "cached": True}
        timeout = runtime_float("TIANTIANJIEXI_TIMEOUT_SECONDS", 15)
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

    def delete_cached_payload(self, source_key):
        try:
            cache.delete_many(
                (
                    build_payload_cache_key(source_key),
                    build_payload_cache_metadata_key(source_key),
                )
            )
        except Exception:
            logger.exception(
                "tiantianjiexi_payload_cache_delete_failed",
                extra={"provider": PROVIDER_ID},
            )

    def validate_payload(self, payload, payload_validator=None):
        if not isinstance(payload, dict):
            raise ProviderResultInvalidError("天天解析返回格式无效")
        if payload_validator is not None:
            try:
                valid = bool(payload_validator(payload))
            except ProviderResultInvalidError:
                raise
            except (TypeError, ValueError) as error:
                raise ProviderResultInvalidError("天天解析返回资源结构无效") from error
            if not valid:
                raise ProviderResultInvalidError("天天解析未返回可用媒体资源")
            return payload

        try:
            normalized = TianTianJieXiResultNormalizer.normalize_payload(
                unwrap_api_payload(payload)
            )
        except (TianTianJieXiApiError, TypeError, ValueError) as error:
            raise ProviderResultInvalidError(
                "天天解析返回资源结构无效"
            ) from error
        valid_video = any(
            bool(public_url_or_empty(resource.get("url")))
            for resource in normalized["videos"]
        )
        valid_image = any(
            bool(public_url_or_empty(url))
            for url in normalized["images"]
        )
        valid_audio = bool(public_url_or_empty(normalized["audio_url"]))
        if not (valid_video or valid_image or valid_audio):
            error = ProviderResultInvalidError("天天解析未返回可用媒体资源")
            error.diagnostic_detail = "SERVICE_EMPTY_RESULT"
            raise error
        return payload

    def get_cached_payload_entry(self, source_key, payload_validator=None):
        try:
            payload = cache.get(build_payload_cache_key(source_key))
        except Exception:
            logger.exception("tiantianjiexi_payload_cache_read_failed", extra={"provider": PROVIDER_ID})
            return None, 0
        if payload is None:
            return None, 0
        try:
            self.validate_payload(payload, payload_validator)
        except ProviderResultInvalidError:
            self.delete_cached_payload(source_key)
            return None, 0
        payload_age_ms = 0
        try:
            metadata = cache.get(build_payload_cache_metadata_key(source_key))
            if isinstance(metadata, dict) and metadata.get("stored_at"):
                payload_age_ms = max(
                    0,
                    int((time.time() - float(metadata["stored_at"])) * 1000),
                )
        except Exception:
            logger.exception(
                "tiantianjiexi_payload_cache_metadata_read_failed",
                extra={"provider": PROVIDER_ID},
            )
        return payload, payload_age_ms

    def get_cached_payload(self, source_key, payload_validator=None):
        payload, _payload_age_ms = self.get_cached_payload_entry(
            source_key,
            payload_validator,
        )
        return payload

    def cache_payload(self, source_key, payload):
        ttl_seconds = runtime_int("TIANTIANJIEXI_PAYLOAD_CACHE_TTL_SECONDS", 120)
        if ttl_seconds <= 0 or not isinstance(payload, dict):
            return
        try:
            cache.set(build_payload_cache_key(source_key), payload, timeout=ttl_seconds)
            cache.set(
                build_payload_cache_metadata_key(source_key),
                {"stored_at": time.time()},
                timeout=ttl_seconds,
            )
        except Exception:
            logger.exception("tiantianjiexi_payload_cache_write_failed", extra={"provider": PROVIDER_ID})

    def failure_cache_status(self, source_key):
        try:
            value = cache.get(build_failure_cache_key(source_key))
        except Exception:
            logger.exception("tiantianjiexi_failure_cache_read_failed", extra={"provider": PROVIDER_ID})
            ttl_seconds = max(
                1,
                runtime_int("TIANTIANJIEXI_FAILURE_CACHE_TTL_SECONDS", 8),
            )
            return {
                "hit": True,
                "reason": "cache_unavailable",
                "retry_after_seconds": ttl_seconds,
                "failure_class": ProviderFailureClass.UNKNOWN,
            }
        if value:
            try:
                cache.add(FAILURE_HIT_COUNT_KEY, 0, timeout=24 * 60 * 60)
                cache.incr(FAILURE_HIT_COUNT_KEY)
            except Exception:
                logger.exception("tiantianjiexi_failure_hit_count_failed", extra={"provider": PROVIDER_ID})
        if not value:
            return {
                "hit": False,
                "reason": "",
                "retry_after_seconds": 0,
                "failure_class": ProviderFailureClass.UNKNOWN,
            }

        ttl_seconds = max(
            1,
            runtime_int("TIANTIANJIEXI_FAILURE_CACHE_TTL_SECONDS", 8),
        )
        if isinstance(value, dict):
            failure_class = normalize_provider_failure_class(
                value.get("failure_class")
            )
            retry_at = float(value.get("retry_at") or 0)
            if not retry_at and value.get("failed_at"):
                retry_at = float(value["failed_at"]) + ttl_seconds
            retry_after = (
                max(0, math.ceil(retry_at - time.time()))
                if retry_at
                else ttl_seconds
            )
            reason = str(value.get("reason") or "failed")[:120]
            if retry_after <= 0:
                try:
                    cache.delete(build_failure_cache_key(source_key))
                except Exception:
                    logger.exception(
                        "tiantianjiexi_failure_cache_expired_delete_failed",
                        extra={"provider": PROVIDER_ID},
                    )
            elif not value.get("retry_at"):
                failed_at = time.time()
                try:
                    cache.set(
                        build_failure_cache_key(source_key),
                        {
                            "reason": reason,
                            "failure_class": failure_class.value,
                            "failed_at": failed_at,
                            "retry_at": failed_at + retry_after,
                        },
                        timeout=retry_after,
                    )
                except Exception:
                    logger.exception(
                        "tiantianjiexi_failure_cache_legacy_refresh_failed",
                        extra={"provider": PROVIDER_ID},
                    )
        else:
            retry_after = ttl_seconds
            reason = str(value or "failed")[:120]
            failure_class = ProviderFailureClass.UNKNOWN
            failed_at = time.time()
            try:
                cache.set(
                    build_failure_cache_key(source_key),
                    {
                        "reason": reason,
                        "failure_class": failure_class.value,
                        "failed_at": failed_at,
                        "retry_at": failed_at + ttl_seconds,
                    },
                    timeout=ttl_seconds,
                )
            except Exception:
                logger.exception(
                    "tiantianjiexi_failure_cache_legacy_refresh_failed",
                    extra={"provider": PROVIDER_ID},
                )
        return {
            "hit": retry_after > 0,
            "reason": reason,
            "retry_after_seconds": retry_after,
            "failure_class": failure_class,
        }

    def failure_cache_hit(self, source_key):
        return self.failure_cache_status(source_key)["hit"]

    def cache_failure(
        self,
        source_key,
        reason,
        failure_class=ProviderFailureClass.UNKNOWN,
    ):
        ttl_seconds = runtime_int("TIANTIANJIEXI_FAILURE_CACHE_TTL_SECONDS", 8)
        if ttl_seconds <= 0:
            return
        try:
            failed_at = time.time()
            cache.set(
                build_failure_cache_key(source_key),
                {
                    "reason": str(reason or "failed")[:120],
                    "failure_class": normalize_provider_failure_class(
                        failure_class
                    ).value,
                    "failed_at": failed_at,
                    "retry_at": failed_at + ttl_seconds,
                },
                timeout=ttl_seconds,
            )
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
        window_seconds = max(1, runtime_int("TIANTIANJIEXI_CIRCUIT_WINDOW_SECONDS", 60))
        window = now // window_seconds
        try:
            cache.delete_many((build_circuit_open_key(), build_circuit_failure_key(window)))
        except Exception:
            logger.exception("tiantianjiexi_circuit_success_reset_failed", extra={"provider": PROVIDER_ID})

    def record_failure(self):
        now = int(time.time())
        window_seconds = max(1, runtime_int("TIANTIANJIEXI_CIRCUIT_WINDOW_SECONDS", 60))
        threshold = max(1, runtime_int("TIANTIANJIEXI_CIRCUIT_FAILURE_THRESHOLD", 5))
        open_seconds = max(1, runtime_int("TIANTIANJIEXI_CIRCUIT_OPEN_SECONDS", 120))
        window = now // window_seconds
        try:
            key = build_circuit_failure_key(window)
            cache.add(key, 0, timeout=window_seconds + 5)
            count = cache.incr(key)
            if count >= threshold:
                cache.set(build_circuit_open_key(), now + open_seconds, timeout=open_seconds)
        except Exception:
            logger.exception("tiantianjiexi_circuit_failure_record_failed", extra={"provider": PROVIDER_ID})

    def wait_for_cached_payload(
        self,
        source_key,
        timeout_seconds,
        payload_validator=None,
    ):
        deadline = time.monotonic() + max(0.0, float(timeout_seconds or 0.0))
        while time.monotonic() < deadline:
            payload = self.get_cached_payload(source_key, payload_validator)
            if payload:
                return payload
            time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
        return self.get_cached_payload(source_key, payload_validator)

    def release_single_flight(self, lock_key, lock_token):
        try:
            if lock_key and cache.get(lock_key) == lock_token:
                cache.delete(lock_key)
        except Exception:
            logger.exception("tiantianjiexi_single_flight_release_failed", extra={"provider": PROVIDER_ID})

    @serialized
    def parse(
        self,
        url,
        timeout_seconds=None,
        attempt_controller=None,
        deadline_monotonic=None,
        payload_validator=None,
        continuation_checker=None,
        source_identity=None,
    ):
        started_at = time.perf_counter()

        def continuation_active():
            if not callable(continuation_checker):
                return False
            try:
                return bool(continuation_checker())
            except Exception:
                return False

        def record_attempt(**fields):
            fields.setdefault("continuation", continuation_active())
            return record_provider_attempt(**fields)

        if not provider_configured():
            raise TianTianJieXiAuthError("天天解析 Provider 未启用或账号未配置")
        source = urlsplit(url)
        if ((source.hostname or "").lower() in {"bilibili.com", "www.bilibili.com"}
                and re.fullmatch(r"/video/(?:BV[0-9A-Za-z]{10}|av[1-9][0-9]*)/?", source.path)):
            previous_validator = payload_validator

            def validate_current_facts(payload):
                if payload.get("_service_facts_version") != SERVICE_FACTS_VERSION:
                    return False
                self.validate_payload(payload, previous_validator)
                return True

            payload_validator = validate_current_facts
        source_key = source_key_for_request(url, source_identity)
        cached, payload_age_ms = self.get_cached_payload_entry(
            source_key,
            payload_validator,
        )
        if cached:
            if attempt_controller is not None:
                attempt_controller.reuse_cached_result(
                    "tiantianjiexi",
                    reason="payload_cache",
                )
            record_attempt(
                provider="tiantianjiexi",
                outcome=ProviderOutcome.SUCCESS_PAYLOAD_CACHE,
                cache_hit=True,
                elapsed_ms=(time.perf_counter() - started_at) * 1000,
                payload_age_ms=payload_age_ms,
                candidate_count=len(cached.get("videos") or cached.get("images") or ()),
            )
            return cached
        if self.circuit_is_open():
            if attempt_controller is not None:
                attempt_controller.skip_fresh_work(
                    "tiantianjiexi",
                    reason="circuit_open",
                )
            record_attempt(
                provider="tiantianjiexi",
                outcome=ProviderOutcome.FAILURE_CIRCUIT_OPEN,
                elapsed_ms=(time.perf_counter() - started_at) * 1000,
                failure_class=ProviderFailureClass.CIRCUIT_OPEN,
            )
            raise XhsProviderTimeoutError("天天解析暂时不可用，请稍后再试")
        failure_status = self.failure_cache_status(source_key)
        if failure_status["hit"]:
            retry_after = failure_status["retry_after_seconds"]
            if attempt_controller is not None:
                attempt_controller.skip_fresh_work(
                    "tiantianjiexi",
                    reason="failure_cooldown",
                    retry_after=retry_after,
                )
            record_attempt(
                provider="tiantianjiexi",
                outcome=ProviderOutcome.FAILURE_COOLDOWN,
                elapsed_ms=(time.perf_counter() - started_at) * 1000,
                retry_after_seconds=retry_after,
                failure_class=failure_status["failure_class"],
            )
            raise TianTianJieXiFailureCooldownError(retry_after)

        timeout_seconds = float(timeout_seconds if timeout_seconds is not None else runtime_float("THIRD_PARTY_FRONTEND_TIMEOUT_SECONDS", 3.0) or 3.0)
        execution_deadline = float(deadline_monotonic or 0)
        if execution_deadline:
            execution_timeout_seconds = max(
                0.1,
                execution_deadline - time.monotonic(),
            )
        else:
            execution_timeout_seconds = max(0.1, timeout_seconds)
        lock_key = build_single_flight_key(source_key)
        lock_token = uuid4().hex
        lock_ttl = max(
            int(execution_timeout_seconds) + 5,
            runtime_int("TIANTIANJIEXI_TIMEOUT_SECONDS", 15) + 5,
        )
        try:
            lock_acquired = cache.add(lock_key, lock_token, timeout=lock_ttl)
        except Exception:
            logger.exception("tiantianjiexi_single_flight_acquire_failed", extra={"provider": PROVIDER_ID})
            lock_acquired = True
        if not lock_acquired:
            if attempt_controller is not None:
                attempt_controller.skip_fresh_work(
                    "tiantianjiexi",
                    reason="single_flight",
                )
            cached = self.wait_for_cached_payload(
                source_key,
                execution_timeout_seconds,
                payload_validator,
            )
            if cached:
                _cached, payload_age_ms = self.get_cached_payload_entry(
                    source_key,
                    payload_validator,
                )
                record_attempt(
                    provider="tiantianjiexi",
                    outcome=ProviderOutcome.SUCCESS_SINGLE_FLIGHT,
                    cache_hit=True,
                    single_flight_wait=True,
                    elapsed_ms=(time.perf_counter() - started_at) * 1000,
                    payload_age_ms=payload_age_ms,
                    candidate_count=len(cached.get("videos") or cached.get("images") or ()),
                )
                return cached
            record_attempt(
                provider="tiantianjiexi",
                outcome=ProviderOutcome.FAILURE_TIMEOUT,
                single_flight_wait=True,
                elapsed_ms=(time.perf_counter() - started_at) * 1000,
                failure_class=ProviderFailureClass.TIMEOUT,
            )
            raise XhsProviderTimeoutError("天天解析正在处理中，请稍后重试同一链接。")

        concurrency_acquired = False
        request_reservation = None
        external_call = False
        attempt_consumed = False
        terminal_recorded = False
        try:
            if attempt_controller is not None:
                attempt_controller.prepare_fresh_work("tiantianjiexi")
            concurrency = acquire_concurrency(
                "third-party-tiantianjiexi-global",
                "all",
                runtime_int("TIANTIANJIEXI_GLOBAL_MAX_CONCURRENCY", 2),
            )
            if not concurrency.allowed:
                if attempt_controller is not None:
                    attempt_controller.skip_fresh_work(
                        "tiantianjiexi",
                        reason="provider_busy",
                    )
                record_attempt(
                    provider="tiantianjiexi",
                    outcome=ProviderOutcome.FAILURE_CONCURRENCY,
                    elapsed_ms=(time.perf_counter() - started_at) * 1000,
                    failure_class=ProviderFailureClass.CONCURRENCY,
                )
                terminal_recorded = True
                raise TianTianJieXiRateLimitError("天天解析并发受限")
            concurrency_acquired = True
            request_reservation = reserve_fixed_window(
                "third-party-tiantianjiexi",
                "all",
                runtime_int("TIANTIANJIEXI_REQUEST_LIMIT_PER_MINUTE", 20),
                60,
            )
            if not request_reservation.result.allowed:
                if attempt_controller is not None:
                    attempt_controller.skip_fresh_work(
                        "tiantianjiexi",
                        reason="provider_rate_limit",
                    )
                record_attempt(
                    provider="tiantianjiexi",
                    outcome=ProviderOutcome.FAILURE_RATE_LIMIT,
                    elapsed_ms=(time.perf_counter() - started_at) * 1000,
                    failure_class=ProviderFailureClass.RATE_LIMIT,
                )
                terminal_recorded = True
                raise TianTianJieXiRateLimitError("天天解析请求频率受限")
            if attempt_controller is not None:
                attempt_consumed = bool(
                    attempt_controller.begin_provider("tiantianjiexi")
                )
            request_reservation.commit()

            attempts = max(1, runtime_int("TIANTIANJIEXI_RETRIES", 2))
            deadline = execution_deadline or (
                time.monotonic() + max(0.1, timeout_seconds)
            )
            last_error = None
            for attempt in range(attempts):
                try:
                    remaining_timeout = deadline - time.monotonic()
                    if remaining_timeout <= 0:
                        raise XhsProviderTimeoutError("天天解析接口超时")
                    external_call = True
                    payload = self.client.parse_url(
                        url,
                        timeout_seconds=min(
                            max(0.1, remaining_timeout),
                            runtime_float("TIANTIANJIEXI_TIMEOUT_SECONDS", 15),
                        ),
                    )
                    self.validate_payload(payload, payload_validator)
                    self.cache_payload(source_key, payload)
                    self.clear_failure(source_key)
                    self.record_success()
                    continued = continuation_active()
                    record_attempt(
                        provider="tiantianjiexi",
                        outcome=(
                            ProviderOutcome.SUCCESS_CONTINUATION
                            if continued
                            else ProviderOutcome.SUCCESS_FRESH
                        ),
                        continuation=continued,
                        external_call=True,
                        attempt_consumed=attempt_consumed,
                        elapsed_ms=(time.perf_counter() - started_at) * 1000,
                        candidate_count=len(payload.get("videos") or payload.get("images") or ()),
                    )
                    return payload
                except (TianTianJieXiAuthError, TianTianJieXiRateLimitError,
                        TianTianAdmissionDeferred, TianTianJieXiUncertainError,
                        TianTianJieXiUnsupportedError, XhsProviderTimeoutError):
                    raise
                except Exception as error:
                    last_error = error
                    if attempt >= attempts - 1:
                        raise
            raise last_error or TianTianJieXiApiError("天天解析失败")
        except (
            DouyinUltraAttemptAccountingError,
            DouyinUltraAttemptRateLimitedError,
            DouyinUltraAttemptSupersededError,
        ) as error:
            outcome, failure_class = classify_provider_failure(error)
            if not terminal_recorded:
                record_attempt(
                    provider="tiantianjiexi",
                    outcome=outcome,
                    external_call=external_call,
                    attempt_consumed=attempt_consumed,
                    elapsed_ms=(time.perf_counter() - started_at) * 1000,
                    failure_class=failure_class,
                )
            raise
        except ProviderResultInvalidError as error:
            self.record_failure()
            record_attempt(
                provider="tiantianjiexi",
                outcome=ProviderOutcome.FAILURE_RESULT_INVALID,
                external_call=external_call,
                attempt_consumed=attempt_consumed,
                elapsed_ms=(time.perf_counter() - started_at) * 1000,
                failure_class=ProviderFailureClass.RESULT_INVALID,
            )
            raise
        except Exception as error:
            outcome, failure_class = classify_provider_failure(error)
            self.cache_failure(
                source_key,
                type(error).__name__,
                failure_class,
            )
            if attempt_controller is not None:
                attempt_controller.record_cooldown(
                    "tiantianjiexi",
                    runtime_int("TIANTIANJIEXI_FAILURE_CACHE_TTL_SECONDS", 8),
                )
            self.record_failure()
            if not terminal_recorded:
                record_attempt(
                    provider="tiantianjiexi",
                    outcome=outcome,
                    external_call=external_call,
                    attempt_consumed=attempt_consumed,
                    elapsed_ms=(time.perf_counter() - started_at) * 1000,
                    failure_class=failure_class,
                )
            raise
        finally:
            if request_reservation is not None:
                request_reservation.release()
            if concurrency_acquired:
                release_concurrency("third-party-tiantianjiexi-global", "all")
            self.release_single_flight(lock_key, lock_token)

    def query_account_status(self):
        return self.client.query_account_status()

    def health(self, source_key=""):
        now = int(time.time())
        window_seconds = max(1, runtime_int("TIANTIANJIEXI_CIRCUIT_WINDOW_SECONDS", 60))
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
                failure_value = cache.get(build_failure_cache_key(source_key))
                failure_cache_reason = str(
                    failure_value.get("reason")
                    if isinstance(failure_value, dict)
                    else failure_value
                    or ""
                )
                single_flight_active = bool(cache.get(build_single_flight_key(source_key)))
        except Exception as error:
            cache_error = type(error).__name__
            logger.debug("tiantianjiexi_health_cache_read_failed", extra={"provider": PROVIDER_ID}, exc_info=True)
        if (
            runtime_bool("TIANTIANJIEXI_PROVIDER_ENABLED", False)
            and runtime_bool("TIANTIANJIEXI_HEALTH_QUERY_ACCOUNT_STATUS_ENABLED", True)
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
            "provider_enabled": runtime_bool("TIANTIANJIEXI_PROVIDER_ENABLED", False),
            "api_configured": api_configured(),
            "api_base_url_configured": bool(runtime_str("TIANTIANJIEXI_API_BASE_URL", "https://tiantianjiexi.com/ajax/detail.php")),
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
        if isinstance(payload, ServiceFormatPayload):
            return payload
        title = first_text(payload, ("title", "name"))
        nickname = first_text(payload, ("nickname", "author", "user"))
        description = first_text(payload, ("desc", "description"))
        if nickname and title and nickname not in title:
            title = f"{nickname}_{title}"
        cover = first_text(payload, ("cover", "cover_url", "pic", "image", "thumbnail"))
        audio_url = first_text(payload, ("audio", "audio_url", "music", "music_url"))
        normalized = {
            "title": title,
            "description": description,
            "cover": cover,
            "videos": cls.collect_video_resources(payload),
            "images": cls.collect_image_urls(payload),
            "audio_url": audio_url,
        }
        normalized["_service_format_rows"] = [
            *normalized["videos"],
            *({**service_context(payload), "url": url, "media_type": "image"} for url in normalized["images"]),
            *([{**service_context(payload), "url": audio_url, "media_type": "audio", "work_id": "native-audio-primary",
                "work_label": "Audio"}] if audio_url else []),
        ]
        identity_signals = cls.collect_source_identity_signals(payload)
        if identity_signals:
            normalized["_source_identity_signals"] = identity_signals
        identity_facts = bilibili_identity_facts(payload)
        if identity_facts:
            normalized["_bilibili_identity_facts"] = identity_facts
        # A pre-fix normalized cache cannot regain facts by normalization alone.
        if ("_service_format_rows" not in payload
                or payload.get("_service_facts_version") == SERVICE_FACTS_VERSION):
            normalized["_service_facts_version"] = SERVICE_FACTS_VERSION
        return ServiceFormatPayload(normalized)

    @classmethod
    def collect_source_identity_signals(cls, payload):
        signals = []

        def add(value):
            match = XHS_NOTE_ID_RE.search(str(value or "")[:4096])
            if match:
                note_id = match.group(1).lower()
                if note_id not in signals:
                    signals.append(note_id)

        existing = payload.get("_source_identity_signals") or ()
        if isinstance(existing, (list, tuple)):
            for value in existing[:8]:
                add(value)
        for key in ("note_id", "noteId", "item_id", "itemId"):
            add(payload.get(key))
        for key in (
            "canonical_url",
            "canonicalUrl",
            "source_url",
            "sourceUrl",
            "share_url",
            "shareUrl",
            "link",
        ):
            add(payload.get(key))
        return tuple(signals[:8])

    @classmethod
    def collect_video_resources(cls, payload):
        existing = payload.get("_service_format_rows") if isinstance(payload, ServiceFormatPayload) else None
        resources = ([row for row in existing if row.get("media_type") == "video"]
                     if isinstance(existing, (list, tuple)) else
                     collect_service_video_resources(payload, nested_keys=(
                         "streams", "stream", "video_list", "videoList", "download_url_list",
                         "downloadUrlList", "url_list", "urlList", "play_addr", "playAddr"),
                         infer_quality=infer_quality_from_url))
        return tuple(
            sorted(
                resources,
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
        from ..service_format_selection import attach_service_picker
        if not isinstance(payload, dict):
            raise ValueError("天天解析结果格式无效")
        payload = filter_service_format_rows(self.normalize_payload(payload), rule)
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
        image_suffixes = getattr(rule, "image_media_host_suffixes", ()) or getattr(rule, "media_host_suffixes", ())
        image_urls = []
        for image_url in payload.get("images") or ():
            url = public_url_or_empty(image_url, image_suffixes, platform_id, "images")
            if url and (not valid_videos or (url != cover_url and mixed_image_asset_allowed(payload, url))):
                image_urls.append(url)
        image_assets = tuple(
            MediaAsset(media_type="image", url=url, platform=self.platform_id, preview_url=url,
                       **self.build_provider_asset_fields(rule, source_url, "image", index, url))
            for index, url in enumerate(image_urls, 1)
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
            result = ParseResult(
                platform=self.platform_id,
                post_type="video",
                base_filename=title or "third_party",
                assets=assets + image_assets,
                metadata=self.build_metadata(rule, platform_name, len(valid_videos), source_url, payload),
            )
            return attach_service_picker(result, payload=payload, provider=PROVIDER_ID,
                                         source_url=source_url)

        if image_urls:
            result = ParseResult(
                platform=self.platform_id,
                post_type="image",
                base_filename=title or "third_party",
                assets=image_assets,
                metadata=self.build_metadata(rule, platform_name, len(image_urls), source_url, payload),
            )
            return attach_service_picker(result, payload=payload, provider=PROVIDER_ID,
                                         source_url=source_url)
        audio_rows = [row for row in payload['_service_format_rows'] if row.get('media_type') == 'audio']
        if audio_rows:
            result = ParseResult(
                platform=self.platform_id,
                post_type="audio",
                base_filename=title or "third_party",
                assets=(),
                metadata=self.build_metadata(rule, platform_name, len(audio_rows), source_url, payload),
            )
            return attach_service_picker(result, payload=payload, provider=PROVIDER_ID,
                                         source_url=source_url)
        raise ValueError("天天解析结果不包含允许的媒体资源")
