"""Selection endpoint for unified-pipeline quality pickers.

Reached through /media/service-selected-format/ (see is_unified_request), so
no new Nginx location is needed. Resolves a signed reference to a server-side
selection record and returns what the browser needs. It never relays media:
"download" returns an ordinary browser video item, "link" and "open" return
the variant's public URL.
"""
import json
import logging

from django.http import JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_POST

from django.core import signing

from .services.unified import routes
from .services.unified.presentation import (
    RECORD_TTL_SECONDS, SALT, download_item, link_item, resolve_selection,
)

logger = logging.getLogger(__name__)

ACTIONS = frozenset({"download", "link", "open"})


def _response(data, status=200):
    response = JsonResponse(data, status=status)
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response


def _audit_rejected(request, data, error_code):
    if isinstance(data, dict) and data.get("action") == "download":
        from .services.download_audit import record_transfer_audit
        record_transfer_audit(request, phase="failed", download_type="selected_preflight", error_code=error_code)


def is_unified_request(request):
    """Whether the posted reference was issued by a unified picker."""
    try:
        if request.method != "POST" or len(request.body) > 8192:
            return False
        data = json.loads(request.body)
        token = data.get("resource_token") if isinstance(data, dict) else None
        if not isinstance(token, str):
            return False
        signing.loads(token, salt=SALT, max_age=RECORD_TTL_SECONDS)
    except (ValueError, TypeError, UnicodeError, signing.BadSignature):
        return False
    return True


@never_cache
@require_POST
@csrf_protect
def unified_selected_format_view(request):
    data = None
    try:
        if len(request.body) > 8192:
            raise ValueError("body_too_large")
        data = json.loads(request.body)
        if (not isinstance(data, dict) or set(data) != {"candidate_id", "resource_token", "action"}
                or data.get("action") not in ACTIONS
                or not all(isinstance(data.get(key), str) for key in ("candidate_id", "resource_token"))):
            return _response({"status": "error", "message": "所选格式请求无效。"}, 400)
        record = resolve_selection(data["resource_token"], data["candidate_id"])
        # Switching a platform back also retires the pickers it issued.
        if routes.load_route(record["platform"]) is None:
            raise ValueError("unified_route_disabled")
    except (ValueError, KeyError, TypeError, UnicodeError) as error:
        _audit_rejected(request, data, "SELECTED_FORMAT_UNAUTHORIZED_OR_EXPIRED")
        logger.info("unified_selection_rejected reason=%s", type(error).__name__)
        return _response({"status": "error", "message": "格式信息已失效，请重新解析。"}, 403)

    from . import views
    from .services.rate_limits import consume_fixed_window, get_client_identity
    try:
        if data["action"] in {"link", "open"}:
            limit = consume_fixed_window("unified-link-descriptor", get_client_identity(request), 120, 60)
            if not limit.allowed:
                response = _response({"status": "error", "message": "链接请求过于频繁。"}, 429)
                response["Retry-After"] = str(limit.retry_after)
                return response
            if data["action"] == "open" and record["kind"] != "video":
                raise ValueError("unified_open_not_video")
            return _response({"status": "success", "item": link_item(record), "server_media_relay": False})
        download_item(record)  # Refuses formats the browser cannot save before any request.
        from .services.unified.transport import browser_readable_link, request_origin
        readable = browser_readable_link(record, request_origin(request))
        if readable is None:
            _audit_rejected(request, data, "SELECTED_FORMAT_BROWSER_UNREADABLE")
            return _response({"status": "error", "error_code": "SELECTED_FORMAT_BROWSER_UNREADABLE",
                              "message": "浏览器暂时无法读取此格式，请选择其他清晰度或稍后重试。"}, 409)
        url, size = readable
        item = download_item({**record, "url": url, "size_bytes": record["size_bytes"] or size,
                              "size_exact": record["size_exact"] or bool(size)})
        guard = views.check_download_cost_preflight(request, [item], views.get_download_runtime_setting())
        if guard:
            code, message, status = guard
            _audit_rejected(request, data, code)
            return _response({"status": "error", "error_code": code, "message": message}, status)
        _, limit_response = views.check_download_request_limits(request)
        if limit_response is not None:
            _audit_rejected(request, data, "SELECTED_FORMAT_RATE_LIMITED")
            limit_response["Cache-Control"] = "private, no-store"
            return limit_response
        from .services.download_audit import issue_browser_download_audit
        from .services.provider_strategy import prepare_browser_transport
        item = issue_browser_download_audit(request, prepare_browser_transport(item))
        logger.info("unified_selection_issued platform=%s provider=%s candidate_id=%s",
                    record["platform"], record["provider"], record["candidate_id"])
        return _response({"status": "success", "item": item, "server_media_relay": False})
    except (ValueError, KeyError, TypeError):
        _audit_rejected(request, data, "SELECTED_FORMAT_UNAVAILABLE")
        return _response({"status": "error", "message": "此格式暂不能直接保存；请选择其他格式。"}, 409)
    except Exception as error:
        _audit_rejected(request, data, "SELECTED_FORMAT_PREPARATION_UNAVAILABLE")
        logger.warning("unified_selection_failed exception_class=%s", type(error).__name__)
        return _response({"status": "error", "message": "资源准备服务暂时不可用，请稍后再试。"}, 503)
