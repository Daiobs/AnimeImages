"""One bounded track check for one already-issued, currently admitted candidate."""
from copy import deepcopy
from hashlib import sha256
import json
import math
import time

from django.core.cache import cache

from . import upstream_matching
from .common import XhsParseError

REASONS = frozenset({'metadata_unavailable', 'complete_track_table', 'url_rejected', 'deadline',
    'moov_not_found', 'range_rejected', 'invalid_box_size', 'moov_over_budget', 'request_budget',
    'incomplete_box', 'box_limit', 'incomplete_track_table', 'request_failed', 'execution_failed',
    'runtime_changed', 'runtime_unavailable'})


class CheckInProgress(ValueError):
    pass


def _key(record):
    binding = [record['candidate_id'], record['url'], record['source_digest'],
               record['provider_id'], record['match']]
    return 'ytdlp:selected-track-check:' + sha256(json.dumps(binding, sort_keys=True).encode()).hexdigest()


def _remaining(record):
    value = record.get('expires_at')
    if not isinstance(value, (int, float)) or not math.isfinite(value):
        raise XhsParseError('候选缺少有效期限，请重新解析。')
    remaining = value - time.time()
    if remaining <= 0:
        raise XhsParseError('候选资源已失效，请重新解析。')
    return remaining


def check_message(evidence):
    reason = evidence.get('reason') if isinstance(evidence, dict) else None
    if reason == 'complete_track_table':
        if evidence.get('video') == 'present' and evidence.get('audio') == 'present':
            return '已确认此格式含视频和音频，可尝试保存原文件。'
        if evidence.get('audio') == 'absent':
            return '检查确认此文件没有音轨，未按完整音视频开放保存。'
        return '检查未发现完整视频轨，未开放视频保存。'
    if reason == 'deadline':
        return '所选格式检查超时，未确认轨道；未自动重试。'
    if reason == 'range_rejected':
        return '来源未接受安全的分段读取或要求重定向，未确认轨道。'
    if reason == 'runtime_changed':
        return '运行版本与本次候选不一致，请重新解析；未执行媒体检查。'
    if reason == 'runtime_unavailable':
        return '本地检查执行器暂不可用，未确认媒体轨道。'
    if reason in {'moov_not_found', 'moov_over_budget', 'request_budget', 'incomplete_box',
                  'incomplete_track_table', 'invalid_box_size', 'box_limit'}:
        return '有限读取内未取得完整轨道信息，暂不能确认可保存；未下载整文件。'
    return '所选格式检查未完成，轨道仍未确认；未自动重试。'


def _evidence(value):
    if (not isinstance(value, dict) or value.get('schema') != 'iso_bmff_tracks_v1'
            or value.get('reason') not in REASONS or type(value.get('complete_moov')) is not bool
            or any(value.get(k) not in {'present', 'absent', 'unknown'} for k in ('video', 'audio'))
            or type(value.get('requests')) is not int or not 0 <= value['requests'] <= 2
            or type(value.get('bytes_read')) is not int or not 0 <= value['bytes_read'] <= 256 * 1024):
        raise ValueError('invalid_probe_protocol')
    return {k: value[k] for k in ('schema', 'reason', 'complete_moov', 'video', 'audio', 'requests', 'bytes_read')}


def apply_check_evidence(record):
    # Called only after the original signed token and private bundle resolve.
    # Never refresh that bundle or token, and never mutate native codec facts.
    if not record.get('expires_at') or record['expires_at'] <= time.time():
        return record
    saved = cache.get(_key(record))
    if not isinstance(saved, dict) or saved.get('state') != 'done':
        return record
    evidence = saved['evidence']
    result = deepcopy(record)
    public = result['public']
    public['selected_track_check'] = evidence
    if evidence.get('complete_moov') is True and evidence.get('reason') == 'complete_track_table':
        public['audio_tracks'], public['video_tracks'] = evidence['audio'], evidence['video']
        if evidence['audio'] == evidence['video'] == 'present':
            public['delivery_state'] = 'complete'
        elif evidence['video'] == 'present' and evidence['audio'] == 'absent':
            public['delivery_state'] = 'silent_video'
    return result


def check_selected_record(record):
    from .quality_execution import runtime_version_supported
    from .ytdlp_selection import PROVIDER, capability, context_required
    remaining = _remaining(record)
    existing = apply_check_evidence(record)
    if existing['public'].get('selected_track_check'):
        existing['_check_cached'] = True
        return existing
    if capability(record['public'])['can_download']:
        return record
    if (record['provider_id'] != PROVIDER or not capability(record['public'])['can_check']
            or context_required(record.get('headers'), headers_present=record.get('headers_present'),
                                requires_cookie=record.get('requires_cookie'))):
        raise ValueError('selected_format_not_probe_eligible')
    # LocMemCache is thread-atomic locally; production requires shared Redis.
    # Keep an interrupted claim until original candidate expiry: never retry an
    # unknown check or overlap it, and never extend the original authority.
    key = _key(record)
    if not cache.add(key, {'state': 'pending'}, timeout=remaining):
        existing = apply_check_evidence(record)
        if existing['public'].get('selected_track_check'):
            existing['_check_cached'] = True
            return existing
        raise CheckInProgress('selected_check_in_progress')
    try:
        command, version, _ = upstream_matching._context(PROVIDER)
        if not runtime_version_supported(PROVIDER, version) or record['match'].get('version') != version:
            raise ValueError('selected_runtime_changed')
        evidence = _evidence(upstream_matching._call(command, {
            'action': 'probe_tracks', 'provider_id': PROVIDER, 'version': version, 'url': record['url'],
        }, timeout=6.0))
    except Exception as error:
        reason = 'execution_failed'
        if isinstance(error, ValueError) and str(error) == 'selected_runtime_changed':
            reason = 'runtime_changed'
        elif isinstance(error, upstream_matching.UpstreamMatchingError):
            reason = ('deadline' if error.code in {'execution_timeout', 'network_timeout'} else
                      'runtime_changed' if error.code == 'version_changed' else
                      'runtime_unavailable' if error.code in {'executor_unavailable', 'unsupported_executor'} else
                      'execution_failed')
        evidence = dict(schema='iso_bmff_tracks_v1', reason=reason, complete_moov=False,
                        video='unknown', audio='unknown', requests=None, bytes_read=None)
    # An expired candidate cannot acquire new facts or download authority.
    cache.set(key, {'state': 'done', 'evidence': evidence}, timeout=_remaining(record))
    return apply_check_evidence(record)
