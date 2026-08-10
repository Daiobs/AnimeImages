import logging
import math
import threading
import time
from dataclasses import dataclass
from uuid import uuid4

from django.core.cache import caches


logger = logging.getLogger(__name__)

TIANTIANJIEXI_PARSE_LEASE_KEY = "provider-lease:tiantianjiexi:parse"
TIANTIANJIEXI_GLOBAL_MAX_CONCURRENCY = 1
MAX_PROVIDER_LEASE_WAIT_SECONDS = 0.25
_LOCAL_CACHE_ATOMIC_LOCK = threading.RLock()
_COMPARE_AND_DELETE_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""


def cache_backend_is_cross_process(cache_backend=None):
    backend = cache_backend or caches["default"]
    backend_class = type(backend)
    return (
        backend_class.__module__ == "django.core.cache.backends.redis"
        and backend_class.__name__ == "RedisCache"
    )


def _redis_compare_and_delete(cache_backend, key, token):
    safe_key = cache_backend.make_and_validate_key(key)
    client_wrapper = cache_backend._cache
    client = client_wrapper.get_client(safe_key, write=True)
    serialized_token = client_wrapper._serializer.dumps(token)
    return bool(
        client.eval(
            _COMPARE_AND_DELETE_SCRIPT,
            1,
            safe_key,
            serialized_token,
        )
    )


def release_owned_cache_key(cache_backend, key, token):
    """Delete a cache lease only when the caller still owns its token."""
    try:
        if cache_backend_is_cross_process(cache_backend):
            return _redis_compare_and_delete(cache_backend, key, token)
        with _LOCAL_CACHE_ATOMIC_LOCK:
            if cache_backend.get(key) != token:
                return False
            return bool(cache_backend.delete(key))
    except Exception:
        logger.exception(
            "provider_lease_release_failed",
            extra={"lease_key": key},
        )
        return False


@dataclass
class CacheLease:
    cache_backend: object
    key: str
    token: str
    ttl_seconds: int
    _released: bool = False

    def release(self):
        if self._released:
            return False
        self._released = True
        return release_owned_cache_key(
            self.cache_backend,
            self.key,
            self.token,
        )


@dataclass(frozen=True)
class CacheLeaseAttempt:
    acquired: bool
    reason: str
    waited_ms: int
    shared_backend: bool
    lease: CacheLease | None = None


def acquire_cache_lease(
    key,
    *,
    ttl_seconds,
    wait_seconds=0.0,
    require_cross_process=True,
    cache_backend=None,
    token=None,
    monotonic=time.monotonic,
    sleeper=time.sleep,
):
    backend = cache_backend or caches["default"]
    shared_backend = cache_backend_is_cross_process(backend)
    started_at = monotonic()
    if require_cross_process and not shared_backend:
        return CacheLeaseAttempt(
            acquired=False,
            reason="shared_cache_required",
            waited_ms=0,
            shared_backend=False,
        )

    ttl_seconds = max(1, int(math.ceil(float(ttl_seconds or 0))))
    wait_seconds = max(
        0.0,
        min(MAX_PROVIDER_LEASE_WAIT_SECONDS, float(wait_seconds or 0.0)),
    )
    deadline = started_at + wait_seconds
    owner_token = token or uuid4().hex

    while True:
        try:
            if shared_backend:
                acquired = backend.add(key, owner_token, timeout=ttl_seconds)
            else:
                with _LOCAL_CACHE_ATOMIC_LOCK:
                    acquired = backend.add(key, owner_token, timeout=ttl_seconds)
        except Exception:
            logger.exception(
                "provider_lease_acquire_failed",
                extra={"lease_key": key},
            )
            return CacheLeaseAttempt(
                acquired=False,
                reason="lease_backend_error",
                waited_ms=max(0, int((monotonic() - started_at) * 1000)),
                shared_backend=shared_backend,
            )

        if acquired:
            return CacheLeaseAttempt(
                acquired=True,
                reason="acquired",
                waited_ms=max(0, int((monotonic() - started_at) * 1000)),
                shared_backend=shared_backend,
                lease=CacheLease(
                    cache_backend=backend,
                    key=key,
                    token=owner_token,
                    ttl_seconds=ttl_seconds,
                ),
            )

        remaining = deadline - monotonic()
        if remaining <= 0:
            return CacheLeaseAttempt(
                acquired=False,
                reason="provider_busy",
                waited_ms=max(0, int((monotonic() - started_at) * 1000)),
                shared_backend=shared_backend,
            )
        sleeper(min(0.025, remaining))
