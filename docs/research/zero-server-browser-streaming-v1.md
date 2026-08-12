# Zero-Server Browser Streaming Feasibility v1

Date: 2026-08-12

Status: `ZERO_SERVER_SERVICE_WORKER_STREAMING_POC_PASS`

This report contains no source URL, signed CDN URL, query value, Provider
credential, cookie, manifest token, or user input. Only approved CDN hostnames
and non-sensitive measurements are recorded.

## Boundary

The experiment lives only on `research/zero-server-browser-streaming-v1` and
is not imported by Single, Batch, or any production template. It was not
deployed. Production remained on the known-good
`a5e0254e001d56429008178ce39364be071da24a`.

The prototype uses this data plane:

```text
Page control plane
  -> one-time opaque descriptor in Service Worker memory
  -> same-origin synthetic download navigation
  -> Service Worker fetches Provider/CDN with credentials omitted
  -> upstream ReadableStream passes through a counting TransformStream
  -> synthetic attachment Response
  -> Chrome Download Manager
  -> disk
```

It does not use Blob, File System Access, full-file buffering, Cache Storage,
IndexedDB, local/session storage, or AnimeImages media relay endpoints. The
signed URL exists only in page/worker memory. The page clears its URL field and
retains only the opaque ID and expected byte count after registration; the
worker deletes the descriptor from its map when the synthetic URL is consumed.

## Security Properties

- Descriptor IDs are one-use random 128-bit hexadecimal values.
- HTTPS media hosts are restricted to `365yg.com` and its subdomains; HTTP is
  accepted only for the loopback benchmark.
- Content type is restricted to ordinary binary/video media.
- Declared and actual stream sizes are bounded to 2 GiB in the POC.
- A known expected size must match both declared `Content-Length` and final
  bytes received.
- Fetch uses `credentials: omit`, `cache: no-store`, and `no-referrer`.
- The synthetic response sets attachment semantics and `nosniff`.
- No production template references the POC files.

These controls are research constraints, not a claim that the code is ready
for production. Productionization still needs a server-issued descriptor
contract, browser compatibility policy, Service Worker versioning, concurrent
download UX, and explicit failure recovery.

## Controlled Results

| Test | Evidence | Result |
| --- | --- | --- |
| 100 MiB stream | Exact 104,857,600-byte file; source completed in 12.506 s and worker in 12.380 s | PASS |
| Early task creation | At 10 s the `.crdownload` existed at 22,020,096 bytes while the source was incomplete | PASS |
| Refresh at 36% | Page reloaded at 37,879,808 / 104,857,600 bytes; download continued and finalized exactly | `SW_DOWNLOAD_SURVIVES_PAGE_REFRESH` |
| 300 MiB stream | Exact 314,572,800-byte file; worker duration 24.924 s | PASS |
| Route-on-click | Exact 20,971,520-byte file; descriptor registration plus launch in the click path | PASS |
| Playback | No media playback page opened | PASS |
| AnimeImages media plane | No prepare, stream, status, proxy, or other media-body request | 0 requests / 0 bytes |

A physical tab-close test was not run; it remains optional follow-up evidence.

## Memory Comparison

The controlled 300 MiB Service Worker run had a 35.3 MiB peak total-Chrome RSS
delta and a 25.6 MiB peak renderer delta. The 300 MiB Blob run had a 739.5 MiB
peak total-Chrome delta and a 403.7 MiB peak renderer delta. Process-family RSS
is noisy, but the order-of-magnitude difference and exact output demonstrate
that the Service Worker path did not retain the complete media in page memory.

| 300 MiB path | Total Chrome peak delta | Renderer peak delta | Renderer/file ratio |
| --- | ---: | ---: | ---: |
| Browser Blob | 739.5 MiB | 403.7 MiB | 1.35x |
| Service Worker stream | 35.3 MiB | 25.6 MiB | 0.08x |

## Real TianTianJieXi Result

One fresh ordinary TianTianJieXi MP4 from `v11-default.365yg.com` was used.

| Measurement | Result |
| --- | --- |
| Provider | `tiantianjiexi` |
| Expected bytes | 110,691,390 |
| Worker bytes | 110,691,390 |
| Final disk bytes | 110,691,390 |
| Worker stream duration | 50.193 s |
| Browser task before completion | `.crdownload` grew from 14,434,304 bytes through 109,543,424 bytes before rename |
| Browser console errors | 0 |
| Playback page | 0 |
| Blob / File System API / full buffering | 0 / 0 / 0 |
| AnimeImages media requests / bytes | 0 / 0 |

An earlier descriptor was delayed for several minutes by a browser-control
reconnection and failed with `UPSTREAM_FETCH_FAILED`. An immediately generated
fresh descriptor passed. This is evidence that URL liveness must be explicit
in any production descriptor design; it is not evidence for persisting or
automatically refreshing signed URLs in the Service Worker.

## Lifecycle Conclusion

Once Chrome owns the synthetic attachment response, the Service Worker fetch
event can outlive a page reload. The controlled refresh test proves that the
download manager continued to receive bytes and produced an exact file after
the originating page was replaced. The prototype does not claim survival
across browser shutdown, Service Worker process failure, or a deliberate
download cancel.

## Decision

The native Service Worker attachment-stream primitive is viable in Chrome on
macOS and is the recommended next architecture:

`SERVICE_WORKER_STREAMING`

It improves the two Blob limitations that matter most: download-manager task
creation happens before complete media acquisition, and memory does not scale
linearly with full file size. It also survived page refresh in the controlled
test.

The next task should productionize this as a separate, reviewable feature with:

1. A short-lived, signed, server-issued ordinary-media descriptor that exposes
   no Provider credential and can be consumed only once.
2. Shared Single/Batch launch and progress contracts.
3. Explicit signed-resource expiry handling and user-requested reparse.
4. Service Worker upgrade, scope, concurrency, cancellation, and failed-file UX.
5. Chrome acceptance first, then Safari/Firefox capability gates with no
   automatic ordinary-video Server Relay fallback.
6. Automated proof that ordinary media never reaches AnimeImages endpoints.

Until that independent task passes, production should continue using the
known-good Blob path. The research POC must never be deployed directly.
