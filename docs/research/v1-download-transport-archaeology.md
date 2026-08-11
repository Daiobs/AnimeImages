# V1 Download Transport Archaeology

Date: 2026-08-12

This document records only non-sensitive transport evidence. It intentionally
omits source URLs, signed CDN URLs, Provider credentials, cookies, tokens, and
private user data.

## Scope

The primary audit window starts with the TianTianJieXi integration chain on
2026-07-06 and ends with PR #23 on 2026-08-09. Intermediate PR commits and
surviving historical refs were inspected in addition to squash commits on
`main`.

Each implementation is classified by its real media data plane:

- `NATIVE_CDN_TO_BROWSER`: CDN response is handed directly to the browser's
  native download machinery.
- `BROWSER_FETCH_BLOB`: page JavaScript fetches the complete CDN response and
  saves an object URL.
- `SERVER_ATTACHMENT_TO_BROWSER_MANAGER`: AnimeImages reads the CDN body and
  serves an attachment response to the browser.
- `SERVER_ZIP`: AnimeImages creates or serves an archive.
- `MIXED_FALLBACK`: more than one of the above can be selected or one path
  automatically falls back to another.
- `UNKNOWN`: the data plane cannot be established from code and runtime
  evidence.

## Pre-TianTian Context

| Commit | Classification | Evidence |
| --- | --- | --- |
| `a66daf0` | `NATIVE_CDN_TO_BROWSER` intent | Signed KuKuTool videos were routed as browser-direct resources. |
| `7922c6a` | `BROWSER_FETCH_BLOB` | Direct routes used browser `fetch`, then File System Access when selected or Blob/object URL otherwise. |
| `a60105f` | `NATIVE_CDN_TO_BROWSER` intent | Single direct videos used an anchor handoff and no longer automatically fell back to server transfer. |
| `8d453b2` | `NATIVE_CDN_TO_BROWSER` intent | The direct-video fast path avoided pre-download work before the browser handoff. |
| `dae4d03` | `MIXED_FALLBACK` | KuKuTool single videos could retry through a server attachment. |
| `26bc31f` | `SERVER_ATTACHMENT_TO_BROWSER_MANAGER` | Native single videos were routed through the server attachment path. |
| `2cce720` | `SERVER_ATTACHMENT_TO_BROWSER_MANAGER` | PR #7 stabilized a hidden-frame browser-manager handoff whose media body was still served by AnimeImages. |

Names such as "browser managed" or "native browser" do not establish the data
plane. PR #7 is therefore not classified as CDN-to-browser.

## TianTian To PR #23 Timeline

| Time / commit | Classification | Effective TianTian behavior |
| --- | --- | --- |
| 2026-07-06 `0165561` through `0c353bf` | `SERVER_ATTACHMENT_TO_BROWSER_MANAGER` | TianTian parsing existed, but the route policy still treated only KuKuTool as browser-first. |
| 2026-07-06 `9699a94` | `NATIVE_CDN_TO_BROWSER` first-click path within `MIXED_FALLBACK` | Route policy changed TianTian to direct. The first click used a cross-origin anchor targeted at a hidden iframe with `download` and `referrerPolicy="no-referrer"`; a repeated click could use server single-file transfer. |
| 2026-07-06 `8a4dffa` | `MIXED_FALLBACK` | The same native handoff was followed automatically by server single-file transfer after 800 ms for TianTian and KuKuTool. |
| 2026-07-06 `a7adce5` | `MIXED_FALLBACK` with `BROWSER_FETCH_BLOB` primary | TianTian and KuKuTool videos up to 1 GiB used browser fetch and Blob/object URL first; failure automatically used server attachment. |
| 2026-07-07 `42304a9` (PR #10) | `MIXED_FALLBACK` with `BROWSER_FETCH_BLOB` primary | The merged TianTian Provider Strategy contained the Blob-first implementation, not the short-lived native-only first-click implementation. |
| 2026-07-07 `6abd299` and `7496ddf` | `BROWSER_FETCH_BLOB` for Batch | Batch added complete browser fetch, chunks, Blob/object URL save, and then visible in-page transfer progress. Failure handed the URL to an anchor. |
| 2026-07-07 through 2026-07-18 (`c732e72` to `faf71f6`) | `MIXED_FALLBACK` | Single remained Blob-first with server fallback. Batch remained browser Blob with a direct-anchor fallback. |
| 2026-07-23 `b79279f` | `MIXED_FALLBACK` | TianTian and KuKuTool were marked not browser-fetch-safe. Single moved to same-origin prepare/stream server attachment; Batch still used browser Blob. |
| 2026-07-24 `fec1db2` through `57c54ba` | `SERVER_ATTACHMENT_TO_BROWSER_MANAGER` for Single | Changes optimized and instrumented the single upstream server stream and browser handoff state. They did not restore a CDN-to-browser data plane. |
| 2026-08-09 `485951b` (PR #23) | `MIXED_FALLBACK` | Single TianTian used server prepare/stream attachment. Batch TianTian still used browser fetch/chunks/Blob. ZIP and generated output remained server paths. |

## Isolated Candidate Reproduction

Five clean detached worktrees were created and treated as read-only:

- `9699a94`: native Provider handoff boundary
- `8a4dffa`: automatic server-fallback boundary
- `42304a9`: merged TianTian Blob-first strategy
- `faf71f6`: last pre-reliability Single/Batch Blob period
- `485951b`: final PR #23 mixed Single-server/Batch-Blob baseline

Each historical route/strategy test passed in its own worktree. A single fresh
TianTian ordinary MP4 was reused for browser probes:

- CDN host: `v9-default.365yg.com`
- Expected bytes: `110,691,390`
- Descriptor SHA-256: `b7accd0d84c5057c7217d52ff9b82604ea9b4bf9dcac3198ac52ace9810c4799`

The exact native iframe/anchor primitive used at `9699a94` and `8a4dffa`
issued immediately but did not create a download task with the current CDN
response. It loaded the playable MP4 target instead. This matches the earlier
anchor, hidden-iframe, and redirect browser matrix.

The historical Blob fetch options used by `42304a9` and `faf71f6` currently
receive HTTP 403. Header isolation established that the CDN rejects the page
Referer; CORS itself is not the blocker. A fetch with identical semantics plus
`referrerPolicy: "no-referrer"` succeeds.

The `485951b` strategy test confirms TianTian is classified as observable
server attachment. The earlier production-like Gunicorn reproduction confirms
that this path transfers ordinary media through AnimeImages and can be cut off
by the 60-second worker timeout.

## Last Known Native CDN Browser Period

`LAST_KNOWN_NATIVE_CDN_BROWSER_PERIOD` is the PR #10 intermediate-code interval:

```text
9699a94 (2026-07-06 17:11 +0800)
through immediately before
8a4dffa (2026-07-06 17:25 +0800)
```

This is a code-history period, not a proven deployed `main` period. PR #10 was
squash-merged only after the Blob and automatic server-fallback commits were
already present. The native primitive is identifiable, but it cannot be
reproduced as a download task against today's CDN response.

Status:

`HISTORICAL_NATIVE_PATH_IDENTIFIED_BUT_EXTERNAL_BEHAVIOR_CHANGED`

## Browser Blob Recovery Proof

The same current TianTian sample completed a real Chrome macOS browser-side
fetch and Blob save when `referrerPolicy: "no-referrer"` was added:

- response: HTTP 200, CORS-readable body
- response acquisition: 483 ms
- first chunk: 485 ms
- full browser fetch: 5,279 ms
- object-URL handoff: 5,342 ms total
- received bytes: `110,691,390`
- Blob bytes: `110,691,390`
- saved file bytes: `110,691,390`
- playback navigation: none
- browser console errors: zero
- AnimeImages media requests: zero
- AnimeImages media bytes: zero

Chrome automation could not inspect the internal Downloads UI, but the exact
file was created in the browser download directory by the object-URL path. The
saved file size exactly matched both the CDN Content-Length and accumulated
browser bytes.

Status:

`TIANTIAN_BROWSER_BLOB_RECOVERY_PASS`

## Recovery Decision

Browser Blob is approved as Recovery v1 for ordinary TianTian and KuKuTool
media. It is not declared the permanent large-file architecture. The minimum
product implementation must:

- add `referrerPolicy: "no-referrer"` to the browser CDN fetch;
- share one ordinary-media primitive between Single and Batch;
- never automatically fall back to Server Relay for ordinary media;
- keep generated YouTube merges, ZIPs, and other server-generated outputs as a
  separate server-required class;
- keep AnimeImages ordinary media requests and bytes at zero;
- report generic user states without transport terminology.

Backlog: `BROWSER_BLOB_LARGE_FILE_LIMITATION`. Recovery validation covers the
100 MiB class. Memory behavior for 300 MiB and 500 MiB+ files must be measured
or conservatively bounded in a later streaming-direct design task.
