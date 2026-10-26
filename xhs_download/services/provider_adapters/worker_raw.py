"""Pure decoding of the opt-in Worker transport, never an upstream client."""
import base64
from copy import deepcopy
from datetime import date, datetime
import math
from types import SimpleNamespace

from ..pipeline import Failure, FailureKind


def _decode(node, *, objects=False, depth=0):
    if depth > 128:
        raise ValueError("INVALID_RAW_TRANSPORT")
    if node is None or type(node) in (bool, int, str):
        return node
    if type(node) is float and math.isfinite(node):
        return node
    if type(node) is not dict or "t" not in node or "v" not in node:
        raise ValueError("INVALID_RAW_TRANSPORT")
    tag, value = node["t"], node["v"]
    def child(item):
        return _decode(item, objects=objects, depth=depth + 1)
    if tag == "dict":
        return {child(key): child(item) for key, item in value}
    if tag in {"list", "tuple"}:
        values = [child(item) for item in value]
        return tuple(values) if tag == "tuple" else values
    if tag in {"datetime", "date"}:
        return (datetime if tag == "datetime" else date).fromisoformat(value)
    if tag == "bytes":
        return base64.b64decode(value, validate=True)
    if tag == "enum":
        return child(value)
    if tag == "object":
        fields = child(value)
        if type(fields) is not dict or not isinstance(node.get("name"), str):
            raise ValueError("INVALID_RAW_TRANSPORT")
        if objects:
            if node.get("name") != "VideoInfo" or not node.get("module", "").startswith("videodl."):
                raise ValueError("NOT_VIDEOINFO")
            return SimpleNamespace(**fields)
        return {"_class": node["name"], "fields": fields}
    raise ValueError("INVALID_RAW_TRANSPORT")


def decode_raw(raw, *, many=False, objects=False):
    if type(raw) is not dict or "worker_raw_v1" not in raw:
        return raw
    records = raw["worker_raw_v1"]
    if set(raw) != {"worker_raw_v1"} or type(records) is not list or not records:
        raise ValueError("INVALID_RAW_TRANSPORT")
    if many:
        return [_decode(item, objects=objects) for item in records]
    # yt-dlp may observe an eager root and its children; the root is the actual
    # returned object. No playlist is manufactured from unrelated observations.
    return _decode(records[0], objects=objects)


def captured_fetch(provider, source_url, budget, invoke, *, prefix, many=False, objects=False,
                   expected_metadata=None):
    from concurrent.futures import wait
    import time
    from ..pipeline_shadow_capture import collect_raw
    if not isinstance(budget, (int, float)) or not math.isfinite(budget) or budget <= 0:
        return Failure(FailureKind.TIMEOUT, prefix + "_BUDGET_EXHAUSTED")
    try:
        deadline = time.monotonic() + budget
        legacy_error = None
        pending = ()
        with collect_raw() as session:
            try:
                invoke()
            except Exception as error:
                legacy_error = error
            finally:
                if session.futures:
                    _, pending = wait(session.futures, timeout=max(0, deadline - time.monotonic()))
        if pending or time.monotonic() >= deadline:
            return Failure(FailureKind.TIMEOUT, prefix + "_TIMEOUT")
        records = [record for record in session.records
                   if record.provider == provider and record.source_url == source_url]
        if provider in session.errors or len(records) != 1:
            if legacy_error is not None:
                return worker_failure(legacy_error, prefix)
            return Failure(FailureKind.INVALID_RESULT, prefix + "_NO_RAW")
        record = records[0]
        if expected_metadata and any(record.metadata.get(key) != value
                                     for key, value in expected_metadata.items()):
            return Failure(FailureKind.INVALID_RESULT, prefix + "_NO_RAW")
        return deepcopy(decode_raw(record.raw, many=many, objects=objects))
    except Exception as error:
        return worker_failure(error, prefix)


def worker_failure(error, prefix):
    import json
    import subprocess
    from .failures import failure_for_http
    code = getattr(error, "code", "")
    code = code.lower() if isinstance(code, str) else ""
    cause = getattr(error, "__cause__", None)
    status = getattr(getattr(error, "response", None), "status_code", None)
    if type(status) is int and 100 <= status <= 599 and not 200 <= status < 300:
        return failure_for_http(status)
    if (isinstance(error, (TimeoutError, subprocess.TimeoutExpired))
            or isinstance(cause, (TimeoutError, subprocess.TimeoutExpired))
            or type(error).__name__ == "XhsProviderTimeoutError" or code in {
        "network_timeout", "execution_timeout", "worker_timeout", "provider_timeout", "parsehub_timeout",
    }):
        kind, suffix = FailureKind.TIMEOUT, "TIMEOUT"
    elif code in {"auth_required", "access_denied", "network_denied", "execution_guard"}:
        kind, suffix = FailureKind.UNAVAILABLE, "UNAVAILABLE"
    elif code in {"provider_unsupported", "parsehub_unsupported_post", "extractor_mismatch"}:
        kind, suffix = FailureKind.UNSUPPORTED, "UNSUPPORTED"
    elif (isinstance(error, (ValueError, TypeError, KeyError)) or isinstance(cause, json.JSONDecodeError)
          or type(error).__name__ == "ExternalCommandOutputTooLarge" or code in {
        "invalid_protocol", "invalid_parse_request", "no_formats", "output_limit", "unsupported_result", "identity_mismatch",
        "provider_output_too_large", "provider_invalid_json", "provider_schema_invalid", "provider_result_empty",
        "parsehub_invalid_json", "parsehub_result_invalid", "parsehub_identity_mismatch", "parsehub_output_too_large",
    }):
        kind, suffix = FailureKind.INVALID_RESULT, "INVALID_RESULT"
    else:
        kind, suffix = FailureKind.UNAVAILABLE, "UNAVAILABLE"
    return Failure(kind, prefix + "_" + suffix)
