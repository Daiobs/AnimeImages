"""Short-lived parsed-media information, separate from download authority."""

from hashlib import sha256
import math
from urllib.parse import urlsplit
from uuid import uuid4

from django.core import signing
from django.core.cache import cache

from .common import XhsParseError, validate_public_download_url
from .provider_lab.contracts import safe_public_text

MAX_CANDIDATES = 64
TTL = 300
SALT = 'upstream-candidate-info-v1'


def candidate_url(value, *, validator=None):
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


def issue_candidate(url, metadata, *, ordinal, provider, source_url, media_type, state, match=None):
    key = uuid4().hex
    supplied_headers = metadata.get('http_headers') or metadata.get('headers') or {}
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
    headers_present = bool(supplied_headers) or metadata.get('headers_present') is True
    requires_cookie = metadata.get('requires_cookie') is True or any(name.lower() == 'cookie' for name in headers)
    public = {
        'candidate_id': key, 'ordinal': ordinal, 'provider_id': provider,
        'media_type': media_type, 'delivery_state': state,
        'protocol': safe_public_text(metadata.get('protocol') or urlsplit(url).scheme, 40),
        'format_label': safe_public_text(metadata.get('format_note') or metadata.get('quality_label')
                                         or metadata.get('format_id'), 120),
        'extension': safe_public_text(metadata.get('ext') or metadata.get('extension'), 16),
        'metadata_source': 'upstream_declared',
        'work_label': safe_public_text(metadata.get('work_label'), 160),
        'format_id': safe_public_text(metadata.get('format_id'), 120),
        'vcodec': safe_public_text(metadata.get('vcodec'), 80) or None,
        'acodec': safe_public_text(metadata.get('acodec'), 80) or None,
        'size_is_estimate': not bool(metadata.get('filesize')) and bool(metadata.get('filesize_approx')),
        'requires_context': headers_present or requires_cookie,
        'headers_present': headers_present, 'requires_cookie': requires_cookie,
    }
    for target, sources in {
        'width': ('width',), 'height': ('height',), 'fps': ('fps',),
        'bitrate': ('tbr', 'bitrate'), 'duration': ('duration',),
        'size_bytes': ('filesize', 'filesize_approx', 'size_bytes'),
    }.items():
        public[target] = next((_number(metadata[s]) for s in sources if metadata.get(s) is not None), None)
    token = signing.dumps({'id': key}, salt=SALT)
    public['resource_token'] = token
    cache.set('upstream:candidate:' + key, {
        'candidate_id': key, 'url': url, 'source_url': source_url,
        'source_digest': sha256(source_url.encode()).hexdigest(),
        'provider_id': provider, 'public': public, 'match': dict(match or {'provider_id': provider}),
        'headers': headers, 'headers_present': headers_present, 'requires_cookie': requires_cookie,
    }, timeout=TTL)
    return public


def resolve_candidate_record(token, candidate_id):
    try:
        payload = signing.loads(str(token), salt=SALT, max_age=TTL)
        if payload != {'id': str(candidate_id)}:
            raise ValueError()
        record = cache.get('upstream:candidate:' + str(candidate_id))
        if not isinstance(record, dict) or not candidate_url(record.get('url')):
            raise ValueError()
        return record
    except (signing.BadSignature, ValueError, TypeError):
        raise XhsParseError('候选资源已失效，请重新解析。') from None
