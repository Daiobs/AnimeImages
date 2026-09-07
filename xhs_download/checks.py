import importlib.util
from urllib.parse import urlparse

from django.conf import settings
from django.core.checks import Error, Warning as CheckWarning, register

from .services.env_config import runtime_bool, runtime_list, runtime_str
from .services.provider_daily_validation import (
    NON_DELIVERY_EMAIL_BACKENDS,
    provider_validation_email_backend_is_importable,
)
from .services.platforms.douyin_kukutool import (
    get_kukutool_runtime_status,
    get_kukutool_sidecar_status,
)
from .services.platforms.gallerydl_generic import GalleryDlGenericProvider
from .services.platforms.ytdlp_generic import YtDlpGenericProvider
from .services.youtube_merge import get_youtube_merge_health


@register()
def check_provider_validation_email_configuration(app_configs, **kwargs):
    del app_configs, kwargs
    if not runtime_bool("PROVIDER_VALIDATION_EMAIL_DELIVERY_ENABLED", False):
        return []

    from .services.mail_channel import resolve_mail_channel
    channel = resolve_mail_channel()
    if channel.source != "env":
        errors = list(channel.errors)
        if not channel.recipients:
            errors.append("RECIPIENTS_REQUIRED")
        return [Error("Admin mail configuration: " + code, id="xhs_download.E020") for code in errors] + _check_mail_report_url()

    issues = []
    recipients = tuple(
        str(value or "").strip()
        for value in runtime_list("PROVIDER_VALIDATION_EMAIL_RECIPIENTS", ())
        if str(value or "").strip()
    )
    backend = runtime_str(
        "DJANGO_EMAIL_BACKEND",
        "django.core.mail.backends.console.EmailBackend",
    )
    if not provider_validation_email_backend_is_importable(backend):
        issues.append(
            Error(
                "Provider validation email delivery requires an importable Django email backend.",
                id="xhs_download.E019",
            )
        )
    if not recipients:
        issues.append(
            Error(
                "Provider validation email delivery requires at least one configured recipient.",
                id="xhs_download.E013",
            )
        )
    if not runtime_str("DJANGO_DEFAULT_FROM_EMAIL", ""):
        issues.append(
            Error(
                "Provider validation email delivery requires DJANGO_DEFAULT_FROM_EMAIL.",
                id="xhs_download.E014",
            )
        )
    if runtime_bool("DJANGO_EMAIL_USE_TLS", False) and runtime_bool(
        "DJANGO_EMAIL_USE_SSL", False
    ):
        issues.append(
            Error(
                "Provider validation email delivery cannot enable TLS and SSL together.",
                id="xhs_download.E015",
            )
        )
    if backend.endswith("smtp.EmailBackend") and not runtime_str(
        "DJANGO_EMAIL_HOST", ""
    ):
        issues.append(
            Error(
                "Provider validation SMTP delivery requires DJANGO_EMAIL_HOST.",
                id="xhs_download.E016",
            )
        )
    if getattr(settings, "REQUIRE_PRODUCTION_CONFIG", not settings.DEBUG) and (
        backend in NON_DELIVERY_EMAIL_BACKENDS
    ):
        issues.append(
            Error(
                "Production Provider validation delivery requires a real email backend.",
                id="xhs_download.E017",
            )
        )
    return issues + _check_mail_report_url()


def _check_mail_report_url():
    issues = []
    report_url = runtime_str("PROVIDER_VALIDATION_ADMIN_REPORT_URL", "")
    if report_url:
        parsed = urlparse(report_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            issues.append(
                Error(
                    "Provider validation Admin report URL must be an http/https URL without credentials, query, or fragment.",
                    id="xhs_download.E018",
                )
            )
    return issues


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
