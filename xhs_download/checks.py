import importlib.util
import os

from django.conf import settings
from django.core.checks import Error, Warning as CheckWarning, register

from .services.platforms.douyin_kukutool import (
    get_kukutool_runtime_status,
    get_kukutool_sidecar_status,
)
from .services.platforms.gallerydl_generic import GalleryDlGenericProvider
from .services.platforms.ytdlp_generic import YtDlpGenericProvider
from .services.youtube_merge import get_youtube_merge_health


def third_party_specific_provider_enabled():
    return bool(
        getattr(settings, "THIRD_PARTY_KUKUTOOL_PROVIDER_ENABLED", False)
        or getattr(settings, "YTDLP_PROVIDER_ENABLED", False)
        or getattr(settings, "GALLERYDL_PROVIDER_ENABLED", False)
        or getattr(settings, "YOUTUBE_MERGE_PROVIDER_ENABLED", False)
    )


def provider_check_message(message, *, hint=None, id):
    check_class = (
        Error
        if getattr(settings, "PROVIDER_STRICT_CAPABILITY_CHECKS", False)
        else CheckWarning
    )
    return check_class(message, hint=hint, id=id)


@register()
def check_redis_cache_dependency(app_configs, **kwargs):
    cache_url = str(getattr(settings, "DJANGO_CACHE_URL", "") or "")
    if not cache_url.startswith(("redis://", "rediss://")):
        return []
    if importlib.util.find_spec("redis") is not None:
        return []
    return [
        Error(
            "Redis cache is configured but the redis Python package is not installed.",
            hint="Install project dependencies with `python -m pip install -r requirements.txt`.",
            id="xhs_download.E008",
        )
    ]


@register()
def check_tiantianjiexi_shared_concurrency_backend(app_configs, **kwargs):
    if not getattr(settings, "TIANTIANJIEXI_PROVIDER_ENABLED", False):
        return []

    issues = []
    production = getattr(settings, "REQUIRE_PRODUCTION_CONFIG", not settings.DEBUG)
    cache_backend = str(
        settings.CACHES.get("default", {}).get("BACKEND", "")
    )
    if production and cache_backend != "django.core.cache.backends.redis.RedisCache":
        issues.append(
            Error(
                "Production TianTianJieXi parsing requires the shared Redis cache backend.",
                hint=(
                    "Set DJANGO_CACHE_URL to Redis before enabling "
                    "TIANTIANJIEXI_PROVIDER_ENABLED. The provider fails closed "
                    "without a cross-process lease backend."
                ),
                id="xhs_download.E013",
            )
        )

    configured_limit = str(
        os.environ.get("TIANTIANJIEXI_GLOBAL_MAX_CONCURRENCY", "1") or "1"
    ).strip()
    if configured_limit != "1":
        issues.append(
            CheckWarning(
                "TIANTIANJIEXI_GLOBAL_MAX_CONCURRENCY is fixed at 1 by the provider lease.",
                hint="Set the legacy environment value to 1 to match effective behavior.",
                id="xhs_download.W014",
            )
        )
    return issues


@register()
def check_kukutool_runtime(app_configs, **kwargs):
    if not settings.KUKUTOOL_PROVIDER_ENABLED:
        return []

    issues = []
    sidecar_base_url = getattr(settings, "KUKUTOOL_SIDECAR_BASE_URL", "") or ""
    if (
        getattr(settings, "REQUIRE_PRODUCTION_CONFIG", not settings.DEBUG)
        and not settings.KUKUTOOL_NODE_BINARY
        and not sidecar_base_url
    ):
        issues.append(
            provider_check_message(
                "Production KuKuTool requires an explicit KUKUTOOL_NODE_BINARY or KUKUTOOL_SIDECAR_BASE_URL.",
                id="xhs_download.E001",
            )
        )

    if settings.ULTRA_HD_PROVIDER_ENABLED:
        runtime = get_kukutool_runtime_status()
        sidecar = get_kukutool_sidecar_status()
        if not runtime["available"] and not sidecar["available"]:
            issues.append(
                provider_check_message(
                    "KuKuTool runtime is unavailable: "
                    f"{runtime['error']}; sidecar: {sidecar.get('error')}",
                    id="xhs_download.E002",
                )
            )
    return issues


def check_external_provider_runtime(
    *,
    enabled,
    binary,
    provider_name,
    provider_health,
    explicit_binary_error_id,
    unavailable_error_id,
):
    if not enabled:
        return []

    issues = []
    if getattr(settings, "REQUIRE_PRODUCTION_CONFIG", not settings.DEBUG) and not binary:
        issues.append(
            provider_check_message(
                f"Production {provider_name} requires an explicit executable binary setting.",
                id=explicit_binary_error_id,
            )
        )

    health = provider_health()
    if not health.get("executable_found"):
        issues.append(
            provider_check_message(
                f"{provider_name} executable is unavailable: {health.get('error') or 'unknown error'}",
                id=unavailable_error_id,
            )
        )
    elif not health.get("version_probe_ok"):
        issues.append(
            provider_check_message(
                f"{provider_name} executable failed version probe: {health.get('version_probe_error') or 'unknown error'}",
                id=unavailable_error_id,
            )
        )
    return issues


@register()
def check_generic_external_provider_runtimes(app_configs, **kwargs):
    issues = []
    if (
        getattr(settings, "THIRD_PARTY_PROVIDER_ENABLED", False)
        and not third_party_specific_provider_enabled()
    ):
        issues.append(
            provider_check_message(
                "Third-party provider capability is enabled but no concrete provider is enabled.",
                id="xhs_download.E007",
            )
        )
    issues.extend(
        check_external_provider_runtime(
            enabled=getattr(settings, "YTDLP_PROVIDER_ENABLED", False),
            binary=getattr(settings, "YTDLP_BINARY", ""),
            provider_name="yt-dlp",
            provider_health=lambda: YtDlpGenericProvider().health(version_cache_seconds=0),
            explicit_binary_error_id="xhs_download.E003",
            unavailable_error_id="xhs_download.E004",
        )
    )
    issues.extend(
        check_external_provider_runtime(
            enabled=getattr(settings, "GALLERYDL_PROVIDER_ENABLED", False),
            binary=getattr(settings, "GALLERYDL_BINARY", ""),
            provider_name="gallery-dl",
            provider_health=lambda: GalleryDlGenericProvider().health(version_cache_seconds=0),
            explicit_binary_error_id="xhs_download.E005",
            unavailable_error_id="xhs_download.E006",
        )
    )
    return issues


@register()
def check_youtube_merge_provider_runtime(app_configs, **kwargs):
    if not getattr(settings, "YOUTUBE_MERGE_PROVIDER_ENABLED", False):
        return []

    issues = []
    production = getattr(settings, "REQUIRE_PRODUCTION_CONFIG", not settings.DEBUG)
    if production and not getattr(settings, "YTDLP_BINARY", ""):
        issues.append(
            provider_check_message(
                "Production YouTube merge mode requires an explicit YTDLP_BINARY.",
                id="xhs_download.E009",
            )
        )
    if (
        production
        and getattr(settings, "YOUTUBE_MERGE_REQUIRE_FFMPEG", True)
        and not getattr(settings, "YOUTUBE_MERGE_FFMPEG_BINARY", "")
    ):
        issues.append(
            provider_check_message(
                "Production YouTube merge mode requires an explicit YOUTUBE_MERGE_FFMPEG_BINARY.",
                id="xhs_download.E011",
            )
        )

    health = get_youtube_merge_health(force_version_probe=True)
    ytdlp_health = health.get("ytdlp") or {}
    if not ytdlp_health.get("executable_found"):
        issues.append(
            provider_check_message(
                "YouTube merge yt-dlp executable is unavailable: "
                f"{ytdlp_health.get('error') or 'unknown error'}",
                id="xhs_download.E010",
            )
        )
    elif not ytdlp_health.get("version_probe_ok"):
        issues.append(
            provider_check_message(
                "YouTube merge yt-dlp executable failed version probe: "
                f"{ytdlp_health.get('version_probe_error') or 'unknown error'}",
                id="xhs_download.E010",
            )
        )

    if getattr(settings, "YOUTUBE_MERGE_REQUIRE_FFMPEG", True):
        ffmpeg_health = health.get("ffmpeg") or {}
        if not ffmpeg_health.get("executable_found"):
            issues.append(
                provider_check_message(
                    "YouTube merge ffmpeg executable is unavailable: "
                    f"{ffmpeg_health.get('error') or 'unknown error'}",
                    id="xhs_download.E012",
                )
            )
        elif not ffmpeg_health.get("version_probe_ok"):
            issues.append(
                provider_check_message(
                    "YouTube merge ffmpeg executable failed version probe: "
                    f"{ffmpeg_health.get('version_probe_error') or 'unknown error'}",
                    id="xhs_download.E012",
                )
            )
    return issues
