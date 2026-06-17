from dataclasses import dataclass
from urllib.parse import urlparse

from django.conf import settings

from ..common import (
    XhsParseError,
    XhsProviderTimeoutError,
    host_matches,
    is_blocked_hostname,
)
from .base import BasePlatformAdapter, PlatformCapabilities, ProviderPolicy
from .kukutool_generic import KukutoolGenericProvider, KukutoolResultNormalizer


@dataclass(frozen=True)
class ThirdPartyPlatformRule:
    platform_id: str
    display_name: str
    host_suffixes: tuple[str, ...]
    media_types: tuple[str, ...] = ("image", "video")
    provider_ids: tuple[str, ...] = ("kukutool_generic",)
    media_host_suffixes: tuple[str, ...] = ()

    def supports_hostname(self, hostname):
        return host_matches(hostname, self.host_suffixes)

    def to_dict(self):
        return {
            "platform_id": self.platform_id,
            "display_name": self.display_name,
            "host_suffixes": self.host_suffixes,
            "media_types": self.media_types,
            "provider_ids": self.provider_ids,
            "media_host_suffixes": self.media_host_suffixes,
        }


THIRD_PARTY_PLATFORM_RULES = (
    ThirdPartyPlatformRule(
        platform_id="tiktok",
        display_name="TikTok",
        host_suffixes=("tiktok.com", "tiktokv.com", "vm.tiktok.com", "vt.tiktok.com"),
        media_host_suffixes=(
            "tiktokcdn.com",
            "tiktokcdn-us.com",
            "tiktokv.com",
            "byteoversea.com",
            "muscdn.com",
        ),
    ),
    ThirdPartyPlatformRule(
        platform_id="kuaishou",
        display_name="快手",
        host_suffixes=("kuaishou.com", "kuaishouapp.com", "kwai.com"),
        media_host_suffixes=("kwimgs.com", "yximgs.com"),
    ),
    ThirdPartyPlatformRule(
        platform_id="instagram",
        display_name="Instagram",
        host_suffixes=("instagram.com",),
        media_host_suffixes=("cdninstagram.com", "fbcdn.net"),
    ),
    ThirdPartyPlatformRule(
        platform_id="facebook",
        display_name="Facebook",
        host_suffixes=("facebook.com", "fb.watch"),
        media_host_suffixes=("fbcdn.net",),
    ),
    ThirdPartyPlatformRule(
        platform_id="youtube",
        display_name="YouTube",
        host_suffixes=("youtube.com", "youtu.be"),
        media_host_suffixes=("googlevideo.com", "ytimg.com"),
    ),
    ThirdPartyPlatformRule(
        platform_id="x",
        display_name="X / Twitter",
        host_suffixes=("x.com", "twitter.com"),
        media_host_suffixes=("twimg.com",),
    ),
)


def get_third_party_rule(platform_id):
    platform_id = (platform_id or "").strip().lower()
    for rule in THIRD_PARTY_PLATFORM_RULES:
        if rule.platform_id == platform_id:
            return rule
    return None


def parse_third_party_asset_id(asset_id):
    parts = str(asset_id or "").split(":")
    if len(parts) < 5 or parts[0] != "third_party":
        return None
    platform_id = parts[1]
    source_key = parts[2]
    media_type = parts[3]
    index = parts[4]
    if not get_third_party_rule(platform_id):
        return None
    if not source_key or len(source_key) > 64:
        return None
    if media_type not in {"image", "video"}:
        return None
    if not index.isdigit():
        return None
    return {
        "platform_id": platform_id,
        "source_key": source_key,
        "media_type": media_type,
        "index": index,
    }


def third_party_media_url_is_allowed(platform_id, url):
    rule = get_third_party_rule(platform_id)
    if not rule:
        return False
    parsed = urlparse(url or "")
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    if is_blocked_hostname(parsed.hostname):
        return False
    return host_matches(parsed.hostname, rule.media_host_suffixes)


def third_party_manifest_item_is_allowed(item):
    if not isinstance(item, dict):
        return False
    parsed_asset = parse_third_party_asset_id(item.get("asset_id"))
    if not parsed_asset:
        return False
    if item.get("provider_resource_id") != parsed_asset["source_key"]:
        return False
    expected_cache_key = f"third_party:kukutool_generic:{parsed_asset['source_key']}"
    if item.get("provider_cache_key") != expected_cache_key:
        return False
    return third_party_media_url_is_allowed(parsed_asset["platform_id"], item.get("url"))


class GenericThirdPartyAdapter(BasePlatformAdapter):
    platform_id = "third_party"
    display_name = "第三方通用平台"
    page_host_suffixes = tuple(
        suffix
        for rule in THIRD_PARTY_PLATFORM_RULES
        for suffix in rule.host_suffixes
    )
    capabilities = PlatformCapabilities(
        media_types=("image", "video"),
        batch_download=True,
        cover=True,
    )
    provider_policy = ProviderPolicy(
        primary_provider="third_party",
        third_party_providers=("kukutool_generic",),
    )
    platform_rules = THIRD_PARTY_PLATFORM_RULES

    def __init__(self, runner=None, provider=None, normalizer=None):
        self.provider = provider or KukutoolGenericProvider(runner=runner)
        self.normalizer = normalizer or KukutoolResultNormalizer(
            platform_id=self.platform_id
        )

    def get_rule_for_url(self, url):
        parsed = urlparse(url or "")
        hostname = parsed.hostname or ""
        if parsed.scheme not in {"http", "https"} or not hostname:
            return None
        if is_blocked_hostname(hostname):
            return None
        for rule in self.platform_rules:
            if rule.supports_hostname(hostname):
                return rule
        return None

    def supports_url(self, url):
        return self.get_rule_for_url(url) is not None

    def supports_platform(self, platform_id):
        platform_id = (platform_id or "").strip().lower()
        return platform_id == self.platform_id or any(
            rule.platform_id == platform_id for rule in self.platform_rules
        )

    def parse_url(self, url):
        self.require_supported_url(url)
        rule = self.get_rule_for_url(url)
        platform_name = rule.display_name if rule else self.display_name
        if not getattr(settings, "THIRD_PARTY_PROVIDER_ENABLED", False):
            raise XhsParseError("第三方通用解析 Provider 当前未启用。")
        if not getattr(settings, "THIRD_PARTY_PUBLIC_ACCESS_ENABLED", False):
            raise XhsParseError("第三方通用解析暂未开放。")
        try:
            payload = self.provider.parse(url)
        except XhsProviderTimeoutError:
            raise
        except Exception as error:
            raise XhsParseError(f"{platform_name} 第三方解析失败：{error}") from error

        try:
            return self.normalizer.normalize(
                rule=rule,
                platform_name=platform_name,
                source_url=url,
                payload=payload,
            )
        except Exception as error:
            raise XhsParseError(f"{platform_name} 第三方解析结果异常：{error}") from error

    def describe(self):
        description = super().describe()
        description["platform_rules"] = tuple(
            rule.to_dict() for rule in self.platform_rules
        )
        return description
