"""Request-local raw observations. No persistence, network or routing decisions.

Explicit context propagation keeps concurrent parses isolated, including calls
that outlive the foreground budget. Disabled hooks do not copy upstream data.
"""
from contextlib import contextmanager
from concurrent.futures import Future
from contextvars import ContextVar, copy_context
from copy import deepcopy
from dataclasses import dataclass, field
from threading import Lock


_current = ContextVar("pipeline_raw_capture", default=None)


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

    def add(self, record):
        with self._lock:
            # Bound memory independently of upstream collection sizes. Never
            # fail the old request when a shadow observation is unavailable.
            if len(self._records) < 32:
                self._records.append(record)
            else:
                self.errors.add(record.provider)

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


def capture_error(provider, source_url=""):
    session = _current.get()
    if session is not None:
        try:
            with session._lock:
                session.errors.add(provider)
        except Exception:
            pass


def capture_raw(provider, raw, source_url, **metadata):
    session = _current.get()
    if session is not None:
        try:
            # Later legacy normalization may mutate the object. Capture before
            # that mutation, but never expose this snapshot to a response/cache.
            session.add(RawObservation(provider, deepcopy(raw), source_url, deepcopy(metadata)))
        except Exception:
            capture_error(provider)


def capture_result(result, display=None):
    session = _current.get()
    if session is not None:
        try:
            session.observe_result(result, display)
        except Exception:
            capture_error("unknown")


def capture_legacy_sources(result, sources):
    session = _current.get()
    if session is not None:
        try:
            with session._lock:
                session.legacy_sources[id(result)] = dict(sources)
        except Exception:
            capture_error("unknown")


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
        except Exception:
            capture_error("unknown")


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
    except Exception:
        capture_error("unknown")
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
