"""Small shared distinction between customary headers and required access context."""
from collections.abc import Mapping
from hashlib import sha256
import json


_DEFAULT_MEDIA_HEADERS = frozenset({
    'user-agent', 'accept', 'accept-encoding', 'accept-language',
    'referer', 'origin', 'connection', 'cache-control', 'pragma', 'sec-fetch-mode',
})


def inherit_request_context(*sources):
    """Merge work-to-format facts without treating defaults as an override grant."""
    sources = [source for source in sources if isinstance(source, Mapping)]
    context = {}
    for key in ('requires_cookie', 'requires_context', 'ip_bound', 'headers_present'):
        values = [source[key] for source in sources if key in source]
        if values:
            context[key] = (True if any(value is True for value in values) else
                            next((value for value in values if value),
                                 None if any(value is None for value in values) else values[-1]))

    headers = {}
    required = set()
    invalid_required = None
    declared_required = False
    opaque = False
    for source in sources:
        supplied_headers = False
        # Both aliases describe media headers. http_headers wins within one
        # level; every format-level name wins over either work-level alias.
        for field in ('headers', 'http_headers'):
            entries = source.get(field)
            if entries is None:
                continue
            if not isinstance(entries, Mapping):
                opaque = True
                continue
            supplied_headers = supplied_headers or bool(entries)
            for name, value in entries.items():
                if not isinstance(name, str):
                    opaque = True
                    continue
                headers[name.lower()] = (name, value)
        if source.get('headers_present') and not supplied_headers:
            opaque = True
        if 'required_headers' not in source:
            continue
        declared_required = True
        names = source['required_headers']
        if names is None:
            continue
        if isinstance(names, str):
            names = (names,)
        if (not isinstance(names, (list, tuple, set, frozenset))
                or any(not isinstance(name, str) or not name.strip() for name in names)):
            invalid_required = source['required_headers']
        else:
            required.update(name.strip().lower() for name in names)
    context['headers'] = dict(headers.values())
    if declared_required:
        context['required_headers'] = invalid_required if invalid_required is not None else sorted(required)
    if opaque:
        context['requires_context'] = True
    if any(name == 'cookie' and value not in (None, '') for name, (_, value) in headers.items()):
        context['requires_cookie'] = True
    return context


def _required_context(headers, required_headers, requires_cookie, requires_context, ip_bound, headers_present):
    opaque = bool(headers_present and not headers)
    if headers is None:
        headers = {}
    if not isinstance(headers, Mapping):
        opaque = True
        headers = {}
    if required_headers is None:
        required_headers = ()
    elif isinstance(required_headers, str):
        required_headers = (required_headers,)
    elif not isinstance(required_headers, (tuple, list, set, frozenset)):
        opaque = True
        required_headers = ()
    if any(not isinstance(name, str) or not name.strip() for name in required_headers):
        opaque = True
        required_headers = ()
    required = tuple(sorted({name.strip().lower() for name in required_headers}))
    # Default header values are not known requirements. Explicitly required
    # defaults and all custom/credential headers retain their private values
    # in the fingerprint input, never in the public descriptor or cache key.
    relevant = sorted(
        (str(name).strip().lower(), str(value)) for name, value in headers.items()
        if value not in (None, '') and (
            str(name).strip().lower() not in _DEFAULT_MEDIA_HEADERS
            or str(name).strip().lower() in required)
    )
    return (required, relevant, bool(requires_cookie), bool(requires_context), bool(ip_bound), opaque)


def request_context_compatible(headers=None, *, required_headers=(), requires_cookie=None,
                               requires_context=None, ip_bound=None, headers_present=False):
    """No *known* navigation conflict; this does not turn unknown facts into False."""
    return not any(_required_context(
        headers, required_headers, requires_cookie, requires_context, ip_bound, headers_present))


def request_context_fingerprint(headers=None, *, required_headers=(), requires_cookie=None,
                                requires_context=None, ip_bound=None, headers_present=False):
    context = _required_context(
        headers, required_headers, requires_cookie, requires_context, ip_bound, headers_present)
    return sha256(json.dumps(context, ensure_ascii=True, separators=(',', ':')).encode()).hexdigest()
