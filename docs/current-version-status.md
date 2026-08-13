# Current Version Status

Date: 2026-08-14

## Source Of Truth

| Concern | Source of truth |
| --- | --- |
| Development code | Latest fetched `origin/main` |
| Deployed code | Exact production SHA and immutable production checkpoint |
| Runtime configuration | Production environment and persisted Admin runtime settings |
| Human-readable status | This file, backed by deployment/reconciliation evidence |

Never infer a production deployment from a merge to `main`.

The pre-reconciliation main was:

```text
d98b3098bd55031c859c125e44c5d84b1f458f17
checkpoint/main-pre-production-reconciliation-v1
```

The reconciliation PR reconstructs the accepted production behavior on top of
that main. After it merges, use the exact commit resolved by:

```bash
git fetch --prune origin
git rev-parse origin/main
```

The immutable converged reference is
`checkpoint/main-production-converged-v1`, created after the squash merge.

## Current Production

Production remains intentionally unchanged at:

```text
code SHA:          87afba321a2dc459b7daebcd1cc9eea6735b60ba
checkpoint:        checkpoint/production-douyin-ultra-retry-v1
documentation:     8b81041059839691b08c472b0a4ad9586431ec4c
site:              https://download.cosflow.icu/
```

The documentation SHA was not deployed. It records the deployment evidence for
the code SHA. This reconciliation does not deploy, restart, migrate, or change
production runtime settings.

Production topology at the last audit:

```text
Gunicorn:          2 workers x 4 gthreads, timeout 60
service user:      www-data:www-data
bind:              127.0.0.1:8000
Nginx upstream:    http://127.0.0.1:8000
checkout owner:    ubuntu:ubuntu
database:          PostgreSQL
cache/limits:      Redis-backed
```

## Stable Behavior Manifest

The normative behavior contract is
[`production-behavior-baseline-v1.md`](production-behavior-baseline-v1.md).
Changing one of those behaviors requires an explicit `BEHAVIOR_CHANGE` section
in the PR description.

### Douyin Ultra

- A fresh request starts TianTianJieXi, KuKuTool, and Native at T=0.
- Native remains hidden during the six-second foreground window.
- Strong Ultra completed within that window is the first visible result.
- After six seconds, prepared Native may be shown while bounded continuation
  observes work that was already started and can upgrade the same result.
- One fresh external race consumes one user attempt. Cache/cooldown hits,
  single-flight waiters, continuation polls, Native fallback, and automatic
  upgrades do not consume another attempt.
- Production allows 10 fresh Ultra attempts per minute; the tenth is allowed
  and the eleventh is rejected.
- TianTianJieXi and KuKuTool transient failure cooldowns are eight seconds.
- Provider order remains TianTianJieXi, KuKuTool, Native.

### Ordinary Media Download

Ordinary TianTianJieXi and KuKuTool video uses:

```text
Provider/CDN -> Browser Service Worker -> Browser Download Manager -> Disk
```

- `BROWSER_STREAMING` is the default.
- `BROWSER_BLOB` is permitted only for a capability failure before Streaming
  starts. A post-start failure must not create a second transfer.
- Single and Batch use the same ordinary-media primitive.
- Refresh reattaches to the opaque download ID without reparsing or restarting
  the CDN request. Site and Download Manager cancellation reach the upstream.
- Ordinary media does not call `/media/download-prepare/`,
  `/media/download-stream/`, or `/media/download-status/`.
- Ordinary Server Relay and playback/native-open are not fallback paths.
- AnimeImages ordinary media request count and media bytes remain zero.
- Signed media URLs may exist in page and Service Worker memory for handoff,
  but not in logs, browser storage, documentation, or status messages.

YouTube audio/video merge, ZIP creation, and other generated outputs remain
separate server-required resource classes.

### Provider Lifecycle And Limits

- Successful TianTianJieXi payload cache default: 120 seconds.
- Provider manifest maximum age default: 3600 seconds.
- CDN URL lifetime: `CDN_URL_LIFETIME_UNKNOWN`.
- A payload-cache hit can return the existing media URL without another API
  call; manifest validity does not prove CDN reachability.
- TianTianJieXi global request limit remains 20 per minute.
- Effective TianTianJieXi concurrency intent is two. The fixed-at-one lease
  introduced by PR #30 is superseded and is not part of the canonical runtime.
- Environment values provide capability and safe defaults. Persisted Admin
  runtime settings control availability/rollout where a runtime model exists.

### Batch

- Batch scheduling, per-item deadlines, response boundary, progressive tokens,
  retry preservation, and Provider order remain unchanged.
- `BatchParseRuntimeSetting.access_mode` aligns navigation, page access, and
  start/item/retry API access.
- Staff retains raw-link copy/open/export and strategy diagnostics.
- An allowed public user may use validated video/image download actions but is
  not given raw-link UI or export capability.
- Public ordinary video uses the same Service Worker Streaming primitive as
  Single. The card necessarily carries validated handoff fields in browser
  memory; this is not a public raw-link feature.

The access/download behavior above and Batch Runtime Admin clarity were
retained from PR #26/#28. They are
`MAIN_ONLY_NOT_YET_PRODUCTION_DEPLOYED` until a separate deployment is approved.

### Admin And Release Management

- Batch Runtime Admin remains a singleton direct-edit surface with stale-write
  protection, explicit public-expansion confirmation, and non-sensitive audit
  logging. It does not change request-time semantics by itself.
- Release Management reads local Git refs only. The Web process must not fetch,
  own the checkout, or receive a deploy key.
- Production remains record-only while
  `RELEASE_MANAGEMENT_EXECUTOR_COMMAND` is empty and executor helper/unit files
  are not installed.

## Reconciliation Decisions

The complete commit inventory and decision matrix are in
[`research/production-main-reconciliation-v1.md`](research/production-main-reconciliation-v1.md).

Summary:

| Main-only PR | Decision |
| --- | --- |
| #26 Batch visibility/public download | Keep, adapted to shared Streaming; not yet deployed |
| #28 Batch Runtime Admin clarity | Keep; not yet deployed |
| #29 Browser Direct + Relay | Superseded by Service Worker Streaming |
| #30 gthread + fixed-one Provider lease | Keep 2x4 topology; reject fixed-one lease |
| #31 native-open + compatibility Relay | Superseded by Service Worker Streaming |
| #33 Provider lifecycle docs/tests | Keep after factual reconciliation |

## Known Boundaries

1. Production still runs the exact stabilization SHA above. Its behavior is
   represented in canonical main after reconciliation, but deployment lineage
   does not converge until a separately approved main deployment.
2. PR #26/#28 behavior has not been production accepted.
3. Firefox and Edge Streaming acceptance remain untested on the development
   machine; Chrome production and controlled Safari evidence exist.
4. YouTube merge is server-required and should remain runtime-gated.
5. Bilibili high-quality Cookie/member/DASH work remains deferred.
6. Executor installation, Tag rollback, and automatic production deployment
   remain outside the enabled Release Management boundary.

## Next Work

1. Start every product task from freshly fetched `origin/main` in the retained
   long-lived development worktree, one task branch and one PR at a time.
2. Treat a production-only hotfix as P0 `PRODUCTION_CODE_DRIFT` until an audited
   reconciliation records it in main.
3. Perform any production rollout of the converged main as a separate task with
   exact-SHA checks, runtime-value capture, health gates, and rollback.
4. Apply repository branch-protection recommendations in a separate,
   reversible governance task.
5. Delete historical remote branches only in a separate cleanup task using the
   inventory in `docs/research/branch-cleanup-candidates-v1.md`.
