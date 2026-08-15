# Provider Stabilization And Reliability v1

Date: 2026-08-15
Status: production accepted

## Scope And Baseline

The audit started from canonical `origin/main`:

```text
ec1c2f7dbe1debda84ac3ffa5a0358f0f897ccdd
```

The implementation started from the then-accepted production application:

```text
fc1f1368431fb2ff063231dfaa4f35a9c8154430
checkpoint/production-main-converged-v2
```

During the original implementation task, no production SSH, Provider smoke,
deployment, runtime setting, Redis, database, systemd, Nginx, or environment
change was performed. The later independent acceptance is recorded below.

## Production Acceptance v1 Result

The separate production acceptance on 2026-08-15 exact-deployed merged main
candidate `4d91b749be68dc7612bbe4c48dc762ab36c59841`. The pre-deploy Provider and
browser Streaming baseline passed. Candidate startup, local and public health,
configuration integrity, static identity, new and legacy telemetry schema, and
a fresh TianTianJieXi Ultra parse also passed. The fresh parse returned the
Ultra result in approximately 2.5 seconds without showing Native first.

The acceptance stopped at the Provider terminal output gate. The application
created `provider_attempt_completed` records with the new reliability fields,
but the production `request_context` console formatter emitted only its legacy
base fields. Consequently `outcome`, external-call/cache state, attempt
accounting, elapsed time, and payload age were not observable in journal text,
so the required fresh/cache production evidence could not be completed.

The status at acceptance v1 close was:

```text
acceptance classification:             OBSERVABILITY_OUTPUT_CONTRACT_REGRESSION
rollback:                              PASS
production application:               fc1f1368431fb2ff063231dfaa4f35a9c8154430
production checkpoint:                checkpoint/production-main-converged-v2
provider stabilization at v1 close:   NOT YET PRODUCTION ACCEPTED
```

This was an output-plumbing failure, not evidence of a deterministic Provider
runtime failure. No signed media URL, query, credential, or raw Provider
response is part of this record. At v1 close, Production Acceptance v2 still
needed to verify the fresh and payload-cache terminal fields directly in final
journal text before creating a new checkpoint.

## Production Acceptance v2 Result

Production Acceptance v2 reused the immediately preceding recovered baseline
at `fc1f136...`: a fresh TianTianJieXi result and an exact 110,691,390-byte
Chrome Streaming download from the sanitized media hostname
`v5-se-gddgtc-default.365yg.com`, with zero AnimeImages ordinary-media bytes.
It did not repeat the PRE Provider race or PRE download.

The task then exact-deployed:

```text
application SHA: d746ec9ff66532071b8079592f8b4831cae0fe51
checkpoint:      checkpoint/production-provider-stabilization-v1
```

Deployment applied no migration, copied 3 static files with 147 unchanged,
and restarted only `animeimages.service`. Environment, persisted Provider and
Batch runtime settings, systemd, and Nginx hashes remained unchanged;
`NGINX_CONFIG_DRIFT=0`. The served Service Worker matched the candidate source.

The production fresh race selected TianTianJieXi in approximately 2.7 seconds
without displaying Native first. The selected original/Ultra asset reported
110,691,390 bytes on the sanitized media hostname
`v5-se-jltc-default.365yg.com`. Final journal text exposed the fixed allowlisted
terminal fields, including `SUCCESS_FRESH`, `external_call=True`,
`attempt_consumed=True`, and positive elapsed time. Each TianTianJieXi, KuKuTool,
and Native execution emitted at most one terminal event, and the fresh race
consumed one user attempt.

Within the 120-second payload-cache lifetime, the same source produced
`SUCCESS_PAYLOAD_CACHE`, `external_call=False`, `cache_hit=True`,
`attempt_consumed=False`, and a positive payload age. KuKuTool reused the
existing winning result without external work. The independently scheduled
Native metadata Future remained its documented zero-attempt path; it did not
consume another Ultra attempt.

Candidate Chrome Streaming saved the same 110,691,390-byte ordinary MP4 in six
seconds with visible Download Manager progress, speed, and completion. Calls
to `/media/download-prepare/`, `/media/download-stream/`, and
`/media/download-status/` were all zero, AnimeImages ordinary-media bytes were
zero, and neither Server Relay nor a playback page was used. Batch remained
public with public download and staff-only raw-link boundaries intact.

The following ten-minute observation retained `active/running`, zero service
restarts, local/public readiness 200, and zero 5xx, Traceback, unexpected 429,
or severe Redis, continuation, telemetry, and Worker errors. Terminal logs
contained only the explicit diagnostic allowlist and no URL or token. The
acceptance classification is:

```text
PROVIDER_STABILIZATION_V2_PRODUCTION_ACCEPTED
PRODUCTION_RUNTIME_CODE_DRIFT = 0
```

## Provider Runtime Map

| Boundary | TianTianJieXi | KuKuTool | Native |
| --- | --- | --- | --- |
| Entry | `TianTianJieXiGenericProvider.parse` via Douyin adapter | `DouyinKukuToolProvider.parse` / background resolver | `get_fast_native_result` or `DouyinNativeProvider.parse` |
| External call | official API `parse_url` POST | bounded Node/sidecar runner, optional bounded candidate probe | mobile feed/share fetchers |
| Timeout | remaining request/continuation deadline, Provider cap | remaining request/continuation deadline, Provider/probe caps | request/native deadline propagated to fetchers |
| Success cache | source-key payload, fallback TTL 120s | aweme-key validated candidate, fallback TTL 300s and signed-expiry cap | no Provider success cache; shared Douyin display metadata may be reused |
| Failure cache | source-key, fallback 8s | aweme-key, fallback 8s | none |
| Single-flight | distributed cache lock | process-local Future plus distributed lock | race Future only; no Provider cache lock |
| Global rate | 20/min accepted runtime | 30/min accepted runtime | none beyond request path controls |
| Global concurrency | 2 | 4 | bounded executors |
| User attempt | immediately before first real external call, after Provider permits | immediately before runner call, after Provider permits | never consumes Ultra attempt |
| Continuation | observes T=0 Future | observes T=0 Future/cache | prepared fallback, not a new external Ultra attempt |
| Result validation | platform candidate/host/schema before cache write | allowed host, size, object key and signed-expiry/probe validation | adapter `ParseResult.assets` |
| Single/Batch | shared primitive; Batch order Tian -> Native | shared where enabled; Batch order unchanged | shared primitive |

## Confirmed Bugs

### TianTianJieXi success-cache poisoning

The generic Provider previously cached any dictionary before the Douyin adapter
checked media candidates. An illegal host, empty URL, malformed media object,
or invalid declared size could therefore occupy the 120-second success cache.
The next parse reused the bad payload instead of reaching the Provider.

The fix passes the platform validator into both cache admission and cache read.
Invalid legacy cache entries are removed. An invalid fresh payload is classified
as `FAILURE_RESULT_INVALID`, is not success-cached, and does not create an
immediate failure cooldown that would prevent the next fresh attempt.

### Browser rejection ambiguity

The Worker previously folded HTTP status, missing body, rejected content type,
and final-host rejection into one `HTTP_RESPONSE_REJECTED` stage. The fix keeps
the old error/stage compatible while adding fixed, URL-free reason metadata.

## Reliability Findings

- TianTianJieXi and KuKuTool cooldown keys are independent.
- Success clears the matching Provider failure state.
- Legacy failure strings and old dictionary shapes remain readable.
- New TianTianJieXi and KuKuTool failure entries preserve fixed failure
  classes; legacy entries without a class safely report `UNKNOWN`.
- TianTianJieXi lock release and KuKuTool local/distributed future cleanup are
  present in exception/finally paths.
- Continuation polls read cache records only; they do not invoke Provider parse.
- One attempt controller is shared by the fresh Tian/KuKu race. Cache,
  cooldown, waiter, continuation and Native paths remain zero-attempt.
- User, Provider-global rate, and Provider concurrency are separate controls.
- Controlled concurrency tests observed maxima of Tian 2 and KuKu 4 without
  deadlock or extra upstream calls.
- KuKuTool already applied stricter structural validation before its validated
  cache. This task added payload-age metadata and bounded outcome events rather
  than replacing its resolver.
- Native race results become observable before their structured terminal log
  callback runs, so slow logging cannot change the six-second winner timing.
- Same-origin tabs still receive Worker status updates, while the registering
  tab alone reports terminal browser telemetry; one remaining client takes
  ownership if the registering tab has closed.
- Controlled browser acceptance found that a rejected Worker navigation could
  replace the registering page before it sent diagnostics. The download path
  now uses an isolated same-origin frame, preserving the page listener without
  changing the CDN-to-Worker-to-Download-Manager media path.

## Controlled Browser Acceptance

A local controlled media server exercised the real Chrome Service Worker path
without contacting a Provider or production:

- An 8 MiB `video/mp4` completed with exact expected and saved byte counts.
- HTTP 403 produced `HTTP_STATUS_REJECTED` with safe status metadata.
- HTTP 200 `text/html` produced `CONTENT_TYPE_REJECTED`.
- A body interrupted after initial bytes produced `STREAM_BODY_FAILURE`.
- Two same-origin tabs both observed terminal status while each failed transfer
  emitted exactly one telemetry POST in total.
- No download prepare, stream, or status relay endpoint was called, so ordinary
  AnimeImages media requests and media bytes remained zero.

## Theoretical Risks Not Reported As Incidents

- A Provider payload inside cache TTL can still point to a CDN resource that
  becomes unavailable. Cache freshness and CDN reachability are distinct.
- Browser media failures are client-observed and can be forged; they are not an
  authority for server cache invalidation or Provider routing.
- Raw exception classification cannot always distinguish every upstream schema
  failure from a general Provider failure. Fixed safe outcomes intentionally
  avoid copying raw messages.

## Deferred Design

`FRESH_RESOURCE_RETRY_DESIGN_DEFERRED`

Safe fresh retry needs a freshness generation shared across payload cache,
local/distributed single-flight and continuation. The current contracts cannot
guarantee that a bypass request will not attach to old work, and an isolated
ad-hoc key could amplify concurrent Provider calls. No second retry UI, global
cache flush, database state, or telemetry-driven invalidation was added.

## Behavior And Deployment

`NO_INTENDED_BEHAVIOR_CHANGE`

Provider order, timeout, six-second foreground behavior, continuation bounds,
attempt limits, cooldown durations, Batch protocol, and ordinary-media
Streaming are unchanged. The changes reject structurally invalid Tian payloads
earlier and improve diagnostics. Candidate `4d91b749...` was temporarily
deployed only for the failed acceptance v1 attempt and then rolled back.
Production Acceptance v2 subsequently exact-deployed and accepted
`d746ec9...`; the immutable production checkpoint is
`checkpoint/production-provider-stabilization-v1`.
