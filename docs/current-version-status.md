# Current Version Status

Date: 2026-06-07

Branch: `codex/ultra-hd-providers`

## Summary

This version establishes the provider foundation for a universal media downloader:

- One input can route Xiaohongshu, Douyin, and Bilibili links through platform adapters.
- Xiaohongshu, Douyin, and Bilibili baseline parsing remain no-login and native-first.
- Douyin now has an optional ultra-HD provider path backed by TikHub.
- TikHub is disabled by default, hidden behind settings, and protected by cache and daily-limit controls.
- Download output defaults to original files, with ZIP progress for browser downloads.
- Production readiness now checks both shared cache/Redis and the database.
- The preview image proxy has independent request and byte limits.

The version is suitable as a research checkpoint, but not yet as a final ultra-HD solution.

## Douyin Modes

### Default Mode

Default mode is the fast path:

- Provider chain: `native`
- TikHub is not called.
- `native_enhanced` is not called.
- `modal_id` / `aweme_id` links skip the full Douyin desktop page fetch and go directly to the native mobile route.

Local CLI checks on 2026-06-07 showed representative `modal_id` links resolving in about 2-3 seconds when `prefer_ultra_hd=False`.

### Ultra-HD Mode

Ultra-HD mode is optional and must be enabled in the settings modal:

- Requires `ULTRA_HD_PROVIDER_ENABLED=True`.
- Requires `TIKHUB_API_KEY`.
- Requires the user/operator to enable "抖音超高清" in settings.
- Provider chain begins with TikHub, while the native fast path is used as fallback/comparison.
- TikHub candidates are probed before selection.
- A smaller TikHub result should not replace a larger native result.
- Provider failures and selection decisions are logged with structured server-side fields.

Current observed problem: ultra-HD parsing can still take more than 10 seconds in PyCharm/manual browser tests. The latest code avoids the heaviest serial path, but real-world latency remains unresolved.

## Cost Controls

Current controls:

- `TIKHUB_CACHE_TTL_SECONDS`: Django-cache-backed cache for identical TikHub lookups.
- `TIKHUB_DAILY_LIMIT`: shared daily call cap when production uses Redis.
- `ULTRA_HD_PUBLIC_ACCESS_ENABLED`: operator switch controlling visitor access to paid ultra-HD.
- Settings-modal switch: normal parsing does not call TikHub.
- `.env` is loaded locally but ignored by Git.

Limitations:

- Local development uses process-local `LocMemCache`; production requires shared Redis.
- Redis availability, memory use, eviction rate, and persistence still need deployment monitoring.
- There is no per-request visible cost indicator yet.

## Known Issues

1. Douyin ultra-HD latency remains high.

   Manual browser testing still reports 10+ seconds for some ultra-HD requests. The code now avoids default-mode slowdown, but the ultra path needs provider timing logs to identify whether time is spent in TikHub API, TikHub URL probing, native fallback, or CDN redirect/probe latency.

2. TikHub quality is probabilistic.

   TikHub sometimes returns a smaller 720p-like source while native parsing can return a larger 1080p source. The current selector compares probed size and should keep the larger native result, but resolution/codec verification is not yet available.

3. File size is used as the main quality proxy.

   This works reasonably for same-video MP4 variants, but it is not a complete quality metric. A later pass should add optional `ffprobe` or metadata-based width/height/bitrate verification.

4. Provider diagnostics are not visible in the UI.

   The project has diagnostics services, structured provider logs, and a management command, but normal page parsing does not yet show provider timing, selected source, fallback reason, or cost/cached status.

5. Shared Redis is now a production dependency.

   TikHub cost counters and download abuse controls use Django cache. Local development still uses LocMemCache, while production configuration now requires `DJANGO_CACHE_URL`.

6. Xiaohongshu ultra-HD video research is not started.

   Xiaohongshu baseline works, especially for images, but larger video variants seen on competitor sites still need a provider research phase.

7. Bilibili remains baseline-only.

   Higher-quality Bilibili streams usually need cookies, DASH audio/video merge, `ffmpeg`, and sometimes membership entitlement, so this is intentionally out of short-term scope.

8. The current UI does not expose manual quality selection.

   The result area still returns one selected video asset. Competitor-style multi-quality lists may be useful later, especially while provider quality is inconsistent.

9. Download task state is in-memory.

   Browser ZIP progress remains appropriate for local/single-process use. Production disables it by default and falls back to synchronous ZIP responses. Restoring production progress still requires Redis/Celery/RQ or another shared execution/state mechanism.

10. Health checks do not replace monitoring.

   `/health/ready/` detects immediate cache/database availability, but production still needs uptime checks, error aggregation, Redis memory/eviction alerts, database capacity monitoring, and disk/bandwidth metrics.

11. `.env` convenience loader is development-oriented.

    It improves PyCharm setup, but production deployments should use real environment variables or secret storage rather than local files.

12. Download planning is duplicated.

    ZIP archive and server-folder save paths still separately handle image/video/cover output planning. Refactor to a shared `DownloadOutput` pipeline before adding single-file download, object storage, or conversion/transcoding features.

13. DNS rebinding defense is not fully production-grade.

    Current SSRF checks validate host suffixes and literal private IPs plus redirect final hosts. This is acceptable for fixed major-platform CDN hosts, but future user-configurable/provider-configurable domains should add DNS resolution checks and reject private resolved addresses.

## Recommended Next Step

Keep the current production async-task switch disabled and implement the shared download foundation:

- extract a common `DownloadOutput` planning pipeline
- add Redis-backed task state
- execute archive work through Celery/RQ
- store completed ZIP files in a controlled temporary/object-storage location
- issue expiring one-time download URLs
- add retry, cancellation, queue-capacity, and cleanup policies

Provider timing optimization can continue in parallel, but it should not delay replacing the local daemon-thread task model before production progress downloads are enabled.
