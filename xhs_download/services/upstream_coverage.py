"""Bounded presentation of the local extractor inventory, without URL probes."""

import logging
import time
from threading import Lock

from .provider_access import (
    PROVIDER_THIRD_PARTY, UPSTREAM_PROVIDER_IDS, provider_access_summary,
    request_user_is_staff, upstream_match_is_blocked, upstream_match_is_public_allowed,
)

logger = logging.getLogger(__name__)
MAX_COVERAGE_ROWS = 4096
COVERAGE_CACHE_SECONDS = 300
_inventory = ()
_expires_at = 0.0
_inventory_lock = Lock()


def load_coverage_snapshot():
    from .upstream_matching import coverage_snapshot

    return coverage_snapshot()


def normalize_coverage_snapshot(snapshot):
    if not isinstance(snapshot, (dict, list, tuple)):
        return ()
    providers = snapshot.get("providers", snapshot) if isinstance(snapshot, dict) else snapshot
    if isinstance(providers, dict):
        providers = [dict(value, provider_id=key) for key, value in providers.items()
                     if key in UPSTREAM_PROVIDER_IDS and isinstance(value, dict)]
    if not isinstance(providers, (list, tuple)):
        return ()
    rows = []
    seen = set()
    for provider in providers[:len(UPSTREAM_PROVIDER_IDS)]:
        if not isinstance(provider, dict) or provider.get("provider_id") not in UPSTREAM_PROVIDER_IDS:
            continue
        provider_id = provider["provider_id"]
        entries = provider.get("extractors", provider.get("entries", ()))
        if not isinstance(entries, (list, tuple)):
            continue
        for entry in entries[:MAX_COVERAGE_ROWS]:
            if isinstance(entry, str):
                entry = {"extractor_id": entry, "label": entry}
            if not isinstance(entry, dict):
                continue
            extractor_id = str(entry.get("extractor_id") or "")[:240]
            identity = (provider_id, extractor_id)
            if not extractor_id or identity in seen:
                continue
            seen.add(identity)
            rows.append({
                "provider_id": provider_id,
                "extractor_id": extractor_id,
                "label": str(entry.get("label") or extractor_id)[:240],
                "version": str(entry.get("version") or provider.get("version") or "")[:80],
                "available": provider.get("available", True) is not False and entry.get("available", True) is not False,
            })
            if len(rows) >= MAX_COVERAGE_ROWS:
                return tuple(rows)
    return tuple(rows)


def cached_coverage_inventory():
    global _inventory, _expires_at
    if time.monotonic() < _expires_at:
        return _inventory
    # Concurrent requests use the previous snapshot rather than queue inventory work.
    if not _inventory_lock.acquire(blocking=False):
        return _inventory
    try:
        if time.monotonic() >= _expires_at:
            try:
                _inventory = normalize_coverage_snapshot(load_coverage_snapshot())
            except Exception:
                logger.exception("upstream_coverage_snapshot_unavailable")
                _inventory = ()
            _expires_at = time.monotonic() + COVERAGE_CACHE_SECONDS
        return _inventory
    finally:
        _inventory_lock.release()


def coverage_rows_for_request(request, runtime_setting):
    from .platforms.third_party import provider_is_enabled

    access = provider_access_summary(PROVIDER_THIRD_PARTY, request, runtime_setting)
    staff = request_user_is_staff(request)
    enabled = {provider: provider_is_enabled(provider) for provider in UPSTREAM_PROVIDER_IDS}
    rows = []
    for entry in cached_coverage_inventory():
        allowed = bool(
            entry["available"] and access["request_allowed"] and enabled[entry["provider_id"]]
            and not upstream_match_is_blocked(entry, runtime_setting)
            and (staff or upstream_match_is_public_allowed(entry, "", runtime_setting))
        )
        rows.append({**entry, "allowed": allowed, "status_label": "可尝试" if allowed else "暂不可用"})
    return rows
