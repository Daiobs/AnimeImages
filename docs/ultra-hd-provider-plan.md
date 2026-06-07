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
TIKHUB_API_KEY=
TIKHUB_BASE_URL=
TIKHUB_REGION=CN
TIKHUB_CACHE_TTL_SECONDS=1800
TIKHUB_DAILY_LIMIT=20
DOUYIN_PROVIDER_ORDER=tikhub,native_enhanced,native
XHS_PROVIDER_ORDER=native,tikhub
```

Current local-enhanced default:

```text
DOUYIN_PROVIDER_ORDER=native_enhanced,native
```

The default request path uses only `native` so ordinary parsing remains fast even when provider infrastructure is configured. `native_enhanced` is disabled unless `ULTRA_HD_PROVIDER_ENABLED=True` and the request asks for ultra-HD; `native` remains the fallback. TikHub integration uses the official Douyin highest-quality play URL endpoint and is disabled unless `ULTRA_HD_PROVIDER_ENABLED=True`, `TIKHUB_API_KEY` is configured, and the request asks for ultra-HD. When the request asks for ultra-HD, TikHub is promoted ahead of local providers so the switch has clear semantics; however, TikHub and the native fast path run concurrently, and the returned TikHub source is still compared with the native result by probed size, so a smaller TikHub 720p source cannot replace a larger native 1080p source. Provider failures or daily-limit rejections fall back to local parsing. `TIKHUB_CACHE_TTL_SECONDS` caches identical TikHub lookups inside the current Django process, while `TIKHUB_DAILY_LIMIT` can cap paid calls per process per day. A value of `0` means unlimited.

The Django settings module loads the project-root `.env` file before reading environment variables. Real provider keys should live in local `.env` or deployment secrets, not in committed files. The ultra-HD request switch is intentionally placed in the settings modal instead of the hero parser area so normal parsing remains clean and free by default.

Current status note: although the provider path now avoids default-mode slowdown and runs TikHub with the native fast path in parallel, manual PyCharm/browser testing still shows some Douyin ultra-HD parses taking more than 10 seconds. Treat ultra-HD as a research feature until provider timing diagnostics identify the slow segment.

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
9. Add provider timing diagnostics for slow ultra-HD parses. Pending.
10. Repeat the model for Xiaohongshu video sources.

## Success Criteria

- Existing baseline parsing keeps passing tests.
- External provider failure never breaks native parsing.
- Ultra-HD candidates are only selected after lightweight validation.
- The UI continues to show one best video resource unless a later UX decision adds manual source selection.
- Douyin has the first working optional higher-quality provider before Xiaohongshu work begins.
- Ultra-HD latency can be explained through diagnostics before promising competitor-level speed.
