"""Opt-in, memory-only raw transport; no application or optional imports.

Unknown/lazy values fail the whole observation. They are never iterated, called,
stringified, or replaced by a partial projection.
"""
import base64
from datetime import date, datetime
from enum import Enum
import json
import math
import os


MAX_BYTES = 16 * 1024 * 1024
MAX_NODES = 200000


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

    def __init__(self, fd):
        if type(fd) is not int or fd < 3:
            raise ValueError("raw_fd")
        self.fd = fd
        self.records = []
        self.state = [0, 0]
        self.failed = False

    def __call__(self, raw):
        if self.failed:
            return
        try:
            self.records.append(snapshot(raw, self.state))
        except Exception:
            self.failed = True
            self.records.clear()

    def finish(self):
        try:
            if self.failed or not self.records:
                return
            output = json.dumps({"worker_raw_v1": self.records}, ensure_ascii=True,
                                allow_nan=False, separators=(",", ":")).encode("utf-8")
            if len(output) > MAX_BYTES:
                return
            # A stuck/disappearing reader cannot block the legacy result. A
            # partial JSON document is rejected as capture_error by the parent.
            os.set_blocking(self.fd, False)
            remaining = memoryview(output)
            while remaining:
                try:
                    written = os.write(self.fd, remaining[:65536])
                    remaining = remaining[written:]
                except BlockingIOError:
                    return
        except Exception:
            pass
        finally:
            try:
                os.close(self.fd)
            except OSError:
                pass


def observer(request):
    try:
        return Observer(request.get("_shadow_capture_fd"))
    except Exception:
        return None
