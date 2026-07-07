# Codex Handoff

Last updated: 2026-07-08

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

## Current Main Baseline

As of this handoff, `main` is at:

```text
c732e72 Fix batch parse ultra default and runtime controls (#11)
```

This commit only represents the time this document was last updated. Update this section after every `main` merge. If unsure, check the remote baseline:

```bash
git fetch origin
git log -1 --oneline origin/main
```

Recent merged milestones:

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
  - Public users must not see raw signed provider URLs.
- Provider runtime/admin console.
- Observability and health-test admin tools.
- Download strategy experiments to reduce server bandwidth while preserving stable downloads.

## Platform Responsibilities

### Xiaohongshu

- Dedicated adapter.
- Preserve original image bytes.
- Multi-image and video baseline downloads.
- Avoid unnecessary image transcode.

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

Important knobs:

```text
BATCH_PARSE_TOTAL_TIMEOUT_SECONDS=60
BATCH_PARSE_PARALLEL_WORKERS=4
BATCH_PARSE_IP_INTERVAL_SECONDS=20
BATCH_PARSE_DAILY_IP_LIMIT=100
```

Daily changes should happen in:

```text
/admin/xhs_download/batchparseruntimesetting/
```

## Download Strategy State

Current download strategy is mixed:

- Native image resources can prefer direct/browser paths to reduce server traffic.
- Provider videos may try browser-first / browser blob handoff.
- Server attachment fallback remains necessary for reliability.
- Large provider CDN downloads can be slow on the VPS if all bytes pass through the server.

Known tension:

- Browser-managed downloads save server bandwidth but can be blocked by popup/download restrictions.
- Server attachment is stable but consumes server egress and can be much slower than a third-party site that hands the CDN URL directly to the browser.

When changing this area, verify both local Chrome and the production site. Do not assume localhost download behavior matches HTTPS production behavior.

## Production Deployment Notes

Production should run with:

- PostgreSQL.
- Redis/Django cache.
- Gunicorn behind Nginx.
- `DJANGO_ENV=production`.
- `DJANGO_DEBUG=False`.
- `.env.production` readable by the process that runs management commands and systemd.

Preferred deploy path:

```bash
cd /srv/animeimages
./scripts/pre_deploy_check.sh
./scripts/deploy.sh
```

`deploy.sh` deploys `main` by default. Use it for routine production updates after the branch has been merged into `main`. For staging-only branch deploys, set `DEPLOY_BRANCH` explicitly. Do not run `deploy.sh` as root. The production checkout should stay owned by `ubuntu:ubuntu`.

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

## Smoke Checklist For A New Codex Thread

Before starting a new feature:

```bash
cd /Users/xingkong/Documents/Codex/2026-06-03/django-python-pycharm-codex-netlify/djangoProject
git status --short --branch
git log --oneline --decorate --max-count=8
.venv/bin/python manage.py check
.venv/bin/python manage.py makemigrations --check --dry-run
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

Documentation-only PRs may use a shorter body:

- Summary.
- Files changed.
- Verification.

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

Every PR description should include a `Documentation impact` line with one of:

```text
Documentation impact: Not needed
Documentation impact: Updated README/current-version/codex-handoff
Documentation impact: Historical docs updated
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

1. First refactor batch parse services out of `xhs_download/views.py`.
   - Suggested target package: `xhs_download/services/batch_parse/`.
   - Keep behavior stable.
   - Move parsing orchestration, runtime-setting lookup, result shaping, and permission helpers into focused service modules.
   - Add focused tests around the extracted boundaries.
2. Then split batch page JavaScript if needed.
   - Keep `/batch/` behavior stable.
   - Batch parse page JavaScript can move out of `static/js/app.js` when it grows further.
   - Separate settings modal, parse request lifecycle, result rendering, and download handoff logic.
3. Then continue Batch Parse v2 durable job/queue work only after service boundaries are cleaner.
   - Future objects may include durable `BatchParseJob` / `BatchParseItem` records.
   - Scope can include queue, progress, retry, history, and export.
   - Durable queue/progress is not the first refactor target.
   - Do not introduce Celery/RQ before the synchronous batch code is easier to reason about.
4. Provider Strategy v1 remains stabilization and smoke work.
   - Keep TianTianJieXi/KuKuTool smoke fixtures healthy.
   - Continue measuring browser handoff vs server attachment.
   - Treat provider expansion as secondary to batch parse boundary cleanup.
