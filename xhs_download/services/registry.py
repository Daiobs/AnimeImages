import re
from urllib.parse import urlparse

from .common import XhsParseError
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


URL_PATTERN = re.compile(r"https?://[^\s\"'<>]+")
TRAILING_URL_CHARS = "，,。；;！!？?）)]】》"


def extract_url_from_text(value):
    value = (value or "").strip()
    match = URL_PATTERN.search(value)
    candidate = match.group(0).rstrip(TRAILING_URL_CHARS) if match else value
    parsed = urlparse(candidate)
    if parsed.scheme in {"http", "https"} and parsed.hostname:
        return candidate
    return value


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
    ):
        url = extract_url_from_text(url)
        adapter = self.get_adapter_for_url(url)
        return adapter.parse(
            url,
            prefer_ultra_hd=prefer_ultra_hd,
            client_country_code=client_country_code,
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
):
    return default_registry.parse_url(
        url,
        prefer_ultra_hd=prefer_ultra_hd,
        client_country_code=client_country_code,
    )
