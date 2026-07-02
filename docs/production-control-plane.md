# Production Control Plane v1

`/admin/control-plane/` is the unified read-only production control center for AnimeImages. It does not replace existing admin pages. It aggregates their current state so an operator can decide where to look next.

## Purpose

The control plane answers five questions:

1. Is the system healthy right now?
2. Is download traffic still inside the cost guard?
3. Are the main Providers stable?
4. Do Policy, Feedback, or Governance systems have pending work?
5. Which admin page should the operator open next?

## Read-Only Contract

The page is intentionally read-only.

It does not:

- modify Provider runtime settings
- modify DownloadRuntimeSetting
- approve or reject Governance decisions
- run Provider health tests
- call third-party Provider APIs
- trigger downloads or parsing jobs

It only reads local health state, database logs, runtime settings, and cached aggregates.

## Data Sources

The page reads:

- `/health/ready/` runtime readiness helper
- `ParseRequestLog`
- `DownloadRequestLog`
- `ProviderHealthTestRun`
- `ProviderRuntimeSetting`
- `DownloadRuntimeSetting`
- `PolicyLearningRun`
- `PolicyOptimizationProposal`
- `PolicyControlRecord`
- `SelfHealingEvent`
- Feedback Loop summaries
- Governance v8 local context

Every section is wrapped independently. If one model, query, or cache read fails, the page should degrade that section and continue rendering.

## Risk Levels

The top status strip shows one of three risk levels:

- `normal`: health is ready and no obvious threshold is exceeded.
- `warning`: examples include download traffic above 70%, Provider health failures, pending approvals, or many blocked/rate-limited downloads.
- `danger`: examples include `/health/ready/` not ready or download traffic above 90% of the configured global daily byte limit.

The risk level is advisory. It never changes runtime settings automatically.

## Action Center

Action Center turns recent signals into simple next-step suggestions:

- health not ready -> check `/health/ready/`, Redis, and PostgreSQL
- traffic above 70% -> inspect DownloadRuntimeSetting and public rollout
- Provider health failed -> run Provider tests and keep staff-only
- pending approvals -> open Policy Control
- many blocked/rate-limited downloads -> inspect Download Logs and Cost Guard
- no obvious issues -> continue observation

Suggestions are navigation hints only.

## Daily Check Flow

Recommended daily production check:

1. Open `/health/ready/`.
2. Open `/admin/control-plane/`.
3. Check the risk level and Action Center.
4. Check Provider status.
5. Check download traffic and route distribution.
6. Check pending approvals and governance decisions.
7. Open parse/download/provider logs only when the control plane points to a problem.

## Why Provider Tests Are Not Run Automatically

Provider tests can call external services, consume quotas, and trigger upstream rate limits. The control plane must stay cheap, fast, and safe to refresh. Use `/admin/provider-health-tests/` for explicit smoke tests.

## Permissions

Access is allowed for:

- superusers
- staff users with any one of:
  - `xhs_download.view_parserequestlog`
  - `xhs_download.view_downloadrequestlog`
  - `xhs_download.view_providerhealthtestrun`
  - `xhs_download.view_provideradminauditlog`

Non-staff users and staff without permission receive a 404.

## Troubleshooting 500s

If `/admin/control-plane/` fails:

1. Confirm migrations are applied:

```bash
python manage.py showmigrations xhs_download
python manage.py migrate
```

2. Confirm the running service loads the intended production environment.
3. Check the service log:

```bash
journalctl -u animeimages --since "30 minutes ago" | grep control_plane
```

4. Open individual pages such as `/admin/console/`, `/admin/provider-health-tests/`, and `/admin/ops-dashboard/` to isolate which subsystem is failing.

Section failures are logged as `control_plane_section_failed`.

## Deployment Notes

No dedicated migration is required for the control plane itself. It depends on existing observability, Provider health, policy, and governance migrations. Run the normal deploy sequence:

```bash
python manage.py migrate
python manage.py collectstatic --noinput
sudo systemctl restart animeimages
```

## Rollback

Rollback is a normal code rollback:

1. Deploy the previous commit.
2. Run `python manage.py migrate` only if the rollback target requires it.
3. Run `python manage.py collectstatic --noinput`.
4. Restart Gunicorn.

The page is read-only, so rolling it back does not require reverting production data.
