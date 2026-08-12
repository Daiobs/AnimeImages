# Historical Download Transport Timeline

Date: 2026-08-12

This timeline classifies AnimeImages download behavior by the media data plane,
not by UI copy, commit titles, or the mere appearance of Chrome's download
manager. It contains no source URL, signed CDN URL, Provider credential, cookie,
manifest token, or private user data.

## Classification Rules

- `NATIVE_CDN_BROWSER`: the browser download manager owns a request directly to
  the Provider/CDN before the complete media body has arrived.
- `BROWSER_FETCH_BLOB`: page JavaScript fetches the Provider/CDN body and creates
  a Blob/object URL before asking the browser to save it.
- `SERVER_ATTACHMENT_BROWSER_MANAGER`: the browser download manager receives an
  attachment from AnimeImages; AnimeImages remains in the media data plane.
- `SERVER_ZIP`: AnimeImages creates or streams an archive.
- `SERVER_GENERATED_OUTPUT`: AnimeImages must create a new output, such as a
  merged YouTube file.
- `MIXED_FALLBACK`: more than one transport can be selected or one transport
  automatically falls back to another.
- `PLAYBACK_HANDOFF`: a Provider/CDN navigation opens playable media rather than
  creating a download task.
- `UNKNOWN`: retained evidence cannot establish the data plane.

Evidence levels used below are:

- `CODE_ONLY`
- `HISTORICAL_TEST_EVIDENCE`
- `CURRENT_RUNTIME_REPRODUCTION`
- `CURRENT_BROWSER_NETWORK_PROOF`
- `PRODUCTION_ACCEPTANCE`

`Download Manager = intended` is not a runtime pass. A true native pass also
requires a CDN request, zero AnimeImages media bytes, early task creation, a
complete final file, and no Blob, server relay, or playback page.

## Timeline

| Time / commit | Provider | Single path | Batch path | Browser behavior | Media request host | Server media bytes | Blob | Server relay | Download Manager | Fallback | Classification / evidence |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 2026-07-02 10:53 `a66daf0` | KuKuTool | `route_only` returns the signed CDN item; JS clicks a cross-origin `<a download target="_blank">` | No dedicated Batch downloader; multi-item work remains server ZIP | Intended native handoff | Provider/CDN if honored | Intended 0 | No on direct branch | Other routes can use server attachment/ZIP | Intended, not proved | Route-dependent | Native intent only; `CODE_ONLY` |
| 2026-07-02 10:59 `7922c6a` | KuKuTool and direct items | Browser `fetch`; File System Access when selected, otherwise complete Blob/object URL | No dedicated Batch flow | In-page progress, then save | Provider/CDN | 0 on direct branch | Yes unless File System Access | No on successful direct branch | Starts after Blob completion | Later commits add recovery | `BROWSER_FETCH_BLOB`; `HISTORICAL_TEST_EVIDENCE` |
| 2026-07-02 11:21 `926c056` | Direct single video | Browser fetch first | No dedicated Batch flow | Blob/FS save on success | Provider/CDN | 0 on success, positive on fallback | Yes | Yes after direct failure | Depends on selected branch | Automatic server single fallback | `MIXED_FALLBACK`; `CODE_ONLY` |
| 2026-07-02 12:30 `a60105f` | Direct single video | Browser fetch/FS/Blob remains primary; failed fetch hands the raw URL to `_blank` instead of server fallback | No dedicated Batch flow | Blob save, or raw navigation on failure | Provider/CDN | 0 on this direct branch | Primary | Removed from this failure path | Only intended for raw fallback | Playback handoff possible | `MIXED_FALLBACK`, not native-primary; `CURRENT_RUNTIME_REPRODUCTION` |
| 2026-07-02 13:00 `03f3c8a` | Same direct contract | Keeps the `a60105f` product path | No dedicated Batch flow | Same as above | Provider/CDN on direct branch | 0 on direct branch | Primary | Other route classes remain | Not proved | Route-dependent | Production baseline tag/code; no retained native runtime proof |
| 2026-07-02 21:41 `8d453b2` | Single direct video, including eligible KuKuTool | Bypasses browser fetch and immediately clicks `<a download target="_blank">` | No dedicated Batch flow | Intended early native task | Provider/CDN | Intended 0 | No on the single-video fast path | Explicit modes remain | Intended, not proved | None on first fast path | Strong pre-TianTian native candidate; `CURRENT_BROWSER_NETWORK_PROOF` now yields playback |
| 2026-07-02 21:51 `1cdc2c6` | Same | First click targets a hidden no-referrer iframe; repeated click switches to `server_single` | No dedicated Batch flow | Intended native first click plus explicit compatibility retry | CDN first, AnimeImages on retry | 0 first, positive on retry | No | Manual second-click relay | Intended first click | Manual server fallback | `MIXED_FALLBACK`; closest code match to the remembered direct/compatibility period |
| 2026-07-02 21:58 `dae4d03` | KuKuTool | KuKuTool is routed to server single attachment | No dedicated Batch flow | Browser manager can show real progress, but from AnimeImages | AnimeImages | Positive | No | Yes | Yes | Server recovery | `SERVER_ATTACHMENT_BROWSER_MANAGER`; `HISTORICAL_TEST_EVIDENCE` |
| 2026-07-02 22:04 `26bc31f` | Native and Provider single videos | Remaining native single videos also move to server single attachment | No dedicated Batch flow | Browser manager receives AnimeImages attachment | AnimeImages | Positive | No | Yes | Yes | Server-side route handling | `SERVER_ATTACHMENT_BROWSER_MANAGER`; `HISTORICAL_TEST_EVIDENCE` |
| 2026-07-02 22:49 `3c18fb4` | All then-current routes | Merge tree equals `2f34e8f`, so it contains the server-attachment outcome, not the earlier `8d453b2` tree | No dedicated Batch flow | Browser-manager attachment | AnimeImages | Positive | No | Yes | Yes | Server route | Production reflog shows checkout; `CURRENT_RUNTIME_REPRODUCTION` of Git state |
| 2026-07-04 `2cce720` (PR #7) | KuKuTool/native single | Same-origin form submits `server_single_attachment`; Django streams an attachment | No dedicated Batch flow | Chrome download manager starts and shows transfer progress | AnimeImages | Positive | No for single attachment | Yes | Yes | Signed-resource server recovery | Commit title says “native”, but data plane is `SERVER_ATTACHMENT_BROWSER_MANAGER`; PR body confirms this |
| 2026-07-06 `0165561` through `0c353bf` | TianTian added for parsing | TianTian is in `SERVER_PROXY_VIDEO_PROVIDERS`; `BROWSER_FIRST_VIDEO_PROVIDERS` still contains only KuKuTool and the setting default is `server_stream` | Provider-strategy Batch not yet final | Existing server attachment UX | AnimeImages | Positive | No for TianTian | Yes | Yes | Existing Provider recovery | TianTian initially changes parse/quality, not transport; `CODE_ONLY` plus PR commit graph |
| 2026-07-06 17:11 `9699a94` | TianTian and KuKuTool | Adds TianTian to the browser-first set and changes default/runtime migration to `browser_first`; hidden iframe first click, server single on repeated click | Not yet final | Intended CDN-native first click | CDN first, AnimeImages on retry | 0 first, positive on retry | No | Manual fallback | Intended, not proved | Manual second click | Strongest TianTian native candidate; `CURRENT_BROWSER_NETWORK_PROOF` now shows a playable iframe document |
| 2026-07-06 17:25 `8a4dffa` | TianTian and KuKuTool | Same raw handoff, then automatic server single after 800 ms | Not yet final | Possible native navigation followed by relay | CDN then AnimeImages | Positive once fallback starts | No | Yes | Intended, not proved | Automatic 800 ms fallback | `MIXED_FALLBACK`; double-transfer risk by design |
| 2026-07-06 17:44 `a7adce5` | TianTian and KuKuTool | Browser fetch/Blob becomes primary up to 1 GiB; failure submits server single | Not yet final | In-page transfer, then browser save | CDN on success, AnimeImages on fallback | 0 on success, positive on fallback | Yes | Yes | Starts after Blob completion | Automatic server fallback | `MIXED_FALLBACK` with `BROWSER_FETCH_BLOB` primary |
| 2026-07-07 `42304a9` (PR #10) | TianTian and KuKuTool | `download_strategy=browser_blob`, `fallback_strategy=server_attachment`; `directRouteCanUseProviderBlobDownload()` runs before native handoff | Batch added on the branch, then refined in following commits | Existing “download” interaction remains, but full browser fetch occurs first | CDN on Blob success, AnimeImages on fallback | 0 or positive by outcome | Yes | Yes | After Blob completion or on server attachment | Automatic server attachment | Final TianTian integration is Blob-first, not the short native intermediate; `HISTORICAL_TEST_EVIDENCE` |
| 2026-07-07 `a0b76c7` / `7496ddf` | TianTian Batch | Single remains PR #10 mixed path | Complete browser fetch with visible site progress, then Blob save; direct-anchor fallback | Site progress before browser save | CDN on Blob success | 0 on success | Yes | Single fallback remains | After Blob completion | Batch raw-anchor fallback | `BROWSER_FETCH_BLOB` for Batch |
| 2026-07-07 through 2026-07-18 `c732e72` to `faf71f6` | TianTian/KuKuTool | Browser Blob primary plus server attachment fallback | Browser Blob plus raw-anchor fallback | Stable mixed UX | CDN or AnimeImages by branch | 0 or positive | Yes | Yes for Single fallback | Branch-dependent | Mixed | `MIXED_FALLBACK`; no later native-first code boundary found |
| 2026-07-23 `b79279f` | TianTian/KuKuTool | Marks these Providers not browser-fetch-safe; Single moves to prepare/stream server attachment | Batch retains browser Blob | Single native manager from AnimeImages; Batch in-page progress | AnimeImages for Single, CDN for Batch | Positive Single, 0 Batch | Batch only | Yes for Single | Yes for Single attachment | Prepared stream recovery | Mixed data plane |
| 2026-08-09 `485951b` (PR #23) | TianTian/KuKuTool | Local-only prepare, same-origin stream, bounded status/recovery | Browser fetch/chunks/Blob for eligible direct items | Single browser manager; Batch site progress then save | AnimeImages Single, CDN Batch | Positive Single, 0 Batch | Batch | Yes for Single | Yes for Single | Controlled signed-resource recovery | `MIXED_FALLBACK`; `HISTORICAL_TEST_EVIDENCE` |
| 2026-08-10 `e485fa9` (PR #29) and `23bcd9e` (PR #31) | Validated Provider video | Restores raw Provider/CDN handoff intent, retains bounded relay fallback and later launch fixes | Uses same route contract | UI reports browser-managed handoff | Intended CDN, relay when selected | 0 or positive | Bounded small-file path only | Yes | Not proved by acceptance | Relay fallback | PR #29 recorded `DIRECT_DATA_PATH_PASS`, not a download-manager hard-gate pass; later playback regression showed the distinction |
| 2026-08-12 `530d0cc` / `a5e0254` | TianTian/KuKuTool ordinary MP4 | Shared browser fetch with no referrer, complete Blob/object URL save | Same shared ordinary-media primitive | Browser save begins after complete fetch | Provider/CDN | 0 | Yes | No ordinary-video relay fallback | After Blob completion | Reparse/retry only | `BROWSER_FETCH_BLOB`; `PRODUCTION_ACCEPTANCE`, current known-good production |

## Native Candidate Windows

Two short code windows contain a true CDN-native *intent*:

1. Pre-TianTian: `8d453b2` (2026-07-02 21:41 +08:00) through immediately
   before `1cdc2c6` (21:51) for anchor-first with no compatibility retry.
2. TianTian: `9699a94` (2026-07-06 17:11 +08:00) through immediately
   before `8a4dffa` (17:25) for iframe-first with only an explicit second-click
   server fallback.

The first branch was eventually merged through `3c18fb4`, but that merge tree
equals the later server-attachment tree. Production reflog shows `8d453b2` was
fetched as a branch ref while production checked out `3c18fb4`; it does not show
the exact `8d453b2` tree serving traffic. The TianTian native commit was an
intermediate PR #10 child; PR #10 merged only after automatic fallback and Blob
commits. Production fetched the intermediate refs, but retained evidence does
not prove that `9699a94` itself served production traffic.

Therefore:

```text
LAST_KNOWN_NATIVE_CDN_BROWSER_PERIOD = 9699a94 .. before 8a4dffa
DEPLOYMENT_HISTORY = UNKNOWN_FOR_EXACT_NATIVE_TREE
```

This identifies the historical primitive without upgrading code intent into a
claim of historical production success.

## Why TianTian Did Not Feel Like A New Downloader

The PR #10 child history explains the continuity:

1. `0165561` through `0c353bf` added parsing, original-stream selection, and UI
   labels while TianTian still used the existing server attachment route.
2. `9699a94` briefly extended browser-first handoff to TianTian.
3. `8a4dffa` added automatic server fallback 14 minutes later.
4. `a7adce5` made browser fetch/Blob primary 19 minutes after that.
5. The final PR #10 tree (`42304a9`) shipped Blob-first with server fallback.

At final integration, `directRouteCanUseProviderBlobDownload()` was evaluated
before `handoffDirectRouteItemsToBrowser()`. TianTian assets carried a signed
Provider manifest and `download_strategy=browser_blob` with
`fallback_strategy=server_attachment`. Thus TianTian mainly changed which
upstream supplied the ultra-HD asset; the visible parse-card and download
interaction remained familiar.

## URL Semantics

Historical code and fixtures for both KuKuTool and TianTian already use the
`365yg.com`/`douyinvod.com` media host families, `video/tos/...`-style resource
paths, and Provider fields such as `original_video_url`, `play_url`, or the
KuKuTool `video_fullinfo.url`. No retained code or fixture proves a distinct
attachment endpoint, `download=1`, octet-stream endpoint, filename parameter,
or same-origin redirector.

The current sample also uses a `365yg.com` host and a signed media resource
path. This establishes broad contract continuity, not byte-for-byte URL
identity. Historical response headers and redirect chains were not retained,
so changes in CDN response policy remain possible but unproved.

## Bottom Line

- A historical native-intent primitive unquestionably existed in code.
- A historical production `NATIVE_CDN_BROWSER_PASS` is not independently
  provable from the retained evidence.
- Current Chrome reproductions of both historical native primitives load the
  MP4 as a document and create no download task.
- The current CDN response is `video/mp4`, has no
  `Content-Disposition: attachment`, and does not redirect.
- PR #7 and PR #23 demonstrate why “browser download manager” cannot identify
  the data plane: both are server attachment paths.
- Current production must remain on the verified Blob recovery until a future
  native candidate passes every hard gate.
