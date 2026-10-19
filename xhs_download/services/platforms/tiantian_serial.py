"""Same-host account serialization, without persistent outcome admission gates.

The lock file is never unlinked, so existing processes use the same inode.
Legacy outcome records are retained for inspection but cannot block new work.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import hashlib
import json
from datetime import datetime, timezone
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


def _open_account_lock(*, create=True):
    account = hashlib.sha256(runtime_str("TIANTIANJIEXI_PHONE").strip().encode()).hexdigest()
    root = lock_root()
    if create:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.is_symlink() or root.stat().st_uid != os.getuid() or root.stat().st_mode & 0o077:
        raise TianTianAdmissionDeferred("SERVICE_SERIAL_GUARD_UNAVAILABLE")
    flags = (os.O_RDWR | os.O_CREAT) if create else os.O_RDONLY
    fd = os.open(root / (account + ".lock"), flags | os.O_NOFOLLOW, 0o600)
    stat = os.fstat(fd)
    if stat.st_uid != os.getuid() or stat.st_mode & 0o077:
        os.close(fd)
        raise TianTianAdmissionDeferred("SERVICE_SERIAL_GUARD_UNAVAILABLE")
    return account, fd


def _read_state(fd):
    raw = os.pread(fd, 16385, 0)
    if len(raw) > 16384 or raw[:1] not in (b"", b"U", b"C"):
        raise TianTianAdmissionDeferred("SERVICE_SERIAL_STATE_INVALID")
    metadata = {}
    if len(raw) > 1:
        try:
            metadata = json.loads(raw[2:])
            if not isinstance(metadata, dict):
                raise ValueError('invalid metadata')
        except (ValueError, UnicodeError):
            raise TianTianAdmissionDeferred("SERVICE_SERIAL_STATE_INVALID")
    return raw, metadata


def inspect_account_state():
    """Read local serialization status; legacy outcomes are historical only."""
    import fcntl
    result = {
        "mode": "serial_only",
        "unknown_outcome_blocks_requests": False,
        "recovery_required": False,
        "lock_present": False,
        "local_active": False,
    }
    try:
        _, fd = _open_account_lock(create=False)
    except FileNotFoundError:
        return result
    try:
        result.update(lock_present=True, inode=os.fstat(fd).st_ino)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            result["local_active"] = True
            return result
        try:
            raw, metadata = _read_state(fd)
            result["legacy_state"] = {
                "marker": raw[:1].decode() or "EMPTY",
                "modified_at": datetime.fromtimestamp(os.fstat(fd).st_mtime, timezone.utc).isoformat(),
                "metadata": metadata,
                "legacy_unattributed": len(raw) <= 1,
            }
        except TianTianAdmissionDeferred:
            result["legacy_state"] = {"invalid": True}
        return result
    finally:
        os.close(fd)


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
        yield current[2]
        return
    account, fd = _open_account_lock()
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
        token = _active.set((account, fd, waited, os.getpid()))
        yield waited
    finally:
        if token is not None:
            _active.reset(token)
        if acquired:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


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
