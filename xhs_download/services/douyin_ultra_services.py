from dataclasses import dataclass

from ..models import ProviderHealthTestRun, ProviderHealthTestStatus
from .platforms.douyin_providers import configured_douyin_services
from .provider_access import PROVIDER_DOUYIN_ULTRA
from .provider_health_tests import PROVIDER_HEALTH_TESTS
from .provider_settings import get_platform_provider_settings


DOUYIN_ULTRA_NAME = "抖音超清"
SERVICE_LABELS = {"tiantianjiexi": "天天解析", "kukutool": "KuKuTool"}
# A success from any switched-on service decides the row, then a failure, then a skip.
HEALTH_PRECEDENCE = (
    {ProviderHealthTestStatus.SUCCESS},
    {ProviderHealthTestStatus.FAILED, ProviderHealthTestStatus.UNAVAILABLE},
    {ProviderHealthTestStatus.SKIPPED},
)


@dataclass(frozen=True)
class DouyinUltraService:
    name: str
    label: str
    health_key: str
    enabled: bool


@dataclass(frozen=True)
class DouyinUltraSummary:
    services: tuple[DouyinUltraService, ...]
    health_run: ProviderHealthTestRun | None
    health_details: str

    @property
    def provider_names(self):
        return tuple(service.name for service in self.services)

    @property
    def health_keys(self):
        return tuple(service.health_key for service in self.services if service.health_key)


def douyin_ultra_services():
    provider_settings = get_platform_provider_settings("douyin")
    health_keys = {
        item.get("provider"): item["key"]
        for item in PROVIDER_HEALTH_TESTS
        if item.get("provider_access_id") == PROVIDER_DOUYIN_ULTRA
    }
    return tuple(
        DouyinUltraService(
            name=name,
            label=SERVICE_LABELS.get(name, name),
            health_key=health_keys.get(name, ""),
            enabled=provider_settings.provider_is_enabled(name),
        )
        for name in configured_douyin_services(provider_settings.provider_order)
    )


def douyin_ultra_summary():
    # A1: the admin rows follow the configured services. As in
    # douyin_ultra_service_is_usable(), the row is healthy while any
    # switched-on service is usable; switched-off ones only show in the details.
    services = douyin_ultra_services()
    runs = []
    details = []
    for service in services:
        run = None
        if service.enabled and service.health_key:
            run = (
                ProviderHealthTestRun.objects.filter(test_key=service.health_key)
                .order_by("-created_at")
                .first()
            )
        if run is not None:
            runs.append(run)
            details.append(f"{service.label}:{run.status}")
        else:
            details.append(f"{service.label}:{'暂无记录' if service.enabled else '已关闭'}")
    health_run = None
    for statuses in HEALTH_PRECEDENCE:
        matching = [run for run in runs if run.status in statuses]
        if matching:
            health_run = max(matching, key=lambda run: run.created_at)
            break
    return DouyinUltraSummary(
        services=services,
        health_run=health_run,
        health_details="；".join(details),
    )
