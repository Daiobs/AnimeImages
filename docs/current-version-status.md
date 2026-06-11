# Current Version Status

Date: 2026-06-11

Target: merge `codex/douyin-ultra-fast-path` into `main`

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
- Provider chain begins with TikHub, while the native fast path is used only as fallback.
- TikHub candidates with valid declared URL, size, and media type are accepted directly; incomplete metadata uses one Range probe.
- A valid TikHub result is authoritative in ultra mode and is not replaced by native 1080p.
- Provider failures and selection decisions are logged with structured server-side fields.

The previous implementation could exceed 10 seconds in PyCharm/manual browser tests. The current branch replaces that synchronous chain with a shared deadline and validated-result cache; representative browser measurements are still required before treating the latency target as verified.

## Cost Controls

Current controls:

- `TIKHUB_VALIDATED_CACHE_TTL_SECONDS`: caches the already validated TikHub URL and size, avoiding repeated API and CDN probes.
- `TIKHUB_DAILY_LIMIT`: shared daily call cap when production uses Redis.
- `ULTRA_HD_PUBLIC_ACCESS_ENABLED`: operator switch controlling visitor access to paid ultra-HD.
- Settings-modal switch: normal parsing does not call TikHub.
- `.env` is loaded locally but ignored by Git.

Limitations:

- Local development uses process-local `LocMemCache`; production requires shared Redis.
- Redis availability, memory use, eviction rate, and persistence still need deployment monitoring.
- There is no per-request visible cost indicator yet.
- Retries count as separate outbound attempts so the daily cap remains conservative.

## Known Issues

1. Douyin ultra-HD still depends on external latency and source quality.

   The synchronous path now has a shared enhancement budget, short TikHub timeouts, one Range-first probe, and validated-result caching. Cold TikHub requests still commonly take 4-7 seconds and can exceed 7 seconds. The long-term plan is documented in `docs/ultra-hd-provider-plan.md`: persistent timing metrics, connection reuse, Redis single-flight, stale-while-refresh, adaptive regional routing, and background ultra-HD upgrades.

2. TikHub quality and availability are probabilistic.

   The provider can return temporary errors, network timeouts, or unusable HTML/tiny-file payloads. The current path retries once, and mainland retries keep the `.dev` API domain while switching from `region=CN` to `region=HK`. If both attempts fail, the request falls back to native.

3. File size is used as the main quality proxy.

   This works reasonably for same-video MP4 variants, but it is not a complete quality metric. A later pass should add optional `ffprobe` or metadata-based width/height/bitrate verification.

4. Provider diagnostics are not visible in the UI.

   The project has diagnostics services, structured provider logs, and a management command, but normal page parsing does not yet show provider timing, selected source, fallback reason, or cost/cached status.

5. A cold TikHub result can arrive before native cover metadata.

   The result returns the ultra-HD source immediately to avoid adding more latency. If native metadata has not completed, the first render can use the video placeholder instead of a cover image. A future background upgrade task should return metadata and ultra source independently.

6. Shared Redis is now a production dependency.

   TikHub cost counters and download abuse controls use Django cache. Local development still uses LocMemCache, while production configuration now requires `DJANGO_CACHE_URL`.

7. Xiaohongshu ultra-HD video research is not started.

   Xiaohongshu baseline works, especially for images, but larger video variants seen on competitor sites still need a provider research phase.

8. Bilibili remains baseline-only.

   Higher-quality Bilibili streams usually need cookies, DASH audio/video merge, `ffmpeg`, and sometimes membership entitlement, so this is intentionally out of short-term scope.

9. The current UI does not expose manual quality selection.

   The result area still returns one selected video asset. Competitor-style multi-quality lists may be useful later, especially while provider quality is inconsistent.

10. Download task state is in-memory.

   Browser ZIP progress remains appropriate for local/single-process use. Production disables it by default and falls back to synchronous ZIP responses. Restoring production progress still requires Redis/Celery/RQ or another shared execution/state mechanism.

11. Health checks do not replace monitoring.

   `/health/ready/` detects immediate cache/database availability, but production still needs uptime checks, error aggregation, Redis memory/eviction alerts, database capacity monitoring, and disk/bandwidth metrics.

12. `.env` convenience loader is development-oriented.

    It improves PyCharm setup, but production deployments should use real environment variables or secret storage rather than local files.

13. Download planning is duplicated.

    ZIP archive and server-folder save paths still separately handle image/video/cover output planning. Refactor to a shared `DownloadOutput` pipeline before adding single-file download, object storage, or conversion/transcoding features.

14. DNS rebinding defense is not fully production-grade.

    Current SSRF checks validate host suffixes and literal private IPs plus redirect final hosts. This is acceptable for fixed major-platform CDN hosts, but future user-configurable/provider-configurable domains should add DNS resolution checks and reject private resolved addresses.

## Recommended Next Step

Keep the current production async-task switch disabled and implement the shared download foundation:

- extract a common `DownloadOutput` planning pipeline
- add Redis-backed task state
- execute archive work through Celery/RQ
- store completed ZIP files in a controlled temporary/object-storage location
- issue expiring one-time download URLs
- add retry, cancellation, queue-capacity, and cleanup policies

Provider timing optimization is now a tracked long-term stream. The first performance milestone should be Redis single-flight plus persistent provider timing metrics, because those changes reduce paid duplication and reveal whether remaining delay is API generation, network setup, or CDN validation.
