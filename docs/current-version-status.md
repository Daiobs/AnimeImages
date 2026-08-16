# Current Version Status

Date: 2026-08-16

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

The runtime baseline accepted by Download UI & Batch Actions Production
Acceptance v1 is:

```text
runtime SHA: 9ba5702e9a90b9179e21d53393d8e27c39c51833
source:      Download UI & Batch Actions v1 (#42), including Provider Stabilization (#39/#40)
```

Always resolve the current canonical SHA rather than treating the value above
as a permanent pointer:

```bash
git fetch --prune origin
git rev-parse origin/main
```

The production application SHA is the accepted runtime baseline above. The
Batch Photo Gallery candidate advances application behavior on `main`; it has
not been deployed or production accepted.

```text
PRODUCTION_RUNTIME_CODE_DRIFT = 1
REPO_HEAD_AHEAD_DOCS_ONLY = NO
MAIN_RUNTIME_AHEAD_PRODUCTION = YES
```

## Batch Photo Gallery Candidate

The Batch Photo Gallery and Download UX v1 candidate removes the four-image
summary cap and renders every parsed image in parser order without fixed-ratio
cropping. Card previews use a deterministic 960px XHS derivative; the one
global, row-scoped lightbox requests a 1600px derivative only after the user
opens an image. Other platforms retain their existing safe preview candidate.

Each photo has its own browser download action. Multi-image rows also expose a
current-row Download All Images action that reuses the existing validated
download task or synchronous ZIP endpoint. The transport boundaries are:

```text
single image:    Image CDN -> Browser -> Disk
ordinary video: Provider/CDN -> Browser Service Worker -> Download Manager -> Disk
multi-image ZIP: Image CDN -> AnimeImages ZIP generator -> Browser -> Disk
```

The synchronous ZIP receiver enforces a 128 MiB browser-memory boundary and
cancels larger or unknown-length non-streaming responses. Mixed-resource text
exports retain their established video-first order while images remain in
parser order.

Public Batch users retain download and copy-all capabilities without raw-link
UI or staff exports. Staff raw actions, `links.txt`, and `aria2.txt` remain
available. Provider order, Batch deadlines, ordinary Streaming, runtime
settings, and migrations are unchanged.

```text
BATCH_PHOTO_GALLERY = MAIN_CANDIDATE_NOT_YET_PRODUCTION_ACCEPTED
PHOTO_ORIGINAL_VARIANT_SELECTION_NOT_PROVEN
PHOTO_DOWNLOAD_VARIANT_FIDELITY_BACKLOG
```

See
[`research/batch-photo-gallery-download-ux-v1.md`](research/batch-photo-gallery-download-ux-v1.md)
for the implementation, test, browser, and remaining-fidelity evidence.

## Download UI And Batch Actions Acceptance

PR #42 is production accepted. It removes the visible global active-download
panel from Single and Batch pages without removing Service Worker state,
refresh continuation, Download Manager cancellation, failure telemetry, or
Batch card progress. The ordinary-media data plane remains:

```text
Provider/CDN -> Browser Service Worker -> Browser Download Manager -> Disk
```

For users allowed to download from Batch, the result toolbar exposes one
`复制全部成功资源链接` action over the current successful video and image DOM.
Stable DOM order and first-occurrence URL deduplication are preserved. The
following capabilities remain staff-only:

- per-item copy/open/expanded raw links;
- `links.txt` and `aria2.txt` exports;
- the strategy matrix.

Staff exports now cover the same successful video and image sequence as bulk
copy. The production application still retains the four-image preview cap;
the undeployed main candidate described above replaces it with the full photo
gallery.

```text
DOWNLOAD_UI_BATCH_ACTIONS = PRODUCTION_ACCEPTED
ordinary media server bytes = 0
Server Relay = NO
```

The exact production evidence is recorded in
[`research/download-ui-batch-actions-production-acceptance-v1.md`](research/download-ui-batch-actions-production-acceptance-v1.md).

## Provider Stabilization Acceptance

Provider Stabilization & Reliability v1 and its explicit allowlisted terminal
formatter repair are production accepted:

```text
PROVIDER_STABILIZATION = PRODUCTION_ACCEPTED
PROVIDER_TERMINAL_FORMATTER = PRODUCTION_ACCEPTED
```

Acceptance v1 exact-deployed `4d91b749...`. Health, configuration integrity,
static identity, synthetic telemetry, and a fresh TianTianJieXi Ultra parse
passed, but the production console formatter did not render the new Provider
terminal diagnostic fields. Production was therefore rolled back to
`fc1f136...`:

```text
OBSERVABILITY_OUTPUT_CONTRACT_REGRESSION
PROVIDER_TERMINAL_LOG_OUTPUT_INCOMPLETE
```

The immediately following acceptance v2 reused the recovered `fc1f136...`
baseline, exact-deployed `d746ec9...`, and verified the final journal text,
fresh/cache attempt accounting, exact browser Streaming, zero ordinary-media
server bytes, Batch boundaries, and a ten-minute healthy observation window.
No signed media URL, query, token, Cookie, credential, raw response, or stack
was recorded in the acceptance evidence.

## Current Production

Production application code has been deployed and accepted at:

```text
application SHA:  9ba5702e9a90b9179e21d53393d8e27c39c51833
checkpoint:       checkpoint/production-download-ui-batch-actions-v1
rollback:         checkpoint/production-provider-stabilization-v1 -> d746ec9ff66532071b8079592f8b4831cae0fe51
site:             https://download.cosflow.icu/
```

The checkpoint resolves exactly to the application SHA and must remain
immutable. This application includes the accepted PR #26 Batch visibility and
public-download behavior, PR #28 Batch Runtime Admin clarity, and PR #42
Download UI and mixed-media Batch actions.

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
- An allowed anonymous user can access Batch, use validated video/image
  download actions, and copy all successful video and image resource links in
  stable current-DOM order.
- Per-item raw-link copy/open/details, `links.txt`, `aria2.txt`, and strategy
  diagnostics remain staff-only. Staff exports cover the same video and image
  sequence as copy-all.
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
remote branches:               28
main:                           1
checkpoint/*:                  13
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
   release refines `UPSTREAM_MEDIA_INVALID` with fixed safe rejection reasons
   for HTTP status, missing body, content type, and final host. No single CDN
   root cause is currently proven.
3. YouTube high-quality audio/video merge remains server-required and
   runtime-gated.
4. Bilibili high-quality Cookie/member/DASH support remains deferred.
5. Release Management automatic production deployment remains disabled.
6. The Batch gallery download candidate is not production accepted, and the
   current XHS download candidate has not been proven to be the highest-quality
   original variant.
7. GitHub private-repository main protection remains subject to available
   plan/ruleset capabilities.
8. Certbot, Redis/PostgreSQL host configuration, Cloud-init, DNS, and broader
   host recovery are outside the repository-owned infrastructure baseline.

These are current boundaries. Production convergence, Streaming telemetry,
the Nginx telemetry route, Nginx config-as-code, and historical branch cleanup
are complete and are not active blockers.

## Next Product Work

1. Start every new task from a freshly fetched `origin/main`, using one branch
   and one PR for one concrete task.
2. Keep request-scoped fresh-resource retry deferred until cache,
   single-flight, and continuation can share a safe freshness generation.
3. Do not reopen the Streaming architecture without new evidence of an
   application regression.
4. Keep production changes as separate, approved exact-SHA deployment tasks.

Historical reconciliation, failure forensics, infrastructure, and branch audit
documents remain immutable evidence and should not be rewritten as current
status.
