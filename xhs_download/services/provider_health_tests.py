from __future__ import annotations

from concurrent.futures import Future
from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
import re
import time
from typing import Any
from urllib.parse import urlparse

from django.conf import settings

from djangoProject.health import get_runtime_readiness

from ..models import (
    ProviderHealthTestRun,
    ProviderHealthTestSetting,
    ProviderHealthTestStatus,
    ProviderRuntimeSetting,
)
from .downloads import provider_manifest_allows_public_download
from .env_config import runtime_bool as env_runtime_bool, runtime_str
from .common import (
    XhsSecurityError,
    host_matches,
    is_blocked_hostname,
    validate_public_download_url,
)
from .observability import sanitize_observability_text
from .platforms.bilibili import BILIBILI_PAGE_HOST_SUFFIXES
from .platforms.douyin import DouyinAdapter
from .platforms.douyin_kukutool import DouyinKukuToolProvider, get_kukutool_provider_health
from .platforms.douyin_providers import DouyinTianTianJieXiProvider
from .platforms.gallerydl_generic import GalleryDlGenericProvider
from .platforms.tiantianjiexi_generic import (
    TianTianJieXiGenericProvider,
    TianTianJieXiResultNormalizer,
)
from .platforms.third_party import GenericThirdPartyAdapter
from .platforms.ytdlp_generic import YtDlpGenericProvider
from .provider_access import (
    PROVIDER_DOUYIN_ULTRA,
    PROVIDER_THIRD_PARTY,
    PROVIDER_YOUTUBE_MERGE,
    provider_access_summary,
)
from .provider_lab.contracts import ProviderLabError
from .provider_lab.videodl_control import (
    VideodlExecutionOptions,
    execute_controlled_videodl,
    load_videodl_inventory,
)
from .provider_lab.videodl_policy import (
    REVIEWED_COMMON_BACKEND_IDS,
    REVIEWED_DEDICATED_BACKEND_IDS,
    VIDEODL_POLICY_PIN,
    backend_policy,
)
from .registry import parse_media_page
from .youtube_merge import build_youtube_merge_plan, get_youtube_merge_health
from .youtube_merge import YOUTUBE_PAGE_HOST_SUFFIXES


VIDEODL_WORKER = (
    Path(settings.BASE_DIR) / "tools" / "provider_lab" / "runtime" / "workers" / "videodl_worker.py"
)
SAFE_ERROR_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,79}$")
BACKEND_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,79}$")
SENSITIVE_ERROR_VALUE_RE = re.compile(
    r"(?i)\b(cookie|authorization|proxy-authorization|x-api-key|api[-_]?key|"
    r"token|password|secret|header)\s*[:=]\s*[^\s,;]+"
)
LOCAL_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_])/(?:Users|home|srv|tmp|var|private|etc|opt)/[^\s,;\"']+"
)
SAMPLE_URL_POLICY_ERROR_CODE = "SAMPLE_URL_POLICY_REJECTED"
SAMPLE_URL_POLICY_MESSAGE = "测试 URL 未通过公网与平台准入校验。"
_XHS_SAMPLE_HOST_SUFFIXES = ("xiaohongshu.com", "xhslink.com")
_SAMPLE_URL_FIELD_HOST_SUFFIXES = {
    "douyin_url": DouyinAdapter.page_host_suffixes,
    "youtube_url": YOUTUBE_PAGE_HOST_SUFFIXES,
    "third_party_url": GenericThirdPartyAdapter.page_host_suffixes,
    "gallerydl_url": GenericThirdPartyAdapter.page_host_suffixes,
    "xhs_video_url": _XHS_SAMPLE_HOST_SUFFIXES,
    "xhs_gallery_url": _XHS_SAMPLE_HOST_SUFFIXES,
    "bilibili_url": BILIBILI_PAGE_HOST_SUFFIXES,
    "videodl_url": (),
}


PROVIDER_HEALTH_TESTS = (
    {
        "key": "health_ready",
        "name": "Runtime Readiness",
        "description": "验证 database/cache 是否可用。",
        "provider": "runtime",
        "provider_access_id": "",
        "url_field": "",
    },
    {
        "key": "douyin_native_parse",
        "name": "抖音普通解析",
        "description": "使用普通抖音 adapter 解析测试 URL，不强制 KuKuTool。",
        "provider": "native",
        "provider_access_id": "",
        "url_field": "douyin_url",
    },
    {
        "key": "douyin_kukutool_ultra",
        "name": "抖音 KuKuTool 超清（仅 KuKuTool）",
        "description": "只调用 KuKuTool 解析抖音测试 URL，不调用天天解析或 Native；整条超清链路的健康见“抖音超清”汇总行。",
        "provider": "kukutool",
        "provider_access_id": PROVIDER_DOUYIN_ULTRA,
        "url_field": "douyin_url",
    },
    {
        "key": "tiantianjiexi_account_status",
        "name": "天天解析账号状态",
        "description": "只查询天天解析账号状态，不解析真实视频，不消耗解析额度。",
        "provider": "tiantianjiexi_generic",
        "provider_access_id": PROVIDER_THIRD_PARTY,
        "url_field": "",
    },
    {
        "key": "douyin_tiantianjiexi_ultra",
        "name": "抖音天天解析超清",
        "description": "调用天天解析解析抖音测试 URL，可能消耗真实解析额度。",
        "provider": "tiantianjiexi",
        "provider_access_id": PROVIDER_DOUYIN_ULTRA,
        "url_field": "douyin_url",
        "side_effect_warning": "可能消耗真实解析额度",
    },
    {
        "key": "youtube_merge",
        "name": "YouTube 高清合并",
        "description": "验证 yt-dlp + ffmpeg health，并只做候选规划，不下载大文件。",
        "provider": "youtube_merge",
        "provider_access_id": PROVIDER_YOUTUBE_MERGE,
        "url_field": "youtube_url",
    },
    {
        "key": "third_party_ytdlp",
        "name": "第三方 yt-dlp",
        "description": "验证通用 yt-dlp provider health，并轻量解析测试 URL。",
        "provider": "yt_dlp_generic",
        "provider_access_id": PROVIDER_THIRD_PARTY,
        "url_field": "third_party_url",
    },
    {
        "key": "third_party_tiantianjiexi",
        "name": "第三方天天解析",
        "description": "调用天天解析解析第三方测试 URL，可能消耗真实解析额度。",
        "provider": "tiantianjiexi_generic",
        "provider_access_id": PROVIDER_THIRD_PARTY,
        "url_field": "third_party_url",
        "side_effect_warning": "可能消耗真实解析额度",
    },
    {
        "key": "gallery_dl",
        "name": "gallery-dl",
        "description": "验证 gallery-dl provider health，并轻量解析测试 URL。",
        "provider": "gallery_dl_generic",
        "provider_access_id": PROVIDER_THIRD_PARTY,
        "url_field": "gallerydl_url",
    },
    {
        "key": "xhs_video_current",
        "name": "小红书当前视频路线",
        "description": "使用当前正式 XHS 路线轻量解析，并验证视频候选。",
        "provider": "xhs_current",
        "provider_access_id": "",
        "url_field": "xhs_video_url",
    },
    {
        "key": "xhs_gallery_current",
        "name": "小红书当前图集路线",
        "description": "使用当前正式 XHS 路线轻量解析，并验证图片候选。",
        "provider": "xhs_current",
        "provider_access_id": "",
        "url_field": "xhs_gallery_url",
    },
    {
        "key": "bilibili_native_parse",
        "name": "哔哩哔哩 Native 解析",
        "description": "使用当前正式 Bilibili Native 路线验证视频候选。",
        "provider": "bilibili_native",
        "provider_access_id": "",
        "url_field": "bilibili_url",
    },
    {
        "key": "videodl_policy_inventory",
        "name": "videodl exact-pin policy inventory",
        "description": "验证独立环境、exact pin、70/37 policy 和默认拒绝边界，不执行媒体 Backend。",
        "provider": "videodl_repo",
        "provider_access_id": "",
        "url_field": "",
    },
    {
        "key": "videodl_specific_backend",
        "name": "videodl 指定 Dedicated Backend",
        "description": "使用一个明确 allowed 的 Dedicated backend 做非商业内部轻量解析。",
        "provider": "videodl_repo",
        "provider_access_id": "",
        "url_field": "videodl_url",
        "side_effect_warning": "仅限 noncommercial internal use",
    },
    {
        "key": "download_smoke_xhs_gallery",
        "name": "小红书图文下载冒烟",
        "description": "按当前用户路线解析，读取首个默认资源最多 1KB，记录字段覆盖与取数结果，不保存文件。",
        "provider": "xiaohongshu", "provider_access_id": "", "url_field": "xhs_gallery_url",
    },
    {
        "key": "download_smoke_xhs_video",
        "name": "小红书视频下载冒烟",
        "description": "按当前用户路线解析，读取首个默认资源最多 1KB，记录字段覆盖与取数结果，不保存文件。",
        "provider": "xiaohongshu", "provider_access_id": "", "url_field": "xhs_video_url",
    },
    {
        "key": "download_smoke_douyin",
        "name": "抖音下载冒烟",
        "description": "按当前用户默认超清路线解析，读取首个默认资源最多 1KB，记录字段覆盖与取数结果，不保存文件。",
        "provider": "douyin", "provider_access_id": "", "url_field": "douyin_url",
    },
    {
        "key": "download_smoke_bilibili",
        "name": "哔哩哔哩下载冒烟",
        "description": "按当前 Native 优先及获准兜底路线解析，读取首个默认资源最多 1KB，不保存文件。",
        "provider": "bilibili", "provider_access_id": "", "url_field": "bilibili_url",
    },
)


@dataclass(frozen=True)
class ProviderHealthExecutionContext:
    request_id: str
    actor_id: int | None = None
    actor_username: str = ""
    is_staff: bool = False
    trigger: str = "admin"
    suite_run: Any = None
    access_request: Any = None
    test_setting: ProviderHealthTestSetting | None = None
    runtime_setting: ProviderRuntimeSetting | None = None


def provider_health_context_from_request(request) -> ProviderHealthExecutionContext:
    user = getattr(request, "user", None)
    active_staff = bool(
        getattr(user, "is_active", False) and getattr(user, "is_staff", False)
    )
    return ProviderHealthExecutionContext(
        request_id=str(getattr(request, "request_id", "") or ""),
        actor_id=getattr(user, "id", None),
        actor_username=str(getattr(user, "get_username", lambda: "")() or "")[:150],
        is_staff=active_staff,
        trigger="admin",
        access_request=request,
    )


def provider_health_test_keys() -> tuple[str, ...]:
    return tuple(item["key"] for item in PROVIDER_HEALTH_TESTS)


def provider_health_test_by_key(test_key):
    for item in PROVIDER_HEALTH_TESTS:
        if item["key"] == test_key:
            return item
    return None


def validate_daily_test_keys(value) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > len(PROVIDER_HEALTH_TESTS):
        raise ValueError("INVALID_DAILY_TEST_KEYS")
    normalized = tuple(str(item or "") for item in value)
    if len(set(normalized)) != len(normalized):
        raise ValueError("DUPLICATE_DAILY_TEST_KEYS")
    allowed = set(provider_health_test_keys())
    if any(item not in allowed for item in normalized):
        raise ValueError("UNKNOWN_DAILY_TEST_KEY")
    selected = set(normalized)
    return tuple(key for key in provider_health_test_keys() if key in selected)


def validate_daily_videodl_backend_id(value) -> bool:
    backend_id = str(value or "").strip()
    if not backend_id:
        return True
    if not BACKEND_ID_RE.fullmatch(backend_id):
        return False
    if backend_id not in REVIEWED_DEDICATED_BACKEND_IDS:
        return False
    policy = backend_policy(backend_id, "dedicated", registry_present=True)
    return (
        policy.controlled_state == "allowed"
        and not policy.external_sharing_required
        and not policy.browser_execution
    )


def validate_provider_health_sample_url(value, field_name, *, resolve_dns=False) -> str:
    candidate = str(value or "").strip()
    if not candidate:
        return ""
    if field_name not in _SAMPLE_URL_FIELD_HOST_SUFFIXES:
        raise XhsSecurityError(SAMPLE_URL_POLICY_MESSAGE)
    try:
        parsed = urlparse(candidate)
        hostname = str(parsed.hostname or "").strip().lower()
        port = parsed.port
    except (TypeError, ValueError) as error:
        raise XhsSecurityError(SAMPLE_URL_POLICY_MESSAGE) from error
    if (
        parsed.scheme not in {"http", "https"}
        or not hostname
        or hostname.endswith(".")
        or parsed.username is not None
        or parsed.password is not None
        or (port is not None and port not in {80, 443})
        or is_blocked_hostname(hostname)
    ):
        raise XhsSecurityError(SAMPLE_URL_POLICY_MESSAGE)
    allowed_suffixes = _SAMPLE_URL_FIELD_HOST_SUFFIXES[field_name]
    if allowed_suffixes and not host_matches(hostname, allowed_suffixes):
        raise XhsSecurityError(SAMPLE_URL_POLICY_MESSAGE)
    if resolve_dns:
        validate_public_download_url(candidate, SAMPLE_URL_POLICY_MESSAGE)
    return candidate


def provider_test_url_host(test_url):
    parsed = urlparse(str(test_url or ""))
    return (parsed.hostname or "").strip().lower()


def provider_test_url_for(item, test_setting=None):
    field_name = item.get("url_field") or ""
    if not field_name:
        return ""
    test_setting = test_setting or ProviderHealthTestSetting.load()
    return str(getattr(test_setting, field_name, "") or "").strip()


def provider_test_runtime_info(item, context=None, runtime_setting=None):
    provider_id = item.get("provider_access_id") or ""
    if not provider_id:
        return {
            "runtime_mode": "",
            "runtime_label": "不适用",
            "hard_enabled": True,
            "access_reason": "",
        }
    runtime_setting = runtime_setting or (
        context.runtime_setting if context and context.runtime_setting else ProviderRuntimeSetting.load()
    )
    access = provider_access_summary(
        provider_id,
        getattr(context, "access_request", None),
        runtime_setting,
    )
    return {
        "runtime_mode": access["effective_mode"],
        "runtime_label": access["effective_label"],
        "hard_enabled": bool(access["hard_enabled"]),
        "access_reason": access["access_reason"],
    }


def _safe_error_code(value, fallback="PROVIDER_TEST_FAILED") -> str:
    raw = str(value or "").strip()
    if SAFE_ERROR_CODE_RE.fullmatch(raw):
        return raw
    snake = re.sub(r"(?<!^)(?=[A-Z])", "_", raw).upper()
    snake = re.sub(r"[^A-Z0-9_]+", "_", snake).strip("_")
    if not snake:
        snake = fallback
    if not snake[0].isalpha():
        snake = f"E_{snake}"
    return snake[:80]


def provider_result_fingerprint(*, test_key, status, provider, error_code) -> str:
    value = "|".join(
        (
            str(test_key or "")[:64],
            str(status or "")[:16],
            str(provider or "")[:80],
            _safe_error_code(error_code, "NONE"),
        )
    )
    return sha256(value.encode("utf-8")).hexdigest()


def _safe_error_message(value) -> str:
    redacted = SENSITIVE_ERROR_VALUE_RE.sub(
        lambda match: f"{match.group(1)}=[sensitive-redacted]",
        str(value or ""),
    )
    sanitized = sanitize_observability_text(redacted)
    return LOCAL_PATH_RE.sub("[path-redacted]", sanitized)


def provider_health_test_record(
    context,
    item,
    *,
    status,
    started_at,
    provider="",
    error_type="",
    error_code="",
    error_message="",
    test_url="",
    runtime_mode="",
    hard_enabled=False,
    duration_ms=None,
    download_smoke_details=None,
):
    provider_name = str(provider or item.get("provider", ""))[:80]
    fixed_error_code = (
        "" if status == ProviderHealthTestStatus.SUCCESS else _safe_error_code(error_code or error_type)
    )
    if duration_ms is None:
        duration_ms = max(0, round((time.monotonic() - started_at) * 1000))
    return ProviderHealthTestRun.objects.create(
        request_id=str(context.request_id or "")[:128],
        test_key=item["key"],
        status=status,
        provider=provider_name,
        duration_ms=max(0, int(duration_ms or 0)),
        error_type=str(error_type or "")[:120],
        error_code=fixed_error_code,
        error_message_short=_safe_error_message(error_message),
        result_fingerprint=provider_result_fingerprint(
            test_key=item["key"],
            status=status,
            provider=provider_name,
            error_code=fixed_error_code,
        ),
        runtime_mode=str(runtime_mode or "")[:32],
        hard_enabled=bool(hard_enabled),
        is_staff=bool(context.is_staff),
        actor_id=context.actor_id,
        actor_username=str(context.actor_username or "")[:150],
        test_url_host=provider_test_url_host(test_url)[:255],
        suite_run=context.suite_run,
        download_smoke_details=download_smoke_details,
    )


def record_unexecuted_test(context, item, *, error_code):
    return provider_health_test_record(
        context,
        item,
        status=ProviderHealthTestStatus.SKIPPED,
        started_at=time.monotonic(),
        error_type="SuiteDeadlineExceeded",
        error_code=error_code,
        error_message="Suite execution budget exhausted before this test started.",
        test_url=provider_test_url_for(item, context.test_setting),
        duration_ms=0,
    )


def provider_asset_manifest_validation_error(result, allowed_providers, *, require_video=True):
    if not result or not getattr(result, "assets", ()):
        return "未生成 ParseResult 资源"
    assets = tuple(result.assets or ())
    if require_video:
        assets = tuple(asset for asset in assets if asset.media_type == "video")
        if not assets:
            return "未生成视频资源"
    matching_assets = tuple(
        asset for asset in assets if str(asset.provider or "") in allowed_providers
    )
    if not matching_assets:
        return "资源 provider 与预期不一致"
    asset = matching_assets[0]
    if not asset.provider_manifest_token:
        return "资源缺少 provider_manifest_token"
    if not asset.provider_resource_id:
        return "资源缺少 provider_resource_id"
    if not asset.provider_cache_key:
        return "资源缺少 provider_cache_key"
    if not provider_manifest_allows_public_download(asset.to_download_item()):
        return "Provider manifest 未通过下载校验"
    return ""


def build_tiantianjiexi_third_party_parse_result(test_url, timeout_seconds=10.0):
    adapter = GenericThirdPartyAdapter()
    rule = adapter.get_rule_for_url(test_url)
    if not rule:
        raise ValueError("THIRD_PARTY_RULE_UNAVAILABLE")
    payload = TianTianJieXiGenericProvider().parse(test_url, timeout_seconds=timeout_seconds)
    return TianTianJieXiResultNormalizer(platform_id=adapter.platform_id).normalize(
        rule=rule,
        platform_name=rule.display_name,
        source_url=test_url,
        payload=payload,
    )


def _videodl_python() -> str:
    return runtime_str(
        "VIDEODL_PROVIDER_LAB_PYTHON",
        "/tmp/animeimages-provider-lab/videodl/bin/python",
    )


def _videodl_runtime_ready() -> bool:
    return bool(
        env_runtime_bool("VIDEODL_PROVIDER_LAB_ENABLED", False)
        and Path(_videodl_python()).is_file()
        and VIDEODL_WORKER.is_file()
    )


def _validate_current_parse_result(result, *, platform, media_type) -> str:
    if not result or str(getattr(result, "platform", "")) != platform:
        return "RESULT_PLATFORM_MISMATCH"
    assets = tuple(getattr(result, "assets", ()) or ())
    if not any(getattr(asset, "media_type", "") == media_type for asset in assets):
        return "RESULT_MEDIA_TYPE_MISSING"
    return ""


def execute_provider_health_test(context, item):
    started_at = time.monotonic()
    runtime_setting = context.runtime_setting or ProviderRuntimeSetting.load()
    test_setting = context.test_setting or ProviderHealthTestSetting.load()
    runtime_info = provider_test_runtime_info(item, context, runtime_setting)
    test_url = provider_test_url_for(item, test_setting)
    provider = item.get("provider", "")
    download_smoke_details = None

    def record(status, error_type="", error_code="", error_message=""):
        return provider_health_test_record(
            context,
            item,
            status=status,
            started_at=started_at,
            provider=provider,
            error_type=error_type,
            error_code=error_code,
            error_message=error_message,
            test_url=test_url,
            runtime_mode=runtime_info["runtime_mode"],
            hard_enabled=runtime_info["hard_enabled"],
            download_smoke_details=download_smoke_details,
        )

    try:
        key = item["key"]
        if key == "health_ready":
            readiness = get_runtime_readiness()
            if readiness["ready"]:
                return record(ProviderHealthTestStatus.SUCCESS)
            return record(
                ProviderHealthTestStatus.UNAVAILABLE,
                "RuntimeUnavailable",
                "RUNTIME_UNAVAILABLE",
                json.dumps(readiness.get("components", {}), ensure_ascii=False),
            )

        if key == "tiantianjiexi_account_status":
            if not env_runtime_bool("TIANTIANJIEXI_PROVIDER_ENABLED", False):
                return record(ProviderHealthTestStatus.UNAVAILABLE, "CapabilityDisabled", "CAPABILITY_DISABLED")
            health = TianTianJieXiGenericProvider().health()
            if health.get("account_status_available"):
                return record(ProviderHealthTestStatus.SUCCESS)
            return record(
                ProviderHealthTestStatus.UNAVAILABLE,
                "AccountStatusUnavailable",
                "ACCOUNT_STATUS_UNAVAILABLE",
                json.dumps(health, ensure_ascii=False),
            )

        if key == "videodl_policy_inventory":
            if not _videodl_runtime_ready():
                return record(ProviderHealthTestStatus.UNAVAILABLE, "RuntimeUnavailable", "VIDEODL_RUNTIME_UNAVAILABLE")
            inventory = load_videodl_inventory(
                python=_videodl_python(),
                worker=VIDEODL_WORKER,
                request_id=context.request_id,
            )
            dedicated = {row.backend_id for row in inventory if row.backend_category == "dedicated"}
            common = {row.backend_id for row in inventory if row.backend_category == "common"}
            boundary_ok = (
                dedicated == set(REVIEWED_DEDICATED_BACKEND_IDS)
                and common == set(REVIEWED_COMMON_BACKEND_IDS)
                and backend_policy("UnknownBackend", "dedicated").controlled_state == "blocked"
                and backend_policy("UnknownCommon", "common").controlled_state == "blocked"
                and not any(row.browser_execution and row.controlled_available for row in inventory)
                and VIDEODL_POLICY_PIN
            )
            if boundary_ok:
                return record(ProviderHealthTestStatus.SUCCESS)
            return record(ProviderHealthTestStatus.UNAVAILABLE, "PolicyMismatch", "VIDEODL_POLICY_MISMATCH")

        if not test_url:
            return record(ProviderHealthTestStatus.SKIPPED, "TestUrlMissing", "TEST_URL_MISSING", "未配置测试 URL")

        try:
            validate_provider_health_sample_url(
                test_url,
                item.get("url_field") or "",
                resolve_dns=True,
            )
        except XhsSecurityError:
            return record(
                ProviderHealthTestStatus.UNAVAILABLE,
                "SampleUrlPolicyRejected",
                SAMPLE_URL_POLICY_ERROR_CODE,
                SAMPLE_URL_POLICY_MESSAGE,
            )

        if key.startswith("download_smoke_"):
            from .provider_download_smoke import run_download_smoke
            download_smoke_details = run_download_smoke(context, key, test_url)
            provider = download_smoke_details.get("provider") or provider
            download = download_smoke_details["download"]
            if download["status"] == "success":
                return record(ProviderHealthTestStatus.SUCCESS)
            return record(
                ProviderHealthTestStatus.FAILED,
                download_smoke_details.get("error_type") or "DownloadSmokeFailure",
                download.get("error_code") or "DOWNLOAD_SMOKE_FAILED",
                "下载冒烟未通过；阶段和有限取数信息见明细。",
            )

        if key == "douyin_native_parse":
            result = parse_media_page(test_url, prefer_ultra_hd=False)
            return record(ProviderHealthTestStatus.SUCCESS) if result.assets else record(
                ProviderHealthTestStatus.UNAVAILABLE, "NoAssets", "NO_ASSETS", "未解析到资源"
            )

        if key == "douyin_kukutool_ultra":
            if not (
                getattr(settings, "ULTRA_HD_PROVIDER_ENABLED", False)
                and getattr(settings, "KUKUTOOL_PROVIDER_ENABLED", False)
            ):
                return record(ProviderHealthTestStatus.UNAVAILABLE, "CapabilityDisabled", "CAPABILITY_DISABLED")
            health = get_kukutool_provider_health()
            if not health.get("available"):
                return record(ProviderHealthTestStatus.UNAVAILABLE, "ProviderUnavailable", "PROVIDER_UNAVAILABLE", json.dumps(health, ensure_ascii=False))
            adapter = DouyinAdapter()
            deadline = time.monotonic() + 10.0
            parse_context = adapter.build_parse_context(
                test_url,
                fetch_direct_page=not bool(adapter.supports_url(test_url)),
                fast_short_link=True,
                request_deadline=deadline,
            )
            # Only KuKuTool is measured: a resolved empty future stops the
            # provider from submitting Native, and no TianTian call is made.
            empty_native = Future()
            empty_native.set_result(None)
            parse_context.native_fallback_future = empty_native
            result = DouyinKukuToolProvider().parse(adapter, parse_context)
            error = provider_asset_manifest_validation_error(result, {"kukutool"}, require_video=True)
            metadata = result.metadata or {} if result else {}
            selected = metadata.get("selected_provider") or metadata.get("source_provider")
            if not error and selected == "kukutool":
                return record(ProviderHealthTestStatus.SUCCESS)
            return record(ProviderHealthTestStatus.UNAVAILABLE, "InvalidParseResult", "INVALID_PARSE_RESULT", error or "Provider mismatch")

        if key == "douyin_tiantianjiexi_ultra":
            if not (
                getattr(settings, "ULTRA_HD_PROVIDER_ENABLED", False)
                and env_runtime_bool("THIRD_PARTY_PROVIDER_ENABLED", False)
                and env_runtime_bool("TIANTIANJIEXI_PROVIDER_ENABLED", False)
            ):
                return record(ProviderHealthTestStatus.UNAVAILABLE, "CapabilityDisabled", "CAPABILITY_DISABLED")
            adapter = DouyinAdapter()
            deadline = time.monotonic() + 10.0
            parse_context = adapter.build_parse_context(
                test_url,
                fetch_direct_page=not bool(adapter.supports_url(test_url)),
                fast_short_link=True,
                request_deadline=deadline,
            )
            result = DouyinTianTianJieXiProvider().parse(adapter, parse_context)
            error = provider_asset_manifest_validation_error(
                result, {"tiantianjiexi", "tiantianjiexi_generic"}, require_video=True
            )
            metadata = result.metadata or {} if result else {}
            selected = metadata.get("selected_provider") or metadata.get("source_provider")
            if not error and selected in {"tiantianjiexi", "tiantianjiexi_generic"}:
                return record(ProviderHealthTestStatus.SUCCESS)
            return record(ProviderHealthTestStatus.UNAVAILABLE, "InvalidParseResult", "INVALID_PARSE_RESULT", error or "Provider mismatch")

        if key == "youtube_merge":
            health = get_youtube_merge_health()
            if not (health.get("implemented") and health.get("ready")):
                return record(ProviderHealthTestStatus.UNAVAILABLE, "ProviderUnavailable", "PROVIDER_UNAVAILABLE", json.dumps(health, ensure_ascii=False))
            payload = YtDlpGenericProvider().parse(
                test_url, timeout_seconds=min(10.0, float(health.get("timeout_seconds") or 10.0))
            )
            plan = build_youtube_merge_plan(
                payload,
                max_height=int(getattr(settings, "YTDLP_MERGE_DEFAULT_MAX_HEIGHT", 1080) or 1080),
            )
            if plan.get("available"):
                return record(ProviderHealthTestStatus.SUCCESS)
            return record(ProviderHealthTestStatus.UNAVAILABLE, "MergeUnavailable", "MERGE_UNAVAILABLE", json.dumps(plan, ensure_ascii=False))

        if key == "third_party_ytdlp":
            if not env_runtime_bool("THIRD_PARTY_PROVIDER_ENABLED", False):
                return record(ProviderHealthTestStatus.UNAVAILABLE, "CapabilityDisabled", "CAPABILITY_DISABLED")
            health = YtDlpGenericProvider().health(force_version_probe=True)
            if not health.get("ready"):
                return record(ProviderHealthTestStatus.UNAVAILABLE, "ProviderUnavailable", "PROVIDER_UNAVAILABLE", json.dumps(health, ensure_ascii=False))
            payload = YtDlpGenericProvider().parse(test_url, timeout_seconds=10.0)
            return record(ProviderHealthTestStatus.SUCCESS) if payload else record(
                ProviderHealthTestStatus.UNAVAILABLE, "EmptyPayload", "EMPTY_PAYLOAD"
            )

        if key == "gallery_dl":
            if not env_runtime_bool("GALLERYDL_PROVIDER_ENABLED", False):
                return record(ProviderHealthTestStatus.UNAVAILABLE, "CapabilityDisabled", "CAPABILITY_DISABLED")
            health = GalleryDlGenericProvider().health(force_version_probe=True)
            if not health.get("ready"):
                return record(ProviderHealthTestStatus.UNAVAILABLE, "ProviderUnavailable", "PROVIDER_UNAVAILABLE", json.dumps(health, ensure_ascii=False))
            payload = GalleryDlGenericProvider().parse(test_url, timeout_seconds=10.0)
            return record(ProviderHealthTestStatus.SUCCESS) if payload else record(
                ProviderHealthTestStatus.UNAVAILABLE, "EmptyPayload", "EMPTY_PAYLOAD"
            )

        if key == "third_party_tiantianjiexi":
            if not (
                env_runtime_bool("THIRD_PARTY_PROVIDER_ENABLED", False)
                and env_runtime_bool("TIANTIANJIEXI_PROVIDER_ENABLED", False)
            ):
                return record(ProviderHealthTestStatus.UNAVAILABLE, "CapabilityDisabled", "CAPABILITY_DISABLED")
            result = build_tiantianjiexi_third_party_parse_result(test_url, timeout_seconds=10.0)
            error = provider_asset_manifest_validation_error(result, {"tiantianjiexi_generic"}, require_video=False)
            return record(ProviderHealthTestStatus.SUCCESS) if not error else record(
                ProviderHealthTestStatus.UNAVAILABLE, "InvalidParseResult", "INVALID_PARSE_RESULT", error
            )

        if key in {"xhs_video_current", "xhs_gallery_current", "bilibili_native_parse"}:
            result = parse_media_page(test_url, timeout_seconds=15.0)
            expected = {
                "xhs_video_current": ("xiaohongshu", "video"),
                "xhs_gallery_current": ("xiaohongshu", "image"),
                "bilibili_native_parse": ("bilibili", "video"),
            }[key]
            error = _validate_current_parse_result(result, platform=expected[0], media_type=expected[1])
            return record(ProviderHealthTestStatus.SUCCESS) if not error else record(
                ProviderHealthTestStatus.UNAVAILABLE, "InvalidParseResult", error
            )

        if key == "videodl_specific_backend":
            backend_id = str(test_setting.videodl_backend_id or "").strip()
            if not backend_id:
                return record(ProviderHealthTestStatus.SKIPPED, "BackendMissing", "VIDEODL_BACKEND_MISSING")
            if not validate_daily_videodl_backend_id(backend_id):
                return record(ProviderHealthTestStatus.UNAVAILABLE, "PolicyRejected", "VIDEODL_BACKEND_POLICY_REJECTED")
            if not _videodl_runtime_ready():
                return record(ProviderHealthTestStatus.UNAVAILABLE, "RuntimeUnavailable", "VIDEODL_RUNTIME_UNAVAILABLE")
            controlled = execute_controlled_videodl(
                python=_videodl_python(),
                worker=VIDEODL_WORKER,
                request_id=context.request_id,
                source=test_url,
                options=VideodlExecutionOptions(
                    mode="specific_backend",
                    backend_ids=(backend_id,),
                    backend_category="dedicated",
                    external_sharing_confirmed=False,
                ),
            )
            if controlled.resources or (controlled.result and controlled.result.get("assets")):
                return record(ProviderHealthTestStatus.SUCCESS)
            return record(ProviderHealthTestStatus.UNAVAILABLE, "EmptyPayload", "EMPTY_PAYLOAD")

        return record(ProviderHealthTestStatus.FAILED, "UnknownTest", "UNKNOWN_TEST")
    except ProviderLabError as error:
        return record(ProviderHealthTestStatus.FAILED, "ProviderLabError", error.code)
    except Exception as error:
        return record(
            ProviderHealthTestStatus.FAILED,
            type(error).__name__,
            _safe_error_code(type(error).__name__),
            str(error),
        )


def run_provider_health_test(request, item):
    return execute_provider_health_test(provider_health_context_from_request(request), item)


__all__ = [
    "PROVIDER_HEALTH_TESTS",
    "ProviderHealthExecutionContext",
    "execute_provider_health_test",
    "provider_health_context_from_request",
    "provider_health_test_by_key",
    "provider_health_test_keys",
    "provider_test_runtime_info",
    "provider_test_url_for",
    "provider_test_url_host",
    "record_unexecuted_test",
    "run_provider_health_test",
    "validate_daily_test_keys",
    "validate_daily_videodl_backend_id",
    "validate_provider_health_sample_url",
]
