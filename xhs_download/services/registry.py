from .common import XhsParseError, extract_url_from_text
from .platforms import (
    BilibiliAdapter,
    DouyinAdapter,
    GenericThirdPartyAdapter,
    XiaohongshuAdapter,
)


DEFAULT_ADAPTERS = (
    XiaohongshuAdapter(),
    DouyinAdapter(),
    BilibiliAdapter(),
    GenericThirdPartyAdapter(),
)


class PlatformRegistry:
    def __init__(self, adapters=None):
        self.adapters = tuple(adapters or DEFAULT_ADAPTERS)

    def list_platforms(self):
        return tuple(adapter.describe() for adapter in self.adapters)

    def get_adapter_for_platform(self, platform_id):
        platform_id = (platform_id or "").strip().lower()
        for adapter in self.adapters:
            if adapter.supports_platform(platform_id):
                return adapter
        raise XhsParseError("unsupported platform")

    def get_adapter_for_url(self, url):
        url = extract_url_from_text(url)
        for adapter in self.adapters:
            if adapter.supports_url(url):
                return adapter
        raise XhsParseError("暂不支持该平台链接")

    def parse_url(
        self,
        url,
        prefer_ultra_hd=False,
        client_country_code="",
        prefer_youtube_merge=False,
        douyin_provider_order=None,
        timeout_seconds=None,
        deadline_monotonic=None,
        stage_callback=None,
        allow_continuation=False,
        continuation_identity="",
        ultra_attempt_controller=None,
        enable_xhs_livephoto_preview=False,
        enable_xhs_livephoto_staff_preview=False,
    ):
        url = extract_url_from_text(url)
        adapter = self.get_adapter_for_url(url)
        return adapter.parse(
            url,
            prefer_ultra_hd=prefer_ultra_hd,
            client_country_code=client_country_code,
            prefer_youtube_merge=prefer_youtube_merge,
            douyin_provider_order=douyin_provider_order,
            timeout_seconds=timeout_seconds,
            deadline_monotonic=deadline_monotonic,
            stage_callback=stage_callback,
            allow_continuation=allow_continuation,
            continuation_identity=continuation_identity,
            ultra_attempt_controller=ultra_attempt_controller,
            enable_xhs_livephoto_preview=enable_xhs_livephoto_preview,
            enable_xhs_livephoto_staff_preview=enable_xhs_livephoto_staff_preview,
        )


default_registry = PlatformRegistry()


def get_adapter_for_url(url):
    return default_registry.get_adapter_for_url(url)


def get_adapter_for_platform(platform_id):
    return default_registry.get_adapter_for_platform(platform_id)


def list_platforms():
    return default_registry.list_platforms()


def parse_media_page(
    url,
    prefer_ultra_hd=False,
    client_country_code="",
    prefer_youtube_merge=False,
    douyin_provider_order=None,
    timeout_seconds=None,
    deadline_monotonic=None,
    stage_callback=None,
    allow_continuation=False,
    continuation_identity="",
    ultra_attempt_controller=None,
    enable_xhs_livephoto_preview=False,
    enable_xhs_livephoto_staff_preview=False,
):
    return default_registry.parse_url(
        url,
        prefer_ultra_hd=prefer_ultra_hd,
        client_country_code=client_country_code,
        prefer_youtube_merge=prefer_youtube_merge,
        douyin_provider_order=douyin_provider_order,
        timeout_seconds=timeout_seconds,
        deadline_monotonic=deadline_monotonic,
        stage_callback=stage_callback,
        allow_continuation=allow_continuation,
        continuation_identity=continuation_identity,
        ultra_attempt_controller=ultra_attempt_controller,
        enable_xhs_livephoto_preview=enable_xhs_livephoto_preview,
        enable_xhs_livephoto_staff_preview=enable_xhs_livephoto_staff_preview,
    )
