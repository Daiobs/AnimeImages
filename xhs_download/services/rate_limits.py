import hashlib
import logging
import time
from dataclasses import dataclass

from django.conf import settings
from django.core.cache import cache


DAY_SECONDS = 24 * 60 * 60
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LimitResult:
    allowed: bool
    current: int = 0
    limit: int = 0
    retry_after: int = 0


class FixedWindowReservation:
    """A fixed-window slot that is counted only after the caller commits it."""

    def __init__(self, result, *, key="", amount=1, active=False):
        self.result = result
        self._key = str(key or "")
        self._amount = max(1, int(amount or 1))
        self._active = bool(active and self._key)

    def commit(self):
        self._active = False

    def release(self):
        if not self._active:
            return
        self._active = False
        try:
            remaining = cache.decr(self._key, self._amount)
            if remaining < 0:
                cache.set(self._key, 0, timeout=5)
        except ValueError:
            return
        except Exception:
            logger.exception("fixed_window_reservation_release_failed")


def get_client_ip(request):
    remote_addr = (request.META.get("REMOTE_ADDR") or "unknown").strip()
    if getattr(settings, "DOWNLOAD_TRUST_X_FORWARDED_FOR", False):
        forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR", "")
        first_forwarded = forwarded_for.split(",", 1)[0].strip()
        if first_forwarded:
            remote_addr = first_forwarded
    return remote_addr


def get_client_identity(request):
    remote_addr = get_client_ip(request)
    return hashlib.sha256(remote_addr.encode("utf-8")).hexdigest()[:24]


def _increment(key, timeout, amount=1):
    cache.add(key, 0, timeout=timeout)
    try:
        return cache.incr(key, amount)
    except ValueError:
        cache.set(key, amount, timeout=timeout)
        return amount


def consume_fixed_window(scope, identity, limit, period_seconds, amount=1):
    limit = int(limit or 0)
    if limit <= 0:
        return LimitResult(allowed=True)

    now = int(time.time())
    window = now // period_seconds
    retry_after = period_seconds - (now % period_seconds)
    key = f"limits:{scope}:{identity}:{window}"
    current = _increment(key, timeout=retry_after + 5, amount=amount)
    return LimitResult(
        allowed=current <= limit,
        current=current,
        limit=limit,
        retry_after=retry_after,
    )


def reserve_fixed_window(scope, identity, limit, period_seconds, amount=1):
    """Reserve a provider-call slot that can be released before network I/O."""
    limit = int(limit or 0)
    if limit <= 0:
        return FixedWindowReservation(LimitResult(allowed=True))

    now = int(time.time())
    window = now // period_seconds
    retry_after = period_seconds - (now % period_seconds)
    key = f"limits:{scope}:{identity}:{window}"
    current = _increment(key, timeout=retry_after + 5, amount=amount)
    result = LimitResult(
        allowed=current <= limit,
        current=current,
        limit=limit,
        retry_after=retry_after,
    )
    reservation = FixedWindowReservation(
        result,
        key=key,
        amount=amount,
        active=True,
    )
    if not result.allowed:
        reservation.release()
    return reservation


def get_daily_total(scope, identity):
    day = time.strftime("%Y-%m-%d", time.gmtime())
    return int(cache.get(f"limits:{scope}:{identity}:{day}", 0) or 0)


def consume_daily(scope, identity, limit, amount=1):
    limit = int(limit or 0)
    if limit <= 0:
        return LimitResult(allowed=True)

    now = int(time.time())
    day = time.strftime("%Y-%m-%d", time.gmtime(now))
    retry_after = DAY_SECONDS - (now % DAY_SECONDS)
    key = f"limits:{scope}:{identity}:{day}"
    current = _increment(key, timeout=retry_after + DAY_SECONDS, amount=amount)
    return LimitResult(
        allowed=current <= limit,
        current=current,
        limit=limit,
        retry_after=retry_after,
    )


def has_daily_capacity(scope, identity, limit):
    limit = int(limit or 0)
    if limit <= 0:
        return LimitResult(allowed=True)

    current = get_daily_total(scope, identity)
    return LimitResult(
        allowed=current < limit,
        current=current,
        limit=limit,
    )


def acquire_concurrency(scope, identity, limit, ttl_seconds=30 * 60):
    limit = int(limit or 0)
    if limit <= 0:
        return LimitResult(allowed=True)

    key = f"limits:{scope}:{identity}"
    current = _increment(key, timeout=ttl_seconds)
    if current <= limit:
        return LimitResult(allowed=True, current=current, limit=limit)

    release_concurrency(scope, identity)
    return LimitResult(allowed=False, current=current, limit=limit)


def release_concurrency(scope, identity):
    key = f"limits:{scope}:{identity}"
    try:
        cache.decr(key)
    except ValueError:
        return
    except Exception:
        logger.exception(
            "download_concurrency_release_failed",
            extra={"scope": scope, "identity": identity},
        )
