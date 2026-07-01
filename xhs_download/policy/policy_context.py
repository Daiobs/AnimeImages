from dataclasses import dataclass, field

from download_console.models import DownloadRuntimeSetting

from ..services.download_routing import (
    estimate_media_items_bytes,
    item_provider,
    item_type,
    request_is_mobile,
    request_is_staff,
    safe_download_size,
)


@dataclass(frozen=True)
class PolicyContext:
    request: object
    media_items: tuple[dict, ...] = ()
    platform: str = ""
    user_type: str = "visitor"
    is_mobile: bool = False
    provider_hint: str = ""
    runtime_settings: object | None = None
    cost_limits: dict = field(default_factory=dict)
    requested_mode: str = ""
    routing_decision: dict = field(default_factory=dict)

    @property
    def item_count(self):
        return len(self.media_items)

    @property
    def image_count(self):
        return sum(1 for item in self.media_items if item_type(item) == "image")

    @property
    def video_count(self):
        return sum(1 for item in self.media_items if item_type(item) == "video")

    @property
    def estimated_bytes(self):
        return estimate_media_items_bytes(self.media_items)


def infer_policy_provider(media_items, provider_hint=""):
    if provider_hint:
        return str(provider_hint)
    for item in media_items or ():
        provider = item_provider(item)
        if provider:
            return provider
        platform = str(item.get("source_platform") or item.get("platform") or "")
        if platform:
            return platform
    return "native"


def build_cost_limits(runtime_settings):
    if runtime_settings is None:
        return {}
    return {
        "single_video_bytes_limit": safe_download_size(
            getattr(runtime_settings, "single_video_bytes_limit", 0)
        ),
        "single_archive_bytes_limit": safe_download_size(
            getattr(runtime_settings, "single_archive_bytes_limit", 0)
        ),
        "visitor_video_download_enabled": bool(
            getattr(runtime_settings, "visitor_video_download_enabled", True)
        ),
        "staff_large_download_exempt": bool(
            getattr(runtime_settings, "staff_large_download_exempt", False)
        ),
    }


def build_policy_context(
    request,
    media_items,
    *,
    platform="",
    provider_hint="",
    runtime_settings=None,
    requested_mode="",
    routing_decision=None,
):
    normalized_items = tuple(item for item in media_items or () if isinstance(item, dict))
    runtime_settings = runtime_settings or DownloadRuntimeSetting.load()
    return PolicyContext(
        request=request,
        media_items=normalized_items,
        platform=str(platform or ""),
        user_type="staff" if request_is_staff(request) else "visitor",
        is_mobile=request_is_mobile(request),
        provider_hint=infer_policy_provider(normalized_items, provider_hint),
        runtime_settings=runtime_settings,
        cost_limits=build_cost_limits(runtime_settings),
        requested_mode=str(requested_mode or ""),
        routing_decision=dict(routing_decision or {}),
    )
