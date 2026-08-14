# Production Streaming Acceptance Gate v1

Date: 2026-08-14

This runbook classifies ordinary-media Streaming failures during an exact-SHA
production acceptance window. It complements, and does not weaken, the normal
service, readiness, static-integrity, security, and rollback gates.

The media data plane remains:

```text
Provider/CDN -> Browser Service Worker -> Browser Download Manager -> Disk
```

AnimeImages receives only bounded browser diagnostic metadata. It must not
receive ordinary media bodies, automatically retry a CDN fetch, or use Server
Relay as an ordinary-video fallback.

## Evidence Safety

Record only the following browser-observed fields:

- exact application SHA, served Worker hash, and protocol version;
- opaque 32-hex download ID;
- Provider identifier and allowlisted media hostname;
- `errorCode`, fixed `failureClass`, and fixed `failureStage`;
- expected, received, and total byte counts;
- bounded registration, fetch, first-chunk, and stream timings;
- online/visibility state, browser family, and fixed Worker lifecycle states;
- bounded Page probe result when a Provider Streaming attempt fails;
- controlled Streaming result, service health, and ordinary server media bytes.

Never record a source URL, CDN path/query, signed value, credential, Cookie,
Authorization header, manifest token, response body, media chunk, pasted input,
raw browser error message, or stack. A browser `TypeError` is recorded as
`FETCH_REJECTED_TYPE_ERROR`; it is not translated into CORS, DNS, or TLS.

Failure telemetry is best-effort, once per download ID, and not awaited by the
download path. Completion telemetry is disabled in v1. If the page closes,
terminal telemetry may be lost (`PAGE_CLOSED_TELEMETRY_BEST_EFFORT`); the
Browser Download Manager must continue independently.

## Failure Classes

### APP_REGRESSION

Use this classification only when at least one candidate-specific fact exists:

- the exact candidate fails a deterministic controlled Streaming test that the
  rollback baseline passes;
- served Worker/script/protocol bytes differ from the expected candidate;
- candidate-only Worker registration, activation, controller, or scope fails;
- candidate-only JavaScript/runtime exceptions occur;
- a bounded Page fetch succeeds while the same resource repeatedly fails in
  the Worker and the difference reproduces only on the candidate;
- ordinary media enters Server Relay, produces duplicate downloads, or has a
  deterministic byte-integrity failure attributable to candidate code.

`APP_REGRESSION` requires rollback.

### UPSTREAM_TRANSIENT_SUSPECTED

Use this provisional classification when a real Provider parse succeeds but a
single browser fetch rejects while all application hard gates remain healthy:

- service and readiness are healthy;
- served Worker bytes match the candidate;
- controlled candidate Streaming passes;
- no candidate-specific Page/Worker code difference or JavaScript crash exists;
- ordinary media request count and media bytes through AnimeImages remain zero.

A single Provider failure is not an application-regression proof. Record the
safe telemetry, wait a bounded interval, then use at most one fresh sample or
the bounded Page fetch probe before deciding. This is not permission to ignore
the failure or to retry the Worker automatically.

### BROWSER_ENVIRONMENT_SUSPECTED

Use this when application and controlled-source gates pass, but the failure is
isolated to a browser/profile/network state such as offline status, controller
mismatch limited to one profile, or Page and Worker fetches both failing in the
same browser environment without a candidate-specific reproduction.

### SERVICE_WORKER_LIFECYCLE_REGRESSION

Use this when candidate registration, activation, controller acquisition,
root scope, waiting-worker behavior, refresh reattachment, or active-download
survival differs deterministically from the accepted baseline. This is a hard
rollback classification.

### STATIC_DEPLOY_REGRESSION

Use this when the root Worker or dependent script bytes, headers, or expected
asset version do not match the exact deployed SHA. This is a hard rollback
classification.

### UNCLASSIFIED

Use this when evidence is insufficient or contradictory. Preserve the exact
SHA and logs, do not claim CORS/DNS/TLS from a Web Fetch `TypeError`, and obtain
a bounded Page probe plus controlled-source result before making a rollback
decision.

`DEPLOYED_EXTERNAL_STREAMING_FAILURE` is an allowed maintenance-window state
while a repeated external/browser compatibility failure is being classified.
It is not a successful acceptance result and must have an explicit operator
decision before the window closes.

## Hard Rollback Gates

Rollback the exact candidate if any of the following occurs:

- service startup, local readiness, or public readiness fails;
- the served Worker or static dependency does not match the candidate;
- Worker registration/controller or controlled Streaming regresses;
- Douyin Ultra first-result semantics regress;
- ordinary media enters Server Relay or AnimeImages transfers media bytes;
- completed media bytes are corrupt, incomplete, or duplicated;
- persistent application exceptions, permission errors, migration problems, or
  security boundary failures appear.

Do not rollback solely because one Provider CDN `fetch()` rejects when these
hard gates remain healthy. Do rollback when the failure is proven
candidate-specific or meets another hard gate.

## Candidate Acceptance Sequence

1. Record exact candidate and rollback SHAs; keep the production checkpoint.
2. Verify the checkout is clean and the service/static topology is expected.
3. Verify root Worker body/hash and headers: no-store/no-cache,
   `Service-Worker-Allowed: /`, and `nosniff`.
4. Run local/public readiness and inspect persistent application exceptions.
5. Run deterministic controlled Single and Batch Streaming with an exact 4-16
   MiB source. Confirm Download Manager progress, speed/ETA, exact bytes,
   `prepare=0`, `stream=0`, `status=0`, and zero AnimeImages media bytes.
6. Run controlled before-response rejection and after-byte stream-failure
   fixtures. Confirm fixed failure class/stage and URL-free telemetry.
7. Smoke a real Provider resource. On failure, capture one terminal event and
   apply the classification flow below.
8. Verify there is no migration, ordinary Relay, duplicate download, signed URL
   persistence, or security/logging leakage.

## Provider Failure Flow

```text
Provider Streaming failure
  -> verify service, served Worker, JavaScript, and zero server media bytes
  -> run bounded Page probe (headers plus first non-empty chunk only)
  -> verify deterministic controlled Streaming
  -> optionally wait and parse at most one fresh resource
  -> classify APP / UPSTREAM / BROWSER / LIFECYCLE / STATIC / UNCLASSIFIED
  -> apply hard rollback gates and record the operator decision
```

Two different Provider resources failing does not automatically mean
`APP_REGRESSION`. If the bounded Page probe also fails while controlled
Streaming and candidate identity pass, classify the evidence as upstream or
browser compatibility until candidate-specific proof exists.

The diagnostic probe is never invoked by the normal download flow. It performs
one browser GET with credentials omitted, reads headers and the first non-empty
chunk, then cancels/aborts. It returns hostname-only metadata, never a URL, and
does not call AnimeImages media endpoints.

## Rollback Record

If rollback is required, preserve:

- candidate and rollback SHA;
- classification and the hard gate that failed;
- Worker hash/protocol/lifecycle snapshot;
- URL-free terminal telemetry and controlled-source result;
- service/readiness and zero-server-media evidence.

Then use the existing exact-SHA deployment rollback procedure. Do not modify
Provider runtime settings, clear Redis, or change the transport architecture as
an incidental response to a single external fetch failure.

This gate was derived from the redacted evidence in
`docs/research/sw-upstream-fetch-failure-v1.md` and
`research/evidence/sw-upstream-fetch-failure-v1.json`. That audit narrowed the
failure to an upstream/resource or browser-context condition without proving a
canonical-main regression.
