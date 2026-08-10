# Codex Handoff

Last updated: 2026-08-10

This document is the first stop when a new Codex project or thread takes over AnimeImages.

## Repository

### Recent Local Codex Worktree

Recent local Codex work has used:

```text
/Users/xingkong/Documents/Codex/2026-06-03/django-python-pycharm-codex-netlify/djangoProject
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

Do not continue work in stale Codex worktrees or the old `douyin-source-resolver` experiment unless the user explicitly asks.

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

## Last Audited Runtime Baseline

This handoff was audited against the following application runtime baseline:

```text
6b1787f Make release Git state read-only (#18)
```

This SHA is not a real-time pointer to `main`. Fetch and read the current remote baseline before starting work:

```bash
git fetch origin
git log -1 --oneline origin/main
```

Recent merged milestones:

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
  - Allowed public users can parse and download, but must not receive raw-link
    copy/open/export tools.
- Provider runtime/admin console.
- Observability and health-test admin tools.
- Download strategy experiments to reduce server bandwidth while preserving stable downloads.

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
- `DouyinUltraRaceProvider` should not unfairly reduce TianTianJieXi's budget to reserve KuKuTool grace in race mode.

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
- `BatchParseRuntimeSetting.access_mode` is the single source for Batch
  navigation visibility, page access, and start/item/retry API access.
- `disabled` closes Batch for website visitors and staff, `staff` limits it to
  staff, `public_gray` uses the existing deterministic request rollout, and
  `public` allows everyone.
- Douyin original/ultra is checked by default when runtime allows it.
- Runtime settings are stored in `BatchParseRuntimeSetting`.
- Env values are fallback/initial defaults, not the day-to-day operating console.
- Allowed public users can download individual videos and images through the
  existing validated route/prepare/stream/status contract.
- Staff can additionally see raw copy/open/export tools and the strategy matrix.
- Public cards may contain only the existing allowlisted client media fields
  needed for validated routing; do not add Provider responses, headers,
  credentials, cache objects, or raw-link tools.

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

The Batch runtime Admin is a singleton direct-edit surface. Its top panel is
the saved/effective database state; the editable fields below it are only a
draft until `保存并生效` succeeds. An existing database row is authoritative,
while environment values are first-row defaults or database-failure fallback.
Public-expansion changes require explicit client confirmation and server-side
validation, stale forms are rejected using `updated_at`, and successful saves
emit a non-sensitive structured mutation log. This Admin hardening does not
change `batch_parse_access_allowed()`, gray rollout, Provider order, parsing,
scheduling, rate limits, or downloads.

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

Architecture north star for the unreleased Browser Direct restoration
candidate:

> AnimeImages is the media resolution and handoff control plane, not a media relay service.

AnimeImages owns parsing, authorization, Provider-manifest validation, route
selection, and handoff. A validated direct resource sends its media body from
the Provider/CDN to the browser and then to disk. The approved signed URL is
visible in browser Network tools; that does not permit public raw-link buttons
or exports.

- Manifest-validated TianTianJieXi MP4 videos use Browser Streaming Direct.
  Eligible KuKuTool MP4 videos use the same capability when runtime settings
  authorize it.
- The user click opens `showSaveFilePicker()` before network I/O. The browser
  then streams `response.body` into `FileSystemWritableFileStream`; large and
  unknown-size videos never use a whole-file Blob.
- Single and Batch expose one action named `下载` and share the same streaming
  state machine. Opening a media playback tab is not a download.
- Completion requires EOF, writer close, and exact byte equality when a
  reliable expected size exists. Picker cancellation starts no CDN request.
- Direct remains subject to Provider identity, resource ID, cache key, signed
  manifest, scheme, host allowlist, private-address rejection, media type, and
  expected-resource checks, and requires no merge or Cookie-bound retrieval.
- A direct-capable ordinary video never invokes download prepare, stream, or
  status endpoints. A direct failure may be retried or reparsed; it is not
  converted into a server media path by the frontend, routing layer, or Policy
  Engine.
- Expired Provider manifests use a distinct reparse message. Invalid, payload-
  mismatched, and forged manifests remain security rejections.
- Server-side media paths remain only for inherently different work: merge,
  multi-resource ZIP, server-generated output, or required Cookie/header
  retrieval that the browser cannot perform. They are not Browser Direct
  fallback paths.

This candidate is not production behavior until its PR is reviewed, merged,
and deployed. When changing this area, verify local Chrome and, in a separately
approved deployment task, the production site. Do not assume localhost
download behavior matches HTTPS production behavior.

### Web Concurrency Candidate

The stacked `codex/web-concurrency-provider-isolation-v1` candidate tracks a
`2 workers x 4 gthread` Gunicorn unit with the existing 60-second timeout in
`ops/systemd/animeimages.service`. Web concurrency and Provider concurrency are
separate: Single, Batch, and Provider-race TianTianJieXi calls share one
cross-process Redis lease, so at most one real parse request reaches that
upstream. Cache hits bypass the lease; a busy caller waits at most 250 ms and
falls through the existing provider chain with a URL-free `provider_busy`
event. The token-owned lease has a TTL and compare-and-delete release.

Production must use Redis when TianTianJieXi is enabled. LocMem is only a
single-process development fallback and is rejected by the production system
check. The tracked unit has not been installed, so treat the current server as
`PRODUCTION_UNIT_DRIFT` until an explicitly approved rollout verifies the live
unit. Do not mutate production from a feature PR.

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

PR #17 and PR #18 are merged and deployed. `/admin/release-management/` is available in production, but v1 is deliberately main-only and record-only by default. See `docs/release-management.md` for the complete operating model.

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

Before starting a new feature:

```bash
cd /Users/xingkong/Documents/Codex/2026-06-03/django-python-pycharm-codex-netlify/djangoProject
git status --short --branch
git log --oneline --decorate --max-count=8
.venv/bin/python manage.py check
.venv/bin/python manage.py makemigrations --check --dry-run
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

- Small updates can be done locally on a branch.
- Bigger modules should be pushed to GitHub branch for review.
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

## Suggested Next Work

Good next tasks from the current baseline:

1. Keep `docs/codex-handoff.md` and `docs/current-version-status.md` aligned after each major merge.
2. Review the batch parse service boundary created by PR #13 and remove residual `views.py` wrappers.
   - Continue from `xhs_download/services/batch_parse/`; do not restart the extraction.
   - Keep behavior stable and add focused tests around each moved boundary.
3. Then split batch page JavaScript.
   - Keep `/batch/` behavior stable.
   - Move batch page JavaScript out of `static/js/app.js`.
   - Separate settings modal, parse request lifecycle, result rendering, and download handoff logic.
4. Keep Release Management v1 record-only until the versioned root-owned helper, systemd unit, and sudoers boundary are separately installed and a controlled pending job is independently approved and tested on production.
   - Keep Tag/commit switching disabled.
   - Do not add automatic migration rollback in v1.
   - Do not give `www-data` GitHub credentials or Git write access.
5. Provider Strategy v1 remains stabilization and smoke work.
   - Keep TianTianJieXi/KuKuTool smoke fixtures healthy.
   - Continue measuring browser handoff vs server attachment.
   - Treat provider expansion as secondary to batch parse boundary cleanup.
