from dataclasses import dataclass

from django.conf import settings


@dataclass(frozen=True)
class ProviderSettings:
    ultra_hd_enabled: bool
    provider_order: tuple[str, ...]
    kukutool_enabled: bool = True
    kukutool_node_binary: str = ""
    kukutool_timeout_seconds: float = 12.0
    kukutool_probe_timeout_seconds: float = 2.0
    kukutool_max_probe_candidates: int = 2
    kukutool_max_concurrency: int = 2
    kukutool_executor_workers: int = 2
    kukutool_max_pending_tasks: int = 4
    kukutool_global_max_concurrency: int = 4
    kukutool_single_flight_ttl_seconds: int = 30
    kukutool_failure_cache_ttl_seconds: int = 15
    kukutool_cache_poll_interval_seconds: float = 0.1
    kukutool_profile_cache_ttl_seconds: int = 86400
    kukutool_validated_cache_ttl_seconds: int = 300
    kukutool_reference_wait_seconds: float = 0.5
    native_executor_workers: int = 4
    native_max_pending_tasks: int = 8
    tikhub_api_key: str = ""
    tikhub_base_url: str = ""
    tikhub_china_base_url: str = "https://api.tikhub.dev"
    tikhub_region: str = "CN"
    tikhub_daily_limit: int = 0
    ultra_total_timeout_seconds: float = 3.0
    ultra_native_timeout_seconds: float = 3.0
    tikhub_connect_timeout_seconds: float = 1.0
    tikhub_read_timeout_seconds: float = 6.5
    tikhub_max_attempts: int = 2
    tikhub_probe_timeout_seconds: float = 1.8
    tikhub_max_probe_candidates: int = 1
    tikhub_validated_cache_ttl_seconds: int = 600

    def provider_is_enabled(self, name):
        name = (name or "").strip().lower()
        if name == "native":
            return True
        if name == "native_enhanced":
            return self.ultra_hd_enabled
        if name == "kukutool":
            return self.ultra_hd_enabled and self.kukutool_enabled
        if name == "tikhub":
            return self.ultra_hd_enabled and bool(self.tikhub_api_key)
        return self.ultra_hd_enabled

    @property
    def enabled_provider_order(self):
        return tuple(name for name in self.provider_order if self.provider_is_enabled(name))


def normalize_provider_name(name):
    return (name or "").strip().lower().replace("-", "_")


def normalize_provider_order(value, default=("native",)):
    if isinstance(value, str):
        items = value.split(",")
    else:
        items = value or default

    order = []
    seen = set()
    for item in items:
        name = normalize_provider_name(item)
        if not name or name in seen:
            continue
        seen.add(name)
        order.append(name)

    return tuple(order or default)


def get_platform_provider_settings(platform_id):
    platform_id = normalize_provider_name(platform_id)
    if platform_id == "douyin":
        raw_order = ("kukutool", "native")
    elif platform_id in {"xiaohongshu", "xhs"}:
        raw_order = getattr(settings, "XHS_PROVIDER_ORDER", ("native",))
    else:
        raw_order = ("native",)

    return ProviderSettings(
        ultra_hd_enabled=bool(getattr(settings, "ULTRA_HD_PROVIDER_ENABLED", False)),
        provider_order=normalize_provider_order(raw_order),
        kukutool_enabled=bool(getattr(settings, "KUKUTOOL_PROVIDER_ENABLED", True)),
        kukutool_node_binary=getattr(settings, "KUKUTOOL_NODE_BINARY", "") or "",
        kukutool_timeout_seconds=float(getattr(settings, "KUKUTOOL_TIMEOUT_SECONDS", 12.0) or 0),
        kukutool_probe_timeout_seconds=float(
            getattr(settings, "KUKUTOOL_PROBE_TIMEOUT_SECONDS", 2.0) or 0
        ),
        kukutool_max_probe_candidates=max(
            1,
            int(getattr(settings, "KUKUTOOL_MAX_PROBE_CANDIDATES", 2) or 1),
        ),
        kukutool_max_concurrency=max(
            1,
            int(getattr(settings, "KUKUTOOL_MAX_CONCURRENCY", 2) or 1),
        ),
        kukutool_executor_workers=max(
            1,
            int(getattr(settings, "KUKUTOOL_EXECUTOR_WORKERS", 2) or 1),
        ),
        kukutool_max_pending_tasks=max(
            1,
            int(getattr(settings, "KUKUTOOL_MAX_PENDING_TASKS", 4) or 1),
        ),
        kukutool_global_max_concurrency=max(
            0,
            int(getattr(settings, "KUKUTOOL_GLOBAL_MAX_CONCURRENCY", 4) or 0),
        ),
        kukutool_single_flight_ttl_seconds=max(
            1,
            int(getattr(settings, "KUKUTOOL_SINGLE_FLIGHT_TTL_SECONDS", 30) or 1),
        ),
        kukutool_failure_cache_ttl_seconds=max(
            0,
            int(getattr(settings, "KUKUTOOL_FAILURE_CACHE_TTL_SECONDS", 15) or 0),
        ),
        kukutool_cache_poll_interval_seconds=max(
            0.02,
            float(
                getattr(settings, "KUKUTOOL_CACHE_POLL_INTERVAL_SECONDS", 0.1)
                or 0.1
            ),
        ),
        kukutool_profile_cache_ttl_seconds=max(
            0,
            int(getattr(settings, "KUKUTOOL_PROFILE_CACHE_TTL_SECONDS", 86400) or 0),
        ),
        kukutool_validated_cache_ttl_seconds=int(
            getattr(settings, "KUKUTOOL_VALIDATED_CACHE_TTL_SECONDS", 300) or 0
        ),
        kukutool_reference_wait_seconds=float(
            getattr(settings, "KUKUTOOL_REFERENCE_WAIT_SECONDS", 0.5) or 0
        ),
        native_executor_workers=max(
            1,
            int(getattr(settings, "DOUYIN_NATIVE_EXECUTOR_WORKERS", 4) or 1),
        ),
        native_max_pending_tasks=max(
            1,
            int(getattr(settings, "DOUYIN_NATIVE_MAX_PENDING_TASKS", 8) or 1),
        ),
        tikhub_api_key=getattr(settings, "TIKHUB_API_KEY", "") or "",
        tikhub_base_url=getattr(settings, "TIKHUB_BASE_URL", "") or "",
        tikhub_china_base_url=(
            getattr(
                settings,
                "TIKHUB_CHINA_BASE_URL",
                "https://api.tikhub.dev",
            )
            or "https://api.tikhub.dev"
        ),
        tikhub_region=getattr(settings, "TIKHUB_REGION", "CN") or "CN",
        tikhub_daily_limit=int(getattr(settings, "TIKHUB_DAILY_LIMIT", 0) or 0),
        ultra_total_timeout_seconds=float(getattr(settings, "DOUYIN_ULTRA_TOTAL_TIMEOUT_SECONDS", 3.0) or 0),
        ultra_native_timeout_seconds=float(getattr(settings, "DOUYIN_ULTRA_NATIVE_TIMEOUT_SECONDS", 3.0) or 0),
        tikhub_connect_timeout_seconds=float(getattr(settings, "TIKHUB_CONNECT_TIMEOUT_SECONDS", 1.0) or 0),
        tikhub_read_timeout_seconds=float(getattr(settings, "TIKHUB_READ_TIMEOUT_SECONDS", 6.5) or 0),
        tikhub_max_attempts=max(1, int(getattr(settings, "TIKHUB_MAX_ATTEMPTS", 2) or 1)),
        tikhub_probe_timeout_seconds=float(getattr(settings, "TIKHUB_PROBE_TIMEOUT_SECONDS", 1.8) or 0),
        tikhub_max_probe_candidates=max(1, int(getattr(settings, "TIKHUB_MAX_PROBE_CANDIDATES", 1) or 1)),
        tikhub_validated_cache_ttl_seconds=int(
            getattr(settings, "TIKHUB_VALIDATED_CACHE_TTL_SECONDS", 600) or 0
        ),
    )
