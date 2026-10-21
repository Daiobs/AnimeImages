"""Daily parse and bounded-prefix checks, not another download delivery path."""

import json
import re

from django.contrib.auth.models import AnonymousUser
from django.http import HttpRequest

from .common import XhsParseError
from .downloads import probe_media_prefix
from .registry import parse_media_page


SMOKE_PLATFORMS = {
    "download_smoke_xhs_gallery": ("xiaohongshu", "image"),
    "download_smoke_xhs_video": ("xiaohongshu", "video"),
    "download_smoke_douyin": ("douyin", "video"),
    "download_smoke_bilibili": ("bilibili", "video"),
}


def smoke_request(context):
    if context.access_request is not None:
        return context.access_request
    # A scheduled job does not inherit the suite's audit-only is_staff flag.
    request = HttpRequest()
    request.user = AnonymousUser()
    request.META["REMOTE_ADDR"] = "127.0.0.1"
    request.request_id = context.request_id
    return request


def parse_sample(request, source_url, platform):
    from ..views import ultra_hd_is_available
    from .env_config import runtime_default_int
    from .rate_limits import consume_fixed_window, get_client_identity

    options = {"prefer_ultra_hd": False, "probe_media_metadata": False}
    if platform == "douyin":
        from .douyin_ultra_attempts import DouyinUltraAttemptController
        options["prefer_ultra_hd"] = ultra_hd_is_available(request, platform)
        if options["prefer_ultra_hd"]:
            options["ultra_attempt_controller"] = DouyinUltraAttemptController(
                client_identity=get_client_identity(request),
                limit=runtime_default_int("ULTRA_HD_RATE_LIMIT_PER_MINUTE", 10),
                consume=consume_fixed_window,
            )
    elif platform == "xiaohongshu":
        from .platforms.xhs_independent import authorized_services
        options["xhs_service_provider_ids"] = authorized_services(request, source_url)
    elif platform == "bilibili":
        from .platforms.bilibili_fallback import authorized_services
        if authorized_services(request, source_url, reserve_limits=False):
            options["bilibili_service_authorizer"] = lambda: authorized_services(request, source_url)
    return parse_media_page(source_url, **options)


def default_resource(result, media_type, request):
    """Use the template's default selection and the action's original authority."""
    from ..templatetags.ytdlp_picker import ytdlp_presentation
    from ..ytdlp_selection_views import _admitted
    from . import service_format_selection, ytdlp_selection
    from .upstream_candidates import resolve_candidate_record

    metadata = result.metadata or {}
    candidates = metadata.get("upstream_candidates") or []
    presentation = ytdlp_presentation(candidates, metadata.get("upstream_route"))
    if media_type == "video" and presentation:
        group = next((group for group in presentation.get("groups", [presentation])
                      if group.get("videos")), None)
        if group:
            selected = group["selected"]
            record = resolve_candidate_record(selected["resource_token"], selected["candidate_id"])
            provider = record["provider_id"]
            service = provider in service_format_selection.PROVIDERS
            if not _admitted(request, record, provider=provider, service=service):
                raise XhsParseError("SMOKE_SELECTION_NOT_ADMITTED")
            selection = service_format_selection if service else ytdlp_selection
            if service and selected.get("can_legacy_download"):
                item = selection.selected_legacy_descriptor(record)
            else:
                item = selection.selected_descriptor(record)
            asset = next((asset for asset in result.assets if asset.url == item["url"]), None)
            return item, asset, selected
    asset = next((asset for asset in result.assets if asset.media_type == media_type), None)
    if asset is None:
        raise XhsParseError("SMOKE_RESOURCE_MISSING")
    return {**asset.to_download_item(), "size_bytes": asset.size_bytes}, asset, {}


def field_coverage(result, asset, selected):
    metadata = result.metadata or {}
    title = str(metadata.get("title") or result.base_filename or "").strip()
    placeholder_titles = {"", "video", "media", "bilibili", "douyin", "xhs", "xiaohongshu"}
    quality = str(getattr(asset, "quality_label", "") or selected.get("quality_label") or "").strip()
    return {
        "title": title.lower() not in placeholder_titles and not bool(
            re.fullmatch(r"(?:douyin|bilibili|xiaohongshu|xhs)_[A-Za-z0-9]+", title)),
        "cover": bool(getattr(asset, "cover_url", "") or metadata.get("cover_url") or metadata.get("cover")
                      or selected.get("work", {}).get("cover_url")
                      or (getattr(asset, "media_type", "") == "image" and asset.url)),
        "quality": bool(selected.get("height")) or quality.lower() not in {"", "video", "default", "默认清晰度", "原始文件"},
        "size": bool(getattr(asset, "size_bytes", 0) or selected.get("size_bytes")),
    }


def run_download_smoke(context, test_key, source_url):
    from ..views import (
        check_download_cost_preflight, check_download_request_limits,
        get_download_runtime_setting, record_download_archive_bytes,
    )
    from .provider_lab.contracts import safe_error_detail

    platform, media_type = SMOKE_PLATFORMS[test_key]
    request = smoke_request(context)
    details = {
        "schema_version": 1, "platform": platform, "provider": "",
        "stage": "parse",
        "fields": {key: None for key in ("title", "cover", "quality", "size")},
        "access_scope": "staff" if getattr(request.user, "is_staff", False) else "anonymous_policy",
        "download": {"status": "not_run", "error_code": "PARSE_NOT_COMPLETED", "request_count": 0,
                     "bytes_received": 0},
    }
    try:
        result = parse_sample(request, source_url, platform)
        if result.platform != platform:
            raise XhsParseError("SMOKE_PLATFORM_MISMATCH")
        metadata = result.metadata or {}
        first_asset = next((asset for asset in result.assets if asset.media_type == media_type), None)
        details["provider"] = str(getattr(first_asset, "provider", "") or metadata.get("selected_provider")
                                  or metadata.get("source_provider") or "native")
        details["fields"] = field_coverage(result, first_asset, {})
        details["stage"] = "selection"
        item, asset, selected = default_resource(result, media_type, request)
        details["provider"] = str(item.get("provider") or getattr(asset, "provider", "")
                                  or metadata.get("selected_provider") or metadata.get("source_provider") or "native")
        details["fields"] = field_coverage(result, asset, selected)
        details["stage"] = "admission"
        guard = check_download_cost_preflight(request, [item], get_download_runtime_setting())
        if guard:
            details["download"]["error_code"] = guard[0]
        else:
            identity, limit_response = check_download_request_limits(request)
            if limit_response is not None:
                try:
                    details["download"]["error_code"] = json.loads(limit_response.content)["error_code"]
                finally:
                    limit_response.close()
                return details
            details["stage"] = "download"
            details["download"] = probe_media_prefix(item)
            try:
                allowed, code = record_download_archive_bytes(
                    identity, None, archive_size=details["download"]["bytes_received"],
                )
            except Exception:
                details["download"].update(status="failed", error_code="QUOTA_ACCOUNTING_FAILED")
                return details
            if not allowed:
                details["download"].update(status="failed", error_code=code)
    except Exception as error:
        # Do not put exception URLs, signed resource identities or headers in history.
        code = safe_error_detail(getattr(error, "diagnostic_detail", ""))
        http_status = getattr(error, "http_status", None)
        if not code and details["stage"] == "parse" and type(http_status) is int and 100 <= http_status <= 599:
            code = f"PARSE_HTTP_{http_status}"
        if str(error) in {"SMOKE_SELECTION_NOT_ADMITTED", "SMOKE_RESOURCE_MISSING", "SMOKE_PLATFORM_MISMATCH"}:
            code = str(error)
        details["download"]["error_code"] = code or "SMOKE_PARSE_OR_SELECTION_FAILED"
        details["error_type"] = type(error).__name__
    return details
