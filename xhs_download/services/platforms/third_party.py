import inspect
import logging
import time
from dataclasses import dataclass, replace
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
from ..env_config import runtime_bool, runtime_list, runtime_str
from ..provider_runtime_config import runtime_float
from .base import BasePlatformAdapter, PlatformCapabilities, ProviderPolicy
from .gallerydl_generic import GalleryDlGenericProvider, GalleryDlResultNormalizer
from .kukutool_generic import KukutoolGenericProvider, KukutoolResultNormalizer
from .tiantianjiexi_generic import (
    TianTianJieXiGenericProvider,
    TianTianJieXiResultNormalizer,
)
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

RISK_SAFE = "safe"
RISK_NORMAL = "normal"
RISK_SENSITIVE = "sensitive"
RISK_ADULT = "adult"
RISK_LOGIN_REQUIRED = "login_required"
RISK_UNKNOWN = "unknown"
PUBLIC_DENIED_RISK_LEVELS = {RISK_ADULT, RISK_LOGIN_REQUIRED}
BLOCKED_PUBLIC_HOST_SUFFIXES = (
    "pornhub.com",
    "xvideos.com",
    "xnxx.com",
    "onlyfans.com",
)


@dataclass(frozen=True)
class ThirdPartyPlatformRule:
    platform_id: str
    display_name: str
    host_suffixes: tuple[str, ...]
    media_types: tuple[str, ...] = ("image", "video")
    provider_ids: tuple[str, ...] = (
        "kukutool_generic",
        "tiantianjiexi_generic",
        "yt_dlp_generic",
        "gallery_dl_generic",
    )
    media_host_suffixes: tuple[str, ...] = ()
    image_media_host_suffixes: tuple[str, ...] = ()
    video_media_host_suffixes: tuple[str, ...] = ()
    risk_level: str = RISK_UNKNOWN
    public_default: bool = False
    staff_default: bool = True
    notes: str = ""
    upstream_matches: tuple = ()

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
            "risk_level": self.risk_level,
            "public_default": self.public_default,
            "staff_default": self.staff_default,
            "notes": self.notes,
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
        risk_level=RISK_NORMAL,
        notes="短视频平台，默认仅 staff smoke；访客公开需要后台 allowlist。",
    ),
    ThirdPartyPlatformRule(
        platform_id="kuaishou",
        display_name="快手",
        host_suffixes=("kuaishou.com", "kuaishouapp.com", "kwai.com"),
        media_host_suffixes=("kwimgs.com", "yximgs.com"),
        risk_level=RISK_NORMAL,
        notes="短视频平台，默认仅 staff smoke；访客公开需要后台 allowlist。",
    ),
    ThirdPartyPlatformRule(
        platform_id="instagram",
        display_name="Instagram",
        host_suffixes=("instagram.com",),
        media_host_suffixes=("cdninstagram.com", "fbcdn.net"),
        risk_level=RISK_SENSITIVE,
        notes="常见登录/地区/风控限制，默认不向访客公开。",
    ),
    ThirdPartyPlatformRule(
        platform_id="facebook",
        display_name="Facebook",
        host_suffixes=("facebook.com", "fb.watch"),
        media_host_suffixes=("fbcdn.net",),
        risk_level=RISK_SENSITIVE,
        notes="常见登录/地区/风控限制，默认不向访客公开。",
    ),
    ThirdPartyPlatformRule(
        platform_id="youtube",
        display_name="YouTube",
        host_suffixes=("youtube.com", "youtu.be"),
        media_types=("video",),
        provider_ids=("yt_dlp_generic", "kukutool_generic", "tiantianjiexi_generic"),
        media_host_suffixes=("googlevideo.com", "ytimg.com"),
        image_media_host_suffixes=("ytimg.com",),
        video_media_host_suffixes=("googlevideo.com",),
        risk_level=RISK_SAFE,
        public_default=True,
        notes="可作为公开灰度候选，但仍必须命中后台 allowlist。",
    ),
    ThirdPartyPlatformRule(
        platform_id="x",
        display_name="X / Twitter",
        host_suffixes=("x.com", "twitter.com"),
        media_host_suffixes=("twimg.com",),
        risk_level=RISK_SENSITIVE,
        notes="社交平台，内容风险和登录/地区限制较多，默认不公开。",
    ),
    ThirdPartyPlatformRule(
        platform_id="imgur",
        display_name="Imgur",
        host_suffixes=("imgur.com",),
        media_types=("image", "video"),
        provider_ids=("kukutool_generic", "tiantianjiexi_generic", "gallery_dl_generic"),
        media_host_suffixes=("imgur.com",),
        risk_level=RISK_NORMAL,
        notes="图片/动图平台，公开仍需后台 allowlist。",
    ),
    ThirdPartyPlatformRule(
        platform_id="reddit",
        display_name="Reddit",
        host_suffixes=("reddit.com", "redd.it"),
        media_types=("image",),
        provider_ids=("kukutool_generic", "tiantianjiexi_generic", "gallery_dl_generic"),
        media_host_suffixes=("redd.it", "redditmedia.com"),
        risk_level=RISK_SENSITIVE,
        notes="社区内容风险较高，默认不公开。",
    ),
    ThirdPartyPlatformRule(
        platform_id="tumblr",
        display_name="Tumblr",
        host_suffixes=("tumblr.com",),
        media_types=("image",),
        provider_ids=("kukutool_generic", "tiantianjiexi_generic", "gallery_dl_generic"),
        media_host_suffixes=("tumblr.com",),
        risk_level=RISK_SENSITIVE,
        notes="社区内容风险较高，默认不公开。",
    ),
    ThirdPartyPlatformRule(
        platform_id="flickr",
        display_name="Flickr",
        host_suffixes=("flickr.com",),
        media_types=("image",),
        provider_ids=("kukutool_generic", "tiantianjiexi_generic", "gallery_dl_generic"),
        media_host_suffixes=("staticflickr.com",),
        risk_level=RISK_SAFE,
        notes="图片平台，可作为公开灰度候选，但仍必须命中后台 allowlist。",
    ),
    ThirdPartyPlatformRule(
        platform_id="deviantart",
        display_name="DeviantArt",
        host_suffixes=("deviantart.com",),
        media_types=("image",),
        provider_ids=("kukutool_generic", "tiantianjiexi_generic", "gallery_dl_generic"),
        media_host_suffixes=("wixmp.com", "deviantart.net"),
        risk_level=RISK_SENSITIVE,
        notes="社区内容风险较高，默认不公开。",
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
    if getattr(rule, "platform_id", "") == "upstream":
        return 30.0
    base_budget = max(
        0.1,
        float(getattr(settings, "THIRD_PARTY_FRONTEND_TIMEOUT_SECONDS", 3.0) or 3.0),
    )
    if getattr(rule, "platform_id", "") == "youtube":
        return max(
            base_budget,
            runtime_float(
                "youtube_frontend_timeout_seconds",
                getattr(settings, "YOUTUBE_FRONTEND_TIMEOUT_SECONDS", 8.0) or 8.0,
                minimum=3.0,
                maximum=60.0,
            ),
        )
    return base_budget


def build_third_party_rollout_status(
    *,
    enabled,
    public_access_enabled,
    staff_only,
    issue="",
):
    if not enabled or not public_access_enabled:
        return {
            "status": "disabled",
            "reason": "third_party_entrypoint_disabled",
        }
    if issue:
        return {
            "status": "hold",
            "reason": issue,
        }
    if staff_only:
        return {
            "status": "staff_only",
            "reason": "staff_only_gate_enabled",
        }
    return {
        "status": "public_candidate",
        "reason": "ready_but_requires_manual_public_approval",
    }


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


def result_with_provider_diagnostics(
    result,
    provider_id,
    duration_ms,
    attempts,
    *,
    total_duration_ms=None,
):
    metadata = dict(result.metadata or {})
    metadata.update({
        "selected_provider": provider_id,
        "selected_provider_duration_ms": duration_ms,
        "provider_total_duration_ms": (
            int(total_duration_ms)
            if total_duration_ms is not None
            else int(duration_ms)
        ),
        "provider_attempts": tuple(attempts),
        "provider_fallback_count": sum(
            1 for attempt in attempts if attempt.get("status") != "success"
        ),
    })
    return replace(result, metadata=metadata)


def provider_is_enabled(provider_id):
    if provider_id == "videodl_repo":
        from ..quality_execution import provider_available
        return provider_available(provider_id)
    if provider_id == "parsehub_generic":
        return runtime_bool("PARSEHUB_UPSTREAM_ENABLED", False)
    if provider_id == "kukutool_generic":
        return bool(
            getattr(settings, "THIRD_PARTY_KUKUTOOL_PROVIDER_ENABLED", False)
        )
    if provider_id == "tiantianjiexi_generic":
        return bool(
            runtime_bool("TIANTIANJIEXI_PROVIDER_ENABLED", False)
            and runtime_str("TIANTIANJIEXI_PHONE")
            and runtime_str("TIANTIANJIEXI_PASSWORD")
        )
    if provider_id == "gallery_dl_generic":
        return bool(getattr(settings, "GALLERYDL_PROVIDER_ENABLED", False))
    if provider_id == "yt_dlp_generic":
        return bool(getattr(settings, "YTDLP_PROVIDER_ENABLED", False))
    return False


def normalize_provider_id(value):
    return str(value or "").strip().lower().replace("-", "_")


def configured_third_party_provider_order(rule):
    platform_id = getattr(rule, "platform_id", "")
    if platform_id == "youtube":
        setting_name = "THIRD_PARTY_PROVIDER_ORDER_YOUTUBE"
        fallback_order = (
            "yt_dlp_generic",
            "kukutool_generic",
            "tiantianjiexi_generic",
        )
    elif platform_id in {"tiktok", "kuaishou"}:
        setting_name = "THIRD_PARTY_PROVIDER_ORDER_SHORT_VIDEO"
        fallback_order = (
            "kukutool_generic",
            "tiantianjiexi_generic",
            "yt_dlp_generic",
            "gallery_dl_generic",
        )
    else:
        setting_name = "THIRD_PARTY_PROVIDER_ORDER_DEFAULT"
        fallback_order = (
            "kukutool_generic",
            "tiantianjiexi_generic",
            "yt_dlp_generic",
            "gallery_dl_generic",
        )
    configured_order = tuple(
        normalize_provider_id(provider_id)
        for provider_id in runtime_list(setting_name, ())
    )
    if not configured_order:
        configured_order = tuple(
            normalize_provider_id(provider_id)
            for provider_id in runtime_list("THIRD_PARTY_PROVIDER_ORDER", ())
        )
    if not configured_order:
        configured_order = fallback_order
    allowed = tuple(getattr(rule, "provider_ids", ()) or ())
    allowed_set = set(allowed)
    ordered = []
    seen = set()
    for provider_id in configured_order:
        if provider_id not in allowed_set or provider_id in seen:
            continue
        seen.add(provider_id)
        ordered.append(provider_id)
    return tuple(ordered)


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
            "tiantianjiexi_generic",
            "yt_dlp_generic",
            "gallery_dl_generic",
            YOUTUBE_MERGE_PROVIDER_ID,
        ),
    )
    platform_rules = THIRD_PARTY_PLATFORM_RULES
    parse_option_names = (
        "prefer_youtube_merge",
        "timeout_seconds",
        "deadline_monotonic",
        "upstream_provider_ids",
        "upstream_matches",
    )

    def __init__(
        self,
        runner=None,
        provider=None,
        normalizer=None,
        gallerydl_provider=None,
        gallerydl_normalizer=None,
        tiantianjiexi_provider=None,
        tiantianjiexi_normalizer=None,
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
        self.tiantianjiexi_provider = (
            tiantianjiexi_provider or TianTianJieXiGenericProvider()
        )
        self.tiantianjiexi_normalizer = (
            tiantianjiexi_normalizer
            or TianTianJieXiResultNormalizer(platform_id=self.platform_id)
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
            "tiantianjiexi_generic": (
                self.tiantianjiexi_provider,
                self.tiantianjiexi_normalizer,
            ),
            "yt_dlp_generic": (self.ytdlp_provider, self.ytdlp_normalizer),
        }

    def iter_provider_specs(self, rule, *, respect_enabled=True):
        if getattr(rule, "upstream_matches", ()):
            # Legacy diagnostics must not execute dynamic matches through an
            # unrestricted CLI; the product uses the exact-extractor worker.
            return
        provider_ids = configured_third_party_provider_order(rule)
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
        from ..registry import DEFAULT_ADAPTERS
        if any(adapter.platform_id != self.platform_id and adapter.supports_url(url)
               for adapter in DEFAULT_ADAPTERS):
            return None
        # Mature rules retain precedence. Paid services are eligible only through
        # a saved Human route; matching itself never calls their remote APIs.
        if host_matches(hostname, BLOCKED_PUBLIC_HOST_SUFFIXES):
            return None
        from ..upstream_matching import match_upstream_url
        from ..upstream_routes import saved_videodl_matches, saved_service_matches
        matches = tuple(match_upstream_url(url)) + saved_videodl_matches(url) + saved_service_matches(url)
        if matches:
            return ThirdPartyPlatformRule(
                platform_id="upstream", display_name=matches[0]["label"],
                host_suffixes=(hostname,),
                provider_ids=tuple(dict.fromkeys(m["provider_id"] for m in matches)),
                upstream_matches=matches, notes="上游专用解析器匹配，可尝试；未逐站验证。",
            )
        return None

    def supports_url(self, url):
        return self.get_rule_for_url(url) is not None

    def supports_platform(self, platform_id):
        platform_id = (platform_id or "").strip().lower()
        return platform_id == self.platform_id or any(
            rule.platform_id == platform_id for rule in self.platform_rules
        )

    def parse_url(
        self,
        url,
        prefer_youtube_merge=False,
        timeout_seconds=None,
        deadline_monotonic=None,
        upstream_provider_ids=None,
        upstream_matches=None,
    ):
        source_url = self.normalize_source_url(url)
        self.require_supported_url(source_url)
        rule = self.get_rule_for_url(source_url)
        platform_name = rule.display_name if rule else self.display_name
        if not runtime_bool("THIRD_PARTY_PROVIDER_ENABLED", False):
            raise XhsParseError("第三方通用解析 Provider 当前未启用。")
        if getattr(rule, "upstream_matches", ()):
            if upstream_matches is not None:
                authorized = {(m.get('provider_id'), m.get('extractor_id'), m.get('version')) for m in upstream_matches}
                rule = replace(rule, upstream_matches=tuple(
                    m for m in rule.upstream_matches
                    if (m.get('provider_id'), m.get('extractor_id'), m.get('version')) in authorized
                ))
            if upstream_provider_ids is not None:
                rule = replace(rule, upstream_matches=tuple(
                    m for m in rule.upstream_matches if m['provider_id'] in upstream_provider_ids))
            return self.parse_upstream_rule(source_url, rule, timeout_seconds, deadline_monotonic)
        providers = list(self.iter_provider_specs(rule, respect_enabled=True))
        failures = []
        attempts = []
        parse_started_at = time.monotonic()
        if deadline_monotonic:
            deadline = float(deadline_monotonic)
        elif timeout_seconds is not None:
            deadline = parse_started_at + max(0.1, float(timeout_seconds or 0))
        else:
            deadline = parse_started_at + frontend_budget_seconds(rule)
        if prefer_youtube_merge and getattr(rule, "platform_id", "") == "youtube":
            health = get_youtube_merge_health()
            if (
                health.get("ready")
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
                            "total_duration_ms": elapsed_ms(parse_started_at),
                            "attempt_count": len(attempts),
                            "asset_count": len(result.assets),
                        },
                    )
                    return result_with_provider_diagnostics(
                        result,
                        YOUTUBE_MERGE_PROVIDER_ID,
                        duration_ms,
                        attempts,
                        total_duration_ms=elapsed_ms(parse_started_at),
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
                parse_options = {
                    "timeout_seconds": provider_timeout_seconds,
                }
                if provider_id == "tiantianjiexi_generic":
                    try:
                        provider_parse_parameters = inspect.signature(
                            provider.parse
                        ).parameters
                    except (TypeError, ValueError):
                        provider_parse_parameters = {}
                    if (
                        "payload_validator" in provider_parse_parameters
                        or any(
                            parameter.kind == inspect.Parameter.VAR_KEYWORD
                            for parameter in provider_parse_parameters.values()
                        )
                    ):
                        parse_options["payload_validator"] = lambda value: bool(
                            normalizer.normalize(
                                rule=rule,
                                platform_name=platform_name,
                                source_url=source_url,
                                payload=value,
                            ).assets
                        )
                payload = provider.parse(source_url, **parse_options)
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
                        "total_duration_ms": elapsed_ms(parse_started_at),
                        "attempt_count": len(attempts),
                        "asset_count": len(result.assets),
                    },
                )
                return result_with_provider_diagnostics(
                    result,
                    selected_provider_id,
                    duration_ms,
                    attempts,
                    total_duration_ms=elapsed_ms(parse_started_at),
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
                "duration_ms": elapsed_ms(parse_started_at),
                "attempt_count": len(attempts),
                "failure_count": len(failures),
                "failed_providers": tuple(provider_id for provider_id, _error in failures),
                "attempt_statuses": tuple(
                    (
                        attempt.get("provider", ""),
                        attempt.get("status", ""),
                        attempt.get("duration_ms", 0),
                    )
                    for attempt in attempts
                ),
                "failure_summary": summary[:500],
            },
        )
        raise XhsParseError(f"{platform_name} 第三方解析失败：{summary or '没有可用 Provider'}")

    def parse_upstream_rule(self, source_url, rule, timeout_seconds, deadline_monotonic):
        from ..quality_execution import execute_provider, SERVICE_PROVIDER_IDS
        from ..common import validate_public_download_url
        from ..upstream_routes import (route_matches, parse_cache_key, cached_parse_result,
                                       cache_parse_result, apply_format_preferences)

        validate_public_download_url(source_url)
        matches, route = route_matches(source_url, rule.upstream_matches)
        started = time.monotonic()
        deadline = min(started + 30.0, float(deadline_monotonic)) if deadline_monotonic else (
            started + min(30.0, max(0.1, float(timeout_seconds or 30.0))))
        attempts = []
        failure_code = "upstream_unavailable"
        for provider in route["effective_provider_order"]:
            match = next((m for m in matches if m["provider_id"] == provider), None)
            if match is None or not provider_is_enabled(provider):
                if route["mode"] == "manual":
                    attempts.append({"provider": provider, "status": "unavailable", "duration_ms": 0,
                                     "reason": "not_matched_or_not_permitted" if match is None else "disabled"})
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise XhsProviderTimeoutError("扩展解析超时，请稍后再试。")
            attempt_start = time.monotonic()
            try:
                execution = {}
                if provider == "videodl_repo":
                    # Execute exactly the backend admitted above, never re-expand to
                    # a dedicated backend excluded by source/access matching.
                    execution["backend"] = match["extractor_id"]
                else:
                    execution["expected_match"] = match
                if provider in SERVICE_PROVIDER_IDS:
                    execution['service_authorized'] = route['mode'] == 'manual'
                cache_key = parse_cache_key(source_url, route, provider)
                result = cached_parse_result(cache_key, source_url, provider)
                cache_hit = result is not None
                if not cache_hit:
                    result = execute_provider(provider, source_url, timeout_seconds=min(20.0, remaining), **execution)
                if not result.assets and not (result.metadata or {}).get("upstream_candidates"):
                    raise XhsParseError("empty_upstream_result")
                if not cache_hit:
                    cache_parse_result(cache_key, result)
                result = apply_format_preferences(result, route["format_preferences"])
                result = replace(result, metadata={**(result.metadata or {}),
                                                   "upstream_route": {**route, "cache_hit": cache_hit,
                                                       "selected_provider": provider,
                                                       "fallback_reason": "; ".join(
                                                           f"{attempt['provider']}: {attempt['status']}"
                                                           for attempt in attempts)}})
                attempts.append(provider_attempt_record(provider, "success", elapsed_ms(attempt_start)))
                return result_with_provider_diagnostics(result, provider, elapsed_ms(attempt_start), attempts,
                                                        total_duration_ms=elapsed_ms(started))
            except Exception as error:
                # Upstream errors can contain signed URLs/credentials. Keep only class.
                attempts.append(provider_attempt_record(provider, "failed", elapsed_ms(attempt_start)))
                failure_code = str(getattr(error, 'code', 'unsupported_result'))
                logger.info("upstream_parse_failed", extra={"provider": provider, "error_type": type(error).__name__,
                                                          "error_code": failure_code})
        if not attempts:
            raise XhsParseError("匹配的扩展解析器当前未启用或不可执行。")
        if route["mode"] == "manual" and all(a["status"] == "unavailable" for a in attempts):
            raise XhsParseError("已保存的人工路线当前不可执行；设置已保留，请检查配置或明确恢复默认。")
        messages = {
            'auth_required': '该公开链接的上游解析需要登录；本入口不读取账号凭据。',
            'access_denied': '上游拒绝访问该内容，可能存在地区或访问限制。',
            'execution_timeout': '上游解析超时，请稍后再试。',
            'network_timeout': '上游网络连接超时，请稍后再试。',
            'playlist_denied': '本入口仅接受单条内容，不支持账号或播放列表。',
            'delegation_denied': '该链接需要额外的上游跳转解析，当前暂不支持。',
            'unsupported_result': '解析结果没有可交付的图片或完整视频，可能为分离音视频或流媒体。',
            'no_formats': '上游没有返回当前可保存的媒体格式。',
            'unknown_media_tracks': '上游未提供足够的音视频轨道信息，暂时无法确认这是完整视频。',
            'separate_media_tracks': '上游仅提供分离音轨或视频轨，本入口暂不支持合流。',
            'streaming_media': '上游仅提供 HLS/DASH 流媒体，本入口暂不支持保存该格式。',
        }
        raise XhsParseError('扩展解析失败：' + messages.get(failure_code, '当前上游不可用，请稍后重试。'))

    def describe(self):
        description = super().describe()
        description["platform_rules"] = tuple(
            rule.to_dict() for rule in self.platform_rules
        )
        return description
