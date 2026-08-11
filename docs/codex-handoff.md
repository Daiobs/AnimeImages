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

### Provider Resource Lifecycle

- Keep the Provider payload cache, AnimeImages Provider manifest, and upstream
  CDN URL as three independent lifecycles.
- TianTianJieXi payload cache hits can return a previously cached media URL
  without calling the upstream API. A user-visible parse or retry action does
  not imply an uncached upstream parse.
- Manifest validity proves the signed AnimeImages asset/URL identity and token
  age, not upstream CDN reachability or URL lifetime.
- The current code does not know TianTianJieXi CDN URL lifetime. Record it as
  `CDN_URL_LIFETIME_UNKNOWN`; do not infer it from the 120-second payload cache
  or the 3600-second manifest maximum age.
- In tests, use `uncached_upstream_parse` only when an upstream call is proved,
  and `newly_issued_provider_result` only when the Provider has actually
  returned the result in that test.

Read [Provider Resource Lifecycle](provider-resource-lifecycle.md) before
changing cache, manifest, recovery, or download behavior.

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

> Server is the download control plane, not the default media data plane.

AnimeImages owns parsing, authorization, Provider-manifest validation, route
selection, and handoff. A validated direct resource should send its large media
body from the Provider/CDN to the browser. The user has accepted that the final
signed URL is visible in browser Network tools; that does not permit public
raw-link buttons or exports.

- Browser native handoff and browser fetch/Blob are separate capabilities.
  Visible native navigation does not require fetch CORS. Fetch/Blob is allowed
  only for explicitly CORS-safe resources with a known size of at most 32 MiB.
- Manifest-validated TianTianJieXi videos prefer native browser handoff.
  KuKuTool does the same only in its `browser_first` runtime mode; its explicit
  server-stream/server-only modes continue to use relay.
- Large or unknown-size videos must not use Blob-first. If the CDN serves media
  inline, native handoff opens a visible source tab and must not claim that a
  browser download item started or completed.
- Direct remains subject to Provider identity, resource ID, cache key, signed
  manifest, scheme, host allowlist, private-address rejection, media type, and
  expected-resource checks. CORS failure alone is not a relay reason.
- Explicit relay reasons include cookie-bound resources, merge requirements,
  server-only Provider behavior, and a browser handoff that cannot be safely
  authorized. For Direct-capable inline media, relay is available only through
  the separately labeled compatibility action; Direct failure never silently
  changes the next click into relay.
- Single-result and Batch pages use the same route contract. Allowed public
  users may invoke validated downloads, while raw-link copy/open/export UI
  remains staff-only.
- A Direct response records only a successful routing decision and emits a
  `route_selected` structured event. It does not create a completed-download
  record because the server cannot observe browser completion.
- Expired Provider manifests use a distinct reparse message. Invalid, payload-
  mismatched, and forged manifests remain security rejections.
- A retained server stream writes `DownloadRequestLog.success` only when its
  iterator exhausts normally. `actual_bytes` counts bytes actually yielded;
  disconnects and generator/upstream errors are failures.

The following server-relay details remain relevant only when a route has an
explicit relay requirement or the user invokes the controlled fallback:

- Prepare is local-only: it performs request/schema, manifest, route, limit,
  and client-binding checks, then issues separate opaque stream and status
  URLs without contacting the media CDN. The stream claim is one-use; status
  polling is repeatable, cache-only, client-bound, and does not extend the
  60-second TTL. The server-private handoff cache uses a typed field allowlist
  and retains one manifest-validated minimal media URL for the initial GET;
  it never places that URL in the token, status payload, logs, database, or
  browser storage.
- The normal server attachment path opens one upstream media response, checks
  its first non-empty chunk, and continues the `StreamingHttpResponse` from
  that same iterator. Active responses, sockets, and generators are never
  stored in cache. The stream owns its concurrency slot until completion,
  failure, or client disconnect.
- The frontend polls only the download startup states and stops at
  `streaming`, `failed`, `expired`, or its 15-second hard ceiling. It never
  fetches the media body or creates a large JavaScript Blob for this path.
- Response construction does not set `stream_started`. The stream iterator
  sets it once, immediately before yielding the already-validated first chunk.
  It then remains sticky across later failures; closing before iteration keeps
  it false. Exhausting the polling window without confirmation is an
  unconfirmed, retryable state rather than proof that the download failed.
- For server single-file handoff, issuing the browser request keeps the page
  progress bar in its connecting state. It reaches 100% only after status
  confirms `stream_started`, representing confirmed browser handoff rather
  than media bytes transferred through Django. Actual byte progress belongs
  to the browser download UI. Safe stream logs record attempt, refresh, and
  first-byte durations without source or signed media URLs.
- TianTianJieXi recovery is server-only, bounded to one refresh and one
  download retry (at most two media GETs), and must preserve the original
  signed resource identity.
- Server-stream startup has one absolute 50-second budget across the first
  media GET, Provider refresh, recovery GET, and first non-empty chunk.
  Operations receive only the remaining budget. Exhaustion uses
  `DOWNLOAD_STREAM_START_TIMEOUT`, closes the upstream, releases concurrency,
  and does not continue refreshing. The budget stops applying once normal
  stream iteration begins.
- Large provider CDN downloads can be slow on the VPS if all bytes pass through the server.

Known tension:

- Browser-managed downloads save server bandwidth but can be blocked by popup/download restrictions.
- Server attachment is stable but consumes server egress and can be much slower than a third-party site that hands the CDN URL directly to the browser.

This candidate is not production behavior until its PR is reviewed, merged,
and deployed. When changing this area, verify local Chrome and, in a separately
approved deployment task, the production site. Do not assume localhost
download behavior matches HTTPS production behavior. Do not infer fetch CORS
support from a successful native browser navigation.

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
