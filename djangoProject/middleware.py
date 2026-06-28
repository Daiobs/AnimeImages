import re
import uuid
from contextvars import ContextVar


REQUEST_ID_HEADER = "X-Request-ID"
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{8,128}$")
_current_request_id = ContextVar("request_id", default="")


def get_current_request_id():
    return _current_request_id.get("")


def normalize_request_id(value):
    value = str(value or "").strip()
    if _REQUEST_ID_RE.match(value):
        return value
    return uuid.uuid4().hex


class RequestIDMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request_id = normalize_request_id(request.headers.get(REQUEST_ID_HEADER))
        request.request_id = request_id
        token = _current_request_id.set(request_id)
        try:
            response = self.get_response(request)
        finally:
            _current_request_id.reset(token)
        response[REQUEST_ID_HEADER] = request_id
        return response
