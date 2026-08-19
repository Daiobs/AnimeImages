import logging
from time import monotonic

from django.conf import settings
from django.core.cache import cache

from .common import XhsDownloadError, XhsProviderTimeoutError
from .provider_manifest import provider_manifest_is_valid
from .platforms.douyin import DouyinAdapter
from .platforms.douyin_media_policy import validate_douyin_media_url
from .platforms.douyin_kukutool import (
    AWEME_ID_PATTERN,
    DouyinKukuToolProvider,
    build_kukutool_cache_key,
    build_kukutool_failure_cache_key,
    is_byte_cdn_url,
)
from .platforms.douyin_providers import (
    DouyinParseContext,
    DouyinTianTianJieXiProvider,
    get_fast_native_result,
)
from .platforms.kukutool_generic import source_key_for_url
from .platforms.tiantianjiexi_generic import (
    build_failure_cache_key as build_tiantianjiexi_failure_cache_key,
    build_payload_cache_key as build_tiantianjiexi_payload_cache_key,
)
from .provider_settings import get_platform_provider_settings


logger = logging.getLogger(__name__)


class TianTianJieXiRecoveryError(XhsDownloadError):
    def __init__(self, message, *, error_code):
        super().__init__(message)
        self.error_code = error_code


def get_kukutool_manifest_identity(item):
    if not isinstance(item, dict) or item.get("provider") != "kukutool":
        return None
    if not provider_manifest_is_valid(item):
        return None

    aweme_id = str(item.get("provider_resource_id") or "")
    if not AWEME_ID_PATTERN.fullmatch(aweme_id):
        return None
    if not is_byte_cdn_url(item.get("url")):
        return None

    expected_cache_key = build_kukutool_cache_key(aweme_id)
    supplied_cache_key = str(item.get("provider_cache_key") or "")
    if supplied_cache_key != expected_cache_key:
        logger.warning(
            "kukutool_download_manifest_cache_key_rejected",
            extra={"aweme_id": aweme_id},
        )
        return None

    expected_asset_id = f"douyin:{aweme_id}:video:kukutool"
    supplied_asset_id = str(item.get("asset_id") or "")
    if supplied_asset_id != expected_asset_id:
        logger.warning(
            "kukutool_download_manifest_asset_id_rejected",
            extra={"aweme_id": aweme_id},
        )
        return None

    return aweme_id, expected_cache_key


def get_tiantianjiexi_manifest_identity(item):
    if not isinstance(item, dict) or item.get("provider") != "tiantianjiexi":
        return None
    if not provider_manifest_is_valid(item):
        return None

    aweme_id = str(item.get("provider_resource_id") or "")
    if not AWEME_ID_PATTERN.fullmatch(aweme_id):
        return None
    expected_cache_key = f"douyin:tiantianjiexi:validated:{aweme_id}"
    if str(item.get("provider_cache_key") or "") != expected_cache_key:
        logger.warning(
            "tiantianjiexi_download_manifest_cache_key_rejected",
            extra={"aweme_id": aweme_id},
        )
        return None
    expected_asset_id = f"douyin:{aweme_id}:video:tiantianjiexi"
    if str(item.get("asset_id") or "") != expected_asset_id:
        logger.warning(
            "tiantianjiexi_download_manifest_asset_id_rejected",
            extra={"aweme_id": aweme_id},
        )
        return None
    if str(item.get("type") or "").lower() != "video":
        return None

    try:
        validate_douyin_media_url(
            item.get("url"),
            "天天解析抖音 Provider URL 不允许请求",
            existing_suffixes=DouyinAdapter.media_host_suffixes,
        )
    except (TypeError, ValueError):
        return None
    return aweme_id, expected_cache_key


def invalidate_kukutool_download_cache(aweme_id, cache_key):
    for key in (
        cache_key,
        build_kukutool_failure_cache_key(aweme_id),
    ):
        try:
            cache.delete(key)
        except Exception:
            logger.exception(
                "kukutool_download_cache_invalidation_failed",
                extra={"aweme_id": aweme_id, "cache_key": key},
            )


def invalidate_tiantianjiexi_download_cache(aweme_id, cache_key):
    source_url = f"https://www.douyin.com/video/{aweme_id}"
    source_key = source_key_for_url(source_url)
    keys = (
        cache_key,
        build_tiantianjiexi_payload_cache_key(source_key),
        build_tiantianjiexi_failure_cache_key(source_key),
    )
    try:
        cache.delete_many(keys)
    except Exception as error:
        logger.warning(
            "tiantianjiexi_download_cache_invalidation_failed",
            extra={
                "aweme_id": aweme_id,
                "error_type": type(error).__name__,
            },
        )
        raise TianTianJieXiRecoveryError(
            "资源已失效，自动刷新失败，请重新解析。",
            error_code="TIANTIANJIEXI_REFRESH_FAILED",
        ) from error


def refresh_tiantianjiexi_download_item(item, *, deadline_monotonic=None):
    identity = get_tiantianjiexi_manifest_identity(item)
    if not identity:
        raise TianTianJieXiRecoveryError(
            "资源恢复身份无效，请重新解析后再试。",
            error_code="PROXY_FAILED_RECOVERY_BLOCKED",
        )

    aweme_id, cache_key = identity
    now = monotonic()
    if (
        deadline_monotonic is not None
        and float(deadline_monotonic) <= now
    ):
        raise TianTianJieXiRecoveryError(
            "资源连接超时，请重新点击下载或重新解析后再试。",
            error_code="DOWNLOAD_STREAM_START_TIMEOUT",
        )
    invalidate_tiantianjiexi_download_cache(aweme_id, cache_key)
    source_url = f"https://www.douyin.com/video/{aweme_id}"
    provider_settings = get_platform_provider_settings("douyin")
    configured_timeout_seconds = max(
        0.1,
        float(provider_settings.ultra_total_timeout_seconds or 0),
    )
    provider_deadline = now + configured_timeout_seconds
    if deadline_monotonic is not None:
        provider_deadline = min(provider_deadline, float(deadline_monotonic))
    adapter = DouyinAdapter()
    context = DouyinParseContext(
        url=source_url,
        final_url=source_url,
        aweme_id=aweme_id,
        prefer_ultra_hd=True,
        deadline_monotonic=provider_deadline,
    )
    try:
        result = DouyinTianTianJieXiProvider(
            provider_settings=provider_settings,
            reserve_fallback_grace=False,
            suppress_selected_log=True,
        ).parse(adapter, context)
    except XhsProviderTimeoutError as error:
        startup_deadline_exhausted = bool(
            deadline_monotonic is not None
            and monotonic() >= float(deadline_monotonic)
        )
        raise TianTianJieXiRecoveryError(
            (
                "资源连接超时，请重新点击下载或重新解析后再试。"
                if startup_deadline_exhausted
                else "资源刷新超时，请重新解析后再试。"
            ),
            error_code=(
                "DOWNLOAD_STREAM_START_TIMEOUT"
                if startup_deadline_exhausted
                else "TIANTIANJIEXI_REFRESH_TIMEOUT"
            ),
        ) from error
    except Exception as error:
        logger.warning(
            "tiantianjiexi_download_refresh_failed",
            extra={
                "aweme_id": aweme_id,
                "error_type": type(error).__name__,
            },
        )
        raise TianTianJieXiRecoveryError(
            "资源已失效，自动刷新失败，请重新解析。",
            error_code="TIANTIANJIEXI_REFRESH_FAILED",
        ) from error

    assets = tuple(getattr(result, "video_assets", ()) or ())
    if len(assets) != 1:
        raise TianTianJieXiRecoveryError(
            "资源已失效，自动刷新失败，请重新解析。",
            error_code="TIANTIANJIEXI_REFRESH_FAILED",
        )
    refreshed_item = assets[0].to_download_item()
    refreshed_item["size_bytes"] = int(getattr(assets[0], "size_bytes", 0) or 0)
    if get_tiantianjiexi_manifest_identity(refreshed_item) != identity:
        raise TianTianJieXiRecoveryError(
            "刷新后的资源身份不匹配，请重新解析。",
            error_code="PROXY_FAILED_RECOVERY_BLOCKED",
        )
    return refreshed_item


def iter_kukutool_download_recovery_urls(item, *, deadline_monotonic=None):
    identity = get_kukutool_manifest_identity(item)
    if not identity:
        return

    aweme_id, cache_key = identity
    if (
        deadline_monotonic is not None
        and monotonic() >= float(deadline_monotonic)
    ):
        return
    invalidate_kukutool_download_cache(aweme_id, cache_key)
    adapter = DouyinAdapter()
    context = DouyinParseContext(
        url=f"https://www.douyin.com/video/{aweme_id}",
        final_url=f"https://www.douyin.com/video/{aweme_id}",
        aweme_id=aweme_id,
        deadline_monotonic=deadline_monotonic,
    )

    if (
        settings.ULTRA_HD_PROVIDER_ENABLED
        and settings.KUKUTOOL_PROVIDER_ENABLED
        and (
            deadline_monotonic is None
            or monotonic() < float(deadline_monotonic)
        )
    ):
        try:
            validated = DouyinKukuToolProvider().refresh_for_download(
                adapter,
                context,
            )
            if validated and validated.candidate.url:
                yield validated.candidate.url
        except Exception:
            logger.exception(
                "kukutool_download_refresh_failed",
                extra={"aweme_id": aweme_id},
            )

    try:
        remaining = None
        if deadline_monotonic is not None:
            remaining = float(deadline_monotonic) - monotonic()
            if remaining <= 0:
                return
        native_result = get_fast_native_result(
            adapter,
            context,
            timeout_seconds=min(
                float(
                    getattr(
                        settings,
                        "DOUYIN_ULTRA_NATIVE_TIMEOUT_SECONDS",
                        3.0,
                    )
                    or 3.0
                ),
                remaining if remaining is not None else float("inf"),
            ),
        )
    except Exception:
        logger.exception(
            "kukutool_download_native_fallback_failed",
            extra={"aweme_id": aweme_id},
        )
        return

    if native_result and native_result.video_assets:
        native_url = native_result.video_assets[0].url
        if native_url:
            yield native_url
