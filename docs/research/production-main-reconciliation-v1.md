# Production/Main Reconciliation v1

Date: 2026-08-14

## Purpose

This audit reconstructs the production-stable behavior on top of `main`. It is
not a merge of the production branch. The two immutable recovery points are:

```text
main before reconciliation: d98b3098bd55031c859c125e44c5d84b1f458f17
checkpoint/main-pre-production-reconciliation-v1

production code: 87afba321a2dc459b7daebcd1cc9eea6735b60ba
checkpoint/production-douyin-ultra-retry-v1

production documentation head: 8b81041059839691b08c472b0a4ad9586431ec4c
merge base: 485951b1f962c4279ef9b78903f0170e05baae5c
```

The production checkout remains frozen at the production code SHA during this
work. Runtime configuration remains external to Git.

## Decision Vocabulary

- `KEEP`: retain a main-only capability after an explicit compatibility audit.
- `REIMPLEMENTED_BY_PRODUCTION`: retain the outcome through the newer
  production implementation, not the old code path.
- `SUPERSEDED`: exclude the old behavior from the canonical tree.
- `DEFERRED_REAPPLY`: preserve history but do not enable it in this baseline.
- `DOC_TEST_KEEP`: retain factual documentation or tests after updating them to
  the target behavior.
- `CONFLICT_REQUIRES_EXPLICIT_RESOLUTION`: split a mixed change and decide each
  runtime component independently.

## Main-Only Inventory

| SHA / PR | Runtime areas | Production deployed | Decision | Canonical result |
| --- | --- | --- | --- | --- |
| `27c68b9` / #26 | Batch navigation, access alignment, public downloads, staff-only raw tools | No | `KEEP` | Keep access-mode alignment and the raw-link boundary. Public ordinary video downloads must use the production shared browser streaming primitive. This remains `MAIN_ONLY_NOT_YET_PRODUCTION_DEPLOYED`. |
| `de5f38f` / #28 | Batch Runtime Admin UX, stale-write protection, public-expansion confirmation and audit log | No | `KEEP` | Keep the Admin-only control surface. It does not alter the saved runtime values or request-time access algorithm. This remains `MAIN_ONLY_NOT_YET_PRODUCTION_DEPLOYED`. |
| `e485fa9` / #29 | Browser Direct plus bounded Server Relay fallback | No | `SUPERSEDED` | Replace with Service Worker Streaming. Do not restore ordinary-media Relay, native playback, or the old prepare/status fallback. |
| `0cf897b` / #30 | Gunicorn gthread candidate plus a Redis Provider lease fixed at concurrency one | Partly, outside this commit | `CONFLICT_REQUIRES_EXPLICIT_RESOLUTION` | Keep the repository-owned `2 workers x 4 gthread` topology because production independently runs it. Reject the fixed global Provider limit of one; retain the production concurrency intent of two and its bounded rate/concurrency controls. |
| `23bcd9e` / #31 | Native launch/playback plus explicit compatibility Relay | No | `SUPERSEDED` | Exclude native playback and ordinary compatibility Relay. Service Worker Streaming is canonical. |
| `d98b309` / #33 | Provider payload, manifest and CDN lifetime terminology/tests | No runtime change | `DOC_TEST_KEEP` | Keep the three-clock model and cache fact tests, reconciled with the production behavior documents. |

No main-only commit is retained merely because it was already merged. The two
retained UI/control-plane capabilities are called out as not yet deployed so a
future production rollout can review their user impact explicitly.

## Production-Only Inventory

### A. Telemetry and recovery record

| SHA | Subject | Final role |
| --- | --- | --- |
| `bbbf87b` | Correct V1 single-stream byte telemetry | Final historical server-path accounting fix; ordinary media no longer uses that data plane. |
| `374db10` | Start V1 autonomous recovery log | Research artifact. |
| `6f1a0aa` | Document V1 browser-native contract blocker | Research artifact. |

### B. Download archaeology and Blob recovery

| SHA | Subject | Final role |
| --- | --- | --- |
| `651c66e` | Audit historical browser download paths | Research artifact. |
| `e4ac96c` | Record browser download transport archaeology | Research artifact. |
| `530d0cc` | Restore browser-side ordinary video downloads | Shared Single/Batch primitive and Blob recovery foundation. |
| `fbe3e97` | Document ordinary download recovery contract | Documentation retained with newer Streaming precedence. |
| `4fb735b` | Bound stalled browser media downloads | Final bounded Blob behavior. |
| `0282da7` | Record browser stall recovery evidence | Research artifact. |
| `a5e0254` | Record product-flow browser acceptance | Acceptance evidence. |
| `6e82898` | Add shared browser download progress | Final shared UI foundation; ordinary transfer progress is supplied by the Worker path. |

### C. Service Worker Streaming productionization

| SHA | Subject | Final role |
| --- | --- | --- |
| `22838ab` | Add production browser streaming transport | Canonical ordinary-media data plane. |
| `9d709b9` | Ensure service worker intercepts download navigation | Final Worker/navigation lifecycle fix. |
| `65175a8` | Bound local streaming benchmarks to loopback | Final test-only safety boundary. |
| `5af0892` | Prevent provider reparse after result-page refresh | Final refresh/result lifecycle behavior. |
| `e43b59f` | Document production streaming acceptance | Production evidence. |

### D. Douyin Ultra stabilization

| SHA | Subject | Final role |
| --- | --- | --- |
| `b7db54c` | Stabilize Douyin Ultra attempts and continuation | Canonical first-result, continuation, quota and cooldown implementation. |
| `e2a54fc` | Clarify Douyin Ultra retry feedback | Canonical public feedback and safe example configuration. |
| `87afba3` | Honor Ultra runtime defaults outside settings | Canonical default/override precedence and deployed code SHA. |

The production documentation commits after the deployed code SHA are kept as
documentation evidence only:

| SHA | Subject | Role |
| --- | --- | --- |
| `acb2153` | Document Douyin Ultra production deployment | Deployment evidence. |
| `8b81041` | Clarify production Provider orders | Documentation correction. |

## Runtime Resolution

### Ordinary downloads

The final tree uses `BROWSER_STREAMING` for manifest-validated ordinary
TianTianJieXi and KuKuTool videos. `BROWSER_BLOB` is allowed only when Streaming
cannot start. After a Worker task starts, failures do not fall back to Blob or
Server Relay. Single and Batch use the same primitive. YouTube merge, ZIP and
other server-generated outputs remain distinct server-required resource types.

### Douyin Ultra

The final tree starts TianTianJieXi, KuKuTool and Native at T=0. Native is held
for the six-second foreground window. A late external result may upgrade the
same result through bounded continuation. Only a fresh external race consumes
the per-user attempt allowance. Cache hits, failure/cooldown hits, pending
single-flight waits and continuation polls do not consume another attempt.

### Provider concurrency

The PR #30 fixed-at-one Redis lease is not retained. Production uses the
existing bounded provider concurrency mechanism with an effective
TianTianJieXi concurrency target of two. The repository may retain the 2x4
Gunicorn service definition and related deployment documentation, but that does
not imply a Provider concurrency reduction.

### Batch access and Admin

PR #26 access-mode alignment and PR #28 Admin clarity are retained as deliberate
main-only capabilities. They are not claims about the currently deployed tree.
The Batch download action is adapted to the shared Streaming transport; public
cards do not regain raw-link copy/open/export controls.

## Expected Runtime Differences From Production

Only the following main-only differences are permitted:

1. Batch navigation/page/API visibility follows one access-mode decision, and
   an allowed public user may invoke the validated shared download action.
2. Batch Runtime Admin has stale-write protection, explicit public-expansion
   confirmation and a non-sensitive mutation audit record.
3. The repository contains a reviewed 2x4 Gunicorn unit candidate matching the
   independently deployed topology.
4. Provider lifecycle documentation/tests distinguish payload-cache, manifest
   and unknown CDN URL lifetimes.

Any other runtime difference in ordinary download transport, Douyin Ultra,
retry accounting, continuation, Provider order, or runtime-default precedence
is unexplained and blocks reconciliation.

## Deferred Items

- Production rollout of the reconciled main is a separate task.
- Branch deletion is a separate governance task.
- GitHub branch-protection changes are recommendation-only in this task.
- PR #27 and PR #32 may be closed as superseded only after the reconciliation
  PR is merged and main CI succeeds.
