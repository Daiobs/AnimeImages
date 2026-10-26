"""Request-local raw observations. No persistence, network or routing decisions.

Explicit context propagation keeps concurrent parses isolated, including calls
that outlive the foreground budget. Disabled hooks do not copy upstream data.
"""
from contextlib import contextmanager
from concurrent.futures import Future
from contextvars import ContextVar, copy_context
from copy import deepcopy
from dataclasses import dataclass, field
import logging
import re
import sys
from threading import Lock


_current = ContextVar("pipeline_raw_capture", default=None)
logger = logging.getLogger("xhs_download.pipeline_shadow")
MAX_CAPTURE_BYTES = 32 * 1024 * 1024
MAX_CAPTURE_RECORDS = 32
_MAX_RAW_NODES = 200000
_LOG_PROVIDERS = frozenset({
    "bilibili_native", "douyin_native", "xhs_native", "tiantianjiexi_generic",
    "kukutool_generic", "parsehub_generic", "parsehub_xhs_images", "videodl_repo",
    "yt_dlp_generic", "gallery_dl_generic", "tikhub", "unknown",
})
_LOG_ALIASES = {
    "tiantianjiexi": "tiantianjiexi_generic", "kukutool": "kukutool_generic",
    "ytdlp": "yt_dlp_generic", "youtube_merge": "yt_dlp_generic", "yt_dlp_merge": "yt_dlp_generic",
    "mobile_feed": "douyin_native", "mobile_share": "douyin_native",
    "native": "unknown",
}


def log_shadow_failure(provider, reason, error=None, code="SHADOW_ERROR"):
    """Log bounded identifiers only; never stringify upstream values/errors."""
    try:
        provider = _LOG_ALIASES.get(provider, provider) if type(provider) is str else "unknown"
        provider = provider if provider in _LOG_PROVIDERS else "unknown"
        reason = reason if type(reason) is str and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", reason) else "invalid_reason"
        code = code if type(code) is str and re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", code) else "SHADOW_ERROR"
        error_type = type(error).__name__ if isinstance(error, BaseException) else ""
        if error_type and not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,79}", error_type):
            error_type = "Exception"
        logger.warning(
            "shadow_failure provider=%s code=%s reason=%s error_type=%s",
            provider, code, reason, error_type,
            extra={"event": "shadow_failure", "provider": provider, "code": code,
                   "reason": reason, "error_type": error_type},
        )
    except Exception:
        # Diagnostics cannot change a legacy request, including a broken handler.
        pass


class _CaptureRejected(ValueError):
    pass


def _record_bytes(record, limit):
    """Retained-allocation estimate, not wire length or a hard RSS bound.

    Count Python shallow sizes plus actual UTF-8 string bytes, including source
    and metadata. Aliases count again (conservatively). UTF-8 scratch allocations
    are at most 64 KiB; no custom protocols, lazy traversal or upstream imports.
    Worker objects already arrive as lossless tagged builtin containers.
    """
    if type(record) is not RawObservation or type(record.provider) is not str or type(record.source_url) is not str:
        raise _CaptureRejected("raw_type")
    size, nodes = sys.getsizeof(record), 0
    active = set()

    def charge(amount):
        nonlocal size
        size += amount
        if size > limit:
            raise _CaptureRejected("byte_limit")

    def visit(value, depth=0):
        nonlocal nodes
        nodes += 1
        if nodes > _MAX_RAW_NODES or depth > 64:
            raise _CaptureRejected("raw_limit")
        kind = type(value)
        if value is None or kind in (bool, int, float, bytes):
            charge(sys.getsizeof(value))
        elif kind is str:
            charge(sys.getsizeof(value))
            for offset in range(0, len(value), 16384):
                charge(len(value[offset:offset + 16384].encode("utf-8", errors="surrogatepass")))
        elif kind in (dict, list, tuple):
            charge(sys.getsizeof(value))
            identity = id(value)
            if identity in active:
                raise _CaptureRejected("raw_cycle")
            active.add(identity)
            try:
                if kind is dict:
                    for key, item in value.items():
                        visit(key, depth + 1)
                        visit(item, depth + 1)
                else:
                    for item in value:
                        visit(item, depth + 1)
            finally:
                active.remove(identity)
        else:
            raise _CaptureRejected("raw_type")

    for value in (record.provider, record.raw, record.source_url, record.metadata):
        visit(value)
    return size


@dataclass(frozen=True)
class RawObservation:
    provider: str
    raw: object
    source_url: str
    metadata: dict = field(default_factory=dict)


class Capture:
    def __init__(self):
        self._lock = Lock()
        self._records = []
        self._raw_bytes = 0
        self._pending_records = 0
        self.errors = set()
        self.result = None
        self.display = None
        self.futures = []
        self.on_settled = None
        self.legacy_sources = {}
        self.legacy_candidates = {}
        self.started = False

    @property
    def records(self):
        with self._lock:
            return tuple(self._records)

    @property
    def raw_bytes(self):
        """Estimated bytes committed or reserved by in-flight copies."""
        with self._lock:
            return self._raw_bytes

    def record_error(self, provider, reason, error=None):
        try:
            with self._lock:
                self.errors.add(provider)
        except Exception as tracking_error:
            log_shadow_failure("unknown", "capture_error_tracking", tracking_error)
        log_shadow_failure(provider, reason, error)

    def add(self, record):
        provider = record.provider if type(record) is RawObservation else "unknown"
        reserved = 0
        try:
            with self._lock:
                if len(self._records) + self._pending_records >= MAX_CAPTURE_RECORDS:
                    raise _CaptureRejected("record_limit")
                available = MAX_CAPTURE_BYTES - self._raw_bytes
            size = _record_bytes(record, available)
            # Estimation is outside the lock; count and bytes are rechecked and
            # reserved together before any copy can run concurrently.
            with self._lock:
                if len(self._records) + self._pending_records >= MAX_CAPTURE_RECORDS:
                    raise _CaptureRejected("record_limit")
                if self._raw_bytes + size > MAX_CAPTURE_BYTES:
                    raise _CaptureRejected("byte_limit")
                self._raw_bytes += size
                self._pending_records += 1
                reserved = size
            copied = RawObservation(record.provider, deepcopy(record.raw), record.source_url, deepcopy(record.metadata))
            with self._lock:
                self._records.append(copied)
                self._pending_records -= 1
                reserved = 0
        except Exception as error:
            if reserved:
                with self._lock:
                    self._raw_bytes -= reserved
                    self._pending_records -= 1
            reason = error.args[0] if type(error) is _CaptureRejected else "capture_copy"
            self.record_error(provider, reason, None if type(error) is _CaptureRejected else error)

    def observe_result(self, result, display=None):
        if result is not None:
            with self._lock:
                if result is not self.result:
                    self.display = None
                self.result = result
                if display is not None:
                    self.display = display


def current_capture():
    return _current.get()


def capture_enabled():
    return _current.get() is not None


def capture_error(provider, source_url="", *, reason="capture_error", error=None):
    session = _current.get()
    if session is not None:
        try:
            session.record_error(provider, reason, error)
        except Exception as tracking_error:
            log_shadow_failure(provider, "capture_error_tracking", tracking_error)


def capture_raw(provider, raw, source_url, **metadata):
    session = _current.get()
    if session is not None:
        try:
            session.add(RawObservation(provider, raw, source_url, metadata))
        except Exception as error:
            capture_error(provider, reason="capture_raw", error=error)


def capture_result(result, display=None):
    session = _current.get()
    if session is not None:
        try:
            session.observe_result(result, display)
        except Exception as error:
            capture_error("unknown", reason="capture_result", error=error)


def capture_legacy_sources(result, sources):
    session = _current.get()
    if session is not None:
        try:
            with session._lock:
                session.legacy_sources[id(result)] = dict(sources)
        except Exception as error:
            capture_error("unknown", reason="capture_legacy_sources", error=error)


def capture_legacy_candidate(record):
    """Old selected-resource evidence, never an adapter's raw input or grant."""
    session = _current.get()
    if session is not None:
        try:
            snapshot = deepcopy({key: record[key] for key in (
                "candidate_id", "url", "provider_id", "public", "headers",
                "service_legacy_item", "filename_title", "headers_present", "requires_cookie",
                "requires_context", "required_headers", "ip_bound",
            ) if key in record})
            # Tokens are not comparison facts, even in this transient snapshot.
            snapshot.get("public", {}).pop("resource_token", None)
            with session._lock:
                session.legacy_candidates[record["candidate_id"]] = snapshot
        except Exception as error:
            capture_error("unknown", reason="capture_legacy_candidate", error=error)


@contextmanager
def collect_raw(session=None):
    session = session or Capture()
    token = _current.set(session)
    try:
        yield session
    finally:
        _current.reset(token)


def submit_with_capture(executor, function, /, *args, **kwargs):
    if not capture_enabled():
        return executor.submit(function, *args, **kwargs)
    context = copy_context()
    future = executor.submit(context.run, function, *args, **kwargs)
    session = _current.get()
    try:
        with session._lock:
            session.futures.append(future)
            callback = session.on_settled
        if callback is not None:
            future.add_done_callback(callback)
    except Exception as error:
        capture_error("unknown", reason="capture_future_tracking", error=error)
    return future


def bind_capture(function):
    if not capture_enabled():
        return function
    context = copy_context()
    session = _current.get()
    finished = Future()
    with session._lock:
        session.futures.append(finished)
        callback = session.on_settled
    if callback is not None:
        finished.add_done_callback(callback)
    def call(*args, **kwargs):
        try:
            return context.copy().run(function, *args, **kwargs)
        finally:
            # Future.done() precedes its callbacks. Track the legacy callback
            # too, so continuation selection is captured before comparison.
            if not finished.done():
                finished.set_result(None)
    return call
