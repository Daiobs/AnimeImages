"""Short-lived parsed-media information, separate from download authority."""

from hashlib import sha256
from itertools import islice
import json
import math
import time
from urllib.parse import urlsplit
from uuid import uuid4

from django.core import signing
from django.core.cache import cache

from .common import XhsDNSResolutionError, XhsParseError, validate_public_download_url
from .provider_lab.contracts import ASSET_ROLES, MAX_RETAINED_CANDIDATES, exact_public_identity, safe_public_text
from .request_context import inherit_request_context

MAX_CANDIDATES = MAX_RETAINED_CANDIDATES
MAX_CANDIDATE_BUNDLE_BYTES = 2 * 1024 * 1024
TTL = 300
SALT = 'upstream-candidate-info-v1'


def candidate_url(value, *, validator=None, raise_dns_error=False):
    validator = validator or validate_public_download_url
    url = str(value or '')
    try:
        parsed = urlsplit(url)
        if (not url or len(url) > 8192 or parsed.scheme not in {'http', 'https'}
                or parsed.username is not None or parsed.password is not None
                or parsed.port not in {None, 80, 443} or '\\' in url
                or any(ord(c) <= 32 or ord(c) == 127 for c in url)):
            return ''
        validator(url)
    except XhsDNSResolutionError:
        if raise_dns_error:
            raise
        return ''
    except (ValueError, XhsParseError):
        return ''
    return url


def _number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) and number >= 0 else None
    except (ValueError, TypeError, OverflowError):
        return None


def _candidate_record(url, metadata, *, ordinal, provider, source_url, media_type, state, match=None):
    key = uuid4().hex
    context = inherit_request_context(metadata)
    supplied_headers = context['headers']
    headers = {}
    budget = 8192
    if isinstance(supplied_headers, dict):
        for name, value in list(supplied_headers.items())[:32]:
            if (not isinstance(name, str) or not isinstance(value, str)
                    or not name.isascii() or len(name) > 128 or len(value) > 4096
                    or any(ord(c) < 32 or ord(c) == 127 for c in name + value)):
                continue
            budget -= len(name) + len(value)
            if budget < 0:
                break
            headers[name] = value
    headers_present = bool(supplied_headers) or context.get('headers_present') is True
    requires_cookie = context.get('requires_cookie')
    if any(name.lower() == 'cookie' and value for name, value in headers.items()):
        requires_cookie = True
    from .provider_strategy import media_request_context
    from .ytdlp_selection import context_required
    request_context = {**media_request_context(context),
                       'headers_present': headers_present, 'requires_cookie': requires_cookie}
    needs_context = context_required(supplied_headers, **request_context)
    protocol = metadata.get('protocol')
    path = urlsplit(url).path.lower()
    if not protocol:
        protocol = ('m3u8' if path.endswith('.m3u8') else 'dash' if path.endswith('.mpd')
                    else urlsplit(url).scheme)
    if state == 'candidate_only' and ('m3u8' in str(protocol) or 'dash' in str(protocol)):
        state = 'streaming_media'
    public = {
        'candidate_id': key, 'ordinal': ordinal, 'provider_id': provider,
        'media_type': media_type, 'delivery_state': state,
        'protocol': safe_public_text(protocol, 40),
        'language': safe_public_text(metadata.get('language'), 80) or None,
        'format_label': safe_public_text(metadata.get('format_note') or metadata.get('quality_label')
                                         or metadata.get('format_id'), 120),
        'extension': safe_public_text(metadata.get('ext') or metadata.get('extension'), 16),
        'metadata_source': safe_public_text(metadata.get('metadata_source'), 80) or 'upstream_declared',
        'work_label': safe_public_text(metadata.get('work_label'), 160),
        'format_id': safe_public_text(metadata.get('format_id'), 120),
        'work_id': exact_public_identity(metadata.get('work_id')),
        'upstream_asset_id': exact_public_identity(metadata.get('upstream_asset_id', metadata.get('asset_id'))),
        'role': metadata.get('role') if isinstance(metadata.get('role'), str) and metadata['role'] in ASSET_ROLES else 'unknown',
        'upstream_role': safe_public_text(metadata.get('upstream_role', metadata.get('role')), 120),
        'requires_merge': metadata.get('requires_merge') if type(metadata.get('requires_merge')) is bool else None,
        'audio_asset_present': metadata.get('audio_asset_present') if type(metadata.get('audio_asset_present')) is bool else None,
        'vcodec': safe_public_text(metadata.get('vcodec'), 80) or None,
        'acodec': safe_public_text(metadata.get('acodec'), 80) or None,
        'size_is_estimate': metadata['size_is_estimate'] if type(metadata.get('size_is_estimate')) is bool else
                            not bool(metadata.get('filesize')) and bool(metadata.get('filesize_approx')),
        'requires_context': needs_context,
        'headers_present': headers_present, 'requires_cookie': requires_cookie,
    }
    if provider == 'yt_dlp_generic':
        public['download_context_required'] = needs_context
        public['has_drm'] = metadata.get('has_drm') is True
        public['dynamic_range'] = safe_public_text(metadata.get('dynamic_range'), 30) or None
        public['audio_bitrate'] = _number(metadata.get('abr'))
    for track, codec in (('audio_tracks','acodec'),('video_tracks','vcodec')):
        explicit = metadata.get(track)
        codec_value = str(metadata.get(codec) or '').strip().lower()
        public[track] = explicit if isinstance(explicit, str) and explicit in {'unknown','present','absent'} else (
            'absent' if codec_value == 'none' else 'present' if codec_value not in {'','unknown'} else 'unknown')
    relationships = metadata.get('relationships')
    public['relationships'] = []
    if isinstance(relationships, (list, tuple)):
        for relation in relationships[:16]:
            if not isinstance(relation, dict):
                continue
            item = {key:exact_public_identity(relation.get(key)) for key in
                    ('source_asset_id', 'target_asset_id')}
            item['relationship_type'] = safe_public_text(relation.get('relationship_type'), 120)
            if all(item.values()):
                public['relationships'].append(item)
    for target, sources in {
        'width': ('width',), 'height': ('height',), 'fps': ('fps',),
        'bitrate': ('tbr', 'bitrate'), 'duration': ('duration',),
        'size_bytes': ('filesize', 'filesize_approx', 'size_bytes'),
    }.items():
        public[target] = next((_number(metadata[s]) for s in sources if metadata.get(s) is not None), None)
    record = {
        'candidate_id': key, 'url': url, 'source_url': source_url,
        'source_digest': sha256(source_url.encode()).hexdigest(),
        'provider_id': provider, 'public': public, 'match': dict(match or {'provider_id': provider}),
        'headers': headers, 'headers_present': headers_present, 'requires_cookie': requires_cookie,
        **request_context,
        'expires_at': time.time() + TTL,
    }
    if isinstance(metadata.get('_filename_title'), str):
        record['filename_title'] = safe_public_text(metadata['_filename_title'], 160)
    if provider in {'tiantianjiexi_generic', 'kukutool_generic', 'tiantianjiexi', 'kukutool'}:
        from .service_format_selection import candidate_facts
        public.update(candidate_facts(metadata, url, media_type))
        public['dedicated_default'] = metadata.get('_dedicated_default') is True
        if provider in {'tiantianjiexi', 'kukutool'}:
            record['dedicated_resource_id'] = metadata.get('_dedicated_resource_id', '')
        if metadata.get('_service_legacy_item'):
            record['service_legacy_item'] = dict(metadata['_service_legacy_item'])
    return public, record


def issue_candidates(specs, *, work=None):
    """Issue one bounded parse bundle without exhausting per-entry caches."""
    bundle_id = uuid4().hex
    records = {}
    candidates = []
    encoded_bytes = 2
    for spec in islice(specs, MAX_CANDIDATES):
        public, record = _candidate_record(**spec)
        if not candidates and public['provider_id'] == 'yt_dlp_generic' and work:
            from .ytdlp_selection import public_work
            projection = public_work(work)
            if projection:
                public['work'] = projection
                record['work'] = dict(work)
        elif work and public['provider_id'] in {'tiantianjiexi_generic', 'kukutool_generic', 'tiantianjiexi', 'kukutool'}:
            from .service_format_selection import public_service_work
            public['work'] = public_service_work(work)
            record['work'] = dict(work)
        key = public['candidate_id']
        public['resource_token'] = signing.dumps({'id':key,'bundle':bundle_id}, salt=SALT)
        entry_bytes = len(json.dumps({key:record}, ensure_ascii=True, separators=(',', ':')).encode()) - 2
        added_bytes = entry_bytes + bool(records)
        if encoded_bytes + added_bytes > MAX_CANDIDATE_BUNDLE_BYTES:
            break
        encoded_bytes += added_bytes
        records[key] = record
        candidates.append(public)
    if records:
        cache.set('upstream:candidate-bundle:' + bundle_id, records, timeout=TTL)
    return candidates


def issue_candidate(url, metadata, *, ordinal, provider, source_url, media_type, state, match=None):
    return issue_candidates([dict(url=url, metadata=metadata, ordinal=ordinal, provider=provider,
                                 source_url=source_url, media_type=media_type, state=state, match=match)])[0]


def resolve_issued_candidate_record(token, candidate_id, *, display_only=False):
    """Resolve immutable issuance facts, without selected-check display overlays."""
    try:
        payload = signing.loads(str(token), salt=SALT, max_age=TTL)
        if not isinstance(payload, dict) or payload.get('id') != str(candidate_id):
            raise ValueError()
        if set(payload) == {'id','bundle'} and isinstance(payload['bundle'],str):
            records = cache.get('upstream:candidate-bundle:' + payload['bundle'])
            record = records.get(str(candidate_id)) if isinstance(records,dict) else None
        elif payload == {'id':str(candidate_id)}:
            record = cache.get('upstream:candidate:' + str(candidate_id))
        else:
            raise ValueError()
        validator = None
        if (display_only and isinstance(record, dict)
                and record.get('provider_id') in {'tiantianjiexi', 'kukutool'}):
            from .platforms.douyin_media_policy import validate_douyin_media_url
            validator = validate_douyin_media_url
        if not isinstance(record, dict) or not candidate_url(record.get('url'), validator=validator):
            raise ValueError()
        return record
    except (signing.BadSignature, ValueError, TypeError):
        raise XhsParseError('候选资源已失效，请重新解析。') from None


def resolve_candidate_record(token, candidate_id, *, display_only=False):
    record = resolve_issued_candidate_record(token, candidate_id, display_only=display_only)
    try:
        if record.get('provider_id') == 'yt_dlp_generic':
            from .ytdlp_format_check import apply_check_evidence
            return apply_check_evidence(record)
    except (ValueError, TypeError):
        raise XhsParseError('候选资源已失效，请重新解析。') from None
    return record
