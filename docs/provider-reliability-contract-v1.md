# Provider Reliability Contract v1

Date: 2026-08-15

## Planes And Authority

Provider parsing and ordinary-media delivery are separate systems:

```text
CONTROL PLANE
Browser -> AnimeImages -> Provider parse API

MEDIA PLANE
Provider/CDN -> Browser Service Worker -> Browser Download Manager -> Disk

CLIENT DIAGNOSTICS
Browser -> AnimeImages small sanitized JSON
```

AnimeImages remains the resolution and handoff control plane for ordinary
Provider video. It does not relay ordinary media bytes. Browser telemetry is
client-observed, non-authoritative evidence: it cannot call a Provider, change
a cache, consume an attempt, alter routing, or trigger a reparse.

## Runtime Contract

The accepted Douyin Ultra behavior remains unchanged:

- TianTianJieXi, KuKuTool, and Native start at T=0 for a fresh race.
- Native remains hidden during the six-second foreground window.
- Already-started Provider work may be observed by bounded continuation.
- Provider order remains TianTianJieXi, KuKuTool, Native. Batch remains
  TianTianJieXi, Native.
- One fresh Ultra race consumes one user attempt, regardless of how many
  external Providers actually start.
- Payload-cache reuse, cooldown, a single-flight waiter, continuation polling,
  Native fallback, and a late automatic upgrade consume no new user attempt.
- User Ultra and generic third-party limits remain 10 per minute.
- TianTianJieXi and KuKuTool failure cooldowns remain eight seconds.
- TianTianJieXi remains limited to 20 requests/minute and two concurrent calls.
- KuKuTool remains limited to 30 requests/minute and four global concurrent
  calls in the accepted production runtime configuration.

## Production Acceptance

This contract is production accepted at:

```text
application SHA: d746ec9ff66532071b8079592f8b4831cae0fe51
checkpoint:      checkpoint/production-provider-stabilization-v1
```

Acceptance v1 found that the application created structured Provider terminal
fields but the production console formatter omitted them, so it rolled back to
`fc1f136...`. Acceptance v2 verified the repaired explicit allowlist in final
systemd journal text, a fresh TianTianJieXi success with one consumed user
attempt, payload-cache reuse with no new TianTianJieXi external call or user
attempt, and an exact 110,691,390-byte Chrome Streaming download with zero
AnimeImages ordinary-media bytes. Environment, runtime settings, systemd, and
Nginx remained unchanged and the post-deploy observation was healthy.

The Native metadata Future remains separate from Ultra attempt accounting and
has no Provider success cache. Its execution can remain observable on a cached
TianTianJieXi request without consuming another user attempt; this does not
change the cache or quota contract above.

## Provider Outcomes

`provider_attempt_completed` uses fixed internal outcomes:

```text
SUCCESS_FRESH
SUCCESS_EXISTING_RESULT
SUCCESS_PAYLOAD_CACHE
SUCCESS_SINGLE_FLIGHT
SUCCESS_CONTINUATION
FAILURE_COOLDOWN
FAILURE_TIMEOUT
FAILURE_HTTP
FAILURE_NETWORK
FAILURE_SCHEMA
FAILURE_NO_MEDIA
FAILURE_RESULT_INVALID
FAILURE_RATE_LIMIT
FAILURE_CONCURRENCY
FAILURE_CIRCUIT_OPEN
FAILURE_UNKNOWN
```

These values are diagnostics, not user-facing error copy. A terminal event is
bounded to fixed fields: Provider, outcome, external-call/cache/single-flight/
continuation/attempt booleans, elapsed and payload age, safe retry-after,
candidate count, hostname only, expected bytes, and a fixed failure class.
It never includes source URL, media path/query, signed value, Cookie,
Authorization, Provider credential, response body, raw exception, or stack.
`SUCCESS_EXISTING_RESULT` identifies a winning result already carried by the
request/attempt context, including a superseding peer or Native result; it is
intentionally distinct from a Provider payload-cache hit.
`SUCCESS_CONTINUATION` means the same external call that started at T=0
completed after the foreground deadline; it is not a second Provider call or
a continuation-poll attempt. Native Future terminal observations are emitted
from a completion callback so structured logging cannot delay result
visibility in the Provider race.

## Success Cache

- TianTianJieXi payload cache uses its existing runtime TTL (code fallback:
  120 seconds). A separate cache timestamp supports payload-age diagnostics
  without changing the cached payload contract.
- KuKuTool validated-candidate cache uses its existing runtime TTL (code
  fallback: 300 seconds), capped by signed expiry where known. Cached entries
  retain legacy compatibility; entries without a timestamp safely report age
  zero.
- A payload-cache hit means only that the Provider payload is inside its cache
  TTL. It does not prove current CDN reachability.
- CDN signed URL lifetime remains `CDN_URL_LIFETIME_UNKNOWN`.
- Structural validation happens before TianTianJieXi success-cache admission.
  Douyin requires at least one non-empty HTTPS/HTTP candidate on an allowed
  Douyin media host and rejects a negative declared size. Generic third-party
  parsing validates through the platform normalizer and its host rules.
- Invalid legacy cache entries are deleted and may be followed by normal fresh
  Provider work. Structural validation does not perform a server-side CDN HEAD
  or media GET.

## Failure Cooldown

- TianTianJieXi and KuKuTool use Provider-specific failure keys. One Provider's
  cooldown does not block its peer, Native, or an already-started continuation.
- A cooldown hit performs no external call and consumes no user attempt.
- Legacy string and older dictionary entries are normalized during reads; no
  Redis flush is required. New cooldown entries retain a fixed failure class;
  legacy entries without one safely normalize to `UNKNOWN`.
- Provider success clears that Provider's corresponding failure state.

## Single-Flight And Continuation

- Same-resource TianTianJieXi work uses a distributed cache lock. KuKuTool uses
  a process-local future plus a distributed cache lock.
- A waiter performs no duplicate external call and consumes no user attempt.
- Lock/future release is in bounded exception/finally paths. Timeout, schema
  rejection, submission failure, and other exceptions cannot permanently own
  the key.
- Continuation observes only Provider work created at T=0. Polling reads the
  bounded continuation record and never starts another Provider call.
- Provider execution, result grace, and frontend poll count remain bounded;
  Python threads are not force-killed.

## Fresh Resource Retry

`FRESH_RESOURCE_RETRY_DESIGN_DEFERRED`

The current UI can submit a new parse action, but the server has no safe,
generation-aware `fresh_resource_retry` contract. A naive success-cache bypass
could attach to a normal single-flight that publishes an older payload, while
separate bypass keys could create duplicate Provider storms. Continuation also
indexes work without a freshness generation.

A future implementation must isolate one request-scoped fresh generation while
still coalescing simultaneous fresh requests. It must continue to enforce user
attempt limits, Provider global rate/concurrency, failure cooldown, circuit,
permissions, and CSRF. It must not globally flush caches and must not trust
browser telemetry as an invalidation signal.

## Browser Response Rejection

`UPSTREAM_MEDIA_INVALID` remains the compatible error code. The Service Worker
adds a fixed `responseRejectReason`, evaluated in this order:

| Reason | Observed fact | Not proven |
| --- | --- | --- |
| `HTTP_STATUS_REJECTED` | A response was acquired but `response.ok` was false | Provider parse failure |
| `BODY_UNAVAILABLE` | The accepted response exposed no readable body | CORS root cause |
| `CONTENT_TYPE_REJECTED` | The normalized type was outside the media allowlist | CDN lifetime or Provider outage |
| `FINAL_HOST_REJECTED` | The final response host was outside the media allowlist | Malicious Provider intent |
| `UNKNOWN_RESPONSE_REJECTED` | Old client or no safe finer fact | A specific network cause |

Other existing classes remain distinct:

- `FETCH_REJECTED_BEFORE_RESPONSE`: browser fetch rejected before a response.
- `STREAM_BODY_FAILURE`: response passed the gate, then body streaming failed.
- `SIZE_VALIDATION_FAILURE`: bounded size or exact-byte validation failed.

`TypeError` alone is not proof of CORS. Telemetry records only status 0 or
100-599, content type class `VIDEO`, `OCTET_STREAM`, `MISSING`, or `OTHER`, and
an allowed hostname or `unknown`. Raw status text and headers are excluded.

## Compatibility And Non-Goals

- Old telemetry payloads without the four new fields remain accepted.
- Telemetry remains POST-only, CSRF-protected, 8 KiB bounded, independently
  rate-limited to 30/minute, structured-log only, and non-persistent.
- Worker status remains visible to every same-origin tab, but one registering
  client owns each terminal telemetry report. If that tab is gone, one active
  client is selected. Worker push and direct status/cancel replies carry the
  same owner decision, preventing duplicate reports without hiding status.
- The same-origin Worker download path is opened in an isolated hidden frame.
  A rejected media response therefore cannot replace the application page or
  remove the registering page's terminal telemetry listener.
- Single and Batch share the same Provider validation/cache primitives.
- This contract does not change Batch access, scheduling, deadlines, raw-link
  boundaries, Provider order/timeouts, ordinary-media transport, or server-
  generated outputs.
- There is no automatic Worker retry, Range retry, host retry, Provider
  reparse, cache invalidation, Server Relay fallback, or database migration.
