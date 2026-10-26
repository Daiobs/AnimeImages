from .common import XhsParseError, extract_url_from_text
from .pipeline_shadow import observe_result
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
        upstream_provider_ids=None,
        upstream_matches=None,
        xhs_service_provider_ids=(),
        bilibili_service_authorizer=None,
        probe_media_metadata=True,
    ):
        url = extract_url_from_text(url)
        adapter = self.get_adapter_for_url(url)
        if upstream_matches is not None and "upstream_matches" not in adapter.parse_option_names:
            raise XhsParseError("上游解析权限接口暂不可用。")
        options = dict(
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
            upstream_provider_ids=upstream_provider_ids,
            upstream_matches=upstream_matches,
        )
        if not probe_media_metadata:
            options["probe_media_metadata"] = False
        if adapter.platform_id == "xiaohongshu" and xhs_service_provider_ids:
            from .platforms.xhs_independent import parse_independent

            def native_parse(deadline):
                native_options = {**options, "deadline_monotonic": deadline}
                # The independent service branch already owns the TianTian attempt.
                if "tiantianjiexi_generic" in xhs_service_provider_ids:
                    native_options["prefer_ultra_hd"] = False
                return adapter.parse(url, **native_options)

            return parse_independent(
                url, native_parse=native_parse, provider_ids=xhs_service_provider_ids,
                timeout_seconds=timeout_seconds, deadline_monotonic=deadline_monotonic,
            )
        if adapter.platform_id == "bilibili" and bilibili_service_authorizer is not None:
            from .platforms.bilibili_fallback import parse_fallback

            return parse_fallback(
                url,
                native_parse=lambda deadline: adapter.parse(
                    url, **{**options, "deadline_monotonic": deadline}),
                authorize=bilibili_service_authorizer,
                timeout_seconds=timeout_seconds, deadline_monotonic=deadline_monotonic,
                **({"probe_media_metadata": False} if not probe_media_metadata else {}),
            )
        return adapter.parse(url, **options)


default_registry = PlatformRegistry()


def get_adapter_for_url(url):
    return default_registry.get_adapter_for_url(url)


def get_adapter_for_platform(platform_id):
    return default_registry.get_adapter_for_platform(platform_id)


def list_platforms():
    return default_registry.list_platforms()


@observe_result
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
    upstream_provider_ids=None,
    upstream_matches=None,
    xhs_service_provider_ids=(),
    bilibili_service_authorizer=None,
    probe_media_metadata=True,
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
        upstream_provider_ids=upstream_provider_ids,
        upstream_matches=upstream_matches,
        xhs_service_provider_ids=xhs_service_provider_ids,
        bilibili_service_authorizer=bilibili_service_authorizer,
        **({"probe_media_metadata": False} if not probe_media_metadata else {}),
    )
