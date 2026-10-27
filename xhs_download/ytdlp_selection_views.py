"""Resolve issued candidates for browser actions or their original delivery.

Not a media proxy, extractor, format-selector interpreter or persistent grant.
Every action rechecks current source/Provider admission and the private cache.
"""
import json
import logging
from time import time
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


def _audit_rejected_download(request, data, error_code):
    if (not isinstance(data, dict) or not isinstance(data.get('action'), str)
            or data['action'] not in {'download', 'legacy'}):
        return
    from .services.download_audit import record_transfer_audit
    # Before authorization, no client-supplied resource identity is trusted.
    record_transfer_audit(request, phase='failed', download_type='selected_preflight',
                          error_code=error_code)


def _admitted(request, record, *, provider=PROVIDER, service=False):
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
    if service and provider in {'tiantianjiexi', 'kukutool'}:
        from .services.platforms.douyin import DouyinAdapter
        from .services.provider_access import PROVIDER_DOUYIN_ULTRA, provider_is_allowed_for_request
        from .services.provider_settings import get_platform_provider_settings
        from .services.service_format_selection import source_identity_matches
        return bool(record['provider_id'] == provider and match.get('provider_id') == provider
                    and match.get('extractor_id') == 'douyin:' + provider
                    and DouyinAdapter().supports_url(source)
                    and source_identity_matches(provider, source, record.get('dedicated_resource_id'))
                    and provider_is_allowed_for_request(PROVIDER_DOUYIN_ULTRA, request)
                    and get_platform_provider_settings('douyin').provider_is_enabled(provider))
    if service:
        from .services.provider_access import third_party_public_access_summary
        from .services.platforms.third_party import GenericThirdPartyAdapter
        from .services.platforms.bilibili_fallback import canonical_video_source, authorized_services as bilibili_services
        if canonical_video_source(source):
            allowed = provider in bilibili_services(request, source, reserve_limits=False)
        elif XiaohongshuAdapter().supports_url(source):
            allowed = provider in authorized_services(request, source, reserve_limits=False)
        else:
            summary = third_party_public_access_summary(source, request, runtime)
            if summary.get('platform_id') == 'upstream':
                allowed = summary.get('allowed') and provider in summary.get('upstream_provider_ids', ())
            else:
                rule = GenericThirdPartyAdapter().get_rule_for_url(source)
                allowed = summary.get('allowed') and rule is not None and provider in rule.provider_ids
        return bool(allowed and record['provider_id'] == provider and match.get('provider_id') == provider
                    and provider_is_enabled(provider) and not upstream_match_is_blocked(match, runtime))
    allowed_source = (provider in authorized_services(request, source, reserve_limits=False)
                      if XiaohongshuAdapter().supports_url(source)
                      else third_party_public_access_allowed(source, request=request, runtime_setting=runtime))
    return bool(record['provider_id'] == provider and match.get('provider_id') == provider
                and provider_is_enabled(provider)
                and not upstream_match_is_blocked(match, runtime) and allowed_source
                and (request_user_is_staff(request) or upstream_match_is_public_allowed(
                    match, urlsplit(source).hostname, runtime)))


@never_cache
@require_POST
@csrf_protect
def ytdlp_selected_format_view(request, *, service=False):
    data = None
    if service:
        from .services import service_format_selection as selection
        allowed_actions = {'download', 'preview', 'cover', 'link', 'open', 'legacy'}
    else:
        from .services import ytdlp_selection as selection
        allowed_actions = {'download', 'preview', 'cover', 'link', 'open', 'check'}
    try:
        if len(request.body) > 8192:
            raise ValueError()
        data = json.loads(request.body)
        if (not isinstance(data, dict) or set(data) != {'candidate_id', 'resource_token', 'action'}
                or data.get('action') not in allowed_actions
                or not all(isinstance(data.get(k), str) for k in ('candidate_id', 'resource_token'))):
            return _response({'status': 'error', 'message': '所选格式请求无效。'}, 400)
        record = resolve_candidate_record(data['resource_token'], data['candidate_id'])
        provider = record.get('provider_id') if service else PROVIDER
        if (service and provider not in selection.PROVIDERS) or not _admitted(request, record, provider=provider, service=service):
            raise ValueError()
    except (ValueError, KeyError, TypeError, UnicodeError, XhsParseError):
        _audit_rejected_download(request, data, 'SELECTED_FORMAT_UNAUTHORIZED_OR_EXPIRED')
        return _response({'status': 'error', 'message': '格式信息已失效或当前无权使用，请重新解析。'}, 403)
    except Exception as error:
        _audit_rejected_download(request, data, 'SELECTED_FORMAT_ADMISSION_UNAVAILABLE')
        logger.warning('ytdlp_selection_admission_failed exception_class=%s', type(error).__name__)
        return _response({'status': 'error', 'message': '格式授权服务暂时不可用，请稍后再试。'}, 503)

    from . import views
    try:
        if data['action'] == 'check':
            from .services.ytdlp_format_check import CheckInProgress, check_selected_record
            from .services.ytdlp_selection import choice
            from .services.rate_limits import consume_fixed_window, get_client_identity
            public = record['public']
            guard = views.check_download_cost_preflight(request, [{
                'type': public.get('media_type'), 'size_bytes': public.get('size_bytes') or 0,
            }], views.get_download_runtime_setting())
            if guard:
                code, message, status = guard
                return _response({'status': 'error', 'error_code': code, 'message': message}, status)
            limit = consume_fixed_window('ytdlp-selected-check', get_client_identity(request), 12, 60)
            if not limit.allowed:
                response = _response({'status': 'error', 'message': '格式检查过于频繁，请稍后再试。'}, 429)
                response['Retry-After'] = str(limit.retry_after)
                return response
            try:
                checked = check_selected_record(record)
            except CheckInProgress:
                return _response({'status': 'error', 'message': '该格式已在检查或结果尚未确认，请勿重复提交。'}, 409)
            except XhsParseError:
                return _response({'status': 'error', 'message': '格式信息已失效，请重新解析。'}, 403)
            # Recheck admission and original expiry after the bounded child.
            try:
                current = resolve_candidate_record(data['resource_token'], data['candidate_id'])
            except XhsParseError:
                return _response({'status': 'error', 'message': '格式信息已失效，请重新解析。'}, 403)
            if not _admitted(request, current):
                return _response({'status': 'error', 'message': '当前无权使用此格式。'}, 403)
            projected = choice(checked['public'])
            logger.info('ytdlp_selected_track_checked candidate_id=%s reason=%s requests=%s bytes_read=%s cached=%s',
                        record['candidate_id'], projected.get('selected_track_check', {}).get('reason', 'already_complete'),
                        projected.get('selected_track_check', {}).get('requests', 0),
                        projected.get('selected_track_check', {}).get('bytes_read', 0), checked.get('_check_cached', False))
            return _response({'status': 'success', 'candidate': projected,
                              'check': projected.get('selected_track_check', {
                                  'reason': 'already_complete', 'requests': 0, 'bytes_read': 0}),
                              'check_cached': checked.get('_check_cached', False), 'server_media_relay': False})
        if data['action'] in {'link', 'open'}:
            from .services.rate_limits import consume_fixed_window, get_client_identity
            limit = consume_fixed_window('ytdlp-link-descriptor', get_client_identity(request), 120, 60)
            if not limit.allowed:
                response = _response({'status': 'error', 'message': '链接请求过于频繁。'}, 429)
                response['Retry-After'] = str(limit.retry_after)
                return response
            item = (selection.selected_open_descriptor(record) if data['action'] == 'open'
                    else selection.selected_link_descriptor(record))
            return _response({'status': 'success', 'item': item,
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
        item = (selection.selected_legacy_descriptor(record) if service and data['action'] == 'legacy'
                else selection.selected_descriptor(record))
        guard = views.check_download_cost_preflight(request, [item], views.get_download_runtime_setting())
        if guard:
            code, message, status = guard
            _audit_rejected_download(request, data, code)
            return _response({'status': 'error', 'error_code': code, 'message': message}, status)
        _, limit_response = views.check_download_request_limits(request)
        if limit_response is not None:
            _audit_rejected_download(request, data, 'SELECTED_FORMAT_RATE_LIMITED')
            limit_response['Cache-Control'] = 'private, no-store'
            return limit_response
        if data['action'] == 'preview':
            if not selection.capability(record['public'])['can_preview']:
                return _response({'status': 'error', 'message': '此格式暂不支持内嵌预览。'}, 409)
        if data['action'] == 'download' and item.get('type') == 'video':
            from .services.provider_strategy import prepare_browser_transport
            item = prepare_browser_transport(item, request_context=record)
            # Metadata is bounded, but may finish after the original grant.
            # Do not renew the candidate or reuse a prior admission decision.
            current = resolve_candidate_record(data['resource_token'], data['candidate_id'])
            if not _admitted(request, current, provider=provider, service=service):
                _audit_rejected_download(request, data, 'SELECTED_FORMAT_UNAUTHORIZED_OR_EXPIRED')
                return _response({'status': 'error', 'message': '当前无权使用此格式。'}, 403)
            if item.get('selection_expires_at', 0) <= time() * 1000:
                raise ValueError('selected_format_expired')
        # Issuing information is not proof that a file was downloaded or saved.
        if data['action'] == 'download' and item.get('selection_delivery') != 'legacy_manifest':
            from .services.download_audit import issue_browser_download_audit
            item = issue_browser_download_audit(request, item)
        logger.info('ytdlp_selected_descriptor_issued action=%s candidate_id=%s', data['action'], record['candidate_id'])
        return _response({'status': 'success', 'item': item,
                          'server_media_relay': None if item.get('selection_delivery') == 'legacy_manifest' else False})
    except (ValueError, KeyError, TypeError, XhsParseError) as error:
        _audit_rejected_download(request, data, 'SELECTED_FORMAT_UNAVAILABLE')
        message = ('视频链接已过期，请重新解析。' if data['action'] == 'open' and str(error) == 'selected_video_navigation_expired'
                   else '此格式暂不能在新标签页播放。' if data['action'] == 'open'
                   else '此格式暂不能直接保存；请查看格式限制或选择其他格式。')
        return _response({'status': 'error', 'message': message}, 409)
    except Exception as error:
        _audit_rejected_download(request, data, 'SELECTED_FORMAT_PREPARATION_UNAVAILABLE')
        logger.warning('ytdlp_selected_descriptor_failed exception_class=%s', type(error).__name__)
        return _response({'status': 'error', 'message': '资源准备服务暂时不可用，请稍后再试。'}, 503)


def service_selected_format_view(request):
    # Unified pickers share this Nginx-exposed path. Their references carry a
    # salt of their own, so neither kind of token validates as the other.
    from .unified_views import is_unified_request, unified_selected_format_view
    if is_unified_request(request):
        return unified_selected_format_view(request)
    return ytdlp_selected_format_view(request, service=True)
