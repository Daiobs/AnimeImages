import hashlib
import ipaddress
import logging
import time
from dataclasses import dataclass

from django.conf import settings
from django.core.cache import cache

from .env_config import runtime_list

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


def _client_address(value):
    value = str(value or "").strip()
    if "%" in value:
        return None
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return None
    return getattr(address, "ipv4_mapped", None) or address


def _trusted_proxy_networks():
    try:
        networks = tuple(
            ipaddress.ip_network(value, strict=True)
            for value in runtime_list("DOWNLOAD_TRUSTED_PROXY_CIDRS")
        )
    except (TypeError, ValueError):
        return ()
    # An invalid or universal trust configuration fails closed as a whole.
    if any(network.prefixlen == 0 for network in networks):
        return ()
    return networks


def get_client_ip(request):
    peer = _client_address(request.META.get("REMOTE_ADDR"))
    if peer is None:
        return "unknown"
    if not getattr(settings, "DOWNLOAD_TRUST_X_FORWARDED_FOR", False):
        return str(peer)
    networks = _trusted_proxy_networks()
    if not any(peer in network for network in networks):
        return str(peer)
    forwarded = str(request.META.get("HTTP_X_FORWARDED_FOR") or "")
    if not forwarded:
        return str(peer)
    chain = forwarded.rsplit(",", 20)
    current = peer
    parsed_length = 0
    # Each trusted proxy appends its peer. Never cross an untrusted/invalid boundary.
    # Ignore untrusted prefixes before enforcing limits, so padding cannot change identity.
    for index, value in enumerate(reversed(chain)):
        if not any(current in network for network in networks):
            break
        parsed_length += len(value) + 1
        if index >= 20 or parsed_length > 2048:
            return str(peer)
        current = _client_address(value)
        if current is None:
            return str(peer)
    return str(current)


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
