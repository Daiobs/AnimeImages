"""Which link to a selected file the viewer's browser can read.

Hosts of one CDN differ in CORS: one mirror answers a cross-origin read and
another does not. When the user saves a file, check the record's links in
order with the request a browser would send (Origin, no cookies, no Referer)
and hand back the first one the browser may read. Only the first byte is
requested; the body never passes through the server.

A link that never answered (timeout, network error) is unknown rather than
unreadable: from the server's network a slow CDN can miss the check while the
viewer's browser reads it. When no link proved readable but one stayed
unknown, the browser tries that link (§3 rule 6); only links that all answered
without CORS are refused.
"""
import time
from urllib.parse import urlsplit

import requests

from ..common import HEADERS, XhsDNSResolutionError, XhsError, request_get_with_retry
from ..quality import parse_content_range_total

PROBE_BUDGET_SECONDS = 6.0
PER_LINK_TIMEOUT = (2.0, 3.0)


def readable_by(response, origin):
    allowed = (response.headers.get("Access-Control-Allow-Origin") or "").strip()
    return allowed == "*" or allowed == origin


def browser_readable_link(record, origin, *, request=request_get_with_retry, clock=time.monotonic):
    """(url, size_bytes) of the first link this origin may read, or None.

    Size 0 means unknown, as for a link the browser tries without a check.
    """
    deadline = clock() + PROBE_BUDGET_SECONDS
    headers = {key: value for key, value in HEADERS.items() if key.lower() not in {"referer", "cookie"}}
    headers.update({"Origin": origin, "Range": "bytes=0-0"})
    unknown = None
    for url in record.get("mirrors") or [record["url"]]:
        if clock() >= deadline:
            unknown = unknown or url
            break
        try:
            response = request(url, headers=headers, timeout=PER_LINK_TIMEOUT, retries=1, stream=True,
                               accepted_statuses=(200, 206), deadline_monotonic=deadline)
        except (requests.Timeout, requests.ConnectionError, TimeoutError):
            unknown = unknown or url
            continue
        except XhsDNSResolutionError as error:
            if error.temporary:
                unknown = unknown or url
            continue
        except (XhsError, requests.RequestException, OSError, ValueError):
            # An answered status, or a link the server refuses to request.
            continue
        try:
            if response.status_code in {200, 206} and readable_by(response, origin):
                size = (parse_content_range_total(response.headers.get("Content-Range"))
                        if response.status_code == 206 else 0)
                return url, size or 0
        finally:
            response.close()
    return (unknown, 0) if unknown else None


def request_origin(request):
    url = urlsplit(request.build_absolute_uri("/"))
    return f"{url.scheme}://{url.netloc}"
