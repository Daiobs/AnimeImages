"""Observation-only integration. Public comparison never waits on the request.

Public persistence is an allowlisted counter projection. Health/Daily retain
scrubbed details on their existing run, never raw upstream bodies or headers.
"""
from concurrent.futures import Future, ThreadPoolExecutor, wait
from contextvars import ContextVar
from functools import wraps
from threading import BoundedSemaphore, Lock
import time

from django.db import OperationalError, close_old_connections, transaction
from django.db.models import F
from django.utils import timezone

from .pipeline_shadow_capture import collect_raw, current_capture, capture_result, log_shadow_failure


_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="pipeline-shadow")
_slots = BoundedSemaphore(16)
_pending = set()
_pending_lock = Lock()
_platforms = frozenset({"bilibili", "douyin", "xiaohongshu", "third_party", "upstream",
                       "youtube", "weibo", "kuaishou", "twitter", "tiktok", "instagram"})
_DB_WRITE_ATTEMPTS = 3
_daily_finished = ContextVar("pipeline_shadow_daily_finished", default=None)


def _database_locked(error):
    if not isinstance(error, OperationalError):
        return False
    cause = error.__cause__
    sqlite_code = getattr(cause, "sqlite_errorcode", None)
    if isinstance(sqlite_code, int):
        return sqlite_code & 0xff in {5, 6}  # SQLITE_BUSY / SQLITE_LOCKED (including extended codes).
    if getattr(cause, "sqlstate", None) in {"40P01", "55P03"}:
        return True  # Deadlock / lock unavailable: the transaction is rolled back.
    message = str(error).lower()
    return message in {"database is locked", "database table is locked", "database schema is locked"} or (
        message.startswith("database table is locked:") or message.startswith("database schema is locked:"))


def _write_telemetry(write, provider, reason, code="SHADOW_ERROR"):
    # Background only. Retry the whole atomic write, not upstream work or all
    # dimensions: a rolled-back increment must never count an attempt twice.
    for attempt in range(_DB_WRITE_ATTEMPTS):
        try:
            with transaction.atomic():
                write()
            return True
        except Exception as error:
            log_shadow_failure(provider, reason, error, code=code)
            if not _database_locked(error) or attempt + 1 == _DB_WRITE_ATTEMPTS:
                return False
            time.sleep(0.05 * (attempt + 1))


def public_enabled():
    try:
        from ..models import ProviderHealthTestSetting
        return ProviderHealthTestSetting.objects.filter(singleton_key=1).values_list(
            "public_shadow_enabled", flat=True).first() is True
    except Exception as error:
        log_shadow_failure("unknown", "public_setting_read", error)
        return False


def persist_public(session, date):
    from ..models import PipelineShadowDailyCount
    from .pipeline_shadow_compare import compare_capture, count_codes
    from .provider_adapters.shadow import provider_key
    close_old_connections()
    try:
        platform = getattr(session.result, "platform", "")
        platform = platform if platform in _platforms else "unknown"
        try:
            details = compare_capture(session, platform=platform)
            codes = set(count_codes(details))
        except Exception as error:
            log_shadow_failure("unknown", "public_compare", error)
            codes = {("unknown", "SHADOW_ERROR")}
        for provider, code in codes:
            # No source, work, title, user, trace, URL or detail is accepted by
            # the public writer. Unknown provider values never become dimensions.
            def increment():
                row, _ = PipelineShadowDailyCount.objects.get_or_create(
                    date=date, platform=platform, provider=provider_key(provider), difference_code=code)
                PipelineShadowDailyCount.objects.filter(pk=row.pk).update(count=F("count") + 1)
            _write_telemetry(increment, provider, "public_persist", code)
    except Exception as error:
        log_shadow_failure("unknown", "public_persist", error)
    finally:
        close_old_connections()


def _after_capture(session, write):
    completion = Future()
    with _pending_lock:
        _pending.add(completion)
    def complete():
        completion.set_result(None)
        with _pending_lock:
            _pending.discard(completion)
    def background():
        close_old_connections()
        try:
            write()
        except Exception as error:
            log_shadow_failure("unknown", "comparison_background", error)
        finally:
            close_old_connections()
            _slots.release()
            complete()
    queued = False
    guard = Lock()
    def settled(_future=None):
        nonlocal queued
        try:
            with guard:
                with session._lock:
                    ready = all(future.done() for future in session.futures)
                if queued or not ready:
                    return
                queued = True
            if _slots.acquire(blocking=False):
                try:
                    _executor.submit(background)
                except Exception as error:
                    log_shadow_failure("unknown", "comparison_submit", error)
                    _slots.release()
                    complete()
            else:
                log_shadow_failure("unknown", "comparison_slots_full")
                complete()
        except Exception as error:
            log_shadow_failure("unknown", "comparison_settle", error)
            if not completion.done():
                complete()
    with session._lock:
        session.on_settled = settled
        futures = tuple(session.futures)
    for future in futures:
        future.add_done_callback(settled)
    settled()


def finish_public(session, date):
    # Existing Provider futures keep their own deadlines. Observe their settled
    # results without waiting, cancelling, or creating more upstream work.
    if session.started or session.records or session.errors:
        _after_capture(session, lambda: persist_public(session, date))


def public_shadow(function):
    @wraps(function)
    def call(*args, **kwargs):
        if current_capture() is not None or not public_enabled():
            return function(*args, **kwargs)
        with collect_raw() as session:
            date = timezone.localdate()
            try:
                return function(*args, **kwargs)
            finally:
                try:
                    finish_public(session, date)
                except Exception as error:
                    log_shadow_failure("unknown", "public_schedule", error)
    return call


def observe_result(function):
    @wraps(function)
    def call(*args, **kwargs):
        session = current_capture()
        if session is not None:
            session.started = True
        try:
            result = function(*args, **kwargs)
        except Exception as error:
            capture_result(getattr(error, "fallback_result", None))
            raise
        capture_result(result)
        return result
    return call


def health_shadow(function):
    @wraps(function)
    def call(context, item):
        with collect_raw() as session:
            run = function(context, item)
        try:
            daily_finished = _daily_finished.get()
            if daily_finished is not None:
                with session._lock:
                    session.futures.append(daily_finished)
            # A background connection must only update a committed health row.
            # Neither comparison nor its write consumes the Daily caller's budget.
            transaction.on_commit(
                lambda: _after_capture(session, lambda: _persist_health(session, run, item)),
                robust=True,
            )
        except Exception as error:
            log_shadow_failure(item.get("provider", "unknown"), "health_schedule", error)
        return run
    return call


def daily_shadow_scope(function):
    """Release shadow writers only after Daily finishes its own business writes."""
    @wraps(function)
    def call(*args, **kwargs):
        if _daily_finished.get() is not None:
            return function(*args, **kwargs)
        finished = Future()
        token = _daily_finished.set(finished)
        try:
            return function(*args, **kwargs)
        finally:
            _daily_finished.reset(token)
            finished.set_result(None)
    return call


def _persist_health(session, run, item):
    provider = item.get("provider", "unknown")
    try:
        from .pipeline_shadow_compare import compare_capture
        details = compare_capture(session, expected_provider=item.get("provider", ""),
                                  platform=getattr(session.result, "platform", ""))
    except Exception as error:
        log_shadow_failure(provider, "health_compare", error)
        details = {"schema": 1, "observations": [{"provider": "unknown",
            "difference_codes": ["SHADOW_ERROR"], "differences": [],
            "field_coverage": {}, "fact_gaps": [],
            "invariants": {"checked": False, "violations": []}}]}
    # Keep the baseline business record/result even if shadow storage is
    # unavailable; no second call to record() or to an upstream.
    if _write_telemetry(lambda: type(run).objects.filter(pk=run.pk).update(shadow_details=details),
                        provider, "health_persist"):
        run.shadow_details = details


def wait_for_shadow(timeout=10):
    """Deterministic local/contract-test drain; never called by a request."""
    with _pending_lock:
        futures = tuple(_pending)
    return wait(futures, timeout=timeout).not_done
