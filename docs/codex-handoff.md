# Codex Handoff

Last updated: 2026-07-07

This document is the first stop when a new Codex project or thread takes over AnimeImages.

## Repository

Use the real AnimeImages repository:

```text
/Users/xingkong/Documents/Codex/2026-06-03/django-python-pycharm-codex-netlify/djangoProject
```

Do not continue work in stale Codex worktrees or the old `douyin-source-resolver` experiment unless the user explicitly asks.

Remote:

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

Recent merged milestones:

- PR #11: Batch parse runtime setting, limited parallel parsing, default Douyin ultra checkbox.
- PR #10: Provider Strategy v1 with TianTianJieXi official API and Douyin ultra race.
- PR #8: Collapsible admin sidebar and admin center pages.
- PR #7: Native single-file browser download stabilization.

## Product Shape

AnimeImages is no longer just a Xiaohongshu downloader. The current product shape is:

- Universal parser homepage.
- Dedicated pages for Xiaohongshu, Douyin, Bilibili.
- Staff/admin batch parsing page.
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

After updating code on the server:

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
python manage.py check
python manage.py makemigrations --check --dry-run
```

If the change touches providers or downloads:

```bash
python manage.py inspect_third_party_provider --health --json
python manage.py smoke_tiantianjiexi_provider --url "<douyin test url>" --mode douyin-ultra
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
- Do not open a PR until the user confirms the task is ready.
- Do not merge to `main` unless the user explicitly asks.
- Use SSH remote already configured for GitHub.

Default branch naming:

```text
codex/<task-name>
```

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

1. Continue Provider Strategy v1 polishing:
   - Stable TianTianJieXi smoke fixtures.
   - Batch parse public/staff product rules.
   - Download handoff reliability and bandwidth measurement.
2. Improve documentation hygiene:
   - Mark historical policy/autonomous docs as archival.
   - Keep `docs/current-version-status.md` updated after each major PR.
3. Add a durable background queue only when downloads/merge jobs need progress that survives worker restarts.
4. Expand long-tail platforms only after provider health, platform allowlist, and download strategy are proven.
