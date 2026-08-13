# Service Worker Streaming Productionization v1

Date: 2026-08-13

This report contains no source URL, signed CDN URL, query value, Provider
credential, cookie, manifest token, or user input. Real-resource identity lived
only in the browser and Service Worker memory and was not persisted.

## Status

```text
SERVICE_WORKER_STREAMING_PRODUCTION_STABLE
```

| Item | Value |
| --- | --- |
| Blob recovery rollback SHA | `a5e0254e001d56429008178ce39364be071da24a` |
| Streaming POC SHA | `2667f1e2f980d9fb22f1b54ac9db2fffb8a68ca9` |
| Production streaming SHA | `5af0892bc7f01ec73a5a3c1ba1b55814364e5e6a` |
| Production checkpoint | `checkpoint/production-service-worker-streaming-v1` |
| Branch | `feat/service-worker-streaming-production-v1` |
| Production deploy | Successful; no rollback |

## Architecture

Ordinary TianTianJieXi and KuKuTool video downloads use:

```text
Provider/CDN
    -> browser Service Worker fetch
    -> streaming synthetic attachment response
    -> browser Download Manager
    -> disk
```

AnimeImages is the resolution and handoff control plane. It does not carry the
ordinary video body. Single and Batch call the same browser API and consume the
same progress contract.

The default transport is `BROWSER_STREAMING`. `BROWSER_BLOB` is allowed only
when Streaming fails before a Service Worker upstream fetch or Download
Manager task starts. After Streaming starts, failure is terminal for that
attempt and cannot silently start a second Blob transfer. `SERVER_REQUIRED`
remains a distinct class for YouTube audio/video merge, ZIP creation, and other
server-generated output. Server Relay is not an ordinary-video fallback.

The signed media URL is held only in current page memory and the active Service
Worker's in-memory descriptor map. It is cleared at terminal state and is never
written to browser storage, Cache Storage, IndexedDB, the database, logs, or
this report. The opaque `downloadId` is random and contains no resource
identity.

The worker is served from `/ordinary-media-download-sw.js` with root scope,
`no-store`, `Service-Worker-Allowed: /`, and `nosniff`. It deliberately does
not call `skipWaiting()`: an active worker can finish its streams while an
updated worker waits for normal activation.

## Shared Progress And Recovery

Streaming and Blob emit one URL-free progress shape containing download ID,
safe filename and Provider name, state/stage, received and total bytes,
percent, rolling speed, ETA, and timestamps. UI updates are throttled; unknown
totals are indeterminate.

The Service Worker supports URL-free status messages for listing active
downloads, reading one status, and cancelling one transfer. A refreshed page
queries the active worker and reattaches to the same `downloadId`; it does not
reparse or issue a second media request. Result URLs are replaced with the
platform home URL after rendering so a later browser refresh cannot replay a
Provider POST.

Site cancellation and Download Manager cancellation both abort the upstream
reader. Python, Django, Nginx, and Gunicorn never participate in the ordinary
media transfer.

## Real Production Acceptance

One fresh TianTianJieXi ordinary MP4 was validated in Chrome on macOS.

| Gate | Single | Batch |
| --- | --- | --- |
| Expected bytes | 110,691,390 | 110,691,390 |
| Final bytes | 110,691,390 | 110,691,390 |
| Download Manager created before completion | PASS | PASS |
| Real percent, speed, ETA | PASS | PASS |
| Playback navigation | 0 | 0 |
| Blob / File System API | 0 / 0 | 0 / 0 |
| AnimeImages ordinary media requests | 0 | 0 |
| AnimeImages ordinary media bytes | 0 | 0 |

The Single transfer was refreshed around 35 percent. The Download Manager
continued, the new page reattached to the active transfer, the parse-log count
did not change, and the final file remained exact. Batch parsed and displayed
the fresh result progressively, then downloaded it through the same shared
Streaming primitive.

Production deployed exact SHA `5af0892`, remained clean, and passed local and
public readiness plus homepage checks. `.env.production`, systemd, and Nginx
hashes were unchanged. No migration or infrastructure change was required.

## Cancellation

A controlled 256 MiB Chrome transfer was cancelled from Download Manager. The
worker reported `DOWNLOAD_MANAGER_CANCELLED`; it stopped at 39,845,888 received
bytes, while the source observed a final bounded 40,108,032 bytes and no growth
during the following five seconds. The connection ended as disconnected rather
than completing. No Blob fallback or second request was started.

## Controlled Large-File Evidence

| Payload | Result | Duration | Page JS heap signal | Full buffering |
| --- | --- | ---: | ---: | --- |
| 300 MiB | Exact 314,572,800 bytes | Controlled run | Low; prior renderer delta about 25.6 MiB | No |
| 500 MiB | Exact 524,288,000 bytes | 31.3 s at 16 MiB/s | About 5.4 MiB used JS heap | No |

The 300 MiB run survived a page refresh at about 52 percent and reattached to
the same ID before exact completion. The 500 MiB run kept its progress UI
responsive and generated one source request. These measurements confirm the
streaming implementation does not construct a media-sized Blob or chunk array;
they are not universal browser memory guarantees.

## Stabilization Ladder

### Concurrency

Two 32 MiB downloads received independent IDs, filenames, and progress. In the
independent-cancel run, C was cancelled at 82.8 percent; its source disconnected
after 28,049,408 bytes and no final file appeared. D continued unaffected and
saved exactly 33,554,432 bytes. No upstream or filename state crossed between
the downloads.

### Multiple Refreshes

A 128 MiB controlled transfer was refreshed at approximately 26, 55, and 93
percent. Each page reattached to the same active download and progress kept
increasing. The source received one request and the final file was exactly
134,217,728 bytes.

### Tab Close

The initiating page closed around 25 percent of a 64 MiB transfer. Browser
Download Manager and the Service Worker completed independently, with one
source request and an exact 67,108,864-byte file. Classification:
`TAB_CLOSE_SURVIVES`.

### Service Worker Update

An isolated loopback harness copied the production worker unchanged, started a
96 MiB stream, then served a byte-different worker revision. At approximately
88 percent the registration showed the old worker active and the new worker
waiting. The old worker completed the original ID exactly at 100,663,296 bytes
with one source request. This confirms the no-`skipWaiting()` lifecycle keeps
active streams alive while an update waits.

## Browser Matrix

| Browser | Result |
| --- | --- |
| Chrome macOS | Full production PASS: Single, refresh/reattach, cancel, Batch, concurrency, tab close, update lifecycle, 300/500 MiB |
| Safari macOS | Controlled 16 MiB `browser_streaming` smoke PASS; exact 16,777,216-byte file after Safari's first-download permission prompt |
| Firefox desktop | Not installed; not tested |
| Edge desktop | Not installed; not tested |

An unsupported browser may use Blob only when the Streaming capability fails
before start. It must never fall back to Server Relay.

## Automated And Linux Gates

- Django `xhs_download`: 968 tests passed.
- Provider Strategy: 119 tests passed.
- Node: 86 tests passed.
- `manage.py check`, migration dry run, JavaScript syntax checks, and
  `git diff --check`: passed.
- GitHub Django and Settings Guard workflows at production SHA: passed.
- Isolated Linux candidate at exact SHA: passed Django/static checks, Service
  Worker headers, real parsing, exact 110,691,390-byte browser save, and zero
  candidate media-body traffic.
- Secret and forbidden-storage scans found no real signed URL, credential, full
  media buffering, Cache Storage, IndexedDB, or ordinary relay call in the
  Streaming modules.

## Rollback And Remaining Work

Rollback remains an exact deployment of `a5e0254` if a future production
regression invalidates this checkpoint. No database rollback is needed.

Remaining compatibility work is non-blocking:

- run the same controlled Streaming smoke in Firefox and Edge when installed;
- repeat a fresh KuKuTool provider-specific production smoke;
- keep Service Worker version/update behavior in the release checklist;
- merge or otherwise reconcile the stabilization line with `main` through a
  separately reviewed integration decision; this task did not modify `main` or
  PR #32.
