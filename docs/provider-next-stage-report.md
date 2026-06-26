# Provider Next Stage Report

Date: 2026-06-26

Branch: `codex/provider-next-planning`

## Scope

This stage stabilizes the multi-provider foundation after the provider hardening work. It does not add new public platforms and does not enable broad third-party access. The target is a staff-only validation loop for:

- YouTube merge smoke tests.
- Staff-only gray-release gates.
- Failure logs and timing diagnostics.
- Async queue decision evidence.
- Long-tail platform opening criteria.

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

Last full verification run:

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

## Current Recommendation

Do not open generic third-party parsing broadly yet.

Recommended next operating mode:

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

Run staff smoke in PyCharm/local with real URLs and record results:

1. YouTube merge planning and `--download-smoke`.
2. YouTube normal 1080p `yt_dlp_merge`.
3. Imgur gallery through `gallery_dl_generic`.
4. Flickr image through `gallery_dl_generic` with a deliberately chosen timeout.

If any repeated staff smoke depends on a long-running download or parse, design the Redis plus Celery/RQ queue before public exposure.
