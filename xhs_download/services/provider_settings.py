from dataclasses import dataclass

from django.conf import settings


@dataclass(frozen=True)
class ProviderSettings:
    ultra_hd_enabled: bool
    provider_order: tuple[str, ...]
    tikhub_api_key: str = ""
    tikhub_base_url: str = ""
    tikhub_region: str = "CN"
    tikhub_cache_ttl_seconds: int = 1800
    tikhub_daily_limit: int = 0

    def provider_is_enabled(self, name):
        name = (name or "").strip().lower()
        if name == "native":
            return True
        if name == "native_enhanced":
            return self.ultra_hd_enabled
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
        raw_order = getattr(settings, "DOUYIN_PROVIDER_ORDER", ("native",))
    elif platform_id in {"xiaohongshu", "xhs"}:
        raw_order = getattr(settings, "XHS_PROVIDER_ORDER", ("native",))
    else:
        raw_order = ("native",)

    return ProviderSettings(
        ultra_hd_enabled=bool(getattr(settings, "ULTRA_HD_PROVIDER_ENABLED", False)),
        provider_order=normalize_provider_order(raw_order),
        tikhub_api_key=getattr(settings, "TIKHUB_API_KEY", "") or "",
        tikhub_base_url=getattr(settings, "TIKHUB_BASE_URL", "") or "",
        tikhub_region=getattr(settings, "TIKHUB_REGION", "CN") or "CN",
        tikhub_cache_ttl_seconds=int(getattr(settings, "TIKHUB_CACHE_TTL_SECONDS", 1800) or 0),
        tikhub_daily_limit=int(getattr(settings, "TIKHUB_DAILY_LIMIT", 0) or 0),
    )
