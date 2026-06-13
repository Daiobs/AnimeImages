# Current Version Status

Date: 2026-06-13

## Summary

- The universal input still routes Xiaohongshu, Douyin, and Bilibili through platform adapters.
- Normal Douyin parsing remains native-only and does not call any external provider.
- When the settings-modal ultra-HD switch is enabled, KuKuTool is the only external upstream.
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
KUKUTOOL_PROVIDER_ENABLED=True
KUKUTOOL_NODE_BINARY=/path/to/node
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
KUKUTOOL_PROFILE_CACHE_TTL_SECONDS=86400
KUKUTOOL_VALIDATED_CACHE_TTL_SECONDS=300
KUKUTOOL_REFERENCE_WAIT_SECONDS=0.5
DOUYIN_ULTRA_TOTAL_TIMEOUT_SECONDS=3.0
DOUYIN_ULTRA_NATIVE_TIMEOUT_SECONDS=3.0
DOUYIN_NATIVE_EXECUTOR_WORKERS=4
DOUYIN_NATIVE_MAX_PENDING_TASKS=8
```

Node.js 18 or newer is required. Production should use Redis-backed Django cache so validated results are shared across workers.

## Known Issues

1. KuKuTool uses an undocumented third-party protocol. Upstream profile or encryption changes can break cold parsing.
2. Background tasks remain process-local threads. A worker restart loses queued work, so production should move the helper to a Node sidecar or Celery/RQ.
3. Redis single-flight and global concurrency protect normal operation, but they are coordination controls rather than a durable queue.
4. Structured KuKuTool metadata is trusted when host, TOS path, size, and signature expiry are valid. The actual download path must continue validating redirect hosts and content type.
5. A very fast KuKuTool result may arrive before native cover metadata. The provider waits briefly, then favors response latency.
6. Xiaohongshu ultra-HD research is not part of this iteration.
7. Bilibili remains baseline-only because higher qualities commonly require cookies, DASH merge, and entitlement.
8. Production ZIP progress still requires a shared task queue; local in-memory tasks are not multi-worker safe.

## Next Priorities

1. Replace per-request Node startup with a long-running internal Node sidecar.
2. Persist provider stage timings and calculate P50/P95 from real use.
3. Add a download-time cache invalidation/retry path when a signed CDN URL expires early.
4. Continue the self-owned 365yg/TOS provider research so KuKuTool can eventually be replaced.
