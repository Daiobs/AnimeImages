import logging
import time
from dataclasses import dataclass
from urllib.parse import urlparse

from django.conf import settings

from ..assets import ParseResult
from ..common import (
    XhsParseError,
    XhsProviderTimeoutError,
    extract_url_from_text,
    host_matches,
    is_blocked_hostname,
)
from .base import BasePlatformAdapter, PlatformCapabilities, ProviderPolicy
from .gallerydl_generic import GalleryDlGenericProvider, GalleryDlResultNormalizer
from .kukutool_generic import KukutoolGenericProvider, KukutoolResultNormalizer
from .ytdlp_generic import YtDlpGenericProvider, YtDlpResultNormalizer
from .ytdlp_merge import (
    PROVIDER_ID as YTDLP_MERGE_PROVIDER_ID,
    build_parse_result as build_ytdlp_merge_parse_result,
    default_max_height as ytdlp_merge_default_max_height,
)
from ..youtube_merge import (
    PROVIDER_ID as YOUTUBE_MERGE_PROVIDER_ID,
    build_youtube_merge_parse_result,
    get_youtube_merge_health,
)


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ThirdPartyPlatformRule:
    platform_id: str
    display_name: str
    host_suffixes: tuple[str, ...]
    media_types: tuple[str, ...] = ("image", "video")
    provider_ids: tuple[str, ...] = (
        "kukutool_generic",
        "yt_dlp_generic",
        "gallery_dl_generic",
    )
    media_host_suffixes: tuple[str, ...] = ()
    image_media_host_suffixes: tuple[str, ...] = ()
    video_media_host_suffixes: tuple[str, ...] = ()

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
            "image_media_host_suffixes": self.image_media_host_suffixes,
            "video_media_host_suffixes": self.video_media_host_suffixes,
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
        media_types=("video",),
        provider_ids=("yt_dlp_generic", "kukutool_generic"),
        media_host_suffixes=("googlevideo.com", "ytimg.com"),
        image_media_host_suffixes=("ytimg.com",),
        video_media_host_suffixes=("googlevideo.com",),
    ),
    ThirdPartyPlatformRule(
        platform_id="x",
        display_name="X / Twitter",
        host_suffixes=("x.com", "twitter.com"),
        media_host_suffixes=("twimg.com",),
    ),
    ThirdPartyPlatformRule(
        platform_id="imgur",
        display_name="Imgur",
        host_suffixes=("imgur.com",),
        media_types=("image", "video"),
        provider_ids=("kukutool_generic", "gallery_dl_generic"),
        media_host_suffixes=("imgur.com",),
    ),
    ThirdPartyPlatformRule(
        platform_id="reddit",
        display_name="Reddit",
        host_suffixes=("reddit.com", "redd.it"),
        media_types=("image",),
        provider_ids=("kukutool_generic", "gallery_dl_generic"),
        media_host_suffixes=("redd.it", "redditmedia.com"),
    ),
    ThirdPartyPlatformRule(
        platform_id="tumblr",
        display_name="Tumblr",
        host_suffixes=("tumblr.com",),
        media_types=("image",),
        provider_ids=("kukutool_generic", "gallery_dl_generic"),
        media_host_suffixes=("tumblr.com",),
    ),
    ThirdPartyPlatformRule(
        platform_id="flickr",
        display_name="Flickr",
        host_suffixes=("flickr.com",),
        media_types=("image",),
        provider_ids=("kukutool_generic", "gallery_dl_generic"),
        media_host_suffixes=("staticflickr.com",),
    ),
    ThirdPartyPlatformRule(
        platform_id="deviantart",
        display_name="DeviantArt",
        host_suffixes=("deviantart.com",),
        media_types=("image",),
        provider_ids=("kukutool_generic", "gallery_dl_generic"),
        media_host_suffixes=("wixmp.com", "deviantart.net"),
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
    extra_parts = parts[5:]
    if extra_parts and extra_parts != ["cover"]:
        return None
    platform_id = parts[1]
    source_key = parts[2]
    media_type = parts[3]
    index = parts[4]
    rule = get_third_party_rule(platform_id)
    if not rule:
        return None
    if not source_key or len(source_key) > 64:
        return None
    if media_type not in {"image", "video"} or media_type not in rule.media_types:
        return None
    if not index.isdigit():
        return None
    return {
        "platform_id": platform_id,
        "source_key": source_key,
        "media_type": media_type,
        "index": index,
        "is_cover": bool(extra_parts),
    }


def third_party_media_url_is_allowed(
    platform_id,
    url,
    *,
    media_type="",
    is_cover=False,
):
    rule = get_third_party_rule(platform_id)
    if not rule:
        return False
    parsed = urlparse(url or "")
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    if is_blocked_hostname(parsed.hostname):
        return False
    media_type = str(media_type or "").strip().lower()
    suffixes = rule.media_host_suffixes
    if is_cover or media_type == "image":
        suffixes = rule.image_media_host_suffixes or suffixes
    elif media_type == "video":
        suffixes = rule.video_media_host_suffixes or suffixes
    return host_matches(parsed.hostname, suffixes)


def third_party_manifest_item_is_allowed(item):
    if not isinstance(item, dict):
        return False
    parsed_asset = parse_third_party_asset_id(item.get("asset_id"))
    if not parsed_asset:
        return False
    item_type = str(item.get("type") or "").strip().lower()
    if item_type:
        if item_type not in {"image", "video"}:
            return False
        if parsed_asset["is_cover"]:
            if item_type != "image":
                return False
        elif item_type != parsed_asset["media_type"]:
            return False
    if item.get("provider_resource_id") != parsed_asset["source_key"]:
        return False
    provider = str(item.get("provider") or "")
    rule = get_third_party_rule(parsed_asset["platform_id"])
    if not rule or provider not in rule.provider_ids:
        return False
    expected_cache_key = f"third_party:{provider}:{parsed_asset['source_key']}"
    if item.get("provider_cache_key") != expected_cache_key:
        return False
    return third_party_media_url_is_allowed(
        parsed_asset["platform_id"],
        item.get("url"),
        media_type="image" if parsed_asset["is_cover"] else parsed_asset["media_type"],
        is_cover=parsed_asset["is_cover"],
    )


def elapsed_ms(start_time):
    return int(round((time.monotonic() - start_time) * 1000))


def provider_attempt_record(provider_id, status, duration_ms, error=None):
    record = {
        "provider": provider_id,
        "status": status,
        "duration_ms": duration_ms,
    }
    if error is not None:
        record["error_type"] = type(error).__name__
        record["error"] = str(error)[:300]
    return record


def frontend_budget_seconds(rule=None):
    base_budget = max(
        0.1,
        float(getattr(settings, "THIRD_PARTY_FRONTEND_TIMEOUT_SECONDS", 3.0) or 3.0),
    )
    if getattr(rule, "platform_id", "") == "youtube":
        return max(
            base_budget,
            float(getattr(settings, "YOUTUBE_FRONTEND_TIMEOUT_SECONDS", 8.0) or 8.0),
        )
    return base_budget


def remaining_budget_seconds(deadline):
    return max(0.0, deadline - time.monotonic())


def provider_deadline_exceeded(deadline, *, provider_id, rule, attempts):
    if time.monotonic() < deadline:
        return False
    logger.info(
        "third_party_provider_budget_exhausted",
        extra={
            "provider": provider_id,
            "platform": getattr(rule, "platform_id", ""),
            "attempt_count": len(attempts),
        },
    )
    return True


def result_with_provider_diagnostics(result, provider_id, duration_ms, attempts):
    metadata = dict(result.metadata or {})
    metadata.update({
        "selected_provider": provider_id,
        "selected_provider_duration_ms": duration_ms,
        "provider_attempts": tuple(attempts),
        "provider_fallback_count": sum(
            1 for attempt in attempts if attempt.get("status") != "success"
        ),
    })
    return ParseResult(
        platform=result.platform,
        post_type=result.post_type,
        base_filename=result.base_filename,
        assets=result.assets,
        metadata=metadata,
    )


def provider_is_enabled(provider_id):
    if provider_id == "kukutool_generic":
        return bool(
            getattr(settings, "THIRD_PARTY_KUKUTOOL_PROVIDER_ENABLED", False)
        )
    if provider_id == "gallery_dl_generic":
        return bool(getattr(settings, "GALLERYDL_PROVIDER_ENABLED", False))
    if provider_id == "yt_dlp_generic":
        return bool(getattr(settings, "YTDLP_PROVIDER_ENABLED", False))
    return False


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
        third_party_providers=(
            "kukutool_generic",
            "gallery_dl_generic",
            "yt_dlp_generic",
            YOUTUBE_MERGE_PROVIDER_ID,
        ),
    )
    platform_rules = THIRD_PARTY_PLATFORM_RULES
    parse_option_names = ("prefer_youtube_merge",)

    def __init__(
        self,
        runner=None,
        provider=None,
        normalizer=None,
        gallerydl_provider=None,
        gallerydl_normalizer=None,
        ytdlp_provider=None,
        ytdlp_normalizer=None,
    ):
        self.provider = provider or KukutoolGenericProvider(runner=runner)
        self.normalizer = normalizer or KukutoolResultNormalizer(
            platform_id=self.platform_id
        )
        self.gallerydl_provider = gallerydl_provider or GalleryDlGenericProvider()
        self.gallerydl_normalizer = gallerydl_normalizer or GalleryDlResultNormalizer(
            platform_id=self.platform_id
        )
        self.ytdlp_provider = ytdlp_provider or YtDlpGenericProvider()
        self.ytdlp_normalizer = ytdlp_normalizer or YtDlpResultNormalizer(
            platform_id=self.platform_id
        )

    def provider_specs(self):
        return {
            "kukutool_generic": (self.provider, self.normalizer),
            "gallery_dl_generic": (
                self.gallerydl_provider,
                self.gallerydl_normalizer,
            ),
            "yt_dlp_generic": (self.ytdlp_provider, self.ytdlp_normalizer),
        }

    def iter_provider_specs(self, rule, *, respect_enabled=True):
        provider_ids = tuple(getattr(rule, "provider_ids", ()) or ())
        allowed_provider_ids = set(self.provider_policy.third_party_providers)
        specs = self.provider_specs()
        for provider_id in provider_ids:
            if provider_id not in allowed_provider_ids:
                continue
            if provider_id not in specs:
                continue
            if respect_enabled and not provider_is_enabled(provider_id):
                continue
            provider, normalizer = specs[provider_id]
            yield provider_id, provider, normalizer

    def normalize_source_url(self, url):
        return extract_url_from_text(url)

    def get_rule_for_url(self, url):
        url = self.normalize_source_url(url)
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

    def parse_url(self, url, prefer_youtube_merge=False):
        source_url = self.normalize_source_url(url)
        self.require_supported_url(source_url)
        rule = self.get_rule_for_url(source_url)
        platform_name = rule.display_name if rule else self.display_name
        if not getattr(settings, "THIRD_PARTY_PROVIDER_ENABLED", False):
            raise XhsParseError("第三方通用解析 Provider 当前未启用。")
        if not getattr(settings, "THIRD_PARTY_PUBLIC_ACCESS_ENABLED", False):
            raise XhsParseError("第三方通用解析暂未开放。")
        providers = list(self.iter_provider_specs(rule, respect_enabled=True))
        failures = []
        attempts = []
        deadline = time.monotonic() + frontend_budget_seconds(rule)
        if prefer_youtube_merge and getattr(rule, "platform_id", "") == "youtube":
            health = get_youtube_merge_health()
            if (
                health.get("ready")
                and health.get("public_access_enabled")
                and health.get("implemented")
            ):
                started_at = time.monotonic()
                provider_timeout_seconds = remaining_budget_seconds(deadline)
                try:
                    payload = self.ytdlp_provider.parse(
                        source_url,
                        timeout_seconds=provider_timeout_seconds,
                    )
                    result = build_youtube_merge_parse_result(source_url, payload)
                    duration_ms = elapsed_ms(started_at)
                    attempts.append(
                        provider_attempt_record(
                            YOUTUBE_MERGE_PROVIDER_ID,
                            "success",
                            duration_ms,
                        )
                    )
                    logger.info(
                        "third_party_provider_selected",
                        extra={
                            "provider": YOUTUBE_MERGE_PROVIDER_ID,
                            "platform": getattr(rule, "platform_id", ""),
                            "duration_ms": duration_ms,
                            "attempt_count": len(attempts),
                            "asset_count": len(result.assets),
                        },
                    )
                    return result_with_provider_diagnostics(
                        result,
                        YOUTUBE_MERGE_PROVIDER_ID,
                        duration_ms,
                        attempts,
                    )
                except Exception as error:
                    duration_ms = elapsed_ms(started_at)
                    status = (
                        "timeout"
                        if isinstance(error, XhsProviderTimeoutError)
                        else "failed"
                    )
                    attempts.append(
                        provider_attempt_record(
                            YOUTUBE_MERGE_PROVIDER_ID,
                            status,
                            duration_ms,
                            error,
                        )
                    )
                    failures.append((YOUTUBE_MERGE_PROVIDER_ID, error))
                    logger.info(
                        "third_party_provider_failed",
                        extra={
                            "provider": YOUTUBE_MERGE_PROVIDER_ID,
                            "platform": getattr(rule, "platform_id", ""),
                            "duration_ms": duration_ms,
                            "error_type": type(error).__name__,
                            "error": str(error)[:300],
                        },
                    )
            else:
                logger.info(
                    "youtube_merge_provider_unavailable",
                    extra={
                        "platform": getattr(rule, "platform_id", ""),
                        "enabled": health.get("enabled"),
                        "public_ready": health.get("public_ready"),
                        "public_access_enabled": health.get("public_access_enabled"),
                        "ready": health.get("ready"),
                        "implemented": health.get("implemented"),
                    },
                )
        if not providers:
            logger.info(
                "third_party_provider_unavailable",
                extra={
                    "platform": getattr(rule, "platform_id", ""),
                    "provider_ids": tuple(getattr(rule, "provider_ids", ()) or ()),
                },
            )
            raise XhsParseError(f"{platform_name} 第三方解析没有启用可用 Provider。")
        for provider_id, provider, normalizer in providers:
            if provider_deadline_exceeded(
                deadline,
                provider_id=provider_id,
                rule=rule,
                attempts=attempts,
            ):
                raise XhsProviderTimeoutError("第三方解析超时，请稍后再试。")
            provider_timeout_seconds = remaining_budget_seconds(deadline)
            started_at = time.monotonic()
            try:
                payload = provider.parse(
                    source_url,
                    timeout_seconds=provider_timeout_seconds,
                )
                selected_provider_id = provider_id
                if (
                    provider_id == "yt_dlp_generic"
                    and getattr(settings, "YTDLP_MERGE_PROVIDER_ENABLED", True)
                ):
                    try:
                        result = build_ytdlp_merge_parse_result(
                            rule,
                            platform_name,
                            source_url,
                            payload,
                            max_height=ytdlp_merge_default_max_height(),
                        )
                        selected_provider_id = YTDLP_MERGE_PROVIDER_ID
                    except Exception:
                        result = normalizer.normalize(
                            rule=rule,
                            platform_name=platform_name,
                            source_url=source_url,
                            payload=payload,
                        )
                else:
                    result = normalizer.normalize(
                        rule=rule,
                        platform_name=platform_name,
                        source_url=source_url,
                        payload=payload,
                    )
                duration_ms = elapsed_ms(started_at)
                attempts.append(
                    provider_attempt_record(
                        selected_provider_id,
                        "success",
                        duration_ms,
                    )
                )
                logger.info(
                    "third_party_provider_selected",
                    extra={
                        "provider": selected_provider_id,
                        "platform": getattr(rule, "platform_id", ""),
                        "duration_ms": duration_ms,
                        "attempt_count": len(attempts),
                        "asset_count": len(result.assets),
                    },
                )
                return result_with_provider_diagnostics(
                    result,
                    selected_provider_id,
                    duration_ms,
                    attempts,
                )
            except XhsProviderTimeoutError as error:
                duration_ms = elapsed_ms(started_at)
                attempts.append(
                    provider_attempt_record(provider_id, "timeout", duration_ms, error)
                )
                failures.append((provider_id, error))
                logger.info(
                    "third_party_provider_failed",
                    extra={
                        "provider": provider_id,
                        "platform": getattr(rule, "platform_id", ""),
                        "duration_ms": duration_ms,
                        "error_type": type(error).__name__,
                        "error": str(error)[:300],
                    },
                )
                if provider_deadline_exceeded(
                    deadline,
                    provider_id=provider_id,
                    rule=rule,
                    attempts=attempts,
                ):
                    raise XhsProviderTimeoutError("第三方解析超时，请稍后再试。") from error
                continue
            except Exception as error:
                duration_ms = elapsed_ms(started_at)
                attempts.append(
                    provider_attempt_record(provider_id, "failed", duration_ms, error)
                )
                failures.append((provider_id, error))
                logger.info(
                    "third_party_provider_failed",
                    extra={
                        "provider": provider_id,
                        "platform": getattr(rule, "platform_id", ""),
                        "duration_ms": duration_ms,
                        "error_type": type(error).__name__,
                        "error": str(error)[:300],
                    },
                )
                if provider_deadline_exceeded(
                    deadline,
                    provider_id=provider_id,
                    rule=rule,
                    attempts=attempts,
                ):
                    raise XhsProviderTimeoutError("第三方解析超时，请稍后再试。") from error
                continue

        if len(failures) == 1 and isinstance(failures[0][1], XhsProviderTimeoutError):
            raise failures[0][1]
        summary = "；".join(
            f"{provider_id}: {type(error).__name__}"
            for provider_id, error in failures
        )
        logger.info(
            "third_party_provider_all_failed",
            extra={
                "platform": getattr(rule, "platform_id", ""),
                "attempt_count": len(attempts),
                "failure_count": len(failures),
                "failed_providers": tuple(provider_id for provider_id, _error in failures),
                "failure_summary": summary[:500],
            },
        )
        raise XhsParseError(f"{platform_name} 第三方解析失败：{summary or '没有可用 Provider'}")

    def describe(self):
        description = super().describe()
        description["platform_rules"] = tuple(
            rule.to_dict() for rule in self.platform_rules
        )
        return description
