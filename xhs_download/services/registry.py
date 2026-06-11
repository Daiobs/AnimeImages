import re
from urllib.parse import urlparse

from .common import XhsParseError
from .platforms import BilibiliAdapter, DouyinAdapter, XiaohongshuAdapter


DEFAULT_ADAPTERS = (
    XiaohongshuAdapter(),
    DouyinAdapter(),
    BilibiliAdapter(),
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
        if getattr(adapter, "platform_id", "") == "douyin":
            return adapter.parse_url(
                url,
                prefer_ultra_hd=prefer_ultra_hd,
                client_country_code=client_country_code,
            )
        return adapter.parse_url(url)


default_registry = PlatformRegistry()


def get_adapter_for_url(url):
    return default_registry.get_adapter_for_url(url)


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
