"""Opt-in, memory-only raw transport; no application or optional imports.

Unknown/lazy values fail the whole observation. They are never iterated, called,
stringified, or replaced by a partial projection.
"""
import base64
from datetime import date, datetime
from enum import Enum
import json
import logging
import math
import os
import select
import time


MAX_BYTES = 16 * 1024 * 1024
MAX_NODES = 200000
WRITE_TIMEOUT_SECONDS = 2.0
EXIT_HEADROOM_SECONDS = 0.25
logger = logging.getLogger("pipeline_shadow.worker")


def _warn(reason, error):
    try:
        logger.warning(
            "pipeline_shadow_failure provider=unknown difference_code=SHADOW_ERROR "
            "reason=%s exception_class=%s", reason, type(error).__name__,
        )
    except Exception:
        pass


def snapshot(value, state=None, depth=0):
    state = state if state is not None else [0, 0]
    state[0] += 1
    if state[0] > MAX_NODES or depth > 64:
        raise ValueError("raw_limit")
    if value is None or type(value) in (bool, int):
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("raw_number")
        return value
    if type(value) is str:
        state[1] += len(value.encode("utf-8"))
        if state[1] > MAX_BYTES:
            raise ValueError("raw_limit")
        return value
    def child(item):
        return snapshot(item, state, depth + 1)
    if type(value) is dict:
        return {"t": "dict", "v": [[child(key), child(item)] for key, item in value.items()]}
    if type(value) in (list, tuple):
        return {"t": "list" if type(value) is list else "tuple", "v": [child(item) for item in value]}
    if type(value) in (datetime, date):
        return {"t": type(value).__name__, "v": value.isoformat()}
    if type(value) is bytes:
        return {"t": "bytes", "v": child(base64.b64encode(value).decode("ascii"))}
    if isinstance(value, Enum):
        return {"t": "enum", "v": child(value.value), "name": type(value).__name__}
    cls = type(value)
    if cls.__module__.split(".")[0] in {"parsehub", "videodl"}:
        fields = object.__getattribute__(value, "__dict__")
        if type(fields) is dict:
            return {"t": "object", "name": cls.__name__, "module": cls.__module__, "v": child(fields)}
    raise ValueError("raw_unsupported")


class Observer:
    # The older lab observer deliberately rejects collections. This observer
    # must not change the production Worker's bounded collection traversal.
    allow_collections = True

    def __init__(self, fd, *, deadline=None):
        if type(fd) is not int or fd < 3:
            raise ValueError("raw_fd")
        if deadline is not None and (type(deadline) not in (int, float) or not math.isfinite(deadline)):
            raise ValueError("raw_deadline")
        self.fd = fd
        self.deadline = deadline
        self.records = []
        self.state = [0, 0]
        self.failed = False

    def __call__(self, raw):
        if self.failed:
            return
        try:
            self.records.append(snapshot(raw, self.state))
        except Exception as error:
            self.failed = True
            self.records.clear()
            _warn("snapshot_failed", error)

    def finish(self):
        reason = "serialization_failed"
        try:
            if self.failed or not self.records:
                return
            now = time.monotonic()
            deadline = now + WRITE_TIMEOUT_SECONDS
            deadline_reason = "write_deadline"
            if self.deadline is not None:
                parent_limit = self.deadline - EXIT_HEADROOM_SECONDS
                if parent_limit <= now:
                    _warn("capturebudget", TimeoutError())
                    return
                if parent_limit < deadline:
                    deadline = parent_limit
                    deadline_reason = "capturebudget"
            output = json.dumps({"worker_raw_v1": self.records}, ensure_ascii=True,
                                allow_nan=False, separators=(",", ":")).encode("utf-8")
            if len(output) > MAX_BYTES:
                _warn("output_limit", ValueError())
                return
            if time.monotonic() >= deadline:
                _warn(deadline_reason, TimeoutError())
                return
            # Backpressure shares one fixed deadline across all partial writes.
            # On expiry/disconnect the parent rejects the incomplete document.
            reason = "write_failed"
            os.set_blocking(self.fd, False)
            remaining = memoryview(output)
            while remaining:
                if time.monotonic() >= deadline:
                    _warn(deadline_reason, TimeoutError())
                    return
                try:
                    written = os.write(self.fd, remaining[:65536])
                    if written <= 0:
                        _warn("write_closed", BrokenPipeError())
                        return
                    remaining = remaining[written:]
                except BlockingIOError:
                    timeout = deadline - time.monotonic()
                    if timeout <= 0:
                        _warn(deadline_reason, TimeoutError())
                        return
                    try:
                        if not select.select([], [self.fd], [], timeout)[1]:
                            _warn(deadline_reason, TimeoutError())
                            return
                    except InterruptedError:
                        continue
                except InterruptedError:
                    continue
        except Exception as error:
            _warn(reason, error)
        finally:
            try:
                os.close(self.fd)
            except OSError as error:
                _warn("close_failed", error)


def observer(request):
    if not isinstance(request, dict) or "_shadow_capture_fd" not in request:
        return None
    try:
        return Observer(request.get("_shadow_capture_fd"), deadline=request.get("_shadow_capture_deadline"))
    except Exception as error:
        _warn("observer_setup_failed", error)
        return None
