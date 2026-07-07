# Ultra-HD Provider Plan

Status: historical/research plan. The current production-oriented Douyin ultra
path is Provider Strategy v1: TianTianJieXi preferred, KuKuTool fallback/race
backup, native fallback. Use `docs/codex-handoff.md` and
`docs/current-version-status.md` as the current source of truth.

## Goal

Build a stable provider-based path for higher-quality media downloads without weakening the current no-login baseline parser.

Priority:

1. Douyin ultra-HD research and provider integration.
2. Xiaohongshu higher-quality video/image provider research.
3. Bilibili ultra-HD is explicitly out of scope for the short term because reliable high-quality streams usually require user cookies, DASH audio/video merge, and sometimes paid membership rights.

## North Star

The product should keep one universal link input. After the registry identifies the platform, the platform adapter should ask multiple providers for media candidates, validate them, then return the best stable `MediaAsset` objects to the existing preview and download pipeline.

The user should not need to understand providers, signatures, cookies, or stream variants. The default behavior is "best available stable source"; advanced source details can stay behind logs or later diagnostics UI.

## Provider Layers

### Native Provider

Purpose: keep the current stable no-login behavior.

- Douyin: desktop page, embedded JSON, mobile feed, mobile share page.
- Xiaohongshu: current page HTML, normalized original image URLs, page video key extraction.

This provider must remain available even when external services fail.

### Current KuKuTool Provider

Purpose: provide a temporary high-quality upstream while the self-owned 365yg/TOS source route is researched.

Current rules:

- Disabled globally by default.
- Only receives the extracted `aweme_id`, not arbitrary user input.
- Is the only external upstream in the synchronous ultra-HD path.
- Returns the same internal `MediaCandidate` and `MediaAsset` shapes as native providers.
- Falls back to concurrent native parsing when unavailable.

Environment variables:

```text
ULTRA_HD_PROVIDER_ENABLED=True
ULTRA_HD_PUBLIC_ACCESS_ENABLED=False
DOUYIN_ULTRA_TOTAL_TIMEOUT_SECONDS=5.0
DOUYIN_ULTRA_NATIVE_TIMEOUT_SECONDS=3.0
DOUYIN_ULTRA_KUKUTOOL_GRACE_SECONDS=1.0
DOUYIN_NATIVE_EXECUTOR_WORKERS=4
DOUYIN_NATIVE_MAX_PENDING_TASKS=8
KUKUTOOL_PROVIDER_ENABLED=True
KUKUTOOL_NODE_BINARY=
KUKUTOOL_SIDECAR_BASE_URL=
KUKUTOOL_SIDECAR_TIMEOUT_SECONDS=3.0
KUKUTOOL_SIDECAR_HEALTH_TIMEOUT_SECONDS=0.5
KUKUTOOL_TIMEOUT_SECONDS=12.0
KUKUTOOL_PROBE_TIMEOUT_SECONDS=2.0
KUKUTOOL_DOWNLOAD_TIMEOUT_SECONDS=8.0
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
XHS_PROVIDER_ORDER=native
```

The default request path uses only `native`. Ultra mode starts native and KuKuTool in separate bounded executors and applies a four-second foreground deadline. An in-budget KuKuTool result wins. At the deadline, a completed native result can wait for a short `DOUYIN_ULTRA_KUKUTOOL_GRACE_SECONDS` tail window to catch a nearly-finished KuKuTool result; if KuKuTool is still running, native is returned while KuKuTool continues in the bounded background pool and fills cache. If neither path is complete the request returns a retryable timeout. The upstream result is normalized from `video_fullinfo`, restricted to `365yg.com` and `douyinvod.com`, deduplicated by TOS object key, and ranked primarily by bitrate.

`KUKUTOOL_VALIDATED_CACHE_TTL_SECONDS` caches the selected signed URL and metadata through Django cache. Its effective TTL never extends beyond the CDN signature expiry minus a safety margin. Production Redis therefore shares hot results across workers, while local `LocMemCache` remains process-local.

Short-link resolution stops as soon as any validated Douyin redirect URL exposes an `aweme_id`, including the common first-hop `iesdouyin.com/share/video/{id}` URL. It does not fetch the subsequent desktop video page before starting provider work.

The provider can use a direct Node helper or an internal `douyin-source-resolver` sidecar. Direct helper mode requires Node.js 18 or newer. Production defaults the provider to disabled and requires either an explicit `KUKUTOOL_NODE_BINARY` or `KUKUTOOL_SIDECAR_BASE_URL`; system Node and the Codex runtime are development-only fallbacks under `DEBUG=True`. Runtime validation happens during Django startup, while sidecar health is checked through a short cached `/health` probe. If the sidecar fails inside the active request budget, direct-helper fallback receives only the remaining budget so the foreground parse deadline remains bounded. Bounded executor queues prevent unbounded subprocess backlog. Redis single-flight deduplicates each `aweme_id`, while global concurrency, request-rate limits, a circuit breaker, and a staff kill switch constrain total upstream work. Child processes receive a minimal environment. Successful dynamic profiles are cached across helper processes. Discovery reads at most a bounded homepage and six bounded scripts, and all helper redirects remain on the exact KuKuTool origin.

Local CLI measurement on 2026-06-12 for aweme `7647533902413173321` returned a 67.22MB KuKuTool candidate in 1.915 seconds on a cold request and 0.505 seconds from validated cache. The same test previously fell back to 3.82MB 1080p because a two-second Range probe narrowly timed out; direct acceptance of complete structured metadata removed that incorrect downgrade.

## Long-Term Cold-Latency Plan

Cold ultra-HD requests now depend on the KuKuTool protocol exchange. The first local measurement is within two seconds, but this is not yet a production latency guarantee. The long-term target is:

- validated-cache hit P50 below 1 second
- cold-request P50 below 4 seconds
- cold-request P95 below 8 seconds
- no quality downgrade solely to meet the latency target

Implementation order:

1. Persist provider stage timings by API host, region, endpoint, cache state, and outcome. Build P50/P95 dashboards from real traffic instead of one-off CLI tests.
2. Reuse DNS/TLS connections with a bounded thread-local HTTP session or async client. Benchmark `.dev` and `.io` independently from the returned CDN download speed.
3. Redis single-flight per `aweme_id` and global concurrency limit. Completed on 2026-06-13.
4. Validated results and native metadata are cached separately. On a cached download failure, delete the exact validated entry, refresh once, validate the new response, and fall back to native if the refreshed source also fails. Stale-while-refresh near signature expiry remains future work.
5. Maintain short-lived regional health scores. Route new cold requests through the fastest healthy API host/region combination permitted for that user, while retaining the mainland-accessible `.dev` domain.
6. Replace the interim bounded in-process background pool with a long-running Node sidecar or shared Celery/RQ task. The page can later poll and replace the native source when ultra becomes available.
7. Research a second provider or self-hosted source acquisition path. A hard sub-3-second cold guarantee is not realistic while one external API consistently needs 4-7 seconds.

The synchronous request path should remain as a fallback until Redis-backed tasks and browser polling are proven reliable.

### Cookie Provider

Purpose: use user-owned authenticated sessions when a platform requires login for better sources.

Short-term decision:

- Do not implement for Bilibili ultra-HD now.
- Do not require cookies for Douyin/Xiaohongshu first pass.
- Revisit only if no-login providers cannot produce stable higher-quality sources.

## Shared Candidate Contract

Extend the current `MediaCandidate` gradually instead of returning platform-specific dicts from each provider.

Useful fields:

- `url`
- `media_type`
- `source`
- `quality_label`
- `size_bytes`
- `width`
- `height`
- `bit_rate`
- `codec`
- `expires_at`
- `headers`
- `cover_url`
- `cover_preview_url`
- `requires_merge`
- `audio_url`

The selector should prefer candidates that are both higher quality and validated:

1. Reachable URL.
2. Allowed final host.
3. Larger verified file size when resolution metadata is missing.
4. Higher resolution.
5. Higher bitrate.
6. Browser/download compatibility.
7. Lower operational risk.

## Quality Probe

Add a lightweight probe before trusting an ultra-HD candidate.

First pass:

- `HEAD` request for `Content-Length`.
- Range `GET bytes=0-0` fallback.
- Final host validation.
- Optional content type check.

Later pass:

- Optional `ffprobe` for resolution, codec, bitrate, and duration.
- Cache probe results per URL until expiry.

## Douyin First

### Phase 1: Refactor Boundary

- Move Douyin provider classes into a dedicated module or package once the next provider is added.
- Keep `DouyinAdapter` responsible for platform identity and provider orchestration.
- Keep current native providers as the fallback baseline.

### Phase 2: External API Provider

- Add a disabled-by-default `DouyinExternalProvider`.
- Convert external response fields into `MediaCandidate`.
- Probe candidates before selecting.
- If external provider fails or returns no validated ultra-HD candidate, fall back to native provider result.

### Phase 3: Diagnostics

Add server-side diagnostics logs for:

- provider attempted
- candidate count
- selected candidate source
- size/resolution/bitrate if known
- fallback reason
- provider duration
- TikHub API duration
- candidate probe duration
- cache hit/miss
- daily-limit state

Do not expose noisy diagnostics in the main UI yet.

## Xiaohongshu Second

### Phase 1: Keep Current Adapter Stable

The current Xiaohongshu adapter is compact and working. Do not split it into provider modules until a second independent source exists.

### Phase 2: External Provider

- Add a Xiaohongshu provider only after confirming a stable app/web endpoint or external API shape.
- Compare provider image URLs against current original image normalization because current image quality is already strong.
- Prioritize video quality research before changing image behavior.

## Bilibili Out of Scope

Bilibili higher-quality handling needs a different product decision:

- user cookie handling
- SESSDATA risk and storage
- DASH audio/video stream merge
- ffmpeg dependency
- possible paid membership entitlement

Short-term plan:

- Keep existing baseline Bilibili adapter.
- Do not promise Bilibili ultra-HD.
- Revisit after Douyin and Xiaohongshu provider model is stable.

## Implementation Order

1. Add provider settings and provider-order parsing. Done in `services/provider_settings.py`.
2. Add `QualityProbe` service and tests. Done in `services/quality.py`.
3. Move Douyin provider orchestration behind a clearer provider interface. Done in `services/platforms/douyin_providers.py`.
4. Add Douyin candidate diagnostics service and command. Done in `services/diagnostics.py` and `inspect_douyin_candidates`.
5. Add disabled-by-default `DouyinNativeEnhancedProvider`. Done in `services/platforms/douyin_providers.py`.
6. Manually compare Douyin baseline versus native-enhanced candidates with known links. Done for `modal_id=7618531038729648354`; 8K/4320p/native query variants did not find the 90 MiB source.
7. Add disabled-by-default Douyin external provider if native-enhanced cannot find larger validated sources. Done as `DouyinTikHubProvider`.
8. Add request-level ultra-HD switch plus basic TikHub cost controls. Done with the `ultra_hd` request flag, TikHub cache TTL, and optional daily limit.
9. Add provider timing diagnostics for slow ultra-HD parses. Partially done through structured server logs; UI/debug output is still pending.
10. Repeat the model for Xiaohongshu video sources.

## Success Criteria

- Existing baseline parsing keeps passing tests.
- External provider failure never breaks native parsing.
- Ultra-HD candidates are only selected after lightweight validation.
- The UI continues to show one best video resource unless a later UX decision adds manual source selection.
- Douyin has the first working optional higher-quality provider before Xiaohongshu work begins.
- Ultra-HD latency can be explained through diagnostics before promising competitor-level speed.
