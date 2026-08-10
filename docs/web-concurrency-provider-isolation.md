# Web Concurrency And Provider Isolation

Status: unreleased candidate, not deployed.

## Runtime Candidate

The repository-controlled unit is `ops/systemd/animeimages.service`:

```text
worker_class = gthread
workers = 2
threads = 4
timeout = 60
```

This is approximately eight active synchronous I/O request slots. It is not a
license for eight TianTianJieXi upstream calls. All real TianTianJieXi parsing
uses one Redis-backed lease shared by Single, Batch, Provider-race threads, and
Gunicorn worker processes.

The current production unit was not changed or read during this development
task. Until a separately approved rollout compares the live unit with the
tracked template, record the difference as `PRODUCTION_UNIT_DRIFT`.

## TianTianJieXi Lease

- Effective global parse concurrency is fixed at one.
- A payload cache hit returns before either the single-flight or global lease.
- The same-source single flight waits at most 200 ms for a cache fill.
- Global acquisition waits for the configured short interval and is capped at
  250 ms.
- That wait is charged to the caller's existing Provider deadline; it does not
  extend the configured Provider timeout.
- A busy or unavailable shared lease records a URL-free `provider_busy` event
  and continues through the existing Provider fallback chain.
- Acquisition uses atomic cache `add` (`SET NX EX` on Django's Redis backend).
- Release uses an owner token and Redis compare-and-delete, so an expired old
  owner cannot delete a newer lease.
- The TTL is sized from both the assigned parse budget and the scalar HTTP
  connect/read timeout window, defaults to at least 30 seconds, and is capped
  at 120 seconds.
- A worker crash therefore cannot leak the slot indefinitely.
- Production fails closed when TianTianJieXi is enabled without the shared
  Redis backend.
- Account-status refresh uses a separate ten-second lease and its existing
  response cache; it does not consume the parse slot.

The legacy `TIANTIANJIEXI_GLOBAL_MAX_CONCURRENCY` environment value should be
`1`. The application enforces one regardless of a stale larger value and emits
a system-check warning until the environment is aligned.

## Local Gunicorn Benchmark

Date: 2026-08-10. Host: Apple M4, 16 GiB. Runtime: Python 3.14.5,
Gunicorn 26.0.0, Redis 6.2.14 through an isolated Redislite process, and an
isolated migrated SQLite database. No production service or real Provider was
contacted.

Both modes served the real Django WSGI application. A temporary, uncommitted
WSGI wrapper added a one-second ordinary-parser route, a one-second Batch route,
and a three-second fake TianTianJieXi route using the production lease helper.
Mixed requests also included the real homepage, liveness endpoint, Batch page,
and Admin login page. Each workload was issued concurrently.

| Mode | Requests | Success | 5xx | Wall ms | Requests/s | All p50 ms | All p95 ms | Non-Provider p50 ms | Non-Provider p95 ms | Peak CPU | Peak RSS MiB |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 2 x sync | 4 | 4 | 0 | 3008.38 | 1.330 | 1037.94 | 3006.32 | 1024.33 | 1051.55 | 24.3% | 170.11 |
| 2 x sync | 8 | 8 | 0 | 3008.03 | 2.660 | 2062.94 | 3005.94 | 2062.94 | 2080.92 | 6.0% | 171.56 |
| 2 x sync | 12 | 12 | 0 | 4011.44 | 2.991 | 3119.53 | 4008.55 | 3123.32 | 4008.55 | 4.1% | 171.83 |
| 2 x 4 gthread | 4 | 4 | 0 | 3037.04 | 1.317 | 515.17 | 3036.51 | 15.67 | 1014.68 | 49.2% | 173.92 |
| 2 x 4 gthread | 8 | 8 | 0 | 3015.96 | 2.653 | 40.27 | 3015.04 | 18.50 | 1014.36 | 12.2% | 175.23 |
| 2 x 4 gthread | 12 | 12 | 0 | 3017.48 | 3.977 | 70.57 | 3016.76 | 74.27 | 1015.20 | 10.9% | 176.27 |

The three-second real fake-Provider winner remains in the overall p95 by
design. The useful isolation signal is the non-Provider latency: at 12
concurrent requests its p95 fell from about 4.01 seconds to 1.02 seconds. Total
wall time fell about 25%, throughput increased about 33%, and peak RSS rose by
about 4.4 MiB. No workload produced a 5xx.

For the 8- and 12-request runs, fake Provider requests were handled by both
Gunicorn worker PIDs. In every sync and gthread run:

```text
max_observed_tiantian_upstream_concurrency = 1
```

At eight requests, one fake request used the real slot and one returned
`provider_busy`. At twelve requests, one used the real slot and two returned
`provider_busy`. Busy responses completed through the bounded fallback path
instead of queueing behind the three-second upstream.

The benchmark supports the `2 x 4 gthread` candidate. It does not constitute a
production rollout or replace post-deploy latency, CPU, RSS, Redis, and
fallback-rate monitoring.

## Thread-Safety Audit

| Area | Finding |
| --- | --- |
| Adapter registry | Adapter instances are process singletons, but parse context and result state are request-local. The default adapters do not mutate request data on the instance. |
| TianTianJieXi client | The default client uses the `requests` module, not a shared mutable `requests.Session`. Provider instances keep no request payload or response state. |
| Douyin race executor | `ThreadPoolExecutor` is designed for concurrent submission. The Redis lease is below every TianTianJieXi race/single/Batch call. |
| KuKuTool runtime | Existing mutable executor, capacity, and local-future maps are protected by `Lock`, `RLock`, or `BoundedSemaphore`. Their capacity remains process-local; this PR does not claim a global KuKuTool limit. |
| Download handoff | Tickets and status are cache records. Live responses, sockets, and iterators are not shared through cache. |
| Download tasks | The local in-memory task map is lock-protected and remains disabled for production use. This candidate does not make it a multi-process task queue. |
| Rate limits | Production counters use atomic Redis cache operations. No database cursor or transaction object is shared between threads. |
| Database | Django owns thread-local connections; application code does not retain cursors or transactions in module globals. |
| Redis | Django's Redis backend uses a thread-safe connection pool. Lease release is one atomic Lua compare-and-delete operation. |
| Temporary files | Download/archive/external-tool paths allocate request/task-local temporary files and do not share a mutable filename buffer. |
| Module constants | Provider maps, headers, allowlists, and policy tables are read-only in normal request handling. |

No correctness blocker requiring `GTHREAD_THREAD_SAFETY_BLOCKED` was found.
The local-only task store and process-local KuKuTool capacity remain explicit
architectural limits, not hidden guarantees.

## Rollout And Rollback

Rollout is a separate production task:

1. Confirm production PostgreSQL and Redis readiness.
2. Set the legacy TianTianJieXi global limit to `1` and keep the lease TTL/wait
   at reviewed bounded values.
3. Compare the live unit with `ops/systemd/animeimages.service`.
4. Stage the unit and run `systemd-analyze verify` on Linux.
5. Use the normal reviewed installation and restart procedure.
6. Verify liveness/readiness, homepage, Admin-safe GET, Single, Batch, and
   `provider_busy` fallback telemetry.
7. Monitor worker CPU/RSS, p50/p95, 5xx, Redis errors, lease busy rate, and
   TianTianJieXi upstream concurrency.

Rollback restores the prior two-worker sync unit and restarts through the same
reviewed procedure. The Redis lease may remain because it is independent of
Gunicorn's worker class and protects the upstream in either mode. No database
or migration rollback is required.
