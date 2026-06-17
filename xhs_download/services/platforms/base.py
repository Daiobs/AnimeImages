from dataclasses import dataclass, field
from urllib.parse import urlparse

from ..common import XhsParseError, host_matches, is_blocked_hostname


@dataclass(frozen=True)
class PlatformCapabilities:
    media_types: tuple[str, ...] = field(default_factory=tuple)
    batch_download: bool = True
    cover: bool = False
    ultra_hd: bool = False

    def to_dict(self):
        return {
            "media_types": self.media_types,
            "batch_download": self.batch_download,
            "cover": self.cover,
            "ultra_hd": self.ultra_hd,
        }


@dataclass(frozen=True)
class ProviderPolicy:
    primary_provider: str = "native"
    fallback_providers: tuple[str, ...] = field(default_factory=tuple)
    third_party_providers: tuple[str, ...] = field(default_factory=tuple)
    enhanced_provider_enabled_by: str = ""

    @property
    def provider_ids(self):
        providers = [self.primary_provider]
        providers.extend(self.fallback_providers)
        providers.extend(self.third_party_providers)
        return tuple(provider for provider in providers if provider)

    def to_dict(self):
        return {
            "primary_provider": self.primary_provider,
            "fallback_providers": self.fallback_providers,
            "third_party_providers": self.third_party_providers,
            "enhanced_provider_enabled_by": self.enhanced_provider_enabled_by,
            "provider_ids": self.provider_ids,
        }


class BasePlatformAdapter:
    platform_id = ""
    display_name = ""
    page_host_suffixes = ()
    media_host_suffixes = ()
    headers = {}
    capabilities = PlatformCapabilities()
    provider_policy = ProviderPolicy()
    parse_option_names = ()

    def supports_url(self, url):
        parsed = urlparse(url or "")
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return False
        if is_blocked_hostname(parsed.hostname):
            return False
        return host_matches(parsed.hostname, self.page_host_suffixes)

    def supports_platform(self, platform_id):
        return self.platform_id == (platform_id or "").strip().lower()

    def parse_url(self, url):
        raise NotImplementedError

    def parse(self, url, **options):
        supported_options = {
            name: value
            for name, value in options.items()
            if name in self.parse_option_names
        }
        return self.parse_url(url, **supported_options)

    def parse_html(self, html, source_url=None):
        raise NotImplementedError

    def describe(self):
        return {
            "platform_id": self.platform_id,
            "display_name": self.display_name,
            "page_host_suffixes": self.page_host_suffixes,
            "media_host_suffixes": self.media_host_suffixes,
            "capabilities": self.capabilities.to_dict(),
            "provider_policy": self.provider_policy.to_dict(),
            "parse_option_names": self.parse_option_names,
        }

    def require_supported_url(self, url):
        if not self.supports_url(url):
            raise XhsParseError(f"请输入正确的{self.display_name or self.platform_id}链接")
