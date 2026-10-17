"""Resolve a selected issued yt-dlp candidate for browser-only use.

Not a media proxy, extractor, format-selector interpreter or persistent grant.
Every action rechecks current source/Provider admission and the private cache.
"""
import json
import logging
from urllib.parse import urlsplit

from django.http import JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_POST

from .services.common import XhsParseError
from .services.upstream_candidates import resolve_candidate_record
from .services.ytdlp_selection import PROVIDER, selected_descriptor, selected_link_descriptor

logger = logging.getLogger(__name__)


def _response(data, status=200):
    response = JsonResponse(data, status=status)
    response['Cache-Control'] = 'private, no-store'
    response['X-Content-Type-Options'] = 'nosniff'
    return response


def _admitted(request, record):
    # Same current admission conditions as the existing candidate information
    # endpoint; a cached parse never substitutes for current public access.
    from .services.provider_access import (
        load_runtime_setting, request_user_is_staff, third_party_public_access_allowed,
        upstream_match_is_blocked, upstream_match_is_public_allowed,
    )
    from .services.platforms.third_party import provider_is_enabled
    from .services.platforms.xiaohongshu import XiaohongshuAdapter
    from .services.platforms.xhs_independent import authorized_services
    runtime = load_runtime_setting()
    source, match = record['source_url'], record['match']
    allowed_source = (PROVIDER in authorized_services(request, source, reserve_limits=False)
                      if XiaohongshuAdapter().supports_url(source)
                      else third_party_public_access_allowed(source, request=request, runtime_setting=runtime))
    return bool(record['provider_id'] == PROVIDER and match.get('provider_id') == PROVIDER
                and provider_is_enabled(PROVIDER)
                and not upstream_match_is_blocked(match, runtime) and allowed_source
                and (request_user_is_staff(request) or upstream_match_is_public_allowed(
                    match, urlsplit(source).hostname, runtime)))


@never_cache
@require_POST
@csrf_protect
def ytdlp_selected_format_view(request):
    try:
        if len(request.body) > 8192:
            raise ValueError()
        data = json.loads(request.body)
        if (not isinstance(data, dict) or set(data) != {'candidate_id', 'resource_token', 'action'}
                or data.get('action') not in {'download', 'preview', 'cover', 'link'}
                or not all(isinstance(data.get(k), str) for k in ('candidate_id', 'resource_token'))):
            return _response({'status': 'error', 'message': '所选格式请求无效。'}, 400)
        record = resolve_candidate_record(data['resource_token'], data['candidate_id'])
        if not _admitted(request, record):
            raise ValueError()
    except (ValueError, KeyError, TypeError, UnicodeError, XhsParseError):
        return _response({'status': 'error', 'message': '格式信息已失效或当前无权使用，请重新解析。'}, 403)
    except Exception as error:
        logger.warning('ytdlp_selection_admission_failed exception_class=%s', type(error).__name__)
        return _response({'status': 'error', 'message': '格式授权服务暂时不可用，请稍后再试。'}, 503)

    from . import views
    try:
        if data['action'] == 'link':
            from .services.rate_limits import consume_fixed_window, get_client_identity
            limit = consume_fixed_window('ytdlp-link-descriptor', get_client_identity(request), 120, 60)
            if not limit.allowed:
                response = _response({'status': 'error', 'message': '链接请求过于频繁。'}, 429)
                response['Retry-After'] = str(limit.retry_after)
                return response
            return _response({'status': 'success', 'item': selected_link_descriptor(record),
                              'server_media_relay': False})
        if data['action'] == 'cover':
            from .services.upstream_delivery import safe_resource_url
            from .services.rate_limits import consume_fixed_window, get_client_identity
            limit = consume_fixed_window('ytdlp-cover-descriptor', get_client_identity(request), 60, 60)
            if not limit.allowed:
                response = _response({'status': 'error', 'message': '封面请求过于频繁。'}, 429)
                response['Retry-After'] = str(limit.retry_after)
                return response
            work = record.get('work') or {}
            url = safe_resource_url(work.get('cover_url'))
            if not url:
                return _response({'status': 'error', 'message': '暂无可用封面。'}, 409)
            return _response({'status': 'success', 'url': url})
        item = selected_descriptor(record)
        guard = views.check_download_cost_preflight(request, [item], views.get_download_runtime_setting())
        if guard:
            code, message, status = guard
            return _response({'status': 'error', 'error_code': code, 'message': message}, status)
        _, limit_response = views.check_download_request_limits(request)
        if limit_response is not None:
            limit_response['Cache-Control'] = 'private, no-store'
            return limit_response
        if data['action'] == 'preview':
            from .services.ytdlp_selection import capability
            if not capability(record['public'])['can_preview']:
                return _response({'status': 'error', 'message': '此格式暂不支持内嵌预览。'}, 409)
        # Issuing information is not proof that a file was downloaded or saved.
        logger.info('ytdlp_selected_descriptor_issued action=%s candidate_id=%s', data['action'], record['candidate_id'])
        return _response({'status': 'success', 'item': item, 'server_media_relay': False})
    except (ValueError, KeyError, TypeError, XhsParseError):
        return _response({'status': 'error', 'message': '此格式暂不能直接保存；请查看格式限制或选择其他格式。'}, 409)
    except Exception as error:
        logger.warning('ytdlp_selected_descriptor_failed exception_class=%s', type(error).__name__)
        return _response({'status': 'error', 'message': '资源准备服务暂时不可用，请稍后再试。'}, 503)
