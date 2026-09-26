"""Same-host account admission, shared by all TianTian application entry points.

The file is never unlinked: flock must always refer to the same inode. An
interrupted HTTP request leaves an uncertain marker, not a time-based lease
that could expire while the upstream is still working.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import hashlib
import os
from pathlib import Path
import tempfile
import time
from time import monotonic as clock

from ..common import XhsProviderTimeoutError
from ..env_config import runtime_str


class TianTianAdmissionDeferred(XhsProviderTimeoutError):
    provider_outcome = "SKIPPED_ADMISSION"
    provider_failure_class = "NOT_APPLICABLE"

    def __init__(self, reason, queue_wait_ms=0):
        super().__init__(reason)
        self.reason = reason
        self.queue_wait_ms = queue_wait_ms


_active = ContextVar("tiantian_account_slot", default=None)


def lock_root():
    # All workers/worktrees using this OS account share this path, independent
    # of Django cache aliases and database names. Never stores credentials.
    return Path(tempfile.gettempdir()) / f"animeimages-tiantian-{os.getuid()}"


@contextmanager
def account_slot(wait_seconds=30.0, cancelled=None):
    import fcntl

    account = hashlib.sha256(runtime_str("TIANTIANJIEXI_PHONE").strip().encode()).hexdigest()
    current = _active.get()
    if current is not None and current[0] == account and current[3] == os.getpid():
        if os.pread(current[1], 1, 0) == b"U":
            raise TianTianAdmissionDeferred("SERVICE_PREVIOUS_OUTCOME_UNKNOWN")
        yield current[2]
        return
    root = lock_root()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.is_symlink() or root.stat().st_uid != os.getuid() or root.stat().st_mode & 0o077:
        raise TianTianAdmissionDeferred("SERVICE_SERIAL_GUARD_UNAVAILABLE")
    fd = os.open(root / (account + ".lock"), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    started = clock()
    token = None
    acquired = False
    try:
        while True:
            waited = int((clock() - started) * 1000)
            if callable(cancelled) and cancelled():
                raise TianTianAdmissionDeferred("SERVICE_QUEUE_CANCELLED", waited)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except BlockingIOError:
                if clock() - started >= wait_seconds:
                    raise TianTianAdmissionDeferred("SERVICE_QUEUE_DEFERRED", waited)
                time.sleep(min(0.05, max(0, wait_seconds - (clock() - started))))
        if os.pread(fd, 1, 0) == b"U":
            raise TianTianAdmissionDeferred("SERVICE_PREVIOUS_OUTCOME_UNKNOWN", waited)
        token = _active.set((account, fd, waited, os.getpid()))
        yield waited
    finally:
        if token is not None:
            _active.reset(token)
        if acquired:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def request_in_flight(value):
    current = _active.get()
    if current is None:
        raise RuntimeError("TianTian request without account admission")
    os.pwrite(current[1], b"U" if value else b"C", 0)
    os.fsync(current[1])


def serialized(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        deadline = kwargs.get("deadline_monotonic")
        wait = min(30.0, max(0.0, deadline - clock())) if deadline else 30.0
        with account_slot(wait) as waited:
            if deadline and clock() >= deadline:
                raise TianTianAdmissionDeferred("SERVICE_QUEUE_DEFERRED", waited)
            self.queue_wait_ms = waited
            started = clock()
            try:
                return method(self, *args, **kwargs)
            finally:
                self.execution_ms = int((clock() - started) * 1000)
    return wrapped
