# Provider Next Stage Report

Date: 2026-06-27

Branch: `codex/provider-next-planning`

## Scope

This stage stabilizes the multi-provider foundation after the provider hardening work. It does not add new public platforms and does not enable broad third-party access. The target is a staff-only validation loop for:

- YouTube merge smoke tests.
- Staff-only gray-release gates.
- Failure logs and timing diagnostics.
- Async queue decision evidence.
- Long-tail platform opening criteria.

Current production priority update:

- Staff-only provider smoke is not the pre-launch main goal.
- The immediate target is the production deploy baseline: Ubuntu VPS, Gunicorn, Nginx, PostgreSQL, Redis, HTTPS, explicit production environment, health checks, and rollback procedure.
- Provider smoke continues after launch as staging/staff validation. It must not block or broaden the first public deployment.

## Completed

### YouTube Merge Smoke

`inspect_youtube_merge` now reports a machine-readable `smoke` summary for planning:

- `smoke.recommended_rollout`
- `smoke.merge_candidate_available`
- `smoke.selected_height`
- `smoke.single_file_height`
- `smoke.estimated_size_bytes`
- `smoke.within_frontend_budget`
- `smoke.issues`

The command also supports `--download-smoke`, which runs the real yt-dlp plus ffmpeg merge path and deletes the temporary file by default. Download smoke output includes:

- `download_smoke.ok`
- `download_smoke.duration_ms`
- `download_smoke.size_bytes`
- `download_smoke.content_type`
- `download_smoke.queue.queue_recommended`
- `download_smoke.queue.sync_acceptable`

This separates "planning is fast enough" from "real download/merge should move to a queue".

On 2026-06-27, a real smoke pass also fixed one command-level mismatch: `inspect_youtube_merge` now defaults planning to `YTDLP_MERGE_DEFAULT_MAX_HEIGHT=1080`. Before this fix, the command could plan with the older `YOUTUBE_MERGE_MAX_HEIGHT=4320` default and select 2160p/4320p even when the normal YouTube mode was meant to validate 1080p.

### Staff-Only Gates

Generic third-party parsing now has an explicit `THIRD_PARTY_STAFF_ONLY` switch. The `/results/` flow blocks anonymous or non-staff users when the generic provider entrypoint is staff-only, while staff users can still validate configured providers.

Both `inspect_third_party_provider --health --json` and `/admin/provider-health/` now expose a `rollout` object:

- `disabled`
- `hold`
- `staff_only`
- `public_candidate`

`public_candidate` means the technical checks pass, not that broad public access is approved.

### Timing And Failure Diagnostics

Provider parse diagnostics now include both selected-provider time and whole-chain time:

- `selected_provider_duration_ms`: only the provider that produced the selected result.
- `provider_total_duration_ms`: all provider attempts in the chain, including failed fallbacks.

Page-level logs now include requested/allowed flags, selected provider, fallback count, image/video counts, selected provider duration, and total provider duration. They log only the submitted host, not the full source URL.

### Long-Tail Provider Smoke

`inspect_third_party_provider <url> --provider <provider> --json` now includes a `smoke` summary:

- `recommended_rollout`
- `within_frontend_budget`
- `asset_count`
- `media_types`
- `selected_provider`
- `source_platform`
- `fallback_count`
- `attempt_count`
- `issues`

Successful parses use `staff_only` or `hold`; provider-attempt failures also include `smoke.recommended_rollout=hold` so failed staff smoke can be aggregated with successful smoke results. This gives Imgur, Flickr, YouTube, and future long-tail candidates the same staff-only/hold decision shape.

## Verification

Last full verification run on 2026-06-27:

```bash
.venv/bin/python manage.py check
.venv/bin/python manage.py makemigrations --check --dry-run
.venv/bin/python manage.py test --noinput --verbosity 1
.venv/bin/python manage.py inspect_third_party_provider --health --json
```

Result:

- Django system check: passed.
- Migration dry-run: no changes detected.
- Test suite: `390 tests OK`.
- Third-party health: `ok=true`.
- Current local rollout: `staff_only` because the generic third-party entrypoint now defaults to `THIRD_PARTY_STAFF_ONLY=True` even when a provider is technically ready.

Runtime versions from `inspect_youtube_merge --health --json`:

- yt-dlp: `2026.03.17`
- ffmpeg: `8.1.2`

Health note:

- `inspect_third_party_provider --health --json` reports the generic third-party rollout as `staff_only`.
- `inspect_youtube_merge --health --json` currently reports `youtube_merge.staff_only=false` and `youtube_merge.public_ready=true` in this local environment. This means command-line YouTube merge is technically ready, but product rollout should still be governed by the staff-only generic entrypoint and UI gate before public exposure.

## YouTube Staff Smoke Results

Planning smoke commands:

```bash
.venv/bin/python manage.py inspect_youtube_merge "https://www.youtube.com/watch?v=dt-m5GT2rSc" --json
.venv/bin/python manage.py inspect_youtube_merge "https://www.youtube.com/watch?v=sxVqcCslnAE" --max-height 4320 --json
.venv/bin/python manage.py inspect_youtube_merge "https://www.youtube.com/watch?v=jNQXAC9IVRw" --json
.venv/bin/python manage.py inspect_youtube_merge "https://www.youtube.com/watch?v=dQw4w9WgXcQ" --json
```

Results:

| Scenario | URL | planning_duration_ms | max_height | selected_height | estimated_size_bytes | smoke recommendation | Notes |
| --- | --- | ---: | ---: | ---: | ---: | --- | --- |
| Normal 1080p split audio/video | `https://www.youtube.com/watch?v=dt-m5GT2rSc` | 1951 | 1080 | 1080 | 188344856 | `staff_only` | Complete single-file candidate was only 360p, so 1080p requires merge. |
| Highest quality candidate | `https://www.youtube.com/watch?v=sxVqcCslnAE` | 1809 | 4320 | 4320 | 80232125 | `staff_only` | Explicit high-quality planning selected 4320p format `571`. |
| Low quality / no 1080p | `https://www.youtube.com/watch?v=jNQXAC9IVRw` | 2242 | 1080 | 240 | 742369 | `hold` | Smoke issues: `single_file_is_sufficient`, `selected_height_below_1080p`. |
| Cover fallback probe | `https://www.youtube.com/watch?v=dQw4w9WgXcQ` | 2177 | 1080 | 1080 | 84361446 | `staff_only` | Planning path is healthy; use this class of URL to keep validating signed cover fallback behavior in UI smoke. |

Download smoke command:

```bash
.venv/bin/python manage.py inspect_youtube_merge "https://www.youtube.com/watch?v=sxVqcCslnAE" --max-height 4320 --download-smoke --json
```

Download smoke result:

| URL | selected_height | estimated_size_bytes | download_smoke.duration_ms | download_smoke.size_bytes | content_type | queue_recommended |
| --- | ---: | ---: | ---: | ---: | --- | --- |
| `https://www.youtube.com/watch?v=sxVqcCslnAE` | 4320 | 80232125 | 48183 | 80231323 | `video/mp4` | `true` |

Interpretation:

- Planning is fast enough for staff-only synchronous validation: all planning runs completed in about 1.8-2.3 seconds.
- Real download plus merge is not suitable for synchronous page requests. The successful 4320p smoke took about 48 seconds, so YouTube merge downloads should move to a queue before public exposure.
- Staff-only UI gray release is reasonable for planning and manifest validation, but not for public synchronous download/merge.

## Current Recommendation

Do not open generic third-party parsing broadly yet.

Recommended next operating mode after the production baseline is deployed:

- Keep dedicated adapters as the public path.
- Enable YouTube merge only for staff/local validation.
- Enable generic yt-dlp/gallery-dl only for staff smoke cohorts.
- Treat YouTube merge downloads with `download_smoke.queue.queue_recommended=true` as queue candidates.
- Treat gallery-dl cold parses that exceed the frontend budget as staff-only with raised timeout or queue/sidecar candidates.

## Remaining Risks

- No durable task queue yet. Long-running merge/download tasks still rely on the existing synchronous download path or local in-process task behavior.
- No persisted timing dashboard yet. Logs contain enough fields for P50/P95 analysis, but they are not stored in a first-class model or metrics backend.
- Long-tail platforms still need per-platform smoke evidence for preview loading, content-type validation, final-host validation, and download behavior.
- `public_candidate` is intentionally conservative; it should not be interpreted as product approval to enable public traffic.

## Next Suggested Step

Complete the production deploy baseline first:

1. Use `docs/production-deploy-baseline.md` for the architecture and safety policy.
2. Use `docs/production-deploy-checklist.md` for the server runbook.
3. Deploy only the stable dedicated adapters publicly.
4. Confirm `/health/live/`, `/health/ready/`, ZIP download, preview proxy, and basic adapter smoke.

After that, resume staff smoke in staging with real URLs and record results:

1. YouTube merge planning and `--download-smoke`.
2. YouTube normal 1080p `yt_dlp_merge`.
3. Imgur gallery through `gallery_dl_generic`.
4. Flickr image through `gallery_dl_generic` with a deliberately chosen timeout.

If any repeated staff smoke depends on a long-running download or parse, design the Redis plus Celery/RQ queue before public exposure.
