"""User-facing yt-dlp choices, built from issued evidence rather than client URLs.

This projection never extracts media or changes provider selection. It keeps all
format identities, including formats this browser-only delivery path cannot save.
"""
import math
import re
from time import time
from urllib.parse import urlsplit

from .common import clean_filename
from .provider_lab.contracts import safe_public_text

PROVIDER = 'yt_dlp_generic'
SCHEMA = 'ytdlp_selected_format_v1'
VIDEO_EXTENSIONS = frozenset({'mp4', 'webm', 'mov', 'm4v', 'mkv'})
AUDIO_EXTENSIONS = frozenset({'m4a', 'mp3', 'aac', 'ogg', 'opus', 'flac', 'wav', 'weba', 'webm'})
CONTEXT_POLICY_REVISION = 3
LIMITATIONS = {
    'stream': '这是流媒体格式，当前选择器不合并分片；可在技术详情中复制来源。',
    'merge': '此格式需要合并音视频；当前选择器不将它当作完整文件下载。',
    'context': '此资源需要额外请求上下文，暂不能由浏览器直接保存。',
    'insecure': '这是 HTTP 资源，当前页面不直接下载；可复制来源链接。',
    'unknown': '媒体信息不足，暂不能确认可直接保存的文件。',
    'drm': '此格式标有 DRM，不提供下载操作。',
}


def number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) and result >= 0 else None
    except (ValueError, TypeError, OverflowError):
        return None


def text(value, length=200):
    return safe_public_text(value, length) if isinstance(value, str) else ''


def display_host(value):
    # This value comes from the validated work URL, not arbitrary display text.
    # Preserve the domain without publishing a path, query or signed reference.
    if (not isinstance(value, str) or len(value) > 253 or '.' not in value
            or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label)
                   for label in value.split('.'))):
        return ''
    return value


def public_work(work):
    if not isinstance(work, dict) or work.get('collection'):
        return {}
    return {'schema': SCHEMA, 'title': text(work.get('title'), 300),
            'author': text(work.get('author'), 200), 'duration': number(work.get('duration')),
            'source_host': display_host(work.get('source_host')),
            'cover_available': bool(work.get('cover_url'))}


def context_required(headers, *, headers_present=False, requires_cookie=None,
                     required_headers=(), requires_context=None, ip_bound=None):
    from .request_context import request_context_compatible
    return not request_context_compatible(
        headers, headers_present=headers_present, requires_cookie=requires_cookie,
        required_headers=required_headers, requires_context=requires_context, ip_bound=ip_bound)


def capability(candidate):
    """UI hint only; the action endpoint revalidates the private issued record."""
    protocol = str(candidate.get('protocol') or '').lower()
    state = str(candidate.get('delivery_state') or '')
    kind = candidate.get('media_type')
    ext = str(candidate.get('extension') or '').lower().lstrip('.')
    reason = ''
    if candidate.get('has_drm') is True:
        reason = 'drm'
    elif protocol == 'http' or state == 'insecure_reference':
        reason = 'insecure'
    elif any(marker in protocol for marker in ('m3u8', 'dash', 'm3u')) or state == 'streaming_media':
        reason = 'stream'
    elif candidate.get('requires_merge') is True or (kind == 'video' and (
            state in {'silent_video', 'separate_media_tracks'} or candidate.get('audio_tracks') == 'absent')):
        reason = 'merge'
    elif candidate.get('download_context_required', candidate.get('requires_context', False)):
        reason = 'context'
    elif protocol != 'https':
        reason = 'unknown'
    elif kind == 'video':
        if state != 'complete' or ext not in VIDEO_EXTENSIONS:
            reason = 'unknown'
    elif kind == 'audio':
        if (state != 'audio_only' or candidate.get('audio_tracks') != 'present'
                or candidate.get('video_tracks') != 'absent' or ext not in AUDIO_EXTENSIONS):
            reason = 'unknown'
    else:
        reason = 'unknown'
    can_check = bool(reason == 'unknown' and protocol == 'https' and kind == 'video' and ext == 'mp4'
                     and state in {'unknown_media_tracks', 'candidate_only'}
                     and candidate.get('audio_tracks') != 'absent'
                     and candidate.get('video_tracks') != 'absent'
                     and not candidate.get('selected_track_check'))
    limitation = ('尚未检查此格式的完整音视频，可检查所选视频后再保存。' if can_check
                  else LIMITATIONS.get(reason, ''))
    if reason and candidate.get('selected_track_check'):
        from .ytdlp_format_check import check_message
        limitation = check_message(candidate['selected_track_check'])
    return {'can_download': not reason, 'can_check': can_check, 'limitation': limitation,
            'can_open': can_open_video(candidate),
            'can_preview': not reason and ext in {'mp4', 'webm', 'm4v', 'm4a', 'mp3', 'ogg', 'opus', 'wav'}}


def can_open_video(candidate):
    """Navigation needs no fetch/CORS or codec probe, but retains source restrictions."""
    return bool(candidate.get('media_type') == 'video'
                and candidate.get('protocol') == 'https'
                and not candidate.get('has_drm')
                and not candidate.get('requires_merge')
                and not candidate.get('requires_cookie')
                and not candidate.get('download_context_required', candidate.get('requires_context', False))
                and candidate.get('video_tracks') != 'absent'
                and candidate.get('delivery_state') not in {
                    'streaming_media', 'separate_media_tracks', 'insecure_reference'}
                and str(candidate.get('extension') or '').lower().lstrip('.') in ({''} | VIDEO_EXTENSIONS))


def size_label(candidate):
    size = number(candidate.get('size_bytes'))
    if not size:
        return '大小未知'
    units = ('B', 'KiB', 'MiB', 'GiB')
    unit = 0
    while size >= 1024 and unit < len(units) - 1:
        size /= 1024
        unit += 1
    return ('约 ' if candidate.get('size_is_estimate') else '') + f'{size:.1f} {units[unit]}'


def duration_label(value):
    value = number(value)
    if value is None:
        return ''
    seconds = int(value)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f'{hours}:{minutes:02}:{seconds:02}' if hours else f'{minutes}:{seconds:02}'


def video_quality_label(candidate):
    label = text(candidate.get('format_label'), 80).strip()
    if label.lower() in {'original', 'source', '原画'}:
        return label
    match = re.fullmatch(r'(\d{2,4})[pP](\+)?', label)
    if match:
        return f'{int(match[1])}p{match[2] or ""}'
    height = number(candidate.get('height'))
    return f'{int(height)}p' if height else label or '视频格式'


def video_quality_order(candidate):
    label = video_quality_label(candidate)
    if label.lower() in {'original', 'source', '原画'}:
        return (0, 0, 0)
    match = re.fullmatch(r'(\d{2,4})p(\+)?', label)
    if match:
        return (1, -int(match[1]), -bool(match[2]))
    return (2, 0, 0)


def add_video_variant_labels(videos):
    groups = {}
    for candidate in videos:
        candidate['variant_label'] = ''
        groups.setdefault(candidate['label'].casefold(), []).append(candidate)
    for siblings in groups.values():
        if len(siblings) < 2:
            continue
        facts = []
        for candidate in siblings:
            fps = number(candidate.get('fps'))
            values = [f'{fps:g} fps' if fps else '']
            for field in ('vcodec', 'acodec', 'language', 'extension', 'protocol', 'dynamic_range'):
                value = text(candidate.get(field), 60).strip()
                values.append('' if value.lower() in {'', 'none', 'unknown'} else
                              value.upper() if field == 'extension' else value)
            facts.append(values)
        differing = [index for index in range(len(facts[0]))
                     if len({values[index].casefold() for values in facts if values[index]}) > 1]
        labels = [' · '.join(values[index] for index in differing if values[index]) for values in facts]
        # Equal or missing observable facts cannot establish a technical difference.
        for ordinal, (candidate, label) in enumerate(zip(siblings, labels), 1):
            candidate['variant_label'] = (f'{label} · 版本 {ordinal}' if label else f'版本 {ordinal}') if (
                not label or labels.count(label) > 1) else label


def choice(candidate):
    kind = candidate.get('media_type')
    abr = number(candidate.get('audio_bitrate')) or number(candidate.get('bitrate'))
    label = video_quality_label(candidate) if kind == 'video' else (
        f'{abr:g} kbps' if abr else text(candidate.get('format_label'), 80) or '原始音频')
    facts = [str(candidate.get('extension') or '格式未知').upper()]
    fps = number(candidate.get('fps'))
    if kind == 'video' and fps:
        facts.append(f'{fps:g} fps')
    if kind == 'video':
        facts.append({'present': '含音频', 'absent': '无音频'}.get(candidate.get('audio_tracks'), '音轨未知'))
    language = text(candidate.get('language'), 50)
    if language:
        facts.append(language)
    dynamic_range = text(candidate.get('dynamic_range'), 30)
    if dynamic_range and dynamic_range.upper() != 'SDR':
        facts.append(dynamic_range)
    codec = text(candidate.get('vcodec' if kind == 'video' else 'acodec'), 60)
    if codec and codec not in {'none', 'unknown'}:
        facts.append(codec)
    protocol = text(candidate.get('protocol'), 40)
    if protocol:
        facts.append(protocol)
    return {**candidate, **capability(candidate), 'label': label,
            'detail_label': ' · '.join(facts), 'size_label': size_label(candidate)}


def build_picker(candidates, preferences=None, *, provider=PROVIDER, schema=SCHEMA, choice_factory=choice):
    if not isinstance(candidates, (list, tuple)) or not candidates:
        return None
    if any(not isinstance(c, dict) or c.get('provider_id') != provider for c in candidates):
        return None
    work_candidate = next((c for c in candidates if isinstance(c.get('work'), dict)
                           and c['work'].get('schema') == schema), candidates[0])
    work = work_candidate.get('work', {})
    if not isinstance(work, dict) or work.get('schema') != schema:
        return None  # Old cached/multiwork/other-provider results keep their UI.
    groups = {str(c.get('work_id') or c.get('work_label') or '') for c in candidates}
    if len(groups - {''}) > 1:
        return None
    videos = [choice_factory(c) for c in candidates if c.get('media_type') == 'video' and c.get('role') not in {'cover', 'thumbnail', 'poster'}]
    audios = [choice_factory(c) for c in candidates if c.get('media_type') == 'audio']
    if not videos and not audios:
        return None
    # Keep the existing default ranking independent of the visible quality order.
    ranked_videos = sorted(videos, key=lambda c: (number(c.get('height')) or 0, number(c.get('fps')) or 0,
                                                number(c.get('bitrate')) or 0), reverse=True)
    videos.sort(key=video_quality_order)
    add_video_variant_labels(videos)
    audios.sort(key=lambda c: (number(c.get('audio_bitrate')) or number(c.get('bitrate')) or 0), reverse=True)
    primary = ranked_videos or audios
    direct = [c for c in primary if c['can_download']]
    preferred = []
    if videos and isinstance(preferences, dict) and preferences:
        prefixes = {'h264': ('h264', 'avc1', 'avc3'), 'hevc': ('hevc', 'h265', 'hev1', 'hvc1'),
                    'vp9': ('vp9', 'vp09'), 'av1': ('av1', 'av01')}
        for c in direct:
            ceiling = number(preferences.get('max_height'))
            height = number(c.get('height'))
            codec = preferences.get('preferred_video_codec')
            ext = preferences.get('preferred_container')
            if ceiling and (not height or height > ceiling):
                continue
            if codec and not str(c.get('vcodec') or '').lower().startswith(prefixes.get(codec, ('__unknown__',))):
                continue
            if ext and str(c.get('extension') or '').lower() != ext:
                continue
            preferred.append(c)
    selected = (preferred or direct or primary)[0]
    for group in (videos, audios):
        seen = set()
        visible = 0
        for c in group:
            c['selected'] = c['candidate_id'] == selected['candidate_id']
            if c['media_type'] == 'video':
                c['initial_visible'] = True
                continue
            # Only direct-save representatives belong in the primary tier;
            # all other identities remain selectable in the disclosure.
            key = c.get('height') if c['media_type'] == 'video' else c.get('audio_bitrate') or c.get('bitrate')
            c['initial_visible'] = c['can_download'] and (c['selected'] or (key not in seen and visible < 6))
            if c['initial_visible']:
                seen.add(key)
                visible += 1
    previews = [c for c in primary if c['can_preview']]
    preview = min(previews, key=lambda c: (number(c.get('height')) or 0, number(c.get('size_bytes')) or float('inf'))) if previews else None
    return {'work': work, 'videos': videos, 'audios': audios, 'selected': selected,
            'provider_id': provider, 'selection_schema': schema,
            'preview': preview, 'first': work_candidate, 'is_audio': not videos,
            'duration_label': duration_label(work.get('duration')),
            'video_primary': [c for c in videos if c['initial_visible']],
            'video_other': [c for c in videos if not c['initial_visible']],
            'audio_primary': [c for c in audios if c['initial_visible']],
            'audio_other': [c for c in audios if not c['initial_visible']],
            'all_unavailable': not any(c['can_download'] for c in videos + audios),
            'group_id': 'ytfmt-' + re.sub('[^a-zA-Z0-9_-]', '', str(work_candidate.get('candidate_id', '')))[:80],
            'video_group_count': int(bool(videos)),
            'format_count': len(videos) + len(audios)}


def selected_link_descriptor(record, *, provider=PROVIDER, schema=SCHEMA):
    """Authorized link metadata, not a media fetch or download grant."""
    from .upstream_candidates import candidate_url
    from .work_caption import work_caption_presentation
    public = record.get('public', {})
    if (record.get('provider_id') != provider or not isinstance(public, dict)
            or public.get('provider_id') != provider
            or public.get('media_type') not in {'video', 'audio'}):
        raise ValueError('selected_link_invalid')
    url = candidate_url(record.get('url'))
    if not url:
        raise ValueError('selected_link_url_invalid')
    extension = str(public.get('extension') or '').lower().lstrip('.')
    protocol = str(public.get('protocol') or '').lower()
    if 'm3u8' in protocol:
        extension = 'm3u8'
    elif 'dash' in protocol:
        extension = 'mpd'
    if not re.fullmatch(r'[a-z0-9]{1,12}', extension):
        extension = 'bin'
    # New normalization already distinguished title from body. Old cached labels
    # still need the naming-only caption projection; neither path reissues them.
    title = (record['filename_title'] if 'filename_title' in record else
             work_caption_presentation(public.get('work_label'))['title'])
    name = clean_filename(title or 'media').strip(' .')[:160] or 'media'
    format_id = re.sub(r'[^A-Za-z0-9_-]', '_', str(public.get('format_id') or record['candidate_id']))[:80]
    return {'selection_schema': schema, 'candidate_id': record['candidate_id'],
            'selected_format_id': public.get('format_id') or '', 'url': url,
            'type': public['media_type'], 'provider': provider,
            'filename': f'{name}_{format_id}.{extension}'}


def selected_open_descriptor(record, *, provider=PROVIDER, schema=SCHEMA):
    from .platforms.douyin_kukutool import parse_byte_cdn_url
    from .platforms.douyin_media_policy import is_allowed_douyin_media_hostname
    from .upstream_delivery import safe_resource_url
    from .provider_strategy import media_request_context
    if (not can_open_video(record.get('public', {}))
            or context_required(record.get('headers'), **media_request_context(record))):
        raise ValueError('selected_video_navigation_restricted')
    item = selected_link_descriptor(record, provider=provider, schema=schema)
    if not safe_resource_url(item['url']):
        raise ValueError('selected_video_navigation_url_invalid')
    if is_allowed_douyin_media_hostname(urlsplit(item['url']).hostname):
        expires_at = parse_byte_cdn_url(item['url'])['expires_at']
        if expires_at and expires_at <= time():
            raise ValueError('selected_video_navigation_expired')
    return item


def selection_expires_at(record):
    """Bound a new download task to its original candidate lifetime, in ms."""
    expires_at = number(record.get('expires_at'))
    if expires_at is None:
        raise ValueError('selected_format_expiry_invalid')
    expires_ms = int(expires_at * 1000)
    if expires_ms <= time() * 1000:
        raise ValueError('selected_format_expired')
    return expires_ms


def selected_descriptor(record):
    """Fresh browser-only authority: no caller-supplied URL or format facts."""
    from .upstream_delivery import safe_resource_url
    from .provider_strategy import browser_transport_hint, media_request_context
    public = record.get('public', {})
    if (record.get('provider_id') != PROVIDER or not isinstance(public, dict)
            or public.get('provider_id') != PROVIDER or not capability(public)['can_download']
            or context_required(record.get('headers'), **media_request_context(record))):
        raise ValueError('selected_format_not_direct')
    url = safe_resource_url(record.get('url'))
    if not url:
        raise ValueError('selected_format_url_invalid')
    link = selected_link_descriptor(record)
    item = {**link, 'url': url,
            'selection_expires_at': selection_expires_at(record),
            'type': public['media_type'], 'provider': PROVIDER, 'source_platform': 'upstream',
            'asset_id': 'upstream:selection:' + record['candidate_id'],
            'browser_upstream_bound': True, 'browser_direct_fetch_safe': True,
            'requires_merge': False, 'requires_cookie': record.get('requires_cookie'),
            'request_context_status': 'no_known_requirement',
            'audio_tracks': public.get('audio_tracks'), 'video_tracks': public.get('video_tracks'),
            'size_bytes': number(public.get('size_bytes')) or 0,
            'size_is_estimate': bool(public.get('size_is_estimate'))}
    return {**item, **browser_transport_hint(item)}
