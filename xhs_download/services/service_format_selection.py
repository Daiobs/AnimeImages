"""Small service-result adapter for the existing exact-candidate picker.

Service file declarations retain their existing delivery semantics. They are
not yt-dlp track evidence, and never establish codecs or complete audio tracks.
"""
from dataclasses import replace
import re
from urllib.parse import urlsplit

from . import ytdlp_selection as picker
from .platforms.gallerydl_generic import extension_from_url

PROVIDERS = frozenset({'tiantianjiexi_generic', 'kukutool_generic'})
SCHEMA = 'service_selected_format_v1'


def public_service_work(work):
    return {**picker.public_work(work), 'schema': SCHEMA}


def candidate_facts(metadata, url, kind):
    return {
        'service_file_eligible': metadata.get('_service_picker_issued') is True,
        'download_context_required': metadata.get('requires_context') is True or picker.context_required(
            metadata.get('http_headers') or metadata.get('headers'),
            headers_present=bool(metadata.get('headers_present')),
            requires_cookie=metadata.get('requires_cookie') is True),
        'has_drm': metadata.get('has_drm') is True,
        'same_resource_labels': metadata.get('_same_resource_labels') is True,
    }


def capability(candidate):
    # Apply the same transport/context restrictions, but do not demand missing
    # optional codec facts from a service's already-supported file declaration.
    result = picker.capability(candidate)
    if (candidate.get('service_file_eligible') is True
            and candidate.get('protocol') == 'https'
            and not candidate.get('has_drm')
            and not candidate.get('requires_merge')
            and not candidate.get('download_context_required')
            and candidate.get('delivery_state') not in {
                'streaming_media', 'separate_media_tracks', 'silent_video', 'insecure_reference'}
            and not (candidate.get('media_type') == 'video' and candidate.get('audio_tracks') == 'absent')
            and str(candidate.get('extension') or '').lower() in ({''} | (
                picker.VIDEO_EXTENSIONS if candidate.get('media_type') == 'video' else picker.AUDIO_EXTENSIONS))
            and candidate.get('media_type') in {'video', 'audio'}):
        result = {'can_download': True, 'can_preview': False, 'can_check': False, 'limitation': ''}
    # Service declarations do not borrow a yt-dlp runtime identity for probing.
    result['can_check'] = False
    if not result['can_download'] and '尚未检查' in result['limitation']:
        result['limitation'] = picker.LIMITATIONS['unknown']
    return result


def choice(candidate):
    result = {**picker.choice(candidate), **capability(candidate)}
    if candidate.get('format_label'):
        result['label'] = candidate['format_label']
        result['detail_label'] += ' · 上游标注'
    if candidate.get('same_resource_labels'):
        result['detail_label'] += ' · 同一资源的不同标签'
    return result


def build_service_picker(candidates, preferences=None):
    if not candidates or any(c.get('provider_id') not in PROVIDERS for c in candidates):
        return None
    if len({c['provider_id'] for c in candidates}) != 1:
        return None
    # Mixed/ambiguous media keeps the existing resource UI, not a fake gallery
    # of quality levels. The candidate details and identities remain available.
    if any(c.get('media_type') not in {'video', 'audio', 'image'} for c in candidates):
        return None
    groups = {}
    for c in candidates:
        if c.get('media_type') == 'image':
            continue
        if not c.get('work_id') or c.get('work', {}).get('schema') != SCHEMA:
            return None
        groups.setdefault(c['work_id'], []).append(c)
    result = []
    for rows in groups.values():
        def label_order(row):
            label = str(row.get('format_label') or '')
            match = re.fullmatch(r'\s*(\d{2,4})[pP](\+)?\s*', label)
            return (int(match[1]), bool(match[2])) if match else (0, False)
        rows = sorted(rows, key=label_order, reverse=True)
        group = picker.build_picker(rows, preferences, provider=rows[0]['provider_id'],
                                    schema=SCHEMA, choice_factory=choice)
        if group:
            group['service_picker'] = True
            result.append(group)
    if any(group['videos'] for group in result):
        for group in result:
            group['secondary_audio'] = group['is_audio']
    return ({**result[0], 'groups': result} if len(result) > 1 else result[0]) if result else None


def attach_service_picker(result, *, payload, provider, source_url, match=None):
    """Called only after a real service normalizer/executor, never on imports."""
    from .upstream_candidates import candidate_url, issue_candidates, MAX_CANDIDATES
    from .upstream_delivery import safe_resource_url
    from .quality_execution import SERVICE_VERSIONS
    from .platforms.kukutool_generic import ServiceFormatPayload
    if provider not in PROVIDERS or not isinstance(payload, ServiceFormatPayload):
        return result
    rows = payload.get('_service_format_rows')
    if not isinstance(rows, (list, tuple)) or not rows:
        return result
    match = match or {'provider_id': provider, 'extractor_id': provider,
                      'version': SERVICE_VERSIONS[provider], 'label': provider}
    candidates = []
    counts = {}
    for row in rows:
        if isinstance(row, dict) and row.get('url'):
            counts[row['url']] = counts.get(row['url'], 0) + 1
    groups = {}
    for row in rows[:MAX_CANDIDATES]:
        if not isinstance(row, dict):
            continue
        url = candidate_url(row.get('url'))
        if not url:
            continue
        metadata = {**row, '_service_picker_issued': True,
                    '_same_resource_labels': counts.get(row['url'], 0) > 1}
        metadata['extension'] = row.get('extension') or row.get('ext') or extension_from_url(url)
        metadata.setdefault('work_label', payload.get('title') or result.base_filename)
        groups.setdefault(row.get('work_id') or '', []).append((url, metadata))
    for group_id, values in groups.items():
        work = {'title': values[0][1].get('work_label') or result.base_filename,
                'author': payload.get('author') or payload.get('nickname') or '',
                'duration': payload.get('duration'), 'source_host': urlsplit(source_url).hostname or '',
                'cover_url': safe_resource_url(payload.get('cover')), 'collection': False}
        specs = []
        for url, metadata in values:
            kind = metadata.get('media_type', 'video')
            protocol = str(metadata.get('protocol') or urlsplit(url).scheme)
            state = ('separate_media_tracks' if metadata.get('requires_merge') else
                     'streaming_media' if any(v in protocol for v in ('m3u8', 'dash')) or
                     urlsplit(url).path.endswith(('.m3u8', '.mpd')) else 'candidate_only')
            specs.append(dict(url=url, metadata=metadata, ordinal=len(candidates) + len(specs) + 1,
                              provider=provider, source_url=source_url, media_type=kind, state=state, match=match))
        candidates.extend(issue_candidates(specs, work=work))
    if not candidates:
        return result
    metadata = {**result.metadata, 'upstream_candidates': candidates,
                'candidate_retained_count': len(candidates), 'retained_count': len(candidates)}
    return replace(result, metadata=metadata)


def selected_link_descriptor(record):
    provider = record.get('provider_id')
    if provider not in PROVIDERS or record.get('public', {}).get('work', {}).get('schema') != SCHEMA:
        raise ValueError('service_selection_invalid')
    return picker.selected_link_descriptor(record, provider=provider, schema=SCHEMA)


def selected_descriptor(record):
    from .upstream_delivery import safe_resource_url
    public = record.get('public', {})
    if (public.get('service_file_eligible') is not True or not capability(public)['can_download'] or picker.context_required(
            record.get('headers'), headers_present=record.get('headers_present'),
            requires_cookie=record.get('requires_cookie'))):
        raise ValueError('service_format_not_direct')
    link = selected_link_descriptor(record)
    if not safe_resource_url(link['url']):
        raise ValueError('service_url_invalid')
    return {**link, 'source_platform': 'upstream', 'asset_id': 'upstream:selection:' + record['candidate_id'],
            'browser_upstream_bound': True, 'browser_direct_fetch_safe': True,
            'requires_merge': False, 'requires_cookie': False,
            'service_audio_file': public.get('media_type') == 'audio',
            'audio_tracks': public.get('audio_tracks'), 'video_tracks': public.get('video_tracks'),
            'size_bytes': picker.number(public.get('size_bytes')) or 0,
            'size_is_estimate': bool(public.get('size_is_estimate'))}
