# Web Concurrency And Provider Isolation

Date: 2026-08-14

## Canonical State

Production and the reconciled repository use Gunicorn with two workers and four
gthreads per worker. This provides bounded Web I/O concurrency while retaining
the existing 60-second request timeout.

TianTianJieXi upstream concurrency is an independent Redis-backed counter:

```text
TIANTIANJIEXI_GLOBAL_MAX_CONCURRENCY=2
TIANTIANJIEXI_REQUEST_LIMIT_PER_MINUTE=20
```

- Payload-cache hits return before single-flight and global concurrency
  reservation.
- Same-source cache misses use single-flight coordination.
- A real upstream attempt reserves one global counter slot and releases it on
  success or failure.
- A third concurrent attempt is rejected through the existing bounded Provider
  fallback path.
- Web thread count does not silently alter the Provider counter.

## Superseded PR #30 Lease

PR #30 combined the useful 2x4 gthread topology with a token-owned Redis lease
that fixed TianTianJieXi to one real request. Production later established and
accepted an effective Provider concurrency intent of two. The fixed-at-one
lease, its wait interval, and its lease TTL are therefore historical and are
not part of the canonical runtime.

The focused regression suite verifies two reservations are allowed, a third is
rejected, release restores capacity, cache hits bypass the counter, failures
release it, and the tracked systemd unit remains 2x4 gthread.

## Operational Boundary

The tracked unit in `ops/systemd/animeimages.service` is version-controlled;
merging it does not install or restart the service. Any topology or Provider
limit change requires a separate production task with Redis readiness, p50/p95,
fallback rate, CPU/RSS, health, and rollback checks.
