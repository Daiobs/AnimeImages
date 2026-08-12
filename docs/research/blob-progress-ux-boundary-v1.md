# Blob Progress UX And Boundary v1

Date: 2026-08-12

This report contains no source URL, signed CDN URL, query value, Provider
credential, cookie, manifest token, or user input. The real-media identity was
not persisted after the browser session. Only the approved CDN hostname and
non-sensitive measurements are recorded.

## Scope And Baselines

| Item | Value |
| --- | --- |
| Production known good | `a5e0254e001d56429008178ce39364be071da24a` |
| Research start | `2736c180e5d28b1de5c8057a0d22caa51c491af8` |
| Blob UX branch | `feat/blob-progress-ux-v1` |
| Streaming research branch | `research/zero-server-browser-streaming-v1` (created only after the Blob candidate is stable) |
| Immutable checkpoint | `checkpoint/blob-progress-streaming-start-20260812` -> `2736c180e5d28b1de5c8057a0d22caa51c491af8` |
| Protected `origin/main` during the experiment | `d98b3098bd55031c859c125e44c5d84b1f458f17` |
| Protected PR #32 | Draft/Open at `d93977ec383538871f1199ff0a825de28b99a988`; not modified |

The production candidate changes presentation and page-lifetime safety only.
The ordinary transport remains:

```text
Provider/CDN -> browser fetch -> bounded chunks -> Blob -> object URL -> disk
```

The current code limit remains 1 GiB and the network inactivity timeout remains
30 seconds. Those configuration values are not claims that a 1 GiB Blob is
operationally safe.

## Shared Progress Contract

`static/js/ordinary_media_download.js` is still the only ordinary-video media
reader used by Single and Batch. It emits:

- `stage`
- `receivedBytes`
- `totalBytes`
- `percent`
- `elapsedMs`
- `bytesPerSecond`
- `etaSeconds`

`receivedBytes` advances only after a successful `reader.read()` chunk. Known
totals come only from a positive HTTP `Content-Length`; parsed item size is not
used to fabricate a transfer percentage. Unknown totals therefore show bytes
and speed with an indeterminate bar and no ETA. Completion reaches 100% only
after the complete response has been read. UI emissions are throttled to about
300 ms and speed uses a three-second rolling window; ETA waits for at least 1.5
seconds of samples.

Single reuses its existing progress area. Each Batch card renders the same
event contract. The former Batch-only reader was not restored.

## Refresh Safety

While the page owns an active Blob fetch it registers `beforeunload` and writes
one `sessionStorage` marker. The marker contains only:

- schema version
- active Boolean
- start time
- sanitized Provider name
- sanitized filename
- expected byte count

It contains no URL, query string, token, credential, manifest, or response
data. Completion at Blob handoff, ordinary failure, and abort remove the
protection. A confirmed unload leaves the marker so the next page can explain
that the prior transfer was interrupted; the page never automatically retries
or calls a Provider.

### Chrome Refresh Matrix

The matrix used a deterministic 110,691,390-byte CORS source and the production
shared downloader. The source ran only on loopback and was removed after the
experiment.

| Test | Observation | Result |
| --- | --- | --- |
| R1: refresh near 30%, then cancel navigation | Native leave warning appeared; transfer continued to exactly 110,691,390 bytes; a later normal reload showed no false interruption notice | PASS |
| R2: refresh near 30%, confirm | Transfer stopped, no partial file was handed off, and the reloaded page displayed the interruption notice | PASS |
| R3: refresh near 90%, confirm | Transfer stopped, no partial file was handed off, and the reloaded page displayed the interruption notice | PASS |
| R4: refresh after Blob handoff | No leave warning or interruption notice; saved file remained exact | PASS |

Tab-close behavior is equivalent to a confirmed page unload for the Blob
fetch, but a separate physical tab-close acceptance was not performed.

## Real TianTianJieXi Acceptance

Chrome on macOS used one fresh ordinary TianTianJieXi MP4 from
`v5-default.365yg.com`.

| Measurement | Single | Batch |
| --- | --- | --- |
| Expected bytes | 110,691,390 | 110,691,390 |
| Final bytes | 110,691,390 | 110,691,390 |
| Visible progress | Monotonic samples from near 0 through 100 | Per-card progress reached 100 |
| Speed/ETA | Visible; ETA converged to one second | Visible through the shared card contract |
| Representative uncached duration | 43.5 seconds | Provider parse 6.27 seconds; media completion independently verified |
| Playback navigation | None | None |
| AnimeImages ordinary media-body requests | 0 | 0 |
| AnimeImages ordinary media bytes | 0 | 0 |
| Server Relay / File System API | Not used | Not used |

The Single control-plane route call carried only a bounded descriptor response;
it did not carry media bytes. The Batch request log contained parse and preview
requests only. Single and Batch both invoked the same shared browser primitive.

## Blob Size Boundary

The 300 and 500 MiB cases used a deterministic local CORS byte source at 12
MiB/s. Measurements include the whole Chrome process family, so total-RSS delta
also reflects browser background activity. Renderer delta is the more useful
comparison, but neither metric is equivalent to JavaScript heap.

| Payload | Duration | Peak Chrome total delta | Peak renderer delta | Renderer delta / file | Responsiveness | Classification |
| --- | ---: | ---: | ---: | ---: | --- | --- |
| 100 MiB | 8.51 s | 57.4 MiB | 19.3 MiB | 0.19x | Event-loop lag about 2 ms; exact file | `BLOB_SAFE` |
| 300 MiB | 25.06 s | 739.5 MiB | 403.7 MiB | 1.35x | Event-loop lag about 2 ms; exact file | `BLOB_SAFE` with material memory cost |
| 500 MiB | 41.90 s | 639.9 MiB | 449.5 MiB | 0.90x | Event-loop lag about 2 ms; exact file; renderer remained about 192 MiB above its start after roughly 60 seconds | `BLOB_DEGRADED` |
| 1 GiB | Not run | Not measured | Not measured | Not measured | Avoided to protect the test machine after the 500 MiB result | `NOT_TESTED` |

The 100 MiB baseline was run after earlier browser activity, so its low delta
must not be interpreted as a universal allocation ratio. The 300 MiB run shows
that `chunks[]`, Blob construction, networking, and browser bookkeeping can
temporarily require substantially more memory than the media itself.

Recommended Recovery v1 range: ordinary files through approximately 300 MiB.
The 500 MiB path completed correctly but is degraded because of peak and
retained renderer memory. Files near the current 1 GiB code limit are not
validated and remain `BROWSER_BLOB_LARGE_FILE_LIMITATION` backlog.

## Production Candidate Gate

The following gates have browser evidence:

- real TianTianJieXi Single >=100 MiB: PASS
- real TianTianJieXi Batch >=100 MiB: PASS
- monotonic real-byte progress: PASS
- speed and ETA: PASS
- refresh cancel/confirm matrix: PASS
- exact final bytes: PASS
- AnimeImages ordinary media bytes: 0
- playback navigation: 0
- Server Relay: 0

Deployment status and final readiness are recorded only after the complete
automated suite, Linux deployment, readiness checks, and production browser
acceptance. A failure at any of those gates requires rollback to `a5e0254`.

## Streaming Research Boundary

Any Service Worker attachment-stream experiment belongs only on
`research/zero-server-browser-streaming-v1`. It must not be imported, loaded,
or deployed with this Blob UX candidate. A successful POC would be a future
architecture recommendation, not a production change in this task.
