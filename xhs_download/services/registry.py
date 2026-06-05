from .common import XhsParseError
from .platforms import XiaohongshuAdapter


DEFAULT_ADAPTERS = (
    XiaohongshuAdapter(),
)


class PlatformRegistry:
    def __init__(self, adapters=None):
        self.adapters = tuple(adapters or DEFAULT_ADAPTERS)

    def get_adapter_for_url(self, url):
        for adapter in self.adapters:
            if adapter.supports_url(url):
                return adapter
        raise XhsParseError("暂不支持该平台链接")

    def parse_url(self, url):
        adapter = self.get_adapter_for_url(url)
        return adapter.parse_url(url)


default_registry = PlatformRegistry()


def get_adapter_for_url(url):
    return default_registry.get_adapter_for_url(url)


def parse_media_page(url):
    return default_registry.parse_url(url)
