from django.conf import settings
from django.core.checks import Error, register

from .services.platforms.douyin_kukutool import get_kukutool_runtime_status


@register()
def check_kukutool_runtime(app_configs, **kwargs):
    if not settings.KUKUTOOL_PROVIDER_ENABLED:
        return []

    issues = []
    if (
        getattr(settings, "REQUIRE_PRODUCTION_CONFIG", not settings.DEBUG)
        and not settings.KUKUTOOL_NODE_BINARY
    ):
        issues.append(
            Error(
                "Production KuKuTool requires an explicit KUKUTOOL_NODE_BINARY.",
                id="xhs_download.E001",
            )
        )

    if settings.ULTRA_HD_PROVIDER_ENABLED:
        runtime = get_kukutool_runtime_status()
        if not runtime["available"]:
            issues.append(
                Error(
                    f"KuKuTool runtime is unavailable: {runtime['error']}",
                    id="xhs_download.E002",
                )
            )
    return issues
