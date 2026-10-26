"""Opt-in memory pipe, independent of legacy output caps and return timing."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import os
from threading import BoundedSemaphore
import time

from .pipeline_shadow_capture import capture_enabled, capture_error, capture_raw, submit_with_capture


MAX_RAW_BYTES = 16 * 1024 * 1024
_slots = BoundedSemaphore(8)
_readers = ThreadPoolExecutor(max_workers=8, thread_name_prefix="worker-raw-sideband")


def _receive(read_fd, provider, source_url, metadata):
    chunks, size, overflow = [], 0, False
    try:
        while chunk := os.read(read_fd, 65536):
            size += len(chunk)
            if size > MAX_RAW_BYTES:
                overflow = True
                chunks.clear()
            elif not overflow:
                chunks.append(chunk)
        if overflow:
            raise ValueError("sideband_limit")
        envelope = json.loads(b"".join(chunks))
        if (type(envelope) is not dict or set(envelope) != {"worker_raw_v1"}
                or type(envelope["worker_raw_v1"]) is not list or not envelope["worker_raw_v1"]):
            raise ValueError("sideband_invalid")
        capture_raw(provider, envelope, source_url, **metadata)
    except Exception as error:
        capture_error(provider, source_url, reason="worker_receive", error=error)
    finally:
        os.close(read_fd)
        _slots.release()


@contextmanager
def worker_capture(request, provider, source_url, *, enabled=True, timeout_seconds=None, **metadata):
    if not enabled or not capture_enabled():
        yield request, {}
        return
    read_fd = write_fd = None
    acquired = False
    failure_reason = "worker_setup"
    deadline = None
    try:
        if timeout_seconds is not None:
            # Start earlier than the runner's clock, never extend its deadline.
            deadline = time.monotonic() + max(0.1, float(timeout_seconds or 15.0))
        acquired = _slots.acquire(blocking=False)
        if os.name != "posix" or not acquired:
            failure_reason = "worker_slots_full" if not acquired else "worker_setup"
            raise OSError("sideband_unavailable")
        read_fd, write_fd = os.pipe()
        # Bind context and register before execution; old callers never wait.
        submit_with_capture(_readers, _receive, read_fd, provider, source_url, metadata)
    except Exception as error:
        for fd in (read_fd, write_fd):
            if fd is not None:
                os.close(fd)
        if acquired:
            _slots.release()
        read_fd = write_fd = None
        capture_error(provider, source_url, reason=failure_reason, error=error)
    try:
        if write_fd is None:
            yield request, {}
        else:
            payload = {**request, "_shadow_capture_fd": write_fd}
            if deadline is not None:
                payload["_shadow_capture_deadline"] = deadline
            yield payload, {"pass_fds": (write_fd,)}
    finally:
        if write_fd is not None:
            os.close(write_fd)


def run_with_capture(run, command, request, provider, source_url, *, enabled=True, **kwargs):
    with worker_capture(request, provider, source_url, enabled=enabled,
                        timeout_seconds=kwargs.get("timeout_seconds")) as (payload, options):
        return run(command, input_text=json.dumps(payload), **kwargs, **options)
