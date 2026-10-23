"""Daily parse and bounded-prefix checks, not another download delivery path."""

import json
import re
import time

from django.contrib.auth.models import AnonymousUser
from django.http import HttpRequest

from .common import XhsDownloadError, XhsParseError
from .downloads import probe_media_prefix, reject_upstream_server_download
from .registry import parse_media_page


SMOKE_PLATFORMS = {
    "download_smoke_xhs_gallery": "xiaohongshu",
    "download_smoke_xhs_video": "xiaohongshu",
    "download_smoke_douyin": "douyin",
    "download_smoke_bilibili": "bilibili",
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


def complete_continuation(pending, request, observation):
    """Observe the same bounded continuation as the page, without parsing again."""
    from .douyin_continuation import (
        DOUYIN_CONTINUATION_MAX_POLLS, DOUYIN_CONTINUATION_POLL_AFTER_MS,
        DOUYIN_CONTINUATION_RESULT_GRACE_SECONDS, DOUYIN_CONTINUATION_TTL_SECONDS,
        DouyinContinuationError, issue_douyin_continuation_token, poll_douyin_continuation,
    )
    from .rate_limits import get_client_identity

    identity = get_client_identity(request)
    token = issue_douyin_continuation_token(
        task_id=pending.task_id, client_identity=identity, platform_key="home",
    )
    deadline = time.monotonic() + DOUYIN_CONTINUATION_TTL_SECONDS + DOUYIN_CONTINUATION_RESULT_GRACE_SECONDS
    delay = DOUYIN_CONTINUATION_POLL_AFTER_MS / 1000
    observation.update(status="timeout", error_code="DOUYIN_CONTINUATION_TIMEOUT", poll_count=0)
    for _ in range(DOUYIN_CONTINUATION_MAX_POLLS):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(delay, remaining))
        try:
            outcome, _ = poll_douyin_continuation(token, client_identity=identity)
        except DouyinContinuationError as error:
            observation.update(status="failed", error_code=error.error_code)
            break
        observation["poll_count"] += 1
        if outcome.status == "continuation_pending":
            delay = outcome.poll_after_ms / 1000
            continue
        observation.update(status=outcome.status, error_code=outcome.error_code)
        if outcome.status == "success" and outcome.result is not None:
            return outcome.result
        break
    # The page retains its already displayed result on failed/expired continuation.
    if pending.fallback_result is not None and pending.fallback_result.assets:
        return pending.fallback_result
    raise XhsParseError("SMOKE_CONTINUATION_UNAVAILABLE")


def parse_sample(request, source_url, platform, continuation):
    from ..views import ultra_hd_is_available
    from .douyin_continuation import DouyinContinuationPendingError
    from .env_config import runtime_default_int
    from .rate_limits import consume_fixed_window, get_client_identity

    options = {"prefer_ultra_hd": False, "probe_media_metadata": False}
    if platform == "douyin":
        from .douyin_ultra_attempts import DouyinUltraAttemptController
        options["prefer_ultra_hd"] = ultra_hd_is_available(request, platform)
        if options["prefer_ultra_hd"]:
            options["allow_continuation"] = True
            options["continuation_identity"] = get_client_identity(request)
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
    try:
        return parse_media_page(source_url, **options)
    except DouyinContinuationPendingError as pending:
        return complete_continuation(pending, request, continuation)


def default_resource(result, request):
    """Use the template's default selection and the action's original authority."""
    from ..templatetags.ytdlp_picker import remaining_picker_videos, ytdlp_presentation
    from ..views import build_result_display_context
    from ..ytdlp_selection_views import _admitted
    from . import service_format_selection, ytdlp_selection
    from .upstream_candidates import resolve_candidate_record

    display = build_result_display_context(result, probe_missing_video_sizes=False)
    presentation = ytdlp_presentation(
        display["upstream_candidates"], (result.metadata or {}).get("upstream_route"),
        display["video_items"], display["display_title"], display["base_filename"],
    )
    if presentation:
        for group in presentation.get("groups", [presentation]):
            selected = group.get("selected")
            if not selected or not (selected.get("can_download") or selected.get("can_legacy_download")):
                continue
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
            asset = next((asset for asset in result.assets
                          if item.get("asset_id") and asset.asset_id == item["asset_id"]), None)
            if asset is None:
                asset = next((asset for asset in result.assets if asset.url == item["url"]), None)
            return item, asset, selected
    # Match the ordinary result region: remaining video cards, then image cards.
    items = remaining_picker_videos(presentation, display["video_items"]) + display["image_items"]
    if not items:
        raise XhsParseError("SMOKE_RESOURCE_MISSING")
    item = items[0]
    asset = next((asset for asset in result.assets
                  if asset.url == item["url"] and asset.media_type == item["type"]), None)
    return {**item, "size_bytes": item.get("size_bytes") or getattr(asset, "size_bytes", 0)}, asset, {}


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

    platform = SMOKE_PLATFORMS[test_key]
    request = smoke_request(context)
    details = {
        "schema_version": 1, "platform": platform, "provider": "",
        "media_type": "", "continuation": {"status": "not_needed", "error_code": "", "poll_count": 0},
        "stage": "parse",
        "fields": {key: None for key in ("title", "cover", "quality", "size")},
        "access_scope": "staff" if getattr(request.user, "is_staff", False) else "anonymous_policy",
        "download": {"status": "not_run", "error_code": "PARSE_NOT_COMPLETED", "request_count": 0,
                     "bytes_received": 0},
    }
    try:
        result = parse_sample(request, source_url, platform, details["continuation"])
        metadata = result.metadata or {}
        first_asset = next(iter(result.assets), None)
        if first_asset is not None:
            details["provider"] = str(first_asset.provider or metadata.get("selected_provider")
                                      or metadata.get("source_provider") or "native")
        details["fields"] = field_coverage(result, first_asset, {})
        details["stage"] = "selection"
        item, asset, selected = default_resource(result, request)
        details["media_type"] = item["type"]
        details["provider"] = str(item.get("provider") or getattr(asset, "provider", "")
                                  or metadata.get("selected_provider") or metadata.get("source_provider") or "native")
        details["fields"] = field_coverage(result, asset, selected)
        details["stage"] = "admission"
        guard = check_download_cost_preflight(request, [item], get_download_runtime_setting())
        if guard:
            details["download"]["error_code"] = guard[0]
        else:
            try:
                reject_upstream_server_download(item)
            except XhsDownloadError:
                details["download"]["error_code"] = "SMOKE_BROWSER_ONLY_RESOURCE"
                return details
            if item["type"] not in {"image", "video"}:
                details["download"]["error_code"] = "SMOKE_MEDIA_TYPE_UNSUPPORTED"
                return details
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
        if str(error) in {"SMOKE_SELECTION_NOT_ADMITTED", "SMOKE_RESOURCE_MISSING",
                          "SMOKE_CONTINUATION_UNAVAILABLE"}:
            code = str(error)
        if str(error) == "SMOKE_CONTINUATION_UNAVAILABLE":
            terminal_code = details["continuation"]["error_code"]
            if re.fullmatch(r"DOUYIN_CONTINUATION_[A-Z_]{1,50}", terminal_code):
                code = terminal_code
        details["download"]["error_code"] = code or "SMOKE_PARSE_OR_SELECTION_FAILED"
        details["error_type"] = type(error).__name__
    return details
