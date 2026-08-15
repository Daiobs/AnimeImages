# Current Version Status

Date: 2026-08-15

## Source Of Truth

| Concern | Source of truth |
| --- | --- |
| Development and repository | Latest fetched `origin/main` |
| Production application | Exact deployed SHA plus immutable production checkpoint |
| Production infrastructure | Active host configuration verified against `ops/nginx/` and the systemd baseline |
| Runtime configuration | Production environment and persisted Admin runtime settings |
| Human-readable current status | This file, backed by deployment, acceptance, infrastructure, and repository evidence |

Never infer a production deployment from a merge to `main`. Repository state,
deployed application code, active infrastructure, and runtime settings are
related but separate facts.

## Canonical Repository

The repository baseline before this status reconciliation PR is:

```text
origin/main: 3f73f37fe10da0d9ec6a00bccff0b9ef1f10f0aa
source:      Repository Branch Archive & Cleanup v1 (#37)
```

Always resolve the current canonical SHA rather than treating the value above
as a permanent pointer:

```bash
git fetch --prune origin
git rev-parse origin/main
```

The production application SHA is an ancestor of canonical main. Before the
Provider stabilization candidate documented below, changes after that
production SHA were infrastructure-as-code, repository governance, evidence,
and documentation only.

```text
PRE_PROVIDER_STABILIZATION_RUNTIME_CODE_DRIFT = 0
REPO_HEAD_AHEAD_INFRA_GOVERNANCE_ONLY = YES
```

That earlier SHA difference was not a reason to redeploy production. The
runtime release documented below changes that status on canonical main.

## Provider Stabilization Candidate

This Provider stabilization release is the first post-convergence runtime
change on canonical main and contains behavior not yet accepted on production:

```text
MAIN_RUNTIME_AHEAD_PRODUCTION = YES
PRODUCTION_RUNTIME_CODE_DRIFT = PROVIDER_STABILIZATION_PENDING_ACCEPTANCE
```

The candidate adds structural TianTianJieXi cache admission, fixed Provider
outcome diagnostics, cache-age evidence, and finer browser response rejection
metadata. It does not change Provider order, timeouts, attempt limits,
cooldowns, Batch behavior, or ordinary-media transport. Resolve the exact
merged main SHA from Git; do not substitute the feature head SHA or infer a
deployment from merge state. Production remains at `fc1f136...` until a
separate exact-SHA acceptance task succeeds.

## Current Production

Production application code has been deployed and accepted at:

```text
application SHA:  fc1f1368431fb2ff063231dfaa4f35a9c8154430
checkpoint:       checkpoint/production-main-converged-v2
site:             https://download.cosflow.icu/
```

The checkpoint resolves exactly to the application SHA and must remain
immutable. This application includes the accepted PR #26 Batch visibility and
public-download behavior and PR #28 Batch Runtime Admin clarity.

Production topology at the accepted infrastructure audit:

```text
Gunicorn:          2 workers x 4 gthreads, timeout 60
service user:      www-data:www-data
working directory: /srv/animeimages
bind:              127.0.0.1:8000
Nginx upstream:    127.0.0.1:8000
checkout owner:    ubuntu:ubuntu
database:          PostgreSQL
cache/limits:      Redis-backed
```

## Production Infrastructure

The accepted production Nginx files match the canonical repository baseline:

```text
NGINX_CONFIG_DRIFT = 0
SYSTEMD_BASELINE_MATCH
```

- `ops/nginx/` contains the canonical site template, byte-exact media snippet,
  manifest, read-only drift checker, tests, and recovery instructions.
- `ops/systemd/` is the canonical AnimeImages service baseline.
- `/media/browser-download-telemetry/` is an exact Nginx control-plane route to
  Django. The generic `/media/` catch-all remains protected.
- The accepted route checks returned GET 405, no-CSRF POST 403, valid-CSRF
  invalid-schema POST 400, and valid synthetic telemetry POST 204.
- There is no Provider/CDN media `proxy_pass` in the AnimeImages Nginx chain.
- Certbot, Redis/PostgreSQL host configuration, Cloud-init, DNS, and other
  shared host services remain externally managed.

Merging repository infrastructure files does not install or reload production
configuration. The accepted zero-drift result is backed by the production
infrastructure audit.

## Stable Behavior Manifest

The current accepted behavior is summarized below. The dated reconciliation
manifest in
[`production-behavior-baseline-v1.md`](production-behavior-baseline-v1.md)
remains useful behavior history, but this file supersedes its pre-convergence
deployment-status statements. Changing a behavior still requires an explicit
`BEHAVIOR_CHANGE` section in the PR.

### Douyin Ultra

- A fresh request starts TianTianJieXi, KuKuTool, and Native at T=0.
- Native remains hidden during the six-second foreground window.
- Strong Ultra completed within that window is the first visible result.
- Bounded continuation may upgrade a prepared Native result after six seconds.
- One fresh external race consumes one user attempt. Cache/cooldown hits,
  single-flight waiters, continuation polls, Native fallback, and automatic
  upgrades do not consume another attempt.
- Production permits 10 fresh Ultra attempts per minute; the eleventh is
  rejected.
- TianTianJieXi and KuKuTool transient failure cooldowns are eight seconds.
- Provider order remains TianTianJieXi, KuKuTool, Native.

### Ordinary Media

Ordinary TianTianJieXi and KuKuTool video uses:

```text
Provider/CDN -> Browser Service Worker -> Browser Download Manager -> Disk
```

- `BROWSER_STREAMING` is the default.
- `BROWSER_BLOB` is permitted only for a capability failure before Streaming
  starts. It is not allowed after upstream fetch or Download Manager startup.
- Single and Batch use the same ordinary-media primitive.
- Refresh reattaches to the active opaque download ID without reparsing or
  restarting the CDN request.
- Ordinary media does not call `/media/download-prepare/`,
  `/media/download-stream/`, or `/media/download-status/`.
- Ordinary Server Relay and playback/native-open are not fallback paths.
- AnimeImages ordinary-media request count and media bytes remain zero.
- YouTube merge, ZIP generation, and other server-generated outputs remain
  separate server-required resource classes.

### Browser Failure Telemetry

- Terminal Streaming failure may emit one best-effort, CSRF-protected POST to
  `/media/browser-download-telemetry/`.
- Telemetry is small, sanitized control-plane metadata only. It excludes source
  URL, path/query, signed value, token, Cookie, raw error, stack, and media body.
- Completion telemetry remains disabled in v1 and failure telemetry is not
  awaited by the download path.
- A real Provider/CDN `UPSTREAM_FETCH_FAILED` or `UPSTREAM_MEDIA_INVALID` event
  does not by itself prove an application regression. Application identity,
  controlled Streaming, browser context, and upstream evidence determine the
  classification.

### Batch

- `BatchParseRuntimeSetting.access_mode` controls navigation visibility, page
  access, and start/item/retry API access.
- The accepted production runtime value was `public`; this observed value is
  not a permanent product contract and may be changed through the runtime gate.
- An allowed anonymous user can access Batch and use validated video/image
  download actions.
- Raw-link copy/open/export and strategy diagnostics remain staff-only.
- Batch Provider order, per-link deadlines, response boundary, progressive
  tokens, and retry preservation remain unchanged.
- Public ordinary video uses the same zero-server-media Streaming path as
  Single.

### Admin And Release

- Batch Runtime Admin is a singleton direct-edit surface with stale-write
  protection, explicit public-expansion confirmation, and non-sensitive audit
  logging.
- Release Management reads local Git refs only. The Web process must not fetch,
  own the checkout, or receive a deploy key.
- Production remains record-only while
  `RELEASE_MANAGEMENT_EXECUTOR_COMMAND` is empty and the separately reviewed
  executor helper/unit/sudoers boundary is not installed.

## Repository Governance State

Repository Branch Archive & Cleanup v1 is complete:

```text
remote branches:               26
main:                           1
checkpoint/*:                  11
KEEP_UNIQUE_HISTORICAL refs:   14
authorized historical deletes: 35
open PRs at cleanup close:       0
```

The 14 historical refs are intentionally retained because they contain unique
policy, governance, control-plane, migration, or mixed Provider history. They
are not accidental cleanup debt. Five local-only UNKNOWN refs also remain
outside the remote cleanup authorization. The dirty
`codex/platform-lab-auto-test-system` worktree is protected and was not changed.

See
[`research/repository-branch-archive-cleanup-v1.md`](research/repository-branch-archive-cleanup-v1.md)
for exact branch tips, evidence, decisions, and checkpoint integrity records.

## Known Boundaries

1. Firefox and Edge Streaming production acceptance is not complete. Chrome is
   production-accepted and controlled Safari evidence exists.
2. External/browser/CDN transients remain possible. The Provider stabilization
   candidate refines `UPSTREAM_MEDIA_INVALID` with fixed safe rejection reasons
   for HTTP status, missing body, content type, and final host. Production
   retains the prior coarse diagnostic until the candidate's eventual main SHA
   is separately deployed and accepted; no single CDN root cause is currently
   proven.
3. YouTube high-quality audio/video merge remains server-required and
   runtime-gated.
4. Bilibili high-quality Cookie/member/DASH support remains deferred.
5. Release Management automatic production deployment remains disabled.
6. GitHub private-repository main protection remains subject to available
   plan/ruleset capabilities.
7. Certbot, Redis/PostgreSQL host configuration, Cloud-init, DNS, and broader
   host recovery are outside the repository-owned infrastructure baseline.

These are current boundaries. Production convergence, Streaming telemetry,
the Nginx telemetry route, Nginx config-as-code, and historical branch cleanup
are complete and are not active blockers.

## Next Product Work

1. Start every new task from a freshly fetched `origin/main`, using one branch
   and one PR for one concrete task.
2. After merge, run a separate Provider Stabilization Production Acceptance v1
   exact-SHA rollout; do not add a new Provider or download architecture first.
3. Keep request-scoped fresh-resource retry deferred until cache,
   single-flight, and continuation can share a safe freshness generation.
4. Do not reopen the Streaming architecture without new evidence of an
   application regression.
5. Keep production changes as separate, approved exact-SHA deployment tasks.

Historical reconciliation, failure forensics, infrastructure, and branch audit
documents remain immutable evidence and should not be rewritten as current
status.
