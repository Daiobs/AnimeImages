# Blob Progress And Streaming Closeout v1

Date: 2026-08-12

Primary status: `BLOB_PROGRESS_CANDIDATE_READY`

Streaming status: `ZERO_SERVER_SERVICE_WORKER_STREAMING_POC_PASS`

This closeout contains no source URL, signed CDN URL, query value, Provider
credential, cookie, manifest token, or user input.

## Start

1. Production SHA: `a5e0254e001d56429008178ce39364be071da24a`.
2. Research start SHA: `2736c180e5d28b1de5c8057a0d22caa51c491af8`.
3. Blob UX branch: `feat/blob-progress-ux-v1`.
4. Streaming research branch: `research/zero-server-browser-streaming-v1`.
5. Checkpoint: `checkpoint/blob-progress-streaming-start-20260812` -> `2736c180e5d28b1de5c8057a0d22caa51c491af8`.
6. Protected main during the work: `d98b3098bd55031c859c125e44c5d84b1f458f17`.
7. PR #32: Draft/Open, head `d93977ec383538871f1199ff0a825de28b99a988`, unchanged.

## Current Blob Behavior

8. Transport: CDN browser fetch -> bounded chunks -> Blob -> object URL -> disk.
9. Current code maximum: 1 GiB; this is not an operational safety claim.
10. Fetch/body idle timeout: 30 seconds.
11. Before the candidate, refresh destroyed the page-owned fetch and chunks without a reliable warning or recovery notice.
12. Post-handoff refresh: PASS; the exact saved file remains valid after the browser save handoff.

## Progress UX

13. Shared API: `onProgress` emits stage, bytes, total, percent, elapsed time, smoothed speed, and ETA.
14. Single UI: reuses the existing page progress region.
15. Batch UI: renders the same contract independently per active card.
16. Visual source: presentation cues from the archived V1 Batch progress UI; no legacy downloader restored.
17. Received bytes: incremented only from successful response-body chunks.
18. Percent: derived only from positive HTTP `Content-Length`; completion is 100 after exact read.
19. Speed: three-second rolling-window smoothing using a monotonic clock.
20. ETA: shown only for a known total after at least 1.5 seconds of usable samples.
21. UI update interval: approximately 300 ms maximum frequency.
22. Unknown total: indeterminate bar with received bytes and speed; no fabricated percent or ETA.

## Refresh

23. `beforeunload`: active only while the page owns an unfinished Blob transfer.
24. Active marker: one versioned `sessionStorage` record.
25. Stored fields: active state, start time, sanitized Provider, sanitized filename, and expected bytes.
26. Sensitive data persisted: NO.
27. R1, refresh around 30% and cancel: warning shown, transfer continued, exact 110,691,390-byte file.
28. R2, refresh around 30% and confirm: transfer stopped, no partial handoff, interruption notice shown.
29. R3, refresh around 90% and confirm: same safe interruption behavior; no damaged handoff.
30. R4, refresh after handoff: no stale warning; exact file preserved.
31. Reload message: explains that the prior download may have been interrupted and requires an explicit retry/reparse.
32. Physical tab close: not separately tested; expected to follow confirmed unload semantics but remains unclaimed.

## Blob Boundary

33. 100 MiB: 8.51 s; 57.4 MiB total-Chrome and 19.3 MiB renderer peak deltas; no observable finalize freeze; `BLOB_SAFE`.
34. 300 MiB: 25.06 s; 739.5 MiB total-Chrome and 403.7 MiB renderer peak deltas; exact file and responsive UI; `BLOB_SAFE` with material memory cost.
35. 500 MiB: 41.90 s; 639.9 MiB total-Chrome and 449.5 MiB renderer peak deltas; about 192 MiB renderer retention after 60 seconds; `BLOB_DEGRADED`.
36. 1 GiB: not run to protect the test machine after the 500 MiB result.
37. Renderer peak/file ratios: 0.19x at 100 MiB, 1.35x at 300 MiB, and 0.90x at 500 MiB.
38. UI freeze: no material event-loop freeze observed; about 2 ms measured lag, but memory pressure rose materially.
39. Recommended Blob range: through approximately 300 MiB; 500 MiB and the untested 1 GiB limit stay in `BROWSER_BLOB_LARGE_FILE_LIMITATION` backlog.

## Production UX

40. Production deploy attempted: YES, exact candidate SHA through the existing deploy script.
41. Candidate SHA: `e9b5001a3054ffb060950825d253d234ebad775e`.
42. Single real result: exact 110,691,390-byte TianTianJieXi file, monotonic progress, speed/ETA, no playback.
43. Batch real result: exact same-size file with per-card shared progress; Provider parse observed at 6.27 seconds.
44. AnimeImages ordinary media body: 0 requests / 0 bytes.
45. Playback: 0.
46. Rollback: YES; the post-deploy browser-control gate could not be completed, so the candidate was not retained.
47. Final production SHA: `a5e0254e001d56429008178ce39364be071da24a`.
48. Final production readiness: service active; local and public readiness HTTP 200.

## Streaming POC

49. Design: in-memory one-use descriptor -> same-origin synthetic URL -> Service Worker CDN fetch -> counting TransformStream -> attachment Response -> Chrome Download Manager.
50. Real media path: TianTianJieXi CDN -> browser Service Worker -> Chrome Download Manager -> disk.
51. Download Manager task created: YES.
52. Task created before completion: YES; `.crdownload` was visible and growing while the source remained incomplete.
53. CDN hostname: `v11-default.365yg.com`.
54. AnimeImages ordinary media body: 0 requests / 0 bytes.
55. Blob used: NO.
56. Full buffering used: NO.
57. Real final bytes: 110,691,390, equal to descriptor, worker, and disk totals.
58. 100 MiB result: controlled exact PASS plus real 105.56 MiB exact PASS.
59. 300 MiB result: exact 314,572,800 bytes in 24.924 seconds.
60. Memory comparison: 300 MiB Service Worker renderer delta 25.6 MiB versus Blob 403.7 MiB.

## Streaming Refresh

61. Refresh point: 37,879,808 / 104,857,600 bytes, about 36%.
62. Download continued: YES.
63. Final file after refresh: exact 104,857,600 bytes.
64. Tab close: not tested and not claimed.
65. Lifecycle conclusion: an active Service Worker fetch/download response survived replacement of the originating page; browser shutdown/process-failure survival is not claimed.

## Comparison

66. Browser Blob memory: complete chunks/Blob can scale to hundreds of MiB and showed a 403.7 MiB renderer delta at 300 MiB.
67. Service Worker memory: streaming stayed near a 25.6 MiB renderer delta at 300 MiB.
68. Blob click-to-save: browser save starts only after all media bytes are acquired and the Blob is built.
69. Service Worker click-to-task: the Download Manager task starts while media is still arriving.
70. Blob refresh: pre-handoff refresh interrupts and discards page-owned bytes.
71. Service Worker refresh: controlled download continued and completed exactly.
72. Download Manager UX: Service Worker supplies native attachment progress from the beginning; Blob hands off only at completion.

## Final

73. Blob suitable for approximately 100 MiB: YES.
74. Blob suitable for approximately 300 MiB: YES, with documented material memory cost.
75. Blob suitable for approximately 500 MiB: DEGRADED; not recommended as the default validated range.
76. Progress UX should remain: YES; it accurately improves the current known-good Blob path.
77. Service Worker streaming viable: YES as a Chrome/macOS research POC, not yet a production feature.
78. Next architecture: `SERVICE_WORKER_STREAMING`.
79. Next task: productionize a signed, one-use ordinary-media descriptor and shared Single/Batch Service Worker handoff with expiry, upgrade, concurrency, cancellation, and browser capability gates.
80. Final production SHA: `a5e0254e001d56429008178ce39364be071da24a`.
81. Production safe: YES; production is healthy on the known-good Blob recovery SHA, and no streaming POC code was deployed.
