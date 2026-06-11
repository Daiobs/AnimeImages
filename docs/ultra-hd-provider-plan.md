# Ultra-HD Provider Plan

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

### External API Provider

Purpose: optional higher-quality provider for sources that are hard to reproduce locally.

Candidate examples:

- TikHub or similar API for Douyin video detail and source enumeration.
- TikHub or similar API for Xiaohongshu app/web detail endpoints.

Rules:

- Disabled by default.
- Enabled only when environment variables are configured.
- Must not send user URLs to third-party APIs unless the operator intentionally enables it.
- Must return the same internal candidate shape as native providers.

Suggested environment variables:

```text
ULTRA_HD_PROVIDER_ENABLED=False
ULTRA_HD_PUBLIC_ACCESS_ENABLED=False
TIKHUB_API_KEY=
TIKHUB_BASE_URL=https://api.tikhub.io
TIKHUB_CHINA_BASE_URL=https://api.tikhub.dev
TIKHUB_REGION=CN
TIKHUB_DEFAULT_COUNTRY_CODE=CN
TIKHUB_TRUST_COUNTRY_HEADER=False
TIKHUB_COUNTRY_HEADER=HTTP_CF_IPCOUNTRY
TIKHUB_GEOIP_DATABASE_PATH=
TIKHUB_DAILY_LIMIT=20
DOUYIN_ULTRA_TOTAL_TIMEOUT_SECONDS=15.0
DOUYIN_ULTRA_NATIVE_TIMEOUT_SECONDS=5.0
TIKHUB_CONNECT_TIMEOUT_SECONDS=1.0
TIKHUB_READ_TIMEOUT_SECONDS=6.5
TIKHUB_MAX_ATTEMPTS=2
TIKHUB_PROBE_TIMEOUT_SECONDS=1.8
TIKHUB_MAX_PROBE_CANDIDATES=1
TIKHUB_VALIDATED_CACHE_TTL_SECONDS=600
DOUYIN_PROVIDER_ORDER=tikhub,native
XHS_PROVIDER_ORDER=native,tikhub
```

The default request path uses only `native`. TikHub additionally requires `ULTRA_HD_PUBLIC_ACCESS_ENABLED=True` before a visitor request can reach the paid provider. In ultra mode, short-link resolution and one orchestrator share the same request deadline; the orchestrator runs TikHub and the native fast path concurrently. TikHub uses separate connect/read timeouts, validates only its first preferred candidate with a Range request, and returns native when the enhancement budget expires. The synchronous web path does not run `native_enhanced`; deep multi-source comparison remains available only for diagnostics.

`TIKHUB_VALIDATED_CACHE_TTL_SECONDS` caches the selected URL, verified byte size, content type, and final CDN URL. A cache hit skips both the paid API call and CDN probe. Signed URLs can expire, so the default TTL is 600 seconds. `TIKHUB_DAILY_LIMIT` counts every outbound attempt, including retries, and uses the same Django cache; production requires shared Redis so workers enforce one cache and one paid-call counter.

TikHub routing is request-aware. Mainland China uses `https://api.tikhub.dev` with `region=CN`; all other countries use `https://api.tikhub.io` with the visitor's normalized ISO country code. Validated candidate cache keys include both API host and region, so a CDN URL selected for one region cannot leak into another region's response. Country detection accepts a proxy header only when `TIKHUB_TRUST_COUNTRY_HEADER=True`; otherwise it uses the optional local GeoIP2 database and falls back to `TIKHUB_DEFAULT_COUNTRY_CODE`.

The high-quality endpoint can occasionally return a temporary HTTP/provider `400`, a network timeout, or an unusable HTML/tiny-file payload. `TIKHUB_MAX_ATTEMPTS=2` retries those transient cases plus `429/5xx` responses. A mainland retry keeps the accelerated `api.tikhub.dev` domain but changes the second attempt to `region=HK`, which avoids repeatedly selecting a broken CN CDN response. The 15-second ultra budget is intentionally separate from normal parsing so one retry can finish without downgrading a known 170MB-class source to native 1080p.

Short-link resolution stops as soon as any validated Douyin redirect URL exposes an `aweme_id`, including the common first-hop `iesdouyin.com/share/video/{id}` URL. It does not fetch the subsequent desktop video page before starting provider work.

The Django settings module loads the project-root `.env` file before reading environment variables. Real provider keys should live in local `.env` or deployment secrets, not in committed files. Environment templates are split into `.env.example.local` and `.env.example.production`; production mode requires explicit secret, allowed hosts, and Redis cache URL. The ultra-HD request switch is intentionally placed in the settings modal instead of the hero parser area so normal parsing remains clean and free by default.

Current measured behavior: the paid API can take about 4-6 seconds. Ultra mode treats a valid TikHub candidate as authoritative and returns it immediately without comparing its size to native or waiting for an unfinished native request. When TikHub supplies a valid allowlisted video URL, positive `file_size`, and video content type, the request trusts that structured metadata and skips a redundant CDN Range probe. Cache hits skip native entirely. TikHub failure falls back first to the concurrent native result, then to the normal native provider.

## Long-Term Cold-Latency Plan

Cold ultra-HD requests still depend on a TikHub call that can exceed 7 seconds. This is not considered solved by the current timeout and retry work. The long-term target is:

- validated-cache hit P50 below 1 second
- cold-request P50 below 4 seconds
- cold-request P95 below 8 seconds
- no quality downgrade solely to meet the latency target

Implementation order:

1. Persist provider stage timings by API host, region, endpoint, cache state, and outcome. Build P50/P95 dashboards from real traffic instead of one-off CLI tests.
2. Reuse DNS/TLS connections with a bounded thread-local HTTP session or async client. Benchmark `.dev` and `.io` independently from the returned CDN download speed.
3. Add Redis single-flight per `aweme_id + country route` so concurrent visitors share one paid TikHub request instead of producing duplicate calls.
4. Keep validated results in Redis and implement stale-while-refresh near signed-URL expiry. Delete the cache entry and refresh once when a cached download URL fails.
5. Maintain short-lived regional health scores. Route new cold requests through the fastest healthy API host/region combination permitted for that user, while retaining the mainland-accessible `.dev` domain.
6. Move cold ultra-HD resolution to a shared background task. The page can render native metadata immediately, show provider progress, and replace the video source when the ultra candidate arrives without holding one Django request open.
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
