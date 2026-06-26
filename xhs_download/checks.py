import importlib.util

from django.conf import settings
from django.core.checks import Error, register

from .services.platforms.douyin_kukutool import (
    get_kukutool_runtime_status,
    get_kukutool_sidecar_status,
)
from .services.platforms.gallerydl_generic import GalleryDlGenericProvider
from .services.platforms.ytdlp_generic import YtDlpGenericProvider


def third_party_specific_provider_enabled():
    return bool(
        getattr(settings, "THIRD_PARTY_KUKUTOOL_PROVIDER_ENABLED", False)
        or getattr(settings, "YTDLP_PROVIDER_ENABLED", False)
        or getattr(settings, "GALLERYDL_PROVIDER_ENABLED", False)
    )


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
            Error(
                "Production KuKuTool requires an explicit KUKUTOOL_NODE_BINARY or KUKUTOOL_SIDECAR_BASE_URL.",
                id="xhs_download.E001",
            )
        )

    if settings.ULTRA_HD_PROVIDER_ENABLED:
        runtime = get_kukutool_runtime_status()
        sidecar = get_kukutool_sidecar_status()
        if not runtime["available"] and not sidecar["available"]:
            issues.append(
                Error(
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
            Error(
                f"Production {provider_name} requires an explicit executable binary setting.",
                id=explicit_binary_error_id,
            )
        )

    health = provider_health()
    if not health.get("executable_found"):
        issues.append(
            Error(
                f"{provider_name} executable is unavailable: {health.get('error') or 'unknown error'}",
                id=unavailable_error_id,
            )
        )
    elif not health.get("version_probe_ok"):
        issues.append(
            Error(
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
        and getattr(settings, "THIRD_PARTY_PUBLIC_ACCESS_ENABLED", False)
        and not third_party_specific_provider_enabled()
    ):
        issues.append(
            Error(
                "Third-party public parsing is enabled but no third-party provider is enabled.",
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
