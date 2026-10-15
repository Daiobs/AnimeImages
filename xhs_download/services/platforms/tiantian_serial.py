"""Same-host account admission, shared by all TianTian application entry points.

The file is never unlinked: flock must always refer to the same inode. An
interrupted HTTP request leaves an uncertain marker, not a time-based lease
that could expire while the upstream is still working.
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
from uuid import uuid4

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


def _now():
    return datetime.now(timezone.utc).isoformat()


def _open_account_lock():
    account = hashlib.sha256(runtime_str("TIANTIANJIEXI_PHONE").strip().encode()).hexdigest()
    root = lock_root()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.is_symlink() or root.stat().st_uid != os.getuid() or root.stat().st_mode & 0o077:
        raise TianTianAdmissionDeferred("SERVICE_SERIAL_GUARD_UNAVAILABLE")
    fd = os.open(root / (account + ".lock"), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    stat = os.fstat(fd)
    if stat.st_uid != os.getuid() or stat.st_mode & 0o077:
        os.close(fd)
        raise TianTianAdmissionDeferred("SERVICE_SERIAL_GUARD_UNAVAILABLE")
    return account, fd, root / (account + ".events.jsonl")


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


def _write_state(fd, marker, metadata, journal):
    entry = {**metadata, "marker": marker.decode(), "recorded_at": _now()}
    log = os.open(journal, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        os.write(log, (json.dumps(entry, sort_keys=True)+"\n").encode())
        os.fsync(log)
    finally:
        os.close(log)
    raw = marker + b"\n" + json.dumps(metadata, sort_keys=True).encode()
    os.pwrite(fd, raw, 0)
    os.ftruncate(fd, len(raw))
    os.fsync(fd)


def inspect_or_recover(*, expected_state=None, evidence=None, reference=None):
    """Local operator entry; a free flock alone is never recovery evidence."""
    import fcntl
    account, fd, journal = _open_account_lock()
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise TianTianAdmissionDeferred("SERVICE_ACCOUNT_ACTIVE")
        raw, metadata = _read_state(fd)
        stat = os.fstat(fd)
        state = hashlib.sha256(raw + str((stat.st_ino, stat.st_mtime_ns)).encode()).hexdigest()
        result = {"marker":raw[:1].decode() or "EMPTY", "state_fingerprint":state,
                  "inode":os.fstat(fd).st_ino, "local_active":False,
                  "modified_at":datetime.fromtimestamp(os.fstat(fd).st_mtime, timezone.utc).isoformat(),
                  "metadata":metadata, "legacy_unattributed":len(raw)<=1}
        if expected_state is not None:
            if state != expected_state:
                raise TianTianAdmissionDeferred("SERVICE_RECOVERY_STATE_CHANGED")
            if raw[:1] != b"U":
                raise TianTianAdmissionDeferred("SERVICE_RECOVERY_NOT_UNKNOWN")
            if evidence not in {"remote_terminal_evidence", "human_risk_acceptance"}:
                raise ValueError("explicit recovery evidence required")
            if not reference or len(reference)>120 or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_:" for c in reference):
                raise ValueError("safe evidence reference required")
            recovery = {"operation_id":uuid4().hex, "started_at":_now(),
                        "reason":"RECOVER_FUTURE_REQUESTS_ONLY", "evidence":evidence,
                        "evidence_reference":reference, "previous_state":state,
                        "previous_operation_id":metadata.get("operation_id"),
                        "old_outcome":"UNKNOWN_NOT_REPLAYED"}
            _write_state(fd, b"C", recovery, journal)
            result.update(recovered=True, old_outcome="UNKNOWN_NOT_REPLAYED")
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
        if os.pread(current[1], 1, 0) == b"U":
            raise TianTianAdmissionDeferred("SERVICE_PREVIOUS_OUTCOME_UNKNOWN")
        yield current[2]
        return
    account, fd, journal = _open_account_lock()
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
        raw, _ = _read_state(fd)
        if raw[:1] == b"U":
            raise TianTianAdmissionDeferred("SERVICE_PREVIOUS_OUTCOME_UNKNOWN", waited)
        token = _active.set((account, fd, waited, os.getpid(), journal))
        yield waited
    finally:
        if token is not None:
            _active.reset(token)
        if acquired:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def request_in_flight(value, reason=None):
    current = _active.get()
    if current is None:
        raise RuntimeError("TianTian request without account admission")
    _, previous = _read_state(current[1])
    metadata = ({"operation_id":uuid4().hex, "started_at":_now()} if value else previous)
    metadata = {**metadata, "reason":reason or ("REQUEST_MAY_BE_SENT" if value else "TERMINAL_RESPONSE_RECEIVED")}
    _write_state(current[1], b"U" if value else b"C", metadata, current[4])


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
