"""Small service-result adapter for the existing exact-candidate picker.

Service file declarations retain their existing delivery semantics. They are
not yt-dlp track evidence, and never establish codecs or complete audio tracks.
"""
from dataclasses import replace
import json
import re
from time import time
from urllib.parse import urlsplit

from . import ytdlp_selection as picker
from .platforms.gallerydl_generic import extension_from_url

DOUYIN_PROVIDERS = frozenset({'tiantianjiexi', 'kukutool'})
PROVIDERS = frozenset({'tiantianjiexi_generic', 'kukutool_generic'}) | DOUYIN_PROVIDERS
SCHEMA = 'service_selected_format_v1'


def public_service_work(work):
    return {**picker.public_work(work), 'schema': SCHEMA}


def candidate_facts(metadata, url, kind):
    legacy = metadata.get('_service_legacy_item') or {}
    return {
        'service_file_eligible': metadata.get('_service_picker_issued') is True,
        'download_context_required': metadata.get('requires_context') is True or picker.context_required(
            metadata.get('http_headers') or metadata.get('headers'),
            headers_present=bool(metadata.get('headers_present')),
            requires_cookie=metadata.get('requires_cookie') is True),
        'has_drm': metadata.get('has_drm') is True,
        'same_resource_labels': metadata.get('_same_resource_labels') is True,
        'legacy_asset_id': legacy.get('asset_id', ''),
        'legacy_delivery': 'existing_manifest' if legacy else '',
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
    if (candidate.get('provider_id') in DOUYIN_PROVIDERS
            and not (candidate.get('legacy_asset_id')
                     and candidate.get('legacy_delivery') == 'existing_manifest')):
        result = {'can_download': False, 'can_preview': False, 'can_check': False,
                  'limitation': '此格式缺少有效下载授权，可复制原始资源链接。'}
    # Service declarations do not borrow a yt-dlp runtime identity for probing.
    result['can_check'] = False
    result['can_open'] = picker.can_open_video(candidate)
    if not result['can_download'] and '尚未检查' in result['limitation']:
        result['limitation'] = picker.LIMITATIONS['unknown']
    return result


def legacy_item_for_asset(asset, result, provider, source_url):
    from .provider_strategy import build_download_strategy
    from .downloads import provider_manifest_allows_public_download
    if (asset.provider != provider or asset.media_type != 'video'
            or not source_identity_matches(provider, source_url, asset.provider_resource_id)):
        return None
    item = {**asset.to_download_item(), 'source_platform': result.platform}
    strategy = build_download_strategy(asset, result_platform=result.platform, result_metadata=result.metadata)
    if (strategy['download_strategy'] not in ({'browser_streaming'} if provider in DOUYIN_PROVIDERS else {'server_attachment'})
            or not provider_manifest_allows_public_download(item)):
        return None
    return {**item, 'size_bytes': asset.size_bytes, 'size_is_estimate': asset.size_is_estimate,
            **({'filename': asset.filename} if asset.filename else {})}


def source_identity_matches(provider, source_url, resource_id):
    if provider in DOUYIN_PROVIDERS:
        from .platforms.douyin import DouyinAdapter
        from .platforms.douyin_providers import source_url_matches_aweme_id
        return DouyinAdapter().supports_url(source_url) and source_url_matches_aweme_id(source_url, resource_id)
    from .platforms.kukutool_generic import source_key_for_url
    return resource_id == source_key_for_url(source_url)


def choice(candidate):
    result = {**picker.choice(candidate), **capability(candidate)}
    result['can_legacy_download'] = bool(candidate.get('legacy_asset_id')
                                       and candidate.get('legacy_delivery') == 'existing_manifest')
    if candidate.get('format_label'):
        result['label'] = (picker.video_quality_label(candidate) if candidate.get('media_type') == 'video'
                           else candidate['format_label'])
        result['detail_label'] += ' · 上游标注'
    if candidate.get('same_resource_labels'):
        result['detail_label'] += ' · 同一资源的不同标签'
    return result


def project_bound_aliases(rows):
    """Collapse only the confirmed 1080p/P aliases of one manifest binding.

    A legacy identity is assigned by exact URL and unambiguous work matching.
    No private record lookup, token combination or permission union is needed.
    """
    fields = ('provider_id', 'work_id', 'legacy_asset_id', 'legacy_delivery', 'media_type',
              'protocol', 'delivery_state', 'extension', 'service_file_eligible', 'has_drm',
              'requires_merge', 'audio_tracks', 'video_tracks', 'width', 'height', 'fps',
              'bitrate', 'duration', 'vcodec', 'acodec', 'language', 'role', 'upstream_role',
              'dynamic_range', 'selected_track_check', 'format_id', 'upstream_asset_id',
              'audio_asset_present', 'relationships', 'audio_bitrate', 'metadata_source')
    buckets = {}
    for row in rows:
        # Service normalization does not preserve every possible HDR/track fact.
        # Do not generalize this confirmed spelling alias to other quality labels.
        if (row.get('format_label') not in {'1080p', '1080P'}
                or row.get('media_type') != 'video' or not row.get('work_id')
                or not row.get('legacy_asset_id') or row.get('legacy_delivery') != 'existing_manifest'
                or any(row.get(key) for key in ('requires_context', 'download_context_required',
                                                'headers_present', 'requires_cookie'))):
            continue
        key = json.dumps([row.get(field) for field in fields], sort_keys=True)
        buckets.setdefault(key, []).append(row)
    replacements = {}
    omitted = set()
    for aliases in buckets.values():
        if {row['format_label'] for row in aliases} != {'1080p', '1080P'}:
            continue
        known = [row for row in aliases if picker.number(row.get('size_bytes'))]
        if len({picker.number(row['size_bytes']) for row in known}) > 1:
            continue  # Conflicting upstream facts remain separately inspectable.
        representative = next((row for row in aliases if row.get('dedicated_default')), None)
        representative = representative or next((row for row in known if not row.get('size_is_estimate')), None)
        representative = representative or (known or aliases)[0]
        projected = dict(representative)
        if known and not picker.number(projected.get('size_bytes')):
            evidence = next((row for row in known if not row.get('size_is_estimate')), known[0])
            projected['size_bytes'] = evidence['size_bytes']
            projected['size_is_estimate'] = evidence.get('size_is_estimate', False)
        projected['alias_candidate_ids'] = [row['candidate_id'] for row in aliases]
        replacements[aliases[0]['candidate_id']] = projected
        omitted.update(row['candidate_id'] for row in aliases[1:])
    return [replacements.get(row['candidate_id'], row) for row in rows if row['candidate_id'] not in omitted]


def default_label_order(row):
    """Preserve the service picker's pre-projection default tie-break order."""
    match = re.fullmatch(r'\s*(\d{2,4})[pP](\+)?\s*', str(row.get('format_label') or ''))
    return (int(match[1]), bool(match[2])) if match else (0, False)


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
        raw_rows = rows
        # build_picker applies the new display order after choosing its default.
        rows = sorted(project_bound_aliases(rows), key=default_label_order, reverse=True)
        group = picker.build_picker(rows, preferences, provider=rows[0]['provider_id'],
                                    schema=SCHEMA, choice_factory=choice)
        if group:
            group['service_picker'] = True
            group['legacy_asset_ids'] = list(dict.fromkeys(
                row['legacy_asset_id'] for row in raw_rows
                if row.get('legacy_asset_id') and row.get('legacy_delivery') == 'existing_manifest'))
            if rows[0]['provider_id'] in DOUYIN_PROVIDERS and not preferences:
                default = next((c for c in group['videos'] if c.get('dedicated_default')), None)
                if default:
                    group['selected'] = default
                    for c in group['videos']:
                        c['selected'] = c['candidate_id'] == default['candidate_id']
                    if default['can_download']:
                        default['initial_visible'] = True
                    group['video_primary'] = [c for c in group['videos'] if c['initial_visible']]
                    group['video_other'] = [c for c in group['videos'] if not c['initial_visible']]
            result.append(group)
    if any(group['videos'] for group in result):
        for group in result:
            group['secondary_audio'] = group['is_audio']
    if not result:
        return None
    presentation = {**result[0], 'groups': result} if len(result) > 1 else result[0]
    presentation['video_group_count'] = sum(bool(group['videos']) for group in result)
    return presentation


def attach_service_picker(result, *, payload, provider, source_url, match=None, binding_assets=None):
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
    projection_validator = None
    if provider in DOUYIN_PROVIDERS:
        from .platforms.douyin_media_policy import validate_douyin_media_url
        # Projection does not fetch media. Keep the dedicated CDN policy here;
        # link, cover and save actions still recheck public DNS before delivery.
        projection_validator = validate_douyin_media_url
    candidates = []
    counts = {}
    for row in rows:
        if isinstance(row, dict) and row.get('url'):
            counts[row['url']] = counts.get(row['url'], 0) + 1
    legacy_by_url = {}
    for asset in (result.video_assets if binding_assets is None else binding_assets):
        item = legacy_item_for_asset(asset, result, provider, source_url)
        if item:
            legacy_by_url.setdefault(asset.url, []).append(item)
    # Repeated labels of one file may share its authority. Ambiguous work groups
    # or multiple old identities must retain their independent legacy controls.
    work_ids_by_url = {}
    for row in rows:
        if isinstance(row, dict) and row.get('url'):
            work_ids_by_url.setdefault(row['url'], set()).add(row.get('work_id'))
    groups = {}
    for row in rows[:MAX_CANDIDATES]:
        if not isinstance(row, dict):
            continue
        url = candidate_url(row.get('url'), validator=projection_validator)
        if not url:
            continue
        metadata = {**row, '_service_picker_issued': True,
                    '_same_resource_labels': counts.get(row['url'], 0) > 1,
                    '_service_legacy_item': None}
        old_items = legacy_by_url.get(url, [])
        facts = candidate_facts(metadata, url, row.get('media_type'))
        protocol = str(row.get('protocol') or urlsplit(url).scheme)
        if (len(old_items) == 1 and len(work_ids_by_url.get(url, ())) == 1
                and row.get('work_id') and row.get('media_type') == 'video'
                and protocol in {'http', 'https'}
                and not urlsplit(url).path.lower().endswith(('.m3u8', '.mpd'))
                and not facts['download_context_required'] and not facts['has_drm']
                and not row.get('requires_merge')):
            metadata['_service_legacy_item'] = old_items[0]
        metadata['extension'] = row.get('extension') or row.get('ext') or extension_from_url(url)
        metadata.setdefault('work_label', payload.get('title') or result.base_filename)
        groups.setdefault(row.get('work_id') or '', []).append((url, metadata))
    cover = (candidate_url(payload.get('cover'), validator=projection_validator)
             if projection_validator else safe_resource_url(payload.get('cover')))
    if cover and (urlsplit(cover).scheme != 'https' or urlsplit(cover).fragment):
        cover = ''
    for group_id, values in groups.items():
        work = {'title': values[0][1].get('work_label') or result.base_filename,
                'author': payload.get('author') or payload.get('nickname') or '',
                'duration': payload.get('duration'), 'source_host': urlsplit(source_url).hostname or '',
                'cover_url': cover, 'collection': False}
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
    link = picker.selected_link_descriptor(record, provider=provider, schema=SCHEMA)
    public = record['public']
    legacy = record.get('service_legacy_item') or {}
    if (public.get('media_type') != 'video' or not capability(public)['can_download']
            or public.get('legacy_delivery') != 'existing_manifest'
            or not public.get('legacy_asset_id')
            or legacy.get('asset_id') != public['legacy_asset_id']
            or legacy.get('url') != link['url'] or legacy.get('provider') != provider
            or legacy.get('type') != 'video'):
        return link
    extension = str(public.get('extension') or '').lower().lstrip('.')
    filename = picker.clean_filename(legacy['filename']).strip(' .') if legacy.get('filename') else ''
    stem, _, suffix = filename.rpartition('.')
    if stem and suffix.lower() in picker.VIDEO_EXTENSIONS and (not extension or extension == suffix.lower()):
        link['filename'] = f'{stem[:160]}.{suffix}'
    elif provider in DOUYIN_PROVIDERS and not extension and link['filename'].endswith('.bin'):
        # Preserve the existing dedicated-video filename default, not a new
        # container/track fact. Download authorization is still checked below.
        link['filename'] = link['filename'][:-4] + '.mp4'
    return link


def selected_open_descriptor(record):
    provider = record.get('provider_id')
    if provider not in PROVIDERS or record.get('public', {}).get('work', {}).get('schema') != SCHEMA:
        raise ValueError('service_selection_invalid')
    return picker.selected_open_descriptor(record, provider=provider, schema=SCHEMA)


def selected_legacy_descriptor(record):
    public = record.get('public', {})
    if public.get('legacy_delivery') == 'existing_manifest':
        from .downloads import provider_manifest_allows_public_download
        item = record.get('service_legacy_item') or {}
        if (not item or item.get('url') != record.get('url')
                or item.get('provider') != record.get('provider_id')
                or item.get('type') != public.get('media_type')
                or item.get('asset_id') != public.get('legacy_asset_id')
                or not source_identity_matches(record['provider_id'], record['source_url'], item.get('provider_resource_id'))
                or not provider_manifest_allows_public_download(item)):
            raise ValueError('service_legacy_binding_invalid')
        link = selected_link_descriptor(record)
        return {**item, **link, 'selection_delivery': 'legacy_manifest'}
    raise ValueError('service_legacy_binding_missing')


def selected_descriptor(record):
    from .upstream_delivery import safe_resource_url
    from .provider_strategy import browser_transport_hint
    public = record.get('public', {})
    if (public.get('service_file_eligible') is not True or not capability(public)['can_download'] or picker.context_required(
            record.get('headers'), headers_present=record.get('headers_present'),
            requires_cookie=record.get('requires_cookie'))):
        raise ValueError('service_format_not_direct')
    link = selected_link_descriptor(record)
    if not safe_resource_url(link['url']):
        raise ValueError('service_url_invalid')
    selection_expires_at = picker.selection_expires_at(record)
    if record['provider_id'] in DOUYIN_PROVIDERS:
        from .provider_manifest import provider_manifest_expires_at
        from .platforms.douyin_kukutool import parse_byte_cdn_url
        item = selected_legacy_descriptor(record)
        selection_expires_at = min(
            selection_expires_at, provider_manifest_expires_at(item['provider_manifest_token']) * 1000)
        cdn_expires_at = parse_byte_cdn_url(item['url'])['expires_at']
        if cdn_expires_at:
            selection_expires_at = min(selection_expires_at, cdn_expires_at * 1000)
        if selection_expires_at <= time() * 1000:
            raise ValueError('service_selection_expired')
        item.pop('selection_delivery', None)
        item = {**item, 'browser_upstream_bound': True, 'browser_direct_fetch_safe': True,
                'selection_expires_at': selection_expires_at,
                'requires_merge': False, 'requires_cookie': False,
                'size_bytes': picker.number(public.get('size_bytes')) or 0,
                'size_is_estimate': bool(public.get('size_is_estimate'))}
        return {**item, **browser_transport_hint(item)}
    item = {**link, 'source_platform': 'upstream', 'asset_id': 'upstream:selection:' + record['candidate_id'],
            'selection_expires_at': selection_expires_at,
            'browser_upstream_bound': True, 'browser_direct_fetch_safe': True,
            'requires_merge': False, 'requires_cookie': False,
            'service_audio_file': public.get('media_type') == 'audio',
            'audio_tracks': public.get('audio_tracks'), 'video_tracks': public.get('video_tracks'),
            'size_bytes': picker.number(public.get('size_bytes')) or 0,
            'size_is_estimate': bool(public.get('size_is_estimate'))}
    return {**item, **browser_transport_hint(item)}
