import logging
import re
from hashlib import sha256
from urllib.parse import urlparse

from django.conf import settings

from ..models import ProviderAccessMode, ProviderRuntimeSetting
from .common import extract_url_from_text, host_matches, is_blocked_hostname


logger = logging.getLogger(__name__)

PROVIDER_DOUYIN_ULTRA = "douyin_ultra"
PROVIDER_YOUTUBE_MERGE = "youtube_merge"
PROVIDER_THIRD_PARTY = "third_party"
UPSTREAM_PROVIDER_IDS = ("yt_dlp_generic", "gallery_dl_generic", "parsehub_generic")
UPSTREAM_ROUTE_PROVIDER_IDS = (*UPSTREAM_PROVIDER_IDS, "videodl_repo")
UPSTREAM_PARSE_TIMEOUT_SECONDS = 30.0


def upstream_match_is_blocked(match, runtime_setting):
    provider = str(match.get("provider_id") or "")
    extractor = str(match.get("extractor_id") or "").lower()
    blocked = set(parse_csv_values(getattr(runtime_setting, "third_party_blocked_platforms", "")))
    return bool(blocked.intersection({
        "upstream", f"upstream:{provider}",
        f"upstream:{provider}:{extractor}", f"upstream:extractor:{extractor}",
    }))


def upstream_match_is_public_allowed(match, host, runtime_setting):
    provider = str(match.get("provider_id") or "")
    if provider not in UPSTREAM_ROUTE_PROVIDER_IDS or upstream_match_is_blocked(match, runtime_setting):
        return False
    return third_party_platform_is_public_allowed(
        f"upstream:{provider}", host, runtime_setting,
    ) or third_party_platform_is_public_allowed("upstream", host, runtime_setting)


def update_upstream_bulk_allowlist(value, selected_providers):
    bulk_tokens = {f"upstream:{provider}" for provider in UPSTREAM_PROVIDER_IDS}
    values = [token for token in parse_csv_values(value) if token not in bulk_tokens]
    values.extend(f"upstream:{provider}" for provider in UPSTREAM_PROVIDER_IDS if provider in selected_providers)
    return "\n".join(dict.fromkeys(values))


def request_user_is_staff(request):
    user = getattr(request, "user", None)
    return bool(
        getattr(user, "is_active", False)
        and getattr(user, "is_staff", False)
    )


def load_runtime_setting():
    try:
        return ProviderRuntimeSetting.load()
    except Exception as error:
        if type(error).__name__ != "DatabaseOperationForbidden":
            logger.exception("provider_runtime_setting_load_failed")
        return None


def mode_label(mode):
    return dict(ProviderAccessMode.choices).get(mode, mode)


def default_mode_for_provider(provider_id):
    return ProviderAccessMode.DISABLED


def configured_mode_for_provider(provider_id, runtime_setting=None):
    runtime_setting = runtime_setting or load_runtime_setting()
    if runtime_setting is None:
        return ProviderAccessMode.DISABLED
    if provider_id == PROVIDER_DOUYIN_ULTRA:
        return runtime_setting.douyin_ultra_mode
    if provider_id == PROVIDER_YOUTUBE_MERGE:
        return runtime_setting.youtube_merge_mode
    if provider_id == PROVIDER_THIRD_PARTY:
        return runtime_setting.third_party_mode
    return ProviderAccessMode.DISABLED


def hard_enabled_for_provider(provider_id):
    if provider_id == PROVIDER_DOUYIN_ULTRA:
        return bool(
            getattr(settings, "ULTRA_HD_PROVIDER_ENABLED", False)
            and getattr(settings, "KUKUTOOL_PROVIDER_ENABLED", False)
        )
    if provider_id == PROVIDER_YOUTUBE_MERGE:
        return bool(getattr(settings, "YOUTUBE_MERGE_PROVIDER_ENABLED", False))
    if provider_id == PROVIDER_THIRD_PARTY:
        return bool(getattr(settings, "THIRD_PARTY_PROVIDER_ENABLED", False))
    return False


def effective_mode_for_provider(provider_id, runtime_setting=None):
    return configured_mode_for_provider(provider_id, runtime_setting)


def clamp_percent(value):
    try:
        return min(100, max(0, int(value or 0)))
    except (TypeError, ValueError):
        return 0


def rollout_percent_for_provider(provider_id, runtime_setting=None):
    runtime_setting = runtime_setting or load_runtime_setting()
    if runtime_setting is None:
        return 0
    if provider_id == PROVIDER_DOUYIN_ULTRA:
        return clamp_percent(runtime_setting.douyin_ultra_rollout_percent)
    if provider_id == PROVIDER_YOUTUBE_MERGE:
        return clamp_percent(runtime_setting.youtube_merge_rollout_percent)
    if provider_id == PROVIDER_THIRD_PARTY:
        return clamp_percent(runtime_setting.third_party_rollout_percent)
    return 0


def disabled_reason_for_provider(provider_id, runtime_setting=None):
    runtime_setting = runtime_setting or load_runtime_setting()
    if runtime_setting is None:
        return ""
    if provider_id == PROVIDER_DOUYIN_ULTRA:
        return str(runtime_setting.douyin_ultra_disabled_reason or "")
    if provider_id == PROVIDER_YOUTUBE_MERGE:
        return str(runtime_setting.youtube_merge_disabled_reason or "")
    if provider_id == PROVIDER_THIRD_PARTY:
        return str(runtime_setting.third_party_disabled_reason or "")
    return ""


def request_rollout_identity(request):
    if request is None:
        return ""
    user = getattr(request, "user", None)
    if getattr(user, "is_authenticated", False) is True:
        return f"user:{getattr(user, 'pk', '')}"
    forwarded = str(request.META.get("HTTP_X_FORWARDED_FOR") or "").split(",", 1)[0].strip()
    remote_addr = str(request.META.get("REMOTE_ADDR") or "").strip()
    user_agent = str(request.META.get("HTTP_USER_AGENT") or "").strip()[:120]
    return f"ip:{forwarded or remote_addr}|ua:{user_agent}"


def request_rollout_bucket(provider_id, request=None):
    identity = request_rollout_identity(request)
    if not identity:
        return None
    digest = sha256(f"{provider_id}:{identity}".encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % 100


def request_is_in_public_rollout(provider_id, request=None, percent=0):
    percent = min(100, max(0, int(percent or 0)))
    if percent <= 0:
        return False
    if percent >= 100:
        return True
    bucket = request_rollout_bucket(provider_id, request)
    if bucket is None:
        return False
    return bucket < percent


def provider_access_summary(
    provider_id,
    request=None,
    runtime_setting=None,
    *,
    hard_enabled_override=None,
):
    configured_mode = configured_mode_for_provider(provider_id, runtime_setting)
    default_mode = default_mode_for_provider(provider_id)
    effective_mode = configured_mode
    hard_enabled = (
        hard_enabled_for_provider(provider_id)
        if hard_enabled_override is None
        else bool(hard_enabled_override)
    )
    staff_user = request_user_is_staff(request) if request is not None else False
    rollout_percent = rollout_percent_for_provider(provider_id, runtime_setting)
    rollout_bucket = request_rollout_bucket(provider_id, request)
    rollout_allowed = request_is_in_public_rollout(
        provider_id,
        request,
        rollout_percent,
    )
    disabled_reason = disabled_reason_for_provider(provider_id, runtime_setting)
    hard_gate_reason = "" if hard_enabled else "环境 capability gate 未启用。"
    blocked_reason = ""
    public_allowed = hard_enabled and effective_mode == ProviderAccessMode.PUBLIC
    staff_allowed = hard_enabled and effective_mode in {
        ProviderAccessMode.PUBLIC,
        ProviderAccessMode.PUBLIC_GRAY,
        ProviderAccessMode.STAFF,
    }
    public_gray_allowed = (
        hard_enabled
        and effective_mode == ProviderAccessMode.PUBLIC_GRAY
        and rollout_allowed
    )
    request_allowed = public_allowed or (
        hard_enabled
        and effective_mode == ProviderAccessMode.STAFF
        and staff_user
    ) or (
        hard_enabled
        and effective_mode == ProviderAccessMode.PUBLIC_GRAY
        and (staff_user or rollout_allowed)
    )
    if not hard_enabled:
        access_reason = hard_gate_reason
    elif effective_mode == ProviderAccessMode.DISABLED:
        access_reason = disabled_reason or "后台运行时模式为 disabled。"
    elif effective_mode == ProviderAccessMode.STAFF and not staff_user:
        access_reason = "后台运行时模式为 staff，仅管理员可用。"
    elif effective_mode == ProviderAccessMode.PUBLIC_GRAY and not (
        staff_user or rollout_allowed
    ):
        access_reason = "当前请求未命中公开灰度。"
    else:
        access_reason = "当前请求允许使用。"
    return {
        "provider_id": provider_id,
        "hard_enabled": hard_enabled,
        "configured_mode": configured_mode,
        "configured_label": mode_label(configured_mode),
        "default_mode": default_mode,
        "default_label": mode_label(default_mode),
        "effective_mode": effective_mode,
        "effective_label": mode_label(effective_mode),
        "public_allowed": public_allowed,
        "staff_allowed": staff_allowed,
        "rollout_percent": rollout_percent,
        "rollout_bucket": rollout_bucket,
        "rollout_allowed": rollout_allowed,
        "public_gray_allowed": public_gray_allowed,
        "disabled_reason": disabled_reason,
        "blocked_reason": blocked_reason,
        "hard_gate_reason": hard_gate_reason,
        "access_reason": access_reason,
        "request_allowed": request_allowed,
        "request_is_staff": staff_user,
    }


def provider_is_allowed_for_request(provider_id, request=None):
    return provider_access_summary(provider_id, request)["request_allowed"]


def parse_csv_values(value):
    values = []
    seen = set()
    for item in re.split(r"[\s,;，；]+", str(value or "")):
        normalized = item.strip().lower().strip(".")
        if not normalized or normalized.startswith("#"):
            continue
        if normalized not in seen:
            seen.add(normalized)
            values.append(normalized)
    return tuple(values)


def normalize_public_host_rule(value):
    value = str(value or "").strip().lower()
    if not value:
        return ""
    if value.startswith("*."):
        value = value[2:]
    parsed = urlparse(value if "://" in value else f"https://{value}")
    hostname = parsed.hostname or value
    return hostname.strip().lower().strip(".")


def host_matches_public_rule(host, rules):
    host = str(host or "").strip().lower().strip(".")
    normalized_rules = tuple(
        rule for rule in (normalize_public_host_rule(item) for item in rules) if rule
    )
    return host_matches(host, normalized_rules)


def platform_matches_public_rule(platform_id, rules):
    platform_id = str(platform_id or "").strip().lower()
    return platform_id in {str(rule or "").strip().lower() for rule in rules}


def third_party_platform_is_blocked(platform_id, host, runtime_setting=None):
    from .platforms.third_party import BLOCKED_PUBLIC_HOST_SUFFIXES

    runtime_setting = runtime_setting or load_runtime_setting()
    blocked_platforms = parse_csv_values(
        getattr(runtime_setting, "third_party_blocked_platforms", "")
        if runtime_setting is not None
        else ""
    )
    blocked_hosts = parse_csv_values(
        getattr(runtime_setting, "third_party_blocked_hosts", "")
        if runtime_setting is not None
        else ""
    )
    if platform_matches_public_rule(platform_id, blocked_platforms):
        return True
    return host_matches_public_rule(
        host,
        tuple(blocked_hosts) + tuple(BLOCKED_PUBLIC_HOST_SUFFIXES),
    )


def third_party_platform_is_public_allowed(platform_id, host, runtime_setting=None):
    runtime_setting = runtime_setting or load_runtime_setting()
    allowed_platforms = parse_csv_values(
        getattr(runtime_setting, "third_party_allowed_public_platforms", "")
        if runtime_setting is not None
        else ""
    )
    allowed_hosts = parse_csv_values(
        getattr(runtime_setting, "third_party_allowed_public_hosts", "")
        if runtime_setting is not None
        else ""
    )
    if not allowed_platforms and not allowed_hosts:
        return False
    return platform_matches_public_rule(
        platform_id,
        allowed_platforms,
    ) or host_matches_public_rule(host, allowed_hosts)


def third_party_public_access_summary(query_url, request=None, runtime_setting=None):
    from .platforms.third_party import (
        PUBLIC_DENIED_RISK_LEVELS,
        GenericThirdPartyAdapter,
        provider_is_enabled,
    )

    runtime_setting = runtime_setting or load_runtime_setting()
    query_url = extract_url_from_text(query_url or "")
    parsed = urlparse(query_url or "")
    host = (parsed.hostname or "").strip().lower()
    staff_user = request_user_is_staff(request)
    if parsed.scheme not in {"http", "https"} or not host:
        return {
            "allowed": False,
            "error_code": "INVALID_THIRD_PARTY_URL",
            "message": "第三方链接无效，请检查 URL。",
            "platform_id": "",
            "host": host,
        }
    if is_blocked_hostname(host):
        return {
            "allowed": False,
            "error_code": "BLOCKED_HOST",
            "message": "该链接 host 不允许访问。",
            "platform_id": "",
            "host": host,
        }
    if third_party_platform_is_blocked("", host, runtime_setting):
        return {
            "allowed": False,
            "error_code": "THIRD_PARTY_PLATFORM_BLOCKED",
            "message": "该平台当前不支持访客解析。",
            "platform_id": "",
            "host": host,
        }
    rule = GenericThirdPartyAdapter().get_rule_for_url(query_url)
    if rule is None:
        return {
            "allowed": False,
            "error_code": "UNSUPPORTED_THIRD_PARTY_PLATFORM",
            "message": "该平台暂未进入公开灰度。",
            "platform_id": "",
            "host": host,
        }
    if third_party_platform_is_blocked(rule.platform_id, host, runtime_setting):
        return {
            "allowed": False,
            "error_code": "THIRD_PARTY_PLATFORM_BLOCKED",
            "message": "该平台当前不支持访客解析。",
            "platform_id": rule.platform_id,
            "host": host,
            "risk_level": rule.risk_level,
        }
    if rule.risk_level in PUBLIC_DENIED_RISK_LEVELS:
        internal_staff_allow_adult = bool(
            getattr(settings, "THIRD_PARTY_INTERNAL_STAFF_ALLOW_ADULT", False)
        )
        if not (staff_user and internal_staff_allow_adult):
            return {
                "allowed": False,
                "error_code": "THIRD_PARTY_PLATFORM_RISK_BLOCKED",
                "message": "该平台属于高风险或登录限制平台，暂不向访客开放。",
                "platform_id": rule.platform_id,
                "host": host,
                "risk_level": rule.risk_level,
            }
    access = provider_access_summary(PROVIDER_THIRD_PARTY, request, runtime_setting)
    if not access["request_allowed"]:
        error_code = "THIRD_PARTY_NOT_OPEN"
        message = access["access_reason"] or "第三方通用解析暂未开放。"
        if access["effective_mode"] == ProviderAccessMode.PUBLIC_GRAY:
            error_code = "THIRD_PARTY_ROLLOUT_MISS"
            message = "第三方通用解析暂未向该用户开放。"
        elif access["effective_mode"] not in {
            ProviderAccessMode.PUBLIC,
            ProviderAccessMode.PUBLIC_GRAY,
            ProviderAccessMode.STAFF,
        }:
            message = "第三方通用解析暂未开放。"
        return {
            "allowed": False,
            "error_code": error_code,
            "message": message,
            "platform_id": rule.platform_id,
            "host": host,
            "risk_level": rule.risk_level,
        }
    if rule.platform_id == "upstream":
        matches = tuple(
            match for match in getattr(rule, "upstream_matches", ())
            if isinstance(match, dict)
            and match.get("provider_id") in UPSTREAM_ROUTE_PROVIDER_IDS
            and match.get("provider_id") in rule.provider_ids
            and match.get("extractor_id")
            and provider_is_enabled(match["provider_id"])
            and not upstream_match_is_blocked(match, runtime_setting)
            and ((staff_user and rule.staff_default) or upstream_match_is_public_allowed(match, host, runtime_setting))
        )
        return {
            "allowed": bool(matches),
            "error_code": "" if matches else "THIRD_PARTY_PLATFORM_NOT_ALLOWLISTED",
            "message": "可尝试" if matches else "该链接暂不可用。",
            "platform_id": "upstream",
            "host": host,
            "risk_level": rule.risk_level,
            "upstream_matches": matches,
            "upstream_provider_ids": tuple(dict.fromkeys(match["provider_id"] for match in matches)),
        }
    if staff_user:
        if not rule.staff_default:
            return {
                "allowed": False,
                "error_code": "THIRD_PARTY_PLATFORM_STAFF_BLOCKED",
                "message": "该平台未开放 staff smoke。",
                "platform_id": rule.platform_id,
                "host": host,
                "risk_level": rule.risk_level,
            }
        return {
            "allowed": True,
            "error_code": "",
            "message": "staff smoke allowed",
            "platform_id": rule.platform_id,
            "host": host,
            "risk_level": rule.risk_level,
        }
    if not third_party_platform_is_public_allowed(rule.platform_id, host, runtime_setting):
        return {
            "allowed": False,
            "error_code": "THIRD_PARTY_PLATFORM_NOT_ALLOWLISTED",
            "message": "该平台暂未进入公开灰度。",
            "platform_id": rule.platform_id,
            "host": host,
            "risk_level": rule.risk_level,
        }
    return {
        "allowed": True,
        "error_code": "",
        "message": "public allowlist matched",
        "platform_id": rule.platform_id,
        "host": host,
        "risk_level": rule.risk_level,
    }


def third_party_public_access_allowed(query_url, request=None, runtime_setting=None):
    return third_party_public_access_summary(
        query_url,
        request,
        runtime_setting,
    )["allowed"]
