# Current Version Status

Date: 2026-06-12

## Summary

- The universal input still routes Xiaohongshu, Douyin, and Bilibili through platform adapters.
- Normal Douyin parsing remains native-only and does not call any external provider.
- When the settings-modal ultra-HD switch is enabled, KuKuTool is the only external upstream.
- TikHub and `native_enhanced` remain in the codebase for research and diagnostics, but are ignored by the synchronous ultra-HD request path.
- KuKuTool failure or timeout falls back to concurrent native parsing.

## KuKuTool Path

1. Extract `aweme_id` from the submitted Douyin URL or controlled short-link redirect.
2. Start the native fast path concurrently for metadata and fallback.
3. Invoke the bundled Node.js helper against KuKuTool.
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

## Configuration

```text
ULTRA_HD_PROVIDER_ENABLED=False
ULTRA_HD_PUBLIC_ACCESS_ENABLED=False
KUKUTOOL_PROVIDER_ENABLED=True
KUKUTOOL_NODE_BINARY=/path/to/node
KUKUTOOL_TIMEOUT_SECONDS=12.0
KUKUTOOL_PROBE_TIMEOUT_SECONDS=2.0
KUKUTOOL_MAX_PROBE_CANDIDATES=2
KUKUTOOL_MAX_CONCURRENCY=2
KUKUTOOL_VALIDATED_CACHE_TTL_SECONDS=300
KUKUTOOL_REFERENCE_WAIT_SECONDS=0.5
```

Node.js 18 or newer is required. Production should use Redis-backed Django cache so validated results are shared across workers.

## Known Issues

1. KuKuTool uses an undocumented third-party protocol. Upstream profile or encryption changes can break cold parsing.
2. There is no Redis single-flight lock yet, so simultaneous cold requests for the same `aweme_id` can duplicate upstream work.
3. Process concurrency is bounded per Django worker. A shared queue or global distributed concurrency limit is still desirable for public production.
4. Structured KuKuTool metadata is trusted when host, TOS path, size, and signature expiry are valid. The actual download path must continue validating redirect hosts and content type.
5. A very fast KuKuTool result may arrive before native cover metadata. The provider waits briefly, then favors response latency.
6. Xiaohongshu ultra-HD research is not part of this iteration.
7. Bilibili remains baseline-only because higher qualities commonly require cookies, DASH merge, and entitlement.
8. Production ZIP progress still requires a shared task queue; local in-memory tasks are not multi-worker safe.

## Next Priorities

1. Add Redis single-flight and a short failure cache for KuKuTool cold requests.
2. Persist provider stage timings and calculate P50/P95 from real use.
3. Add a download-time cache invalidation/retry path when a signed CDN URL expires early.
4. Continue the self-owned 365yg/TOS provider research so KuKuTool can eventually be replaced.
