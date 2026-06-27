import json
import re
import time
from urllib.parse import urlparse

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from ...services.common import XhsProviderTimeoutError
from ...services.platforms.gallerydl_generic import GalleryDlGenericProvider
from ...services.platforms.kukutool_generic import KukutoolGenericProvider
from ...services.platforms.external_tools import external_command_output_max_bytes
from ...services.platforms.third_party import (
    GenericThirdPartyAdapter,
    build_third_party_rollout_status,
    elapsed_ms,
    frontend_budget_seconds,
    provider_is_enabled,
    provider_attempt_record,
    remaining_budget_seconds,
    result_with_provider_diagnostics,
)
from ...services.platforms.ytdlp_generic import YtDlpGenericProvider
from ...services.platforms.ytdlp_merge import (
    PROVIDER_ID as YTDLP_MERGE_PROVIDER_ID,
    build_parse_result as build_ytdlp_merge_parse_result,
    default_max_height as ytdlp_merge_default_max_height,
)
from ...services.youtube_merge import get_youtube_merge_health


PROVIDER_OPTION_IDS = {
    "kukutool": "kukutool_generic",
    "gallerydl": "gallery_dl_generic",
    "ytdlp": "yt_dlp_generic",
}
URL_TEXT_PATTERN = re.compile(r"https?://[^\s\"'<>]+")
TRAILING_URL_PUNCTUATION = ".,;:)]}"
SOURCE_URL_KEYS = {"source_url", "canonical_source_url"}


def redacted_url(url):
    parsed = urlparse(url or "")
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return ""
    return f"{parsed.scheme}://{parsed.hostname}/[redacted]"


def redact_text_urls(value):
    def replace(match):
        raw_url = match.group(0)
        trailing = ""
        while raw_url and raw_url[-1] in TRAILING_URL_PUNCTUATION:
            trailing = raw_url[-1] + trailing
            raw_url = raw_url[:-1]
        return (redacted_url(raw_url) or "[redacted-url]") + trailing

    return URL_TEXT_PATTERN.sub(replace, str(value or ""))


def summarize_diagnostic_value(value, *, show_urls=False, key=""):
    if show_urls:
        return value
    if isinstance(value, dict):
        return {
            item_key: summarize_diagnostic_value(
                item_value,
                show_urls=show_urls,
                key=str(item_key),
            )
            for item_key, item_value in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [
            summarize_diagnostic_value(item, show_urls=show_urls)
            for item in value
        ]
    if isinstance(value, str) and key not in SOURCE_URL_KEYS:
        return redact_text_urls(value)
    return value


def summarize_asset(asset, *, show_urls=False):
    summary = {
        "type": asset.media_type,
        "provider": asset.provider,
        "quality_label": asset.quality_label,
        "size_bytes": asset.size_bytes,
        "host": urlparse(asset.url).hostname or "",
        "url": asset.url if show_urls else redacted_url(asset.url),
        "url_redacted": not show_urls,
        "cover_host": urlparse(asset.cover_url).hostname if asset.cover_url else "",
    }
    if show_urls:
        summary["cover_url"] = asset.cover_url
    return summary


def summarize_result(result, *, show_urls=False):
    return {
        "platform": result.platform,
        "post_type": result.post_type,
        "base_filename": result.base_filename,
        "metadata": summarize_diagnostic_value(
            result.metadata,
            show_urls=show_urls,
        ),
        "assets": [
            summarize_asset(asset, show_urls=show_urls)
            for asset in result.assets
        ],
    }


def build_provider_smoke_summary(result, *, rule, attempts):
    metadata = result.metadata or {}
    total_duration_ms = int(metadata.get("provider_total_duration_ms") or 0)
    frontend_budget_ms = int(frontend_budget_seconds(rule) * 1000)
    asset_count = len(result.assets)
    media_types = tuple(sorted({asset.media_type for asset in result.assets}))
    fallback_count = int(metadata.get("provider_fallback_count") or 0)
    issues = []
    if asset_count <= 0:
        issues.append("no_assets")
    if total_duration_ms > frontend_budget_ms:
        issues.append("parse_exceeds_frontend_budget")
    if not media_types:
        issues.append("no_media_types")

    return {
        "parse_duration_ms": total_duration_ms,
        "frontend_budget_ms": frontend_budget_ms,
        "within_frontend_budget": total_duration_ms <= frontend_budget_ms,
        "asset_count": asset_count,
        "media_types": media_types,
        "selected_provider": metadata.get("selected_provider", ""),
        "source_platform": metadata.get("source_platform", ""),
        "fallback_count": fallback_count,
        "attempt_count": len(attempts),
        "issues": tuple(issues),
        "recommended_rollout": "staff_only" if not issues else "hold",
    }


def build_provider_failure_smoke_summary(*, rule, attempts, errors, duration_ms):
    frontend_budget_ms = int(frontend_budget_seconds(rule) * 1000)
    issue_codes = []
    for error in errors:
        status = error.get("status") or "failed"
        if status == "timeout":
            issue_codes.append("provider_timeout")
        elif status == "rejected":
            issue_codes.append("provider_rejected")
        else:
            issue_codes.append("provider_failed")
    if not issue_codes:
        issue_codes.append("no_provider_attempted")

    return {
        "parse_duration_ms": int(duration_ms),
        "frontend_budget_ms": frontend_budget_ms,
        "within_frontend_budget": int(duration_ms) <= frontend_budget_ms,
        "asset_count": 0,
        "media_types": (),
        "selected_provider": "",
        "source_platform": getattr(rule, "platform_id", ""),
        "fallback_count": sum(
            1 for attempt in attempts if attempt.get("status") != "success"
        ),
        "attempt_count": len(attempts),
        "issues": tuple(dict.fromkeys(issue_codes)),
        "recommended_rollout": "hold",
    }


def build_error_payload(
    provider,
    error_type,
    message,
    *,
    status="rejected",
    source_url="",
    platform_id="",
):
    payload = {
        "ok": False,
        "attempts": [],
        "errors": [{
            "provider": provider,
            "status": status,
            "error_type": error_type,
            "error": redact_text_urls(message),
            "duration_ms": 0,
        }],
    }
    if source_url:
        payload["source_url"] = source_url
    if platform_id:
        payload["platform_id"] = platform_id
    return payload


def provider_health_by_id(force_probe_provider_id=""):
    return {
        "kukutool_generic": KukutoolGenericProvider().health(),
        "gallery_dl_generic": GalleryDlGenericProvider().health(
            force_version_probe=force_probe_provider_id == "gallery_dl_generic",
        ),
        "yt_dlp_generic": YtDlpGenericProvider().health(
            force_version_probe=force_probe_provider_id == "yt_dlp_generic",
        ),
    }


def provider_is_config_enabled(provider_id, health):
    if provider_id == "kukutool_generic":
        return bool(health.get("provider_enabled"))
    return bool(health.get("enabled"))


def provider_is_ready(provider_id, health):
    if provider_id == "kukutool_generic":
        return bool(
            health.get("provider_enabled")
            and not (health.get("circuit") or {}).get("open")
        )
    return bool(health.get("enabled") and health.get("ready"))


def summarize_rule_health(adapter, health_by_id):
    rules = []
    for rule in adapter.platform_rules:
        enabled_provider_ids = tuple(
            provider_id
            for provider_id in rule.provider_ids
            if provider_is_enabled(provider_id)
        )
        ready_provider_ids = tuple(
            provider_id
            for provider_id in enabled_provider_ids
            if provider_is_ready(provider_id, health_by_id.get(provider_id, {}))
        )
        rules.append({
            "platform_id": rule.platform_id,
            "display_name": rule.display_name,
            "media_types": rule.media_types,
            "provider_ids": rule.provider_ids,
            "enabled_provider_ids": enabled_provider_ids,
            "ready_provider_ids": ready_provider_ids,
        })
    return rules


def build_health_report(adapter, force_probe_provider_id=""):
    health_by_id = provider_health_by_id(force_probe_provider_id)
    rules = summarize_rule_health(adapter, health_by_id)
    enabled_providers = tuple(
        provider_id
        for provider_id, health in health_by_id.items()
        if provider_is_config_enabled(provider_id, health)
    )
    ready_providers = tuple(
        provider_id
        for provider_id, health in health_by_id.items()
        if provider_is_ready(provider_id, health)
    )
    issues = []
    if (
        getattr(settings, "THIRD_PARTY_PROVIDER_ENABLED", False)
        and not enabled_providers
    ):
        issues.append({
            "code": "no_provider_enabled",
            "message": "Third-party provider capability is enabled but no concrete provider is enabled.",
        })
    for provider_id in enabled_providers:
        if provider_id not in ready_providers:
            health = health_by_id.get(provider_id, {})
            issues.append({
                "code": "provider_not_ready",
                "provider": provider_id,
                "message": health.get("version_probe_error") or health.get("error") or "provider is not ready",
            })
    third_party_enabled = bool(
        getattr(settings, "THIRD_PARTY_PROVIDER_ENABLED", False)
    )
    public_access_enabled = third_party_enabled
    staff_only = False
    rollout = build_third_party_rollout_status(
        enabled=third_party_enabled,
        public_access_enabled=public_access_enabled,
        staff_only=staff_only,
        issue=issues[0]["code"] if issues else "",
    )
    return {
        "ok": not issues,
        "settings": {
            "third_party_provider_enabled": third_party_enabled,
            "third_party_public_access_enabled": public_access_enabled,
            "third_party_staff_only": staff_only,
            "third_party_frontend_timeout_seconds": float(
                getattr(settings, "THIRD_PARTY_FRONTEND_TIMEOUT_SECONDS", 3.0) or 3.0
            ),
            "third_party_external_output_max_bytes": int(
                external_command_output_max_bytes()
            ),
        },
        "enabled_provider_ids": enabled_providers,
        "ready_provider_ids": ready_providers,
        "providers": health_by_id,
        "youtube_merge": get_youtube_merge_health(),
        "platform_rules": rules,
        "rollout": rollout,
        "issues": tuple(issues),
    }


def build_binary_override_error(options, *, source_url="", platform_id=""):
    if options["binary"] and options["provider"] == "auto":
        return build_error_payload(
            options["provider"],
            "BinaryRequiresExplicitProvider",
            "--binary requires an explicit --provider value.",
            source_url=source_url,
            platform_id=platform_id,
        )
    if options["binary"] and options["provider"] not in {"gallerydl", "ytdlp"}:
        return build_error_payload(
            options["provider"],
            "UnsupportedBinaryProvider",
            "--binary is only supported for gallerydl and ytdlp.",
            source_url=source_url,
            platform_id=platform_id,
        )
    return None


def apply_command_overrides(options):
    if options["binary"]:
        if options["provider"] == "gallerydl":
            settings.GALLERYDL_BINARY = options["binary"]
        elif options["provider"] == "ytdlp":
            settings.YTDLP_BINARY = options["binary"]
    if options["timeout"]:
        timeout = options["timeout"]
        settings.THIRD_PARTY_FRONTEND_TIMEOUT_SECONDS = timeout
        if options["provider"] in {"auto", "ytdlp"}:
            settings.YTDLP_TIMEOUT_SECONDS = timeout
        if options["provider"] in {"auto", "gallerydl"}:
            settings.GALLERYDL_TIMEOUT_SECONDS = timeout
        if options["provider"] in {"auto", "kukutool"}:
            settings.THIRD_PARTY_KUKUTOOL_TIMEOUT_SECONDS = timeout


class Command(BaseCommand):
    help = "Inspect generic third-party providers such as KuKuTool and yt-dlp."

    def add_arguments(self, parser):
        parser.add_argument(
            "url",
            nargs="?",
            default="",
            help="Third-party platform URL or share text",
        )
        parser.add_argument(
            "--provider",
            choices=("auto", "kukutool", "gallerydl", "ytdlp"),
            default="auto",
            help="Provider to inspect",
        )
        parser.add_argument(
            "--binary",
            default="",
            help="Provider executable command, e.g. '.venv/bin/python -m yt_dlp'",
        )
        parser.add_argument(
            "--timeout",
            type=float,
            default=0.0,
            help="Provider timeout override in seconds",
        )
        parser.add_argument(
            "--health",
            action="store_true",
            help="Print third-party provider health and platform coverage without parsing a URL",
        )
        parser.add_argument(
            "--show-urls",
            action="store_true",
            help="Include full provider media and diagnostic URLs in JSON output",
        )
        parser.add_argument("--json", action="store_true", dest="as_json", help="Output JSON")

    def handle(self, *args, **options):
        adapter = GenericThirdPartyAdapter()
        if options["health"]:
            error_payload = build_binary_override_error(options)
            if error_payload:
                if options["as_json"]:
                    self.stdout.write(json.dumps(
                        error_payload,
                        ensure_ascii=False,
                        indent=2,
                    ))
                    return
                raise CommandError(error_payload["errors"][0]["error"])
            apply_command_overrides(options)
            force_probe_provider_id = (
                PROVIDER_OPTION_IDS.get(options["provider"], "")
                if options["provider"] != "auto"
                else ""
            )
            report = build_health_report(adapter, force_probe_provider_id)
            if options["as_json"]:
                self.stdout.write(json.dumps(report, ensure_ascii=False, indent=2))
                return
            self.stdout.write(
                "third-party enabled: {enabled}, public access: {public}, staff only: {staff_only}".format(
                    enabled=report["settings"]["third_party_provider_enabled"],
                    public=report["settings"]["third_party_public_access_enabled"],
                    staff_only=report["settings"]["third_party_staff_only"],
                )
            )
            self.stdout.write(
                "enabled providers: {providers}".format(
                    providers=", ".join(report["enabled_provider_ids"]) or "-"
                )
            )
            self.stdout.write(
                "ready providers: {providers}".format(
                    providers=", ".join(report["ready_provider_ids"]) or "-"
                )
            )
            self.stdout.write(
                "rollout: {status} ({reason})".format(
                    status=report["rollout"]["status"],
                    reason=report["rollout"]["reason"],
                )
            )
            for issue in report["issues"]:
                self.stdout.write(
                    "issue: {code} {provider} {message}".format(
                        code=issue["code"],
                        provider=issue.get("provider", ""),
                        message=issue["message"],
                    )
                )
            self.stdout.write("platform coverage:")
            for rule in report["platform_rules"]:
                self.stdout.write(
                    "- {name} ({platform}): media={media}; providers={providers}; enabled={enabled}; ready={ready}".format(
                        name=rule["display_name"],
                        platform=rule["platform_id"],
                        media=", ".join(rule["media_types"]) or "-",
                        providers=", ".join(rule["provider_ids"]) or "-",
                        enabled=", ".join(rule["enabled_provider_ids"]) or "-",
                        ready=", ".join(rule["ready_provider_ids"]) or "-",
                    )
                )
            return

        url = options["url"]
        if not url:
            message = "URL is required unless --health is used."
            if options["as_json"]:
                self.stdout.write(json.dumps(
                    build_error_payload(
                        options["provider"],
                        "MissingUrl",
                        message,
                    ),
                    ensure_ascii=False,
                    indent=2,
                ))
                return
            raise CommandError(message)
        source_url = adapter.normalize_source_url(url)
        rule = adapter.get_rule_for_url(source_url)
        if not rule:
            message = "URL is not supported by the generic third-party adapter."
            if options["as_json"]:
                self.stdout.write(json.dumps(
                    build_error_payload(
                        options["provider"],
                        "UnsupportedUrl",
                        message,
                        source_url=source_url,
                    ),
                    ensure_ascii=False,
                    indent=2,
                ))
                return
            raise CommandError(message)

        error_payload = build_binary_override_error(
            options,
            source_url=source_url,
            platform_id=rule.platform_id,
        )
        if error_payload:
            if options["as_json"]:
                self.stdout.write(json.dumps(
                    error_payload,
                    ensure_ascii=False,
                    indent=2,
                ))
                return
            raise CommandError(error_payload["errors"][0]["error"])
        apply_command_overrides(options)

        selected_provider_id = PROVIDER_OPTION_IDS.get(options["provider"], "")
        providers = [
            spec
            for spec in adapter.iter_provider_specs(
                rule,
                respect_enabled=options["provider"] == "auto",
            )
            if not selected_provider_id or spec[0] == selected_provider_id
        ]

        if not providers:
            provider_error_type = "ProviderNotAllowed"
            if options["provider"] == "auto":
                provider_error_type = "NoEnabledProvider"
                message = (
                    f"No enabled provider is available for "
                    f"{rule.display_name} ({rule.platform_id}). "
                    f"Allowed providers: {', '.join(rule.provider_ids)}."
                )
            else:
                message = (
                    f"Provider {options['provider']} is not allowed for "
                    f"{rule.display_name} ({rule.platform_id})."
                )
            if options["as_json"]:
                self.stdout.write(json.dumps(
                    build_error_payload(
                        options["provider"],
                        provider_error_type,
                        message,
                        source_url=source_url,
                        platform_id=rule.platform_id,
                    ),
                    ensure_ascii=False,
                    indent=2,
                ))
                return
            raise CommandError(message)

        errors = []
        attempts = []
        started_at_total = time.monotonic()
        deadline = started_at_total + frontend_budget_seconds(rule)
        for provider_id, provider, normalizer in providers:
            provider_timeout_seconds = remaining_budget_seconds(deadline)
            if provider_timeout_seconds <= 0:
                errors.append({
                    "provider": provider_id,
                    "status": "timeout",
                    "error_type": "XhsProviderTimeoutError",
                    "error": "third-party provider command budget exhausted",
                    "duration_ms": 0,
                })
                break
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
                            rule.display_name,
                            source_url,
                            payload,
                            max_height=ytdlp_merge_default_max_height(),
                        )
                        selected_provider_id = YTDLP_MERGE_PROVIDER_ID
                    except Exception:
                        result = normalizer.normalize(
                            rule=rule,
                            platform_name=rule.display_name,
                            source_url=source_url,
                            payload=payload,
                        )
                else:
                    result = normalizer.normalize(
                        rule=rule,
                        platform_name=rule.display_name,
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
                result = result_with_provider_diagnostics(
                    result,
                    selected_provider_id,
                    duration_ms,
                    attempts,
                    total_duration_ms=elapsed_ms(started_at_total),
                )
                data = {
                    "ok": True,
                    "provider": selected_provider_id,
                    "source_url": source_url,
                    "platform_id": rule.platform_id,
                    "rule": rule.to_dict(),
                    "smoke": build_provider_smoke_summary(
                        result,
                        rule=rule,
                        attempts=attempts,
                    ),
                    "attempts": summarize_diagnostic_value(
                        attempts,
                        show_urls=bool(options["show_urls"]),
                    ),
                    "result": summarize_result(
                        result,
                        show_urls=bool(options["show_urls"]),
                    ),
                }
                if options["as_json"]:
                    self.stdout.write(json.dumps(data, ensure_ascii=False, indent=2))
                    return
                self.stdout.write(f"provider: {provider_id}")
                self.stdout.write(f"source_url: {source_url}")
                self.stdout.write(f"platform_id: {rule.platform_id}")
                self.stdout.write(f"platform: {rule.display_name} ({rule.platform_id})")
                self.stdout.write(f"post_type: {result.post_type}")
                self.stdout.write(f"base_filename: {result.base_filename}")
                self.stdout.write(f"candidate_count: {result.metadata.get('candidate_count', 0)}")
                self.stdout.write(
                    f"smoke_rollout: {data['smoke']['recommended_rollout']}"
                )
                self.stdout.write(
                    f"smoke_issues: {', '.join(data['smoke']['issues']) or '-'}"
                )
                self.stdout.write("attempts:")
                for attempt in attempts:
                    self.stdout.write(
                        "- {provider} {status} {duration_ms}ms".format(
                            provider=attempt["provider"],
                            status=attempt["status"],
                            duration_ms=attempt["duration_ms"],
                        )
                    )
                for index, asset in enumerate(result.assets, start=1):
                    self.stdout.write(
                        "#{index} {type} {quality} size={size} host={host}".format(
                            index=index,
                            type=asset.media_type,
                            quality=asset.quality_label,
                            size=asset.size_bytes,
                            host=urlparse(asset.url).hostname or "-",
                        )
                    )
                return
            except Exception as error:
                duration_ms = elapsed_ms(started_at)
                status = "timeout" if isinstance(error, XhsProviderTimeoutError) else "failed"
                attempts.append(
                    provider_attempt_record(provider_id, status, duration_ms, error)
                )
                errors.append({
                    "provider": provider_id,
                    "status": status,
                    "error_type": type(error).__name__,
                    "error": str(error),
                    "duration_ms": duration_ms,
                })

        if options["as_json"]:
            duration_ms = elapsed_ms(started_at_total)
            self.stdout.write(json.dumps(
                {
                    "ok": False,
                    "source_url": source_url,
                    "platform_id": rule.platform_id,
                    "smoke": build_provider_failure_smoke_summary(
                        rule=rule,
                        attempts=attempts,
                        errors=errors,
                        duration_ms=duration_ms,
                    ),
                    "errors": summarize_diagnostic_value(
                        errors,
                        show_urls=bool(options["show_urls"]),
                    ),
                    "attempts": summarize_diagnostic_value(
                        attempts,
                        show_urls=bool(options["show_urls"]),
                    ),
                },
                ensure_ascii=False,
                indent=2,
            ))
            return
        raise CommandError(
            "No provider returned a usable result: "
            + "; ".join(f"{item['provider']}={item['error_type']}" for item in errors)
        )
