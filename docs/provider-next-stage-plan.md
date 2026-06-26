# Provider Next Stage Plan

Date: 2026-06-26

Branch: `codex/provider-next-planning`

## Goal

Move the multi-provider layer from "implemented" to "staff-only usable" without opening risky public third-party traffic. This stage is about repeatable smoke tests, gray-release gates, diagnostics, and queue decisions. It is not a platform-expansion sprint.

## 1. YouTube Merge Smoke Test

YouTube merge is required for normal 1080p+ downloads because many YouTube formats are split into video-only and audio-only streams. The smoke test must prove both parse planning and final download behavior.

Run before staff-only rollout:

```bash
.venv/bin/python manage.py inspect_youtube_merge \
  "https://www.youtube.com/watch?v=sxVqcCslnAE" \
  --binary ".venv/bin/python -m yt_dlp" \
  --timeout 15 \
  --json
```

Pass criteria:

- `ok=true`
- `smoke.recommended_rollout=staff_only`
- `smoke.merge_candidate_available=true`
- `smoke.selected_height >= 1080`
- `smoke.within_frontend_budget=true`
- `plan.estimated_size_within_limit=true`
- URLs remain redacted unless `--show-urls` is explicitly used for local debugging.

If `recommended_rollout=hold`, do not expose the switch beyond direct local testing. Fix the issue first or document the reason, such as size limit, timeout, or no merge candidate.

After planning passes, run an explicit download smoke. This may download and merge a large file, so it is not part of normal health checks:

```bash
.venv/bin/python manage.py inspect_youtube_merge \
  "https://www.youtube.com/watch?v=sxVqcCslnAE" \
  --binary ".venv/bin/python -m yt_dlp" \
  --timeout 15 \
  --download-smoke \
  --json
```

Download smoke pass criteria:

- `download_smoke.ok=true`
- `download_smoke.content_type=video/mp4`
- `download_smoke.size_bytes > 0`
- `download_smoke.queue.queue_recommended=false` means the tested file is still acceptable for a synchronous staff path.
- `download_smoke.queue.queue_recommended=true` means the merge/download should be treated as a queue candidate before public or repeated staff use.
- The command completes within a staff-acceptable manual test window.
- The output file is deleted by default. Use `--keep-file` only when manually checking playback, audio, and metadata.

Baseline result on June 26, 2026:

- URL: `https://www.youtube.com/watch?v=sxVqcCslnAE`
- Cold-ish planning smoke: `duration_ms=3634`
- Warm planning smoke after command/cache reuse: `planning_duration_ms=1919`
- `smoke.recommended_rollout=staff_only`
- `smoke.selected_height=4320`
- `smoke.single_file_height=360`
- `smoke.estimated_size_bytes=80232125`
- `smoke.within_frontend_budget=true`
- `smoke.issues=[]`

Baseline download smoke result on June 26, 2026:

- URL: `https://www.youtube.com/watch?v=sxVqcCslnAE`
- `download_smoke.ok=true`
- `download_smoke.content_type=video/mp4`
- `download_smoke.size_bytes=80231323`
- `download_smoke.duration_ms=27473`
- `download_smoke.queue.queue_recommended=true`
- The merged file was deleted automatically because `--keep-file` was not used.

This proves the server-side merge path can produce a real mp4, but the 27-second merge/download time is too slow for ordinary synchronous web requests. Keep it staff-only for now, and move public/high-volume use to a durable background queue.

## 2. Staff-Only Gray Release

Keep provider exposure layered:

- Public users: dedicated adapters plus stable normal paths only.
- Staff users: may enable YouTube highest-quality merge and selected generic provider smoke paths.
- Admin/operator: can inspect provider health and disable provider controls.

Required settings for a local staff-only YouTube merge smoke:

```text
THIRD_PARTY_PROVIDER_ENABLED=True
THIRD_PARTY_PUBLIC_ACCESS_ENABLED=True
THIRD_PARTY_STAFF_ONLY=True
YTDLP_PROVIDER_ENABLED=True
YTDLP_BINARY=.venv/bin/python -m yt_dlp
YTDLP_MERGE_PROVIDER_ENABLED=True
YOUTUBE_MERGE_PROVIDER_ENABLED=True
YOUTUBE_MERGE_PUBLIC_ACCESS_ENABLED=True
YOUTUBE_MERGE_STAFF_ONLY=True
YOUTUBE_MERGE_FFMPEG_BINARY=/path/to/ffmpeg
```

Staff-only must remain enforced in the view layer. The adapter may trust `prefer_youtube_merge=True` as an already-authorized request, but it still must require the provider to be implemented, ready, and public-access-enabled.

The generic third-party entrypoint itself also has a separate `THIRD_PARTY_STAFF_ONLY` switch. Keep it enabled for yt-dlp/gallery-dl long-tail validation so setting `THIRD_PARTY_PUBLIC_ACCESS_ENABLED=True` does not immediately expose every generic platform to anonymous visitors.

Use `inspect_third_party_provider --health --json` as the release gate for the generic entrypoint:

- `rollout.status=disabled`: the entrypoint is intentionally closed.
- `rollout.status=hold`: at least one required provider or configuration check is failing.
- `rollout.status=staff_only`: ready enough for staff/local gray release, but not anonymous public use.
- `rollout.status=public_candidate`: technically ready for public entrypoint exposure, but still requires product approval and platform-specific smoke evidence before enabling broad traffic.

## 3. Failure Logs And Timing

Every provider attempt should leave enough evidence to answer:

- Which provider was attempted?
- Which platform rule was active?
- How long did it take?
- Was the result selected, skipped, timed out, or failed?
- What was the fallback path?
- Did the request exceed the frontend budget?

Current useful fields:

- `media_parse_completed`
- `media_parse_failed`
- `ParseResult.metadata.selected_provider`
- `ParseResult.metadata.selected_provider_duration_ms`
- `ParseResult.metadata.provider_total_duration_ms`
- `ParseResult.metadata.provider_attempts`
- `ParseResult.metadata.provider_fallback_count`
- `third_party_provider_selected`
- `third_party_provider_failed`
- `third_party_provider_all_failed`
- `third_party_provider_budget_exhausted`

The page-level `media_parse_*` events intentionally log only the submitted URL host, not the full URL. They include requested/allowed flags for ultra-HD and YouTube merge, selected provider, platform, fallback count, image/video counts, and duration. Use those events as the first-line gray-release telemetry, then drill into provider-level logs when a provider fails or times out.

Next diagnostic improvement after staff smoke: persist selected provider, duration, platform, and failure reason to a lightweight model or structured log sink so P50/P95 can be calculated from real usage instead of manual CLI timings.

## 4. Async Task Queue Decision

Keep synchronous parsing for now. Move heavy work to a durable queue before public rollout when any of these become true:

- YouTube merge downloads are exposed beyond staff.
- Gallery or album downloads become common.
- ZIP tasks exceed local single-worker assumptions.
- Progress must survive worker restart or multiple Gunicorn workers.
- Provider sidecars need shared concurrency across processes.

Short-term acceptable:

- In-process tasks for local PyCharm and single-worker development.
- Staff-only smoke with bounded timeouts and small concurrency.

Production target:

- Redis-backed task state.
- Celery or RQ workers.
- Durable temporary file/object-storage output.
- Pollable progress and final one-time download URL.
- Clear retry and cleanup policy.

## 5. Long-Tail Platform Opening

Do not open all supported platform rules at once. Add staff-only smoke cohorts:

1. YouTube: `yt_dlp_merge` normal 1080p and `youtube_merge` highest-quality task.
2. Imgur: `gallery_dl_generic` image/gallery smoke.
3. Flickr: `gallery_dl_generic` image smoke, with higher timeout or queue path.
4. X/Twitter or Instagram: only after media host allowlists and download behavior are verified.

Each platform needs evidence for:

- Health-ready provider.
- Parse duration under the chosen budget or routed through async.
- Preview image loads through the signed proxy.
- Download succeeds with content-type and final-host validation.
- Failed download invalidates stale provider cache when applicable.
- Staff-only UI copy does not overpromise quality or speed.

For each long-tail candidate, run `inspect_third_party_provider <url> --provider <provider> --json` and record `smoke.recommended_rollout`. Successful and provider-attempt failure JSON both include this field. Treat `staff_only` as permission to try a small staff cohort only; treat `hold` as a blocker until the listed `smoke.issues` are fixed or the platform is moved to an async/sidecar path.

## Recommendation

Proceed in this order:

1. Run and record YouTube merge smoke with the enhanced `inspect_youtube_merge` output.
2. Enable YouTube merge only for staff in local/staging.
3. Collect selected-provider and failure timing from real staff use.
4. Decide whether YouTube merge download should remain synchronous or move first to the durable queue.
5. Only then add a small gallery-dl staff cohort for Imgur/Flickr.
