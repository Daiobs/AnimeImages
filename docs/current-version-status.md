# Current Version Status

Date: 2026-08-20

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

The application baseline accepted for real-user promotion is:

```text
application SHA: 766cecfd239f594e791c556b4caa69aebf2d1f9f
source:          PR #54 and PR #55 on top of the accepted Production Polish line
checkpoint:      checkpoint/production-promotion-ready-v1
```

Always resolve the current canonical SHA rather than treating the value above
as a permanent pointer:

```bash
git fetch --prune origin
git rev-parse origin/main
```

The production application SHA and immutable checkpoint resolve to the accepted
runtime above. After this docs-only closure merges, repository `main` is
intentionally one documentation commit ahead of the deployed application; its
application code remains the accepted runtime tree.

```text
PRODUCTION_RUNTIME_CODE_DRIFT = 0
REPOSITORY_AHEAD_OF_RUNTIME = DOCS_ONLY
ANIMEIMAGES_PROMOTION_READY = YES
```

## Production Promotion Ready

PR #54 (squash merge `6d85c7c8d673aedecc5d182de497cb6f18b1ff9a`)
unified the supported Douyin URL representations around the resolved
`douyin:<aweme_id>` identity. Short-link source resolution is bounded
separately, so the unchanged six-second Ultra Provider window starts only after
identity resolution. TianTianJieXi payload cache, single-flight, failure
cooldown, and attempt attribution now share that logical identity. Media
admission was extended only to the reviewed exact host
`v3-dy-a-x.ixigua.com`; no broad `ixigua.com` suffix or wildcard family was
allowed. Provider order, retry, cooldown, Browser Streaming, and zero-Relay
contracts remain unchanged.

PR #55 (squash merge `766cecfd239f594e791c556b4caa69aebf2d1f9f`)
restored Douyin video size metadata without weakening that identity model. The
logical cache/work identity remains `douyin:<aweme_id>`, while the
TianTianJieXi request uses an aweme-bound modal representation that retains the
complete Provider candidate metadata. The fix adds no Provider request, HEAD,
Range, or separate media-size probe.

Production acceptance for the two changes established:

- the PR #54 short-link matrix returned Ultra for two of three fresh samples;
- source resolution no longer consumes the Ultra Provider window;
- a reviewed-host 95,097,325-byte MP4 completed through Browser Streaming;
- ordinary-video `prepare / stream / status` calls remained `0 / 0 / 0`;
- AnimeImages ordinary-media bytes remained zero and Server Relay remained off;
- PR #55 restored TianTianJieXi size metadata for three of three samples:
  69,350,574 bytes (66.1 MB), 56,422,549 bytes (53.8 MB), and 79,910,405 bytes
  (76.2 MB);
- the 79,910,405-byte parsed size exactly matched the completed browser save.

Real-device acceptance is complete:

```text
MOBILE_REAL_DEVICE_ACCEPTANCE = COMPLETE
HUMAN_MOBILE_GATE = PASS
MOBILE_SIZE_GATE = PASS
```

The tested Android flow completed the download and the result appeared in the
system Gallery. The tested iOS flow completed the download and the result was
available in Files, but it did not automatically appear in Photos. These are
observations from the devices used for this gate, not claims about every mobile
OS, browser, or version.

```text
ANDROID_DOWNLOAD = PASS / GALLERY OBSERVED
IOS_DOWNLOAD = PASS / FILES OBSERVED
IOS_SAVE_TO_PHOTOS = NOT IMPLEMENTED / NON_BLOCKING
MOBILE_OS_SAVE_DESTINATION_VARIANCE = ACCEPTED_NON_BLOCKING
PROMOTION_BLOCKERS = NONE
PROMOTION_PHASE = REAL_USER_INTRODUCTION
```

## Production Polish Acceptance

Production Polish closed through six reviewed PRs:

- PR #47 retired the public legacy submissions surface and hardened the Admin
  login shell without deleting historical model or migration state.
- PR #48 hid Provider, latency, fallback, strategy, and routing diagnostics
  from public Single and Batch result surfaces while retaining staff detail.
- PR #49 consolidated project-level settings and restored the Batch settings
  trigger without changing Provider or Batch runtime behavior.
- PR #50 added the lightweight feedback workflow: required description,
  optional JPG/PNG/WebP screenshot, optional contact text, protected Admin
  access, and migration `0024_userfeedback`.
- PR #51 added shared browser identity assets and minimal public metadata.
- PR #52 enforced XHS source identity, corrected zero-media Batch results,
  aligned the responsive Gallery, and completed Douyin display metadata from
  already-acquired trusted Provider data.

The exact application SHA `a87ec475...` was deployed with the existing
procedure. No new migration was planned or applied; `0024_userfeedback` was
already present. `collectstatic` succeeded, only the application service was
restarted, Nginx was not reloaded, and local/public home, Batch, and readiness
checks returned 200 with `NRestarts=0`.

Production browser acceptance established:

- XHS note `69453774000000001f00dbb5` returned zero generic-shell media in
  Single and Batch.
- Valid XHS samples retained their titles/authors and ordered media (four
  images in Single and five in Batch). Desktop/tablet previews used centered
  `contain`, mobile used natural single-column sizing, Lightbox navigation and
  Escape worked, and per-image plus five-entry Browser ZIP downloads completed
  without an AnimeImages media relay.
- Douyin aweme `7669858827638845160` showed its real title and trusted cover.
  Browser Streaming created a Download Manager task and saved an exact
  58,154,674-byte MP4 in 24.2 seconds on the first attempt. The ordinary-video
  prepare/stream/status endpoint counts were all zero.
- Public diagnostics remained hidden. Home, Settings, Feedback, Batch, and
  Admin login browser smoke passed with no console errors.
- The acceptance window had no Traceback, Critical, unexpected 5xx, or browser
  download failure. Controlled Batch rate-limit responses and unsuccessful
  Provider candidates were followed by successful accepted results.

```text
PRODUCTION_POLISH = PRODUCTION_ACCEPTED
MIGRATION_0024_USERFEEDBACK = APPLIED
ORDINARY_VIDEO_SERVER_RELAY = NO
RUNTIME_CODE_DRIFT = 0
```

## Browser Photo Archive Acceptance

The first Batch Photo Gallery production acceptance was rolled back. It
validated the uncropped full Gallery, 960px card previews, on-demand 1600px
row-scoped Lightbox, responsive layout, and permission boundaries, but exposed
an incomplete photo-download contract: suffixless binary images could be
misnamed `.jpg`, a cross-origin anchor could save without an extension, and
pure image archives moved CDN bytes through AnimeImages.

The replacement was exact-deployed and production-accepted at `e70848c8...`.
It preserves the Gallery and changes the default photo data paths to:

```text
single image:     Image CDN -> Browser fetch -> Blob -> Download Manager -> Disk
pure image ZIP:   Image CDN -> Browser fetch -> STORE ZIP -> Download Manager -> Disk
ordinary video:  Provider/CDN -> Browser Service Worker -> Download Manager -> Disk
server photo ZIP: explicit compatibility fallback only
```

Production-origin Playwright/Chromium acceptance found readable CORS responses
and exposed `Content-Length` on `ci.xiaohongshu.com`. Single and Batch share
one local Browser Photo module with concurrency 3, a 50 MiB item cap, a 128 MiB
archive cap, incremental CRC32, STORE-only ZIP32, real byte/speed progress,
conditional ETA, cancellation, and no URL persistence. Successful browser
photo paths call no AnimeImages media endpoint. Server ZIP remains available
only after an explicit user action and shares bounded
JPEG/PNG/GIF/WebP/AVIF magic-based format resolution with async archives and
server saves. Unknown binary content never becomes `.jpg`.

Public Batch users retain download and copy-all capabilities without raw-link
UI or staff exports. Staff raw actions, `links.txt`, and `aria2.txt` remain
available. Provider order, Batch deadlines, ordinary Streaming, runtime
settings, and migrations are unchanged.

```text
BROWSER_PHOTO_ARCHIVE = PRODUCTION_ACCEPTED
XHS_BROWSER_FETCH_CORS = PRODUCTION_ORIGIN_PASS
ZIP_IMPLEMENTATION = STORE_ONLY_INTERNAL
SAMPLE_A_B_C_IMAGE_COUNTS = 4 / 5 / 2
SAME_CONTENT_DEFECT = NOT_REPRODUCED
CODEX_CHROME_CONTROL = TOOLING ISSUE
CODEX_IN_APP_BROWSER = TOOLING ISSUE
PHOTO_ORIGINAL_VARIANT_SELECTION_NOT_PROVEN
PHOTO_DOWNLOAD_VARIANT_FIDELITY_BACKLOG
```

The production-origin acceptance used isolated Playwright `1.62.1` with
Chromium `149.0.7827.55`. Sample B returned five ordered resources from
`ci.xiaohongshu.com`; single-photo and Lightbox downloads had correct `.jpg`
filesystem names, JPEG magic, exact source/download hashes, and zero
AnimeImages media endpoint requests. The default five-image archive produced
one valid UTF-8 STORE ZIP in `_01` through `_05` order, passed CRC validation,
matched source bytes 5/5, and showed real intermediate byte, speed, percent,
and ETA updates. Public download/copy boundaries passed in Production; staff,
explicit server fallback, dynamic replacement, format reliability, and
ordinary-video zero-relay boundaries were retained through controlled gates.

The Codex `@chrome` remote-HTTPS bridge remained unstable and the Codex in-app
browser remained automation-blocked. Those were tooling failures, not
AnimeImages regressions; the independent Playwright harness completed the
application acceptance.

See
[`research/browser-photo-archive-format-reliability-v1.md`](research/browser-photo-archive-format-reliability-v1.md)
for the CORS, filesystem filename, ZIP integrity, progress, fallback, memory,
and remaining-fidelity evidence. The earlier Gallery implementation record is
retained as rolled-back design history in
[`research/batch-photo-gallery-download-ux-v1.md`](research/batch-photo-gallery-download-ux-v1.md).

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
copy. The accepted production application also includes the later full photo
Gallery and no longer retains the historical four-image preview cap.

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
application SHA:  766cecfd239f594e791c556b4caa69aebf2d1f9f
checkpoint:       checkpoint/production-promotion-ready-v1
rollback:         checkpoint/production-browser-photo-archive-v1 -> e70848c8b89225beba0838e9b681aaf5b7fdf789
site:             https://download.cosflow.icu/
```

The checkpoint resolves exactly to the application SHA and must remain
immutable. This application includes the earlier accepted Batch visibility,
Runtime Admin, Download UI, Gallery/Lightbox, and browser-first photo archive
milestones, the complete PR #47 through #52 Production Polish release, and the
PR #54/#55 Douyin canonical-identity, short-link Ultra, reviewed media-host,
and size-metadata repairs.

Public users now see user-level Chinese status, retry, and download messages;
active staff retain Provider names, internal status, latency, strategy, and
routing diagnostics. The project settings entry is shared across product
pages, while server runtime gates remain authoritative. The deployed feedback
workflow accepts a required description, optional validated screenshot, and
optional contact text; sensitive-looking values are redacted, screenshot names
are randomized, and attachments are exposed only through permission-checked
Admin views. Public browser identity and the minimal Admin login shell are also
active. These changes do not alter Provider order, Batch deadlines, ordinary
media routing, or the zero-server-media contract.

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

- Supported short, canonical, share, modal, and copied-text URL forms resolve
  to the same `douyin:<aweme_id>` work identity before Provider execution.
- Short-link source resolution has its own bound; it does not consume the
  unchanged six-second Ultra Provider window.
- TianTianJieXi uses an aweme-bound modal request representation while cache,
  single-flight, cooldown, and attempt accounting remain keyed by the logical
  identity.
- The reviewed additional media admission is exact-host only:
  `v3-dy-a-x.ixigua.com`.
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

- Production retires the public `/submissions/*` URL surface while preserving
  the app, model, table, and migration history.
- Anonymous Django Admin login uses a minimal login shell. Authenticated active
  staff retain the existing Admin shell, while Provider runtime mutations
  require a superuser or the explicit Provider runtime change permission.
- Batch Runtime Admin is a singleton direct-edit surface with stale-write
  protection, explicit public-expansion confirmation, and non-sensitive audit
  logging.
- Release Management reads local Git refs only. The Web process must not fetch,
  own the checkout, or receive a deploy key.
- Production remains record-only while
  `RELEASE_MANAGEMENT_EXECUTOR_COMMAND` is empty and the separately reviewed
  executor helper/unit/sudoers boundary is not installed.

### Browser Identity

- Public pages and Django Admin share local favicon and Apple touch icon assets.
- Platform and Batch tabs use concise product-qualified titles and public pages
  expose only minimal description, theme color, and Open Graph metadata.
- The current star/download icon is a temporary, replaceable brand placeholder;
  it is not a final logo or a PWA commitment.

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

## Deferred After Promotion

These items remain outside the promotion gate and require real demand or a
separate evidence-backed diagnostic task:

```text
BROWSER_STREAM_RANGE_RESUME_V1 = DEFERRED_POST_RELEASE
DOWNLOAD_PAGE_CANCEL = DEFERRED_PENDING_USER_FEEDBACK
SUCCESSFUL_DOWNLOAD_STARTUP_TELEMETRY = SEPARATE_FUTURE_DIAGNOSTIC_PR
PHOTO_DOWNLOAD_VARIANT_FIDELITY = DEFERRED / DEMAND-DRIVEN
ADMIN_DASHBOARD_REDESIGN = DEFERRED_UNTIL_REAL_DATA
BAIDU_BROWSER_DOWNLOAD_COMPATIBILITY = DEFERRED_PENDING_REAL_USER_DEMAND
IOS_SAVE_TO_PHOTOS_UX = DEFERRED_PENDING_REAL_USER_FEEDBACK
MOBILE_SAVE_DESTINATION_GUIDANCE = BACKLOG / NOT A PROMOTION BLOCKER
```

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
6. Browser Photo Archive is production accepted, but the current XHS download
   candidate has not been proven to be the highest-quality original variant.
   `PHOTO_DOWNLOAD_VARIANT_FIDELITY` remains `OPEN / NOT STARTED`.
7. GitHub private-repository main protection remains subject to available
   plan/ruleset capabilities.
8. Certbot, Redis/PostgreSQL host configuration, Cloud-init, DNS, and broader
   host recovery are outside the repository-owned infrastructure baseline.
9. Mobile download completion, Ultra, title, cover, and size passed the real
   device gate. The observed Android Gallery versus iOS Files destination
   difference is accepted and non-blocking; automatic iOS Photos placement is
   not implemented.
10. Range resume, page cancellation, successful-start telemetry, photo
    original-variant fidelity, Admin dashboard redesign, Baidu Browser
    compatibility, iOS Photos UX, and mobile destination guidance retain the
    deferred statuses listed above.

These are current boundaries. Production convergence, Streaming telemetry,
the Nginx telemetry route, Nginx config-as-code, and historical branch cleanup
are complete and are not active blockers.

## Next Product Work

The project is now in the `REAL_USER_INTRODUCTION` phase. Do not continue
pre-release opportunistic optimization. Start a new task only when it has at
least one concrete source of evidence: Production Feedback, repeated user
reports, a real failure log, a measurable success-rate issue, demonstrated
interaction friction, or an actual platform requirement.

1. Start every new task from a freshly fetched `origin/main`, using one branch
   and one PR for one concrete task.
2. Keep request-scoped fresh-resource retry deferred until cache,
   single-flight, and continuation can share a safe freshness generation.
3. Do not reopen the Streaming architecture without new evidence of an
   application regression.
4. Keep production changes as separate, approved exact-SHA deployment tasks.
5. Do not treat a hypothetical technical gap as sufficient reason for a broad
   refactor.

Historical reconciliation, failure forensics, infrastructure, and branch audit
documents remain immutable evidence and should not be rewritten as current
status.
