# Current Version Status

Date: 2026-06-26

## Summary

- The universal input still routes Xiaohongshu, Douyin, and Bilibili through platform adapters.
- Normal Douyin parsing remains native-only and does not call any external provider.
- When the settings-modal ultra-HD switch is enabled, KuKuTool is the only external upstream.
- Generic third-party parsing can optionally use disabled-by-default yt-dlp and gallery-dl providers; KuKuTool generic can be disabled independently for staged rollout.
- YouTube server-side merge can be enabled as a controlled yt-dlp + ffmpeg task path; it remains disabled by default and should be gray-released before public traffic.
- TikHub and `native_enhanced` remain in the codebase for research and diagnostics, but are ignored by the synchronous ultra-HD request path.
- The request waits at most three seconds for KuKuTool while native runs concurrently.
- A slow KuKuTool task continues in a bounded background pool and fills Redis cache after native fallback.

## KuKuTool Path

1. Extract `aweme_id` from the submitted Douyin URL or controlled short-link redirect.
2. Start native and KuKuTool in separate bounded executors.
3. Join or create a Redis single-flight task for the `aweme_id`.
4. Read `videos[].video_fullinfo`.
5. Allow only ByteDance CDN URLs on `365yg.com` or `douyinvod.com`.
6. Deduplicate rotating signed URLs by stable TOS object path.
7. Rank by bitrate, declared size, quality score, and quality label.
8. Accept complete, unexpired structured metadata directly; otherwise use a bounded Range probe.
9. Cache the validated signed result with a TTL capped by its signature expiry.

The selected KuKuTool result is authoritative. It is not compared with native file size, which prevents a valid ultra candidate from being replaced by 1080p.

## Measured Result

Local CLI verification on June 12, 2026 used aweme `7647533902413173321`:

- Cold request: 1.915 seconds
- Selected source: KuKuTool `超高清`
- CDN host: `v26-default.365yg.com`
- Declared size: 67.22MB
- Hot validated-cache request: 0.505 seconds

Before the metadata fast path, the same candidate could narrowly exceed the two-second Range budget and incorrectly fall through to a 3.82MB 1080p candidate. Complete KuKuTool metadata now avoids that probe and downgrade.

After the bounded-executor, profile-cache, and same-origin redirect changes on June 13, 2026, a direct helper smoke test for the same aweme completed in 1.422 seconds and still returned four candidates with the 70,481,987-byte `超高清` source ranked first.

## Configuration

```text
ULTRA_HD_PROVIDER_ENABLED=False
ULTRA_HD_PUBLIC_ACCESS_ENABLED=False
KUKUTOOL_PROVIDER_ENABLED=False
KUKUTOOL_NODE_BINARY=/path/to/node
KUKUTOOL_SIDECAR_BASE_URL=
KUKUTOOL_SIDECAR_TIMEOUT_SECONDS=3.0
KUKUTOOL_SIDECAR_HEALTH_TIMEOUT_SECONDS=0.5
TIKHUB_API_KEY=
TIKHUB_BASE_URL=https://api.tikhub.io
TIKHUB_CHINA_BASE_URL=https://api.tikhub.dev
TIKHUB_REGION=CN
TIKHUB_DEFAULT_COUNTRY_CODE=CN
TIKHUB_TRUST_COUNTRY_HEADER=False
TIKHUB_COUNTRY_HEADER=HTTP_CF_IPCOUNTRY
TIKHUB_GEOIP_DATABASE_PATH=
TIKHUB_DAILY_LIMIT=0
TIKHUB_CONNECT_TIMEOUT_SECONDS=1.0
TIKHUB_READ_TIMEOUT_SECONDS=6.5
TIKHUB_MAX_ATTEMPTS=2
TIKHUB_PROBE_TIMEOUT_SECONDS=1.8
TIKHUB_MAX_PROBE_CANDIDATES=1
TIKHUB_VALIDATED_CACHE_TTL_SECONDS=600
KUKUTOOL_TIMEOUT_SECONDS=12.0
KUKUTOOL_PROBE_TIMEOUT_SECONDS=2.0
KUKUTOOL_MAX_PROBE_CANDIDATES=2
KUKUTOOL_MAX_CONCURRENCY=2
KUKUTOOL_EXECUTOR_WORKERS=2
KUKUTOOL_MAX_PENDING_TASKS=4
KUKUTOOL_GLOBAL_MAX_CONCURRENCY=4
KUKUTOOL_SINGLE_FLIGHT_TTL_SECONDS=30
KUKUTOOL_FAILURE_CACHE_TTL_SECONDS=15
KUKUTOOL_CACHE_POLL_INTERVAL_SECONDS=0.1
KUKUTOOL_GLOBAL_REQUEST_LIMIT_PER_MINUTE=30
KUKUTOOL_CIRCUIT_FAILURE_THRESHOLD=5
KUKUTOOL_CIRCUIT_WINDOW_SECONDS=60
KUKUTOOL_CIRCUIT_OPEN_SECONDS=120
KUKUTOOL_PROFILE_CACHE_TTL_SECONDS=86400
KUKUTOOL_VALIDATED_CACHE_TTL_SECONDS=300
KUKUTOOL_REFERENCE_WAIT_SECONDS=0.5
DOUYIN_METADATA_CACHE_TTL_SECONDS=1800
PROVIDER_MANIFEST_MAX_AGE_SECONDS=3600
DOUYIN_ULTRA_TOTAL_TIMEOUT_SECONDS=3.0
DOUYIN_ULTRA_NATIVE_TIMEOUT_SECONDS=3.0
DOUYIN_NATIVE_EXECUTOR_WORKERS=4
DOUYIN_NATIVE_MAX_PENDING_TASKS=8
THIRD_PARTY_PROVIDER_ENABLED=False
THIRD_PARTY_PUBLIC_ACCESS_ENABLED=False
THIRD_PARTY_STAFF_ONLY=True
THIRD_PARTY_RATE_LIMIT_PER_MINUTE=2
THIRD_PARTY_GLOBAL_REQUEST_LIMIT_PER_MINUTE=30
THIRD_PARTY_FRONTEND_TIMEOUT_SECONDS=3.0
YOUTUBE_FRONTEND_TIMEOUT_SECONDS=8.0
THIRD_PARTY_EXTERNAL_OUTPUT_MAX_BYTES=8388608
THIRD_PARTY_KUKUTOOL_PROVIDER_ENABLED=False
THIRD_PARTY_KUKUTOOL_TIMEOUT_SECONDS=12.0
THIRD_PARTY_KUKUTOOL_RETRIES=2
THIRD_PARTY_KUKUTOOL_EXECUTOR_WORKERS=2
THIRD_PARTY_KUKUTOOL_MAX_PENDING_TASKS=4
THIRD_PARTY_KUKUTOOL_GLOBAL_MAX_CONCURRENCY=4
THIRD_PARTY_KUKUTOOL_SINGLE_FLIGHT_TTL_SECONDS=30
THIRD_PARTY_KUKUTOOL_FAILURE_CACHE_TTL_SECONDS=15
THIRD_PARTY_KUKUTOOL_CACHE_POLL_INTERVAL_SECONDS=0.1
THIRD_PARTY_KUKUTOOL_CIRCUIT_FAILURE_THRESHOLD=5
THIRD_PARTY_KUKUTOOL_CIRCUIT_WINDOW_SECONDS=60
THIRD_PARTY_KUKUTOOL_CIRCUIT_OPEN_SECONDS=120
THIRD_PARTY_KUKUTOOL_PROFILE_CACHE_TTL_SECONDS=86400
THIRD_PARTY_KUKUTOOL_PAYLOAD_CACHE_TTL_SECONDS=120
YTDLP_PROVIDER_ENABLED=False
YTDLP_BINARY=/path/to/yt-dlp
YTDLP_TIMEOUT_SECONDS=15.0
YTDLP_EXECUTOR_WORKERS=1
YTDLP_MAX_PENDING_TASKS=2
YTDLP_PAYLOAD_CACHE_TTL_SECONDS=300
YTDLP_FAILURE_CACHE_TTL_SECONDS=30
YTDLP_MERGE_PROVIDER_ENABLED=True
YTDLP_MERGE_DEFAULT_MAX_HEIGHT=1080
YTDLP_MERGE_HIGH_QUALITY_MAX_HEIGHT=4320
YTDLP_MERGE_MAX_FILE_SIZE_BYTES=524288000
YTDLP_MERGE_TIMEOUT_SECONDS=300.0
YTDLP_MERGE_FFMPEG_BINARY=/path/to/ffmpeg
YOUTUBE_MERGE_PROVIDER_ENABLED=False
YOUTUBE_MERGE_PUBLIC_ACCESS_ENABLED=False
YOUTUBE_MERGE_STAFF_ONLY=True
YOUTUBE_MERGE_MAX_HEIGHT=4320
YOUTUBE_MERGE_MAX_FILE_SIZE_BYTES=524288000
YOUTUBE_MERGE_TIMEOUT_SECONDS=300.0
YOUTUBE_MERGE_EXECUTOR_WORKERS=1
YOUTUBE_MERGE_MAX_PENDING_TASKS=1
YOUTUBE_MERGE_REQUIRE_FFMPEG=True
YOUTUBE_MERGE_FFMPEG_BINARY=/path/to/ffmpeg
GALLERYDL_PROVIDER_ENABLED=False
GALLERYDL_BINARY=/path/to/gallery-dl
GALLERYDL_TIMEOUT_SECONDS=15.0
GALLERYDL_EXECUTOR_WORKERS=1
GALLERYDL_MAX_PENDING_TASKS=2
GALLERYDL_MAX_ITEMS=30
GALLERYDL_PAYLOAD_CACHE_TTL_SECONDS=300
GALLERYDL_FAILURE_CACHE_TTL_SECONDS=30
```

KuKuTool can run through the in-process Node helper or an internal `douyin-source-resolver` sidecar. Direct helper mode requires Node.js 18 or newer. Production defaults KuKuTool to disabled and requires either an explicit `KUKUTOOL_NODE_BINARY` or `KUKUTOOL_SIDECAR_BASE_URL` before staging enables it. Runtime discovery happens at process startup, and sidecar health is cached through a short `/health` probe. Sidecar-to-helper fallback is budget-aware: after a sidecar failure, the helper can only use the remaining request budget. Production should use Redis-backed Django cache so validated results, metadata, single-flight, limits, the circuit breaker, and the administrator kill switch are shared across workers.

Production Redis cache configuration now has an explicit dependency check. If `DJANGO_CACHE_URL` points at `redis://` or `rediss://` but the Python `redis` package is missing, `manage.py check` reports `xhs_download.E008` before deployment. Actual Redis connectivity is still checked by `/health/ready/`, not by `manage.py check`.

The generic third-party provider layer is independent from Douyin ultra-HD. `THIRD_PARTY_KUKUTOOL_PROVIDER_ENABLED` controls only the generic KuKuTool path and is separate from the Douyin ultra-HD `KUKUTOOL_PROVIDER_ENABLED` switch, so the app can gray-release `yt-dlp` or `gallery-dl` without invoking the KuKuTool Node helper. KuKuTool generic is disabled by default in both local and production settings and must be explicitly enabled. The yt-dlp fallback is intended to broaden generic video platform coverage, not to replace the self-owned 365yg/TOS research path. When `YTDLP_MERGE_PROVIDER_ENABLED=True`, yt-dlp video results default to the best mergeable video+audio candidate up to `YTDLP_MERGE_DEFAULT_MAX_HEIGHT`, currently 1080p, instead of stopping at a lower complete single-file URL; if no better merge candidate exists, the normal signed direct-file result is used. Higher targets such as 2160p/4320p require the YouTube highest-quality request path or a future platform-specific high-quality switch. The gallery-dl fallback is the matching image/gallery breadth layer for allowlisted public media platforms; static images stay image assets, while animated `.mp4/.webm` resources are surfaced as video assets. Gallery-dl low-quality field filtering is scoped per gallery item: an original candidate suppresses only that item's thumbnail/preview duplicate, while thumbnail-only items elsewhere in the same gallery remain downloadable. Keep all generic third-party providers disabled in production until executable paths, platform allowlists, rate limits, and worker isolation have been verified.

Provider order is platform-specific. YouTube uses `yt_dlp_generic` before KuKuTool generic, video-oriented generic platforms prefer KuKuTool then yt-dlp, and image/gallery platforms prefer KuKuTool then gallery-dl. This keeps the long-tail layer extensible without forcing every platform through the same global order.

Generic provider attempts now include structured timing diagnostics in logs and `ParseResult.metadata`. `selected_provider_duration_ms` records the provider that produced the selected result, while `provider_total_duration_ms` records the whole fallback chain including failed attempts. This is the minimum observability needed before adding more long-tail platforms because a user-visible “没有解析到” can now be traced to a specific provider, duration, error type, and fallback path.

The optional `yt-dlp` and `gallery-dl` providers now have preflight binary checks. Health output reports `executable_found`, `resolved_executable`, `version_probe_ok`, and `ready`; enabled providers run a short cached `--version` probe in health and Django system checks. Checks fail when an enabled provider points to a missing executable, fails the probe, or when production enables it without an explicit binary command. The yt-dlp runner uses `--ignore-config`, and the external command environment sets `YTDLP_NO_PLUGINS=1` plus `PYTHONNOUSERSITE=1`; each command also runs from a temporary cwd with temporary `HOME`, so user-level yt-dlp configuration, plugins, caches, and user-site Python packages cannot silently change provider behavior.

YouTube merge mode is implemented as a controlled server-side task path. It is intended for the common case where YouTube 1080p+ requires separate video-only and audio-only streams to be downloaded and merged with ffmpeg; merging is not synonymous with 4K/8K. `YOUTUBE_MERGE_PROVIDER_ENABLED` enables runtime diagnostics, `YOUTUBE_MERGE_PUBLIC_ACCESS_ENABLED` allows the page setting to become available, and `YOUTUBE_MERGE_STAFF_ONLY=True` limits the setting to staff users instead of anonymous visitors. Production checks require explicit `YTDLP_BINARY` and `YOUTUBE_MERGE_FFMPEG_BINARY` values. YouTube web parsing uses `YOUTUBE_FRONTEND_TIMEOUT_SECONDS`, default 8 seconds, because cold yt-dlp extraction often exceeds the generic three-second third-party budget. The `inspect_youtube_merge` management command can run yt-dlp JSON inspection and build a merge plan: it reports the best complete single-file format, the best video-only stream up to `YOUTUBE_MERGE_MAX_HEIGHT`, the best audio-only stream, the estimated merged size, and the format selector. When `--download-smoke` is used, the command also reports whether the real merge/download duration fits the frontend budget or should be routed through a durable queue. Web parsing returns a signed `youtube_merge` provider resource; when that resource is downloaded, the server runs yt-dlp with ffmpeg and writes the merged mp4 into the existing download archive/task pipeline.

KuKuTool generic and the optional external command providers now use best-effort short-lived payload and failure caches. Repeated requests for the same source URL can reuse parsed JSON, while repeated failures are quickly short-circuited for a small window to protect local worker pools. Cache read/write failures are logged and the provider still attempts a real parse. If a signed KuKuTool generic, `yt-dlp`, or `gallery-dl` direct URL later fails during preview or download with a recoverable HTTP/type error, the matching payload and failure caches are deleted so the next parse can refresh provider output instead of being blocked by stale state. External command subprocess timeout is capped by the platform frontend budget when that budget is lower than the provider-specific timeout: most third-party platforms use `THIRD_PARTY_FRONTEND_TIMEOUT_SECONDS`, while YouTube uses `YOUTUBE_FRONTEND_TIMEOUT_SECONDS`. stdout/stderr is read back from temporary files only after enforcing `THIRD_PARTY_EXTERNAL_OUTPUT_MAX_BYTES`. Those subprocesses also run in isolated temporary cwd/`HOME` directories. The generic adapter stops serial fallback once that shared frontend budget is exhausted, so gray-release requests do not block the page for multiple full external tool timeouts or buffer unbounded extractor output in memory.

These tools are intentionally not part of the base `requirements.txt`. Local tests can install them into `.venv` and point `YTDLP_BINARY` / `GALLERYDL_BINARY` at `".venv/bin/python -m yt_dlp"` and `".venv/bin/python -m gallery_dl"`, while production should use pinned system or container binary commands. Because the process `HOME` is temporary, provider behavior must not depend on user-home config files or cache state.

Do not vendor upstream yt-dlp, gallery-dl, or MeTube source code into this repository. The current integration boundary is an external command plus signed provider manifest. Before distributing a deployment image, pin exact tool versions and review the active upstream licenses and release channels; as of this review, yt-dlp source is Unlicense while packaged builds can include extra third-party notices, gallery-dl is GPL-2.0 and its GitHub project points active development toward Codeberg, and MeTube is AGPL-3.0. MeTube remains a product/queue reference only.

Before opening the generic third-party web entrypoint, run `.venv/bin/python manage.py inspect_third_party_provider --health --json`. The health mode does not require a URL and reports provider readiness, platform coverage, configuration issues, and a shared `rollout` status: `disabled`, `hold`, `staff_only`, or `public_candidate`. The default posture is staff-only; broad public access requires explicitly setting `THIRD_PARTY_STAFF_ONLY=False` and still needs platform-specific smoke evidence. KuKuTool generic cache reads are best effort in this diagnostic path: Redis failures are surfaced as `cache_error` in the JSON payload instead of printing tracebacks before the JSON body. Normal parse diagnostics redact provider media URLs and URLs embedded in error text by default so copied JSON does not leak short-lived CDN links; use `--show-urls` only for local manual verification. Parse diagnostics also include a `smoke` summary on provider success and provider-attempt failure, so staff smoke automation can aggregate `smoke.recommended_rollout` and `smoke.issues` without separate success/failure JSON handling. It is the quickest preflight check for local PyCharm validation and staged production rollout.

For the dedicated Douyin ultra-HD path, run `.venv/bin/python manage.py inspect_douyin_candidates --health --json`. This command reports the KuKuTool runtime, optional `douyin-source-resolver` sidecar health, circuit state, and whether `KUKUTOOL_NODE_BINARY` or `KUKUTOOL_SIDECAR_BASE_URL` is configured.

Local smoke results on June 26, 2026:

- `yt-dlp` 2026.03.17 successfully parsed `https://www.youtube.com/watch?v=sxVqcCslnAE` through `inspect_third_party_provider` with `.venv/bin/python -m yt_dlp` and `--timeout 15`. With `YTDLP_MERGE_PROVIDER_ENABLED=True`, the normal diagnostic path selects `yt_dlp_merge` up to the default 1080p target, even when the payload contains higher 4320p candidates. The YouTube highest-quality UI switch is the path that raises the target to `YOUTUBE_MERGE_MAX_HEIGHT` for 2160p/4320p when enabled and authorized.
- `gallery-dl` 1.32.4 successfully parsed `https://imgur.com/a/4ai9c` through `inspect_third_party_provider` with `.venv/bin/python -m gallery_dl` and `--timeout 10` in about 1.9 seconds, returning one signed `i.imgur.com` direct image asset. A real payload check caught an album metadata pitfall where nested `album.url` could inherit the parent image extension and be misclassified as media; the normalizer now determines a nested URL's media type from its own node plus URL suffix, so page URLs such as `https://imgur.com/a/...` are filtered out.
- `gallery-dl` 1.32.4 successfully parsed `https://www.flickr.com/photos/nasa2explore/53566933692` through `inspect_third_party_provider` with `.venv/bin/python -m gallery_dl` and `--timeout 8` in about 6.5 seconds, returning one allowlisted `live.staticflickr.com` direct image asset. This confirms that gallery-dl image/gallery rollout may need a higher `THIRD_PARTY_FRONTEND_TIMEOUT_SECONDS` than the default 3 seconds, or should be routed through a sidecar/job queue before broad public exposure.
- With `THIRD_PARTY_PROVIDER_ENABLED=True`, `THIRD_PARTY_PUBLIC_ACCESS_ENABLED=True`, `YTDLP_PROVIDER_ENABLED=True`, `YTDLP_BINARY=".venv/bin/python -m yt_dlp"`, `GALLERYDL_PROVIDER_ENABLED=True`, and `GALLERYDL_BINARY=".venv/bin/python -m gallery_dl"`, `manage.py check` passes and `inspect_third_party_provider --health --json` reports both external command providers as enabled and ready. The health report separates rule coverage from runtime state: `provider_ids` shows the platform allowlist and fallback order, while `enabled_provider_ids` / `ready_provider_ids` show the currently active subset. In that configuration YouTube has `yt_dlp_generic` ready, and image/gallery platforms such as Imgur, Reddit, Tumblr, Flickr, and DeviantArt have `gallery_dl_generic` ready.
- The same YouTube and Flickr links were also smoke-tested through the `/results/` AJAX path with third-party public parsing enabled locally; the gallery-dl path used a raised generic timeout. The rendered result regions included the expected `yt_dlp_generic` / `gallery_dl_generic` data attributes, provider notices, and signed provider manifest tokens.
- A first Flickr smoke attempt with `https://www.flickr.com/photos/nasacommons/9457855730/` returned `NotFoundError` from gallery-dl, so it should not be used as this project's smoke fixture.

## Known Issues

1. KuKuTool uses an undocumented third-party protocol. Upstream profile or encryption changes can break cold parsing.
2. Background tasks remain process-local threads. A worker restart loses queued work, so production should move the helper to a Node sidecar or Celery/RQ.
3. Redis single-flight and global concurrency protect normal operation, but they are coordination controls rather than a durable queue.
4. Structured KuKuTool metadata is trusted when host, TOS path, size, and signature expiry are valid. Download validates the final host and content type; on 403/404/HTML/type mismatch it invalidates the exact cache entry, refreshes KuKuTool once, then falls back to native.
5. A cold KuKuTool result may arrive before native cover metadata. Native metadata is cached separately; once both caches are warm, requests skip the fixed reference wait.
6. Xiaohongshu ultra-HD research is not part of this iteration.
7. Bilibili remains baseline-only because higher qualities commonly require cookies, DASH merge, and entitlement.
8. Production ZIP progress still requires a shared task queue; local in-memory tasks are not multi-worker safe.

## Next Priorities

1. Prefer the long-running `douyin-source-resolver` sidecar for staging and production, keeping direct Node startup as a local/development fallback.
2. Design the production download queue along MeTube-like lines: bounded concurrency, durable task state, progress events, and isolated worker processes, but keep it inside this project's own Django/Celery or sidecar architecture.
3. Persist provider stage timings and calculate P50/P95 from real use.
4. Move the interim staff-only provider health/control JSON endpoints into a small administrator UI with alert history.
5. Continue the self-owned 365yg/TOS provider research so KuKuTool can eventually be replaced.
