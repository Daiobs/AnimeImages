import logging

from django.conf import settings
from django.core.cache import cache

from .provider_manifest import provider_manifest_is_valid
from .platforms.douyin import DouyinAdapter
from .platforms.douyin_kukutool import (
    AWEME_ID_PATTERN,
    DouyinKukuToolProvider,
    build_kukutool_cache_key,
    build_kukutool_failure_cache_key,
    is_byte_cdn_url,
)
from .platforms.douyin_providers import (
    DouyinParseContext,
    get_fast_native_result,
)


logger = logging.getLogger(__name__)


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


def iter_kukutool_download_recovery_urls(item):
    identity = get_kukutool_manifest_identity(item)
    if not identity:
        return

    aweme_id, cache_key = identity
    invalidate_kukutool_download_cache(aweme_id, cache_key)
    adapter = DouyinAdapter()
    context = DouyinParseContext(
        url=f"https://www.douyin.com/video/{aweme_id}",
        final_url=f"https://www.douyin.com/video/{aweme_id}",
        aweme_id=aweme_id,
    )

    if (
        settings.ULTRA_HD_PROVIDER_ENABLED
        and settings.KUKUTOOL_PROVIDER_ENABLED
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
        native_result = get_fast_native_result(
            adapter,
            context,
            timeout_seconds=float(
                getattr(settings, "DOUYIN_ULTRA_NATIVE_TIMEOUT_SECONDS", 3.0)
                or 3.0
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
