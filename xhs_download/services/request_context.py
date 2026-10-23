"""Small shared distinction between customary headers and required access context."""
from collections.abc import Mapping
from hashlib import sha256
import json


_DEFAULT_MEDIA_HEADERS = frozenset({
    'user-agent', 'accept', 'accept-encoding', 'accept-language',
    'referer', 'origin', 'connection', 'cache-control', 'pragma', 'sec-fetch-mode',
})


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
