# Codex Handoff

Last updated: 2026-08-20

This document is the first stop when a new Codex project or thread takes over AnimeImages.

## Repository

### Long-Lived Product Worktree

Product development currently uses the retained worktree:

```text
/Users/xingkong/.codex/worktrees/2358/djangoProject
```

This path is not a permanent fixed path. A new Codex thread must first confirm it is in the correct AnimeImages repository:

```bash
git remote -v
git status --short --branch
git log --oneline --decorate --max-count=5
```

The remote must point to:

```text
ssh://git@ssh.github.com:443/Daiobs/AnimeImages.git
```

The main project checkout is for synchronizing and auditing `main`, not routine
development. Keep this product worktree, but never continue a new task on an
old task branch. Fetch `origin`, detach at the latest `origin/main`, then create
one new branch and one PR for the task. Do not create another worktree unless
the user explicitly requests an isolated review or experiment.

### Production Server Checkout

The production server uses a separate checkout:

```text
/srv/animeimages
```

Do not confuse the local Codex worktree with the production server directory. Local development, code edits, branches, commits, and PR preparation happen in the local worktree. Production deployment happens on the server in `/srv/animeimages`.

### Remote

```text
origin ssh://git@ssh.github.com:443/Daiobs/AnimeImages.git
```

Current production site:

```text
https://download.cosflow.icu/
```

## Canonical And Production Baseline

Canonical development always starts from the latest fetched `origin/main`.
The promotion-ready application baseline is:

```text
766cecfd239f594e791c556b4caa69aebf2d1f9f
PR #54 and PR #55 on top of Production Polish
```

Production application code was deployed and accepted at:

```text
766cecfd239f594e791c556b4caa69aebf2d1f9f
checkpoint/production-promotion-ready-v1
```

The checkpoint resolves exactly to the production application SHA. It includes
the accepted Production Polish, Browser Photo Archive, Douyin canonical-source
identity, short-link Ultra, exact reviewed media-host, and video-size metadata
work. Production desktop and real-device acceptance verified Ultra, title,
cover, size, complete Browser Streaming downloads, and zero ordinary-media
server bytes. Migration `0024_userfeedback` remains the latest application
migration; PR #54 and PR #55 added none. After the docs-only closure merges,
repository `main` is intentionally ahead of the production runtime by
documentation only (`RUNTIME_CODE_DRIFT=0`).

```text
ANIMEIMAGES_PROMOTION_READY = YES
PROMOTION_BLOCKERS = NONE
PROMOTION_PHASE = REAL_USER_INTRODUCTION
HUMAN_MOBILE_GATE = PASS
```

On the devices used for the human gate, Android completed into Gallery and iOS
completed into Files rather than automatically into Photos. This destination
variance is accepted and non-blocking; it is not a universal claim about all
Android/iOS browsers or versions.

Fetch and resolve the live repository baseline before starting work:

```bash
git fetch origin
git log -1 --oneline origin/main
```

Do not substitute a checkpoint or production checkout for `origin/main` when
creating product branches. `docs/current-version-status.md` is the current
deployment-status source; dated reconciliation manifests retain historical
behavior context. See that file and `docs/repository-governance.md` first.

Recent merged milestones:

- PR #55: Restored Douyin size metadata by separating canonical work identity
  from the aweme-bound TianTianJieXi request representation, without adding a
  Provider call or media-size probe.
- PR #54: Unified supported Douyin URL forms around `douyin:<aweme_id>`, kept
  short-link resolution outside the six-second Ultra window, and admitted only
  the reviewed exact ixigua media host.
- PR #52: XHS source-identity protection, zero-media Batch correctness,
  responsive Gallery alignment, and Douyin display metadata completion.
- PR #51: Shared browser identity and minimal public metadata.
- PR #50: Lightweight, redacted user feedback with protected screenshot access
  and migration `0024_userfeedback`.
- PR #49: Consolidated project settings and restored the Batch header trigger.
- PR #48: Public result presentation boundary with staff diagnostics retained.
- PR #47: Legacy submissions retirement and hardened Admin login shell.
- PR #45: Browser-first single-photo and pure-photo STORE archives with reliable image format resolution.
- PR #44: Full Batch photo Gallery, responsive Lightbox, and download-all UX.
- PR #43: Download UI and Batch Actions production acceptance reconciliation.
- PR #42: Download UI cleanup and mixed-resource Batch actions.
- PR #19: Synchronized Release Management, deployment, handoff, changelog, README, and PR template documentation with the last audited production state.
- PR #18: Read-only local Git state for Release Management Web requests and an exact-path `safe.directory` production fix.
- PR #17: Admin Release Management v1 with audited main jobs, pinned target SHA, external non-blocking execution, and local Bootstrap Icons.
- PR #16: Production follow-up documentation and user-facing `mobile_feed` provider labeling.
- PR #15: Batch parse provider stability fixes and batch-specific TianTianJieXi budgeting.
- PR #14: Production deploy ownership and Gunicorn runtime hardening.
- PR #13: Initial extraction of batch parse logic into `xhs_download/services/batch_parse/`.
- PR #11: Batch parse runtime setting, limited parallel parsing, default Douyin ultra checkbox.
- PR #10: Provider Strategy v1 with TianTianJieXi official API and Douyin ultra race.
- PR #8: Collapsible admin sidebar and admin center pages.
- PR #7: Native single-file browser download stabilization.

## Product Shape

AnimeImages is no longer just a Xiaohongshu downloader. The current product shape is:

- Universal parser homepage.
- Dedicated pages for Xiaohongshu, Douyin, Bilibili.
- Runtime-gated batch parsing page.
  - Staff can see raw/direct controls.
  - Allowed public users can download validated results, but must not receive
    raw-link copy/open/export capability.
- Provider runtime/admin console.
- Observability and health-test admin tools.
- Browser Streaming for ordinary Provider video, with zero AnimeImages media
  bytes; server paths remain for generated output such as YouTube merges/ZIPs.

## Platform Responsibilities

### Xiaohongshu

- Dedicated adapter.
- Preserve original image bytes.
- Multi-image and video baseline downloads.
- Avoid unnecessary image transcode.
- Treat parsed JSON as diagnostic until it contains a note-like candidate; unrelated JSON must not suppress safe compatibility fallback.
- A matched structured gallery is authoritative only when normalization leaves valid resources. Other-note candidates may use explicit page meta/current-note DOM, but must suppress broad recommendation scans.
- Explicit login/private/deleted/unavailable states precede generic media fallback. Only verified structured public media from the matched current note can override a generic page marker; a current-note terminal declaration always wins.

### Douyin

- Normal mode uses native adapter/fallback for stable baseline parsing.
- Ultra mode uses Provider Strategy v1:
  - TianTianJieXi is the preferred official API upstream.
  - KuKuTool is fallback/race backup.
  - Native remains fallback.
- Supported short, canonical, share, modal, and copied-text URL forms resolve
  to `douyin:<aweme_id>` before Provider execution. Short-link resolution is
  separately bounded and does not consume the six-second Ultra window.
- TianTianJieXi cache, single-flight, cooldown, and attempt accounting use the
  canonical identity, while its request representation is an aweme-bound modal
  URL so the trusted Provider payload retains complete candidate and size
  metadata.
- The reviewed additional media host is the exact
  `v3-dy-a-x.ixigua.com`; do not generalize it to an `ixigua.com` suffix or
  wildcard family without a new security review.
- Fresh Ultra attempts start TianTianJieXi, KuKuTool, and Native at T=0. Native
  remains hidden during the six-second foreground window; a strong Ultra result
  inside that window is the first visible result.
- If the foreground window expires, the prepared Native fallback may be shown
  while a bounded continuation waits for an already-started Ultra Provider and
  can upgrade the same result later.
- One fresh external-Provider race consumes one user attempt. Cache/cooldown
  hits, single-flight waiters, continuation attach/poll, Native fallback, and
  automatic upgrades consume no new attempt.
- Production user limits are 10 Ultra attempts/minute and 10 generic
  third-party attempts/minute. TianTianJieXi/KuKuTool transient cooldowns are
  eight seconds. Provider-global limits and concurrency remain separate and
  unchanged.
- `DouyinUltraRaceProvider` must not unfairly reduce TianTianJieXi's budget to
  reserve KuKuTool grace in race mode.

### Bilibili

- Baseline public mp4/cover parsing only.
- Do not invest in high-quality DASH/cookie/member-rights work unless explicitly requested.

### YouTube / Long-Tail

- yt-dlp and gallery-dl are optional generic Provider capabilities.
- They are not replacements for dedicated Xiaohongshu/Douyin/Bilibili adapters.
- YouTube 1080p+ often needs yt-dlp + ffmpeg merge.
- Public long-tail exposure must go through runtime gate and platform allowlist.

## Provider Gate Model

Provider availability is split into layers:

- `.env` capability gate: the server is allowed/prepared to run the capability.
- Admin runtime gate: disabled / staff / public_gray / public.
- Platform gate: third-party allowlist/blocklist.
- Safety limits: timeouts, concurrency, traffic, file-size and rate limits.

Do not treat `.env *_ENABLED=True` as public launch approval. Routine rollout is done through:

```text
/admin/provider-settings/
```

## Important Admin Pages

```text
/admin/
/admin/provider-settings/
/admin/provider-health/
/admin/provider-health-tests/
/admin/console/
/admin/logs/
/admin/policy-center/
/admin/ops-dashboard/
/admin/xhs_download/batchparseruntimesetting/
/admin/release-management/
```

## Batch Parse State

`/batch/` is the current batch parsing UI.

Current behavior:

- It supports multiple links.
- Douyin original/ultra is checked by default when runtime allows it.
- Runtime settings are stored in `BatchParseRuntimeSetting`.
- Env values are fallback/initial defaults, not the day-to-day operating console.
- Staff can see raw/direct controls.
- Anonymous/public users must not see raw signed provider URLs.

Browser state boundaries:

- `animeimages.parsePreferences.v1` is the shared, versioned source for the Douyin original/ultra and YouTube highest-quality merge preferences on single-link and Batch pages.
- `animeimages.batchParseDraft.v2` contains only the Batch links draft.
- `xhsDownloadSettings` remains the home of single-page download mode, save location, file format, and other download-only settings. Its old parse fields and `animeimages.batchParseDraft.v1` are migration inputs, not primary stores.
- Client preferences never override server availability, access, rollout, Provider order, or timeout gates.

Important knobs:

```text
BATCH_PARSE_TOTAL_TIMEOUT_SECONDS=60
BATCH_PARSE_PARALLEL_WORKERS=4
BATCH_PARSE_TIANTIAN_PARALLEL_WORKERS=1
BATCH_PARSE_MAX_RESPONSE_TIMEOUT_SECONDS=55
BATCH_PARSE_IP_INTERVAL_SECONDS=20
BATCH_PARSE_DAILY_IP_LIMIT=100
```

Daily changes should happen in:

```text
/admin/xhs_download/batchparseruntimesetting/
```

`BATCH_PARSE_TIANTIAN_PARALLEL_WORKERS` and
`BATCH_PARSE_MAX_RESPONSE_TIMEOUT_SECONDS` are environment safety ceilings, not
daily admin controls. The interactive page reserves the existing whole-batch
rate limit through `/batch/start/`, then calls `/batch/item/` one item at a time.
The short-lived signed run token is bound to the client, each index is one-use,
and the server requires the previous index to finish before accepting the next;
parse results are not persisted. The worker and 55-second response
ceilings protect the synchronous no-JavaScript fallback. None of this changes
the single-link Provider Strategy. A Provider still receives only its configured
per-link deadline; the existing bounded completion grace may retain a row that
has already reached the fixed `completed` stage just after that deadline.
Incomplete work remains a timeout. When TianTianJieXi already supplies a usable
title and cover, its valid media result also skips an additional synchronous
native metadata request; cached/direct native metadata and missing-metadata
fallback remain available.

## Download Strategy State

The V1 stabilization contract is:

```text
ordinary Provider video: Provider/CDN -> user browser -> user disk
server-generated output: upstream -> AnimeImages processing -> user browser
```

- Manifest-validated ordinary TianTianJieXi and KuKuTool videos use the shared
  `static/js/ordinary_media_download.js` and
  `static/js/ordinary_media_streaming.js` primitive on both Single and Batch.
- `BROWSER_STREAMING` is the default: a root-scoped Service Worker fetches the
  Provider/CDN response and exposes a synthetic attachment stream to Browser
  Download Manager without a complete Blob or File System API.
- `BROWSER_BLOB` is a capability fallback only before Streaming begins, for
  cases such as missing Service Worker support or registration/activation
  failure. Once upstream fetch or Download Manager startup occurs, Blob
  fallback is forbidden to prevent duplicate media requests and files.
- Refresh queries the active Worker and reattaches the page to the same opaque
  `downloadId`; it does not reparse, restart the media request, or persist the
  signed URL. Site and Download Manager cancellation abort the upstream.
- The ordinary path does not call `/media/download-prepare/`,
  `/media/download-stream/`, or `/media/download-status/`. It does not fall back
  to server proxy, single-video ZIP, or another AnimeImages media relay.
- Ordinary failures offer only retry or reparse. The user sees `下载` and generic
  preparation/acquisition/save states, not transport architecture choices.
- The final signed media URL is intentionally visible to the receiving browser;
  it must not be written to logs, browser storage, documentation, or reports.
- Existing server attachment code remains only for a distinct resource class:
  generated YouTube merges, ZIPs, resources that genuinely require server-held
  Cookie/header context, and other server-generated outputs.
- The removed Batch ordinary-media progress implementation is documented in
  `docs/research/v1-batch-blob-progress-archive.md`; do not restore it as a
  second downloader.
- Chrome production proof for a 110,691,390-byte TianTianJieXi MP4 passed in
  Single and Batch with exact size, Download Manager progress, refresh
  reattachment, no playback, and zero AnimeImages media requests or bytes.
- Controlled Streaming passed 300 and 500 MiB without media-sized JavaScript
  buffering, concurrent cancel isolation, multiple refreshes, tab close, and a
  waiting-worker update. Safari passed a controlled 16 MiB smoke. Firefox and
  Edge remain untested because they were not installed.
- The Worker intentionally does not call `skipWaiting()`; an active version
  finishes existing downloads while a new version waits for normal activation.
- Browser Blob remains a bounded pre-start compatibility path, not the default
  ordinary-video architecture.

The canonical evidence and deployment state are in
`docs/current-version-status.md`,
`docs/research/service-worker-streaming-productionization-v1.md`, and the
production infrastructure audit. The current production application SHA is
`766cecfd239f594e791c556b4caa69aebf2d1f9f`, fixed by
`checkpoint/production-promotion-ready-v1`. Verify real browser behavior, not
only route metadata or unit tests, before changing this contract.

## Production Deployment Notes

Production should run with:

- PostgreSQL.
- Redis/Django cache.
- Gunicorn behind Nginx.
- Gunicorn running as `www-data`; the Web process reads application code and local Git refs but does not own the checkout.
- `DJANGO_ENV=production`.
- `DJANGO_DEBUG=False`.
- `.env.production` readable by the process that runs management commands and systemd.
- `RELEASE_MANAGEMENT_EXECUTOR_COMMAND` empty until the separately reviewed root-owned helper, systemd unit, and sudoers boundary are installed and validated.

Current production topology:

```text
Gunicorn user:  www-data
Gunicorn bind:  127.0.0.1:8000
Nginx upstream: http://127.0.0.1:8000
Checkout owner: ubuntu:ubuntu
```

The current server does not use `/run/animeimages/gunicorn.sock`. The systemd-managed Unix socket documented elsewhere is a future recommended hardening target, not a deployed production fact. Moving to it requires a separate systemd/Nginx change and smoke validation.

Preferred deploy path:

```bash
cd /srv/animeimages
./scripts/pre_deploy_check.sh
./scripts/deploy.sh
```

`deploy.sh` deploys `main` by default. Use it for routine production updates after the branch has been merged into `main`. For staging-only branch deploys, set `DEPLOY_BRANCH` explicitly. Do not run `deploy.sh` as root. The production checkout must stay owned by `ubuntu:ubuntu`; Gunicorn runs separately as `www-data`.

Manual fallback deploy path:

```bash
cd /srv/animeimages
git fetch origin
git reset --hard origin/main
source .venv/bin/activate
set -a
. /srv/animeimages/.env.production
set +a
python manage.py migrate
python manage.py collectstatic --noinput
sudo systemctl restart animeimages
curl -i http://127.0.0.1:8000/health/ready/
```

If sourcing `.env.production` prints `-m: command not found`, check quoted command values:

```text
YTDLP_BINARY="/srv/animeimages/.venv/bin/python -m yt_dlp"
GALLERYDL_BINARY="/srv/animeimages/.venv/bin/python -m gallery_dl"
```

## Admin Release Management v1

PR #17 and PR #18 are merged and deployed. `/admin/release-management/` is available in production, but v1 is deliberately record-only by default. See `docs/release-management.md` for the complete operating model.

- Every production Web request reads local Git refs only. The page has no remote-refresh action, and `www-data` must not run `git fetch`.
- Local `origin/main` and Tag refs are refreshed by the `ubuntu` deployment workflow or a separately controlled operations task.
- Creating a job reads the local `origin/main` and stores the exact deployment commit in `job.target_sha`.
- The database allows only one pending/running execution slot.
- Production remains record-only after the PR #17 and PR #18 rollout. The production environment example must leave `RELEASE_MANAGEMENT_EXECUTOR_COMMAND` empty.
- Without an executor, a release job remains `pending` as an audit record and does not deploy or restart services automatically.
- Django may submit a systemd job, but it must not run or wait for `deploy.sh` in the request thread.
- The v1 executor only accepts `/usr/bin/sudo -n /usr/local/sbin/animeimages-release-submit {job_id}`. Django binds the current numeric job ID and rejects direct systemd, shell, `deploy.sh`, alternate helper paths, wrong IDs, and extra arguments.
- Versioned templates live in `ops/release-executor/`; `scripts/validate_release_executor.sh` performs repository-only checks without sudo, installation, systemd startup, or database writes.
- Production has not installed or enabled this executor boundary. Set `RELEASE_MANAGEMENT_EXECUTOR_COMMAND` in `/srv/animeimages/.env.production` only after separately installing the root-owned helper, systemd unit, and sudoers rule, then validating runtime user, paths, permissions, a controlled pending job, logs, final SHA, and readiness.
- Gunicorn runs as `www-data`, while `/srv/animeimages` remains owned by `ubuntu:ubuntu`. The Web path may read local refs but must never execute `git fetch` or receive the production deploy key.
- Configure exactly `sudo git config --system --add safe.directory /srv/animeimages`. Do not change repository ownership, do not use `safe.directory=*`, and do not add a broader parent path.
- The external management command passes `DEPLOY_TARGET_SHA=job.target_sha`; deployment fails unless the SHA is reachable from `origin/main` and final `HEAD` equals it.
- Tags stay visible for reference. Tag/commit deployment and rollback are disabled until migration-aware rollback safeguards exist.

## Smoke Checklist For A New Codex Thread

Before starting a new feature from the long-lived product worktree:

```bash
cd /Users/xingkong/.codex/worktrees/2358/djangoProject
git fetch --prune origin
git status --short --branch
git switch --detach origin/main
git rev-list --left-right --count HEAD...origin/main
git log --oneline --decorate --max-count=8
/Users/xingkong/Documents/Codex/2026-06-03/django-python-pycharm-codex-netlify/djangoProject/.venv/bin/python manage.py check
/Users/xingkong/Documents/Codex/2026-06-03/django-python-pycharm-codex-netlify/djangoProject/.venv/bin/python manage.py makemigrations --check --dry-run
bash -n ops/release-executor/animeimages-release-submit
./scripts/validate_release_executor.sh
```

If the change touches providers or downloads:

```bash
.venv/bin/python manage.py inspect_third_party_provider --health --json
.venv/bin/python manage.py smoke_tiantianjiexi_provider --url "<douyin test url>" --mode douyin-ultra
```

If the change touches `/batch/`:

- Open `/batch/`.
- Confirm Douyin ultra/default checkbox behavior.
- Confirm staff vs anonymous raw-link visibility.
- Confirm result cards have cover images.
- Confirm download controls do not open raw signed URLs for public users.

If the change touches admin:

- Open `/admin/`.
- Open `/admin/provider-settings/`.
- Open `/admin/provider-health-tests/`.
- Open `/admin/console/`.
- Open `/admin/logs/`.
- Confirm no `OperationalError` from missing migrations.

## Git Workflow Preference

User preference:

- `origin/main` is the only development baseline; production checkpoints and
  historical feature branches are not new-task bases.
- Use the retained product worktree sequentially, with only one active
  development branch at a time.
- Every concrete task uses its own branch and its own PR.
- After a PR merges, verify the tree is clean, fetch, and switch the worktree to
  detached latest `origin/main` before creating the next task branch.
- Do not continue development on a merged task branch or combine unrelated
  tasks in one PR.
- Pushing a branch for review is allowed.
- Opening a Pull Request requires user confirmation unless explicitly requested.
- Do not open a PR until the user confirms the task is ready.
- Once a PR is created, the PR body must follow Extended Description Rules.
- Merging `main` requires an explicit user request.
- Documentation-only branches should still be reviewed before merge.
- Use SSH remote already configured for GitHub.
- For a squash merge, keep the complete design record in the PR body. The squash commit body must retain concise `Summary`, `Validation`, `Deployment`, and `Rollback` information at minimum.

Default branch naming:

```text
codex/<task-name>
```

## Pull Request Description Rules

All non-trivial Pull Requests must include an Extended Description. Do not create PRs with only a one-sentence Summary.

The PR body must include these sections:

### Summary

Explain:

- What this PR changes.
- The main modules, scripts, docs, or runtime surfaces affected.

### Why

Explain:

- Why this change is needed.
- What problem it fixes.
- The background or incident that motivated the change.

### Design / Implementation

Explain:

- The core design.
- The main implementation approach.
- Important architecture decisions.

### Scope

Explain:

- What this PR includes.
- What it explicitly does not include.
- Any boundaries that prevent reviewer confusion.

### Safety

Explain:

- Whether permissions are affected.
- Whether security posture is affected.
- Whether migrations are added.
- Whether Provider, Download, or Runtime behavior changes.

### Documentation Impact

Choose exactly one:

```text
Updated: <files>
Not needed: <reason>
```

### Verification

List the exact test, check, and manual verification commands that were run.

Common examples:

```bash
python manage.py check
python manage.py makemigrations --check --dry-run
python manage.py test xhs_download --noinput
git diff --check
```

### Deployment Notes

If the PR touches any of these areas, call out deployment notes explicitly:

- systemd
- Nginx
- environment variables
- migrations
- server config

### Rollback

Explain:

- How to roll back.
- Whether the PR can be reverted directly.
- Whether database handling is required.

### Non-goals

List work that is intentionally excluded from the PR so reviewers do not infer a broader rollout or architecture change.

Add PR-type-specific sections when relevant:

### Feature PR

Also include:

- User impact.
- Provider / API impact.
- Runtime behavior changes.

### Refactor PR

Also include:

```text
Before:
Current structural problem.

After:
New structure.

Compatibility:
Why existing behavior is preserved.
```

### Infrastructure PR

Also include:

- Server impact.
- Ownership / permission changes.
- systemd changes.
- Nginx changes.
- Deployment risk.

### Documentation PR

Documentation-only PRs still use the Extended Description headings. Keep Design / Implementation, Scope, Safety, Deployment Notes, Rollback, and Non-goals concise when they are not complex, and list all changed documentation under Documentation Impact.

## Documentation Maintenance Rules

Every large PR must explicitly consider documentation. This is part of the handoff contract for future Codex projects.

Update `docs/current-version-status.md` when a PR changes any of:

- product capability;
- parsing or download routing;
- deployment or smoke flow;
- Provider strategy;
- runtime setting behavior;
- admin entrypoints;
- public/staff rollout behavior.

Update `docs/codex-handoff.md` when a PR changes:

- the repository/worktree handoff path;
- development or deployment workflow;
- Git/PR workflow expectations;
- the current source-of-truth documents;
- the next recommended work for a new Codex thread.

Historical plan documents must be marked clearly as `historical`, `archival`, or `research` when they no longer represent the active production route. They can remain useful background, but must not be treated as the current source of truth.

Every PR description must include a `Documentation Impact` section with exactly one of:

```text
Updated: <files>
Not needed: <reason>
```

## Known Pitfalls

- Current Codex desktop worktree can point to a stale repo; always verify the real path.
- `python` may not exist on macOS or Ubuntu unless the venv is activated; use `python3` to create the venv and `.venv/bin/python` or activated `python` afterward.
- New migrations must be applied locally and on the server before opening admin pages.
- Admin pages returning 500 are often unapplied migrations, not template bugs.
- Do not save full signed CDN URLs, account credentials, cookies, Authorization headers, or provider manifest tokens in logs.
- Do not move routine runtime settings back into `.env` if there is already an admin runtime model.
- TikHub is historical/diagnostic and should not be reintroduced as the active Douyin ultra path without explicit approval.
- A production SHA outside main ancestry is `PRODUCTION_CODE_DRIFT`, not an
  alternate development baseline. Reconcile it through an audited PR promptly.
- Production settings and persisted runtime values are not represented merely
  by checking out the same code. Never claim environment parity without an
  explicit runtime audit.

## Suggested Next Work

AnimeImages is in the `REAL_USER_INTRODUCTION` phase. Do not resume
"pre-release polish" by default. A new task should be backed by Production
Feedback, repeated user reports, a real failure log, a measurable success-rate
problem, demonstrated interaction friction, or an actual platform request.

Good next tasks from the promotion-ready baseline:

1. Start from a freshly fetched `origin/main`, with one branch and one PR for
   one concrete task.
2. Keep `BROWSER_STREAM_RANGE_RESUME_V1` deferred until post-release work.
3. Keep `DOWNLOAD_PAGE_CANCEL` deferred pending user feedback.
4. Implement successful download-startup telemetry only in a separate future
   diagnostic PR.
5. Keep photo original-variant fidelity open and not started; do not mix it
   into unrelated XHS reliability work.
6. Keep iOS Save to Photos UX and mobile save-destination guidance deferred
   until real-user feedback establishes demand.
7. Keep Baidu Browser compatibility deferred pending real-user demand.
8. Keep the Admin dashboard redesign deferred until real data exists.
9. Do not reopen the Streaming architecture without new evidence of an
   application regression.
10. Keep production changes in separately approved exact-SHA deployment tasks.
11. Keep Release Management record-only until its separately reviewed executor
    boundary is installed and accepted.
