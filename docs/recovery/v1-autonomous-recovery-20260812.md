# V1 Autonomous Recovery Log - 2026-08-12

This log records only non-sensitive recovery evidence. Source URLs, signed media
URLs, provider credentials, cookies, tokens, passwords, and private user data
must not be added.

## Immutable Start State

- Production baseline: `485951b1f962c4279ef9b78903f0170e05baae5c`
- Autonomy start and telemetry checkpoint: `bbbf87b24c7d5fe40d9eaf9aa36eff3cb3992791`
- GitHub main at start: `d98b3098bd55031c859c125e44c5d84b1f458f17`
- Immutable remote checkpoint: `checkpoint/v1-autonomy-start-20260812`
- Recovery branch: `stabilize/v1-autonomous-recovery-20260812`
- PR #32 remains an unchanged Draft research reference.

Production is intentionally pinned to the confirmed V1 baseline while the
recovery branch is validated independently. GitHub main is historical input,
not the deployment source for this recovery phase.

## Product Contract

Ordinary complete media must follow:

```text
Provider/CDN -> user browser -> user disk
```

AnimeImages remains the parsing, quality-selection, policy, and handoff control
plane. Ordinary media must not use AnimeImages as a media relay. Server-required
outputs, such as a generated YouTube audio/video merge or ZIP, remain a separate
class.

Recovery v1 may use browser `fetch`, a complete browser-side Blob, an object
URL, and an anchor save. This is a user-browser data path: AnimeImages ordinary
media requests and bytes remain zero. A failed ordinary browser download may be
retried or reparsed, but it must not automatically fall back to Server Relay.
File System Access and Service Worker designs remain research-only alternatives.

## Pre-Recovery Evidence

### V1 server-relay isolation

- Sample provider: TianTianJieXi
- Sample CDN host: `v5-se-gddgtc-default.365yg.com`
- Expected media size: `110,691,390` bytes
- Gunicorn worker type: sync, two workers, 60-second timeout
- Chrome hidden-iframe request: terminated at about 60.5 seconds after yielding
  `6,815,744` bytes; no completed browser file
- Curl full-consumer control: terminated at about 60.6 seconds after receiving
  `2,883,827` bytes; curl exit code 18
- Server telemetry for curl: `actual_yielded_bytes=2,883,584`, close reason
  `stream_exception`, exception type `SystemExit`
- Gunicorn logged `WORKER TIMEOUT` while the worker was reading the upstream TLS
  socket.

Decision: the ordinary-media recovery must remove the Server Relay dependency.
Increasing Gunicorn timeout is not the P0 ordinary-download fix. The telemetry
checkpoint remains useful for genuinely server-required outputs.

## Milestones

| Milestone | Status | Commit | Production | Evidence |
| --- | --- | --- | --- | --- |
| M0 autonomy checkpoint | complete | `bbbf87b` | unchanged at `485951b` | checkpoint branch pushed; production readiness 200 |
| M1 TianTian browser recovery | candidate implemented | `530d0cc` | not deployed | 110,691,390-byte Chrome Blob save passed with zero AnimeImages media bytes; product-flow Chrome gate pending |
| M2 Single/Batch unified direct download | candidate implemented | `530d0cc` | not deployed | shared bounded ordinary download primitive; Django 967/967 and focused Node 14/14 passed |
| M3 YouTube verification | pending | pending | not deployed | server-required merge remains separate |
| M4 Batch/Admin clarity | pending | pending | not deployed | begins only after ordinary download stability |

## Recovery Decisions

1. Do not merge or modify PR #32 during recovery.
2. Do not merge the recovery branch into GitHub main during the stabilization
   phase.
3. Every deployable candidate must be committed and pushed before a canary or
   production rollout.
4. A browser handoff is successful only when the complete file lands on disk
   without an AnimeImages media-body request or playback-page result.
5. Browser Blob is permitted for Recovery v1. File System Access, Service
   Worker downloaders, and ordinary-media Server Relay remain outside this
   recovery path.
6. Single and Batch must use the same ordinary-media browser primitive.

## Next Gate

Run the shared implementation through real Single and Batch product flows, then
through an isolated Linux candidate. Preserve server paths only for generated
outputs such as YouTube merges and ZIPs. Production rollout is allowed only
after exact-file, zero-server-media, and no-playback gates pass.

## P0 Provider And Browser-Native Feasibility Audit

Date: 2026-08-12

### Provider payload

A fresh TianTianJieXi parse of the established Douyin test item completed in
about 2.7 seconds and returned six URL fields:

- one parent `videos[0].url`
- five nested `videos[0].streams[].url` entries
- no `download_url`, `play_url`, `video_url`, `source`, or `original` field

The selected original-quality stream was `110,691,390` bytes on
`v11-default.365yg.com`. The normalizer is not choosing a generic playback field
over a distinct download field for this response; no distinct download field
exists.

### CDN response

Both a safe header probe and the real Chrome response showed:

- status `200`
- `Content-Type: video/mp4`
- `Content-Length: 110691390`
- `Access-Control-Allow-Origin: *`
- byte ranges supported
- no `Content-Disposition` header

The URL is fetch-capable and playable, but the response does not instruct the
browser to treat cross-origin navigation as an attachment.

### Official provider behavior

The public TianTianJieXi Batch page uses a direct cross-origin anchor with
`target="_blank"` and a `download` attribute. Its own FAQ documents that a click
can open the video page and suggests Alt-click, Save Link As, or Save Video As.
No separate download endpoint or stronger browser handoff was present in the
observed API payload or public page implementation.

### Real Chrome matrix

| Primitive | Browser result | Download task | Final file |
| --- | --- | --- | --- |
| Cross-origin anchor with `download`, no target | navigated to CDN video document | no | no |
| Historical hidden-iframe target | loaded CDN `video/mp4` as an iframe document | no | no |
| Same-origin zero-byte `302` with attachment header, then CDN | followed redirect to CDN video document | no | no |

All three probes kept AnimeImages media bytes at zero, but none satisfied the
required browser Download Manager gate.

### Historical implementation audit

The repository history does contain the remembered direct-plus-compatible
download period, but it does not contain a previously proven implementation of
the current ordinary-download contract:

| Commit | Historical behavior | Contract result |
| --- | --- | --- |
| `7922c6a` | browser `fetch`, then File System Access or Blob/object URL | excluded; custom browser-side transfer |
| `a60105f` / `03f3c8a` | cross-origin anchor with `download` and `_blank` | same primitive that currently opens playback; no real download-task proof |
| `8a4dffa` | hidden-iframe direct handoff, then automatic server-single fallback | direct handoff is unproven and fallback transfers media through AnimeImages |
| `a7adce5` | Provider browser Blob path, then server-single fallback | excluded Blob and relay paths |
| `2cce720` / `485951b` | hidden-iframe server attachment plus direct retry behavior | ordinary path can depend on AnimeImages media transfer |
| `e485fa9` | capability flag marks TianTian as native-handoff safe | tests prove routing metadata, not a real browser Download Manager task |
| `23bcd9e` | pre-opened tab navigates to the Provider resource | explicitly reports `downloadStarted=false`; playback/manual save behavior |
| `d93977e` | fetch body streamed to File System Access | zero server media bytes, but explicitly excluded from the final contract |

The historical test suites checked route classification, DOM invocation, and
zero server reads. They did not assert a real Chrome download task and complete
file. The remembered compatibility came from combining several transport
paths, not from a lost standard-browser primitive that can be restored as-is.

### Superseded contract blocker

With the currently returned CDN response, a normal web page cannot force a
cross-origin playable MP4 navigation into a native attachment task. The first
recovery contract excluded every browser-side mechanism that could turn the
fetchable response into a saved file. That constraint was later corrected.

- an upstream URL/endpoint that returns `Content-Disposition: attachment`
- Browser fetch followed by Blob/object-URL save
- Browser fetch streamed through File System Access
- a Service Worker/custom downloader
- a browser extension or manual browser save action
- an AnimeImages media relay that supplies attachment semantics

The product owner explicitly approved browser fetch plus Blob/object-URL save as
Recovery v1 while retaining the hard requirement that AnimeImages ordinary
media bytes remain zero. No product code, deployment, runtime configuration,
database, systemd, Nginx, or environment change was made during the blocker
audit.

### Decision

Historical status: `STOP_AND_REPORT_CORE_CONTRACT_DECISION_REQUIRED`

This status is superseded by the approved Browser Blob recovery contract.

## Historical Download Transport Archaeology

The detailed evidence and classification table are recorded in
`docs/research/v1-download-transport-archaeology.md`.

Five detached historical worktrees were audited at `9699a94`, `8a4dffa`,
`42304a9`, `faf71f6`, and `485951b`. Their focused route/strategy tests passed
and confirmed the following boundaries:

- `9699a94`: TianTian first-click CDN anchor/hidden-iframe handoff
- `8a4dffa`: the same handoff followed by automatic server fallback
- `42304a9`: merged TianTian Blob-first strategy with server fallback
- `faf71f6`: Single Blob/fallback plus Batch Blob
- `485951b`: Single server attachment plus Batch Blob

The last identifiable TianTian native-CDN code interval is `9699a94` through
immediately before `8a4dffa`. It was an intermediate PR #10 interval, not a
proven deployed `main` version. Its exact no-Referer anchor/iframe primitive no
longer creates a download task against the current playable CDN response.

Status:

`HISTORICAL_NATIVE_PATH_IDENTIFIED_BUT_EXTERNAL_BEHAVIOR_CHANGED`

## TianTian Browser Blob Hard Proof

A fresh TianTian ordinary MP4 was reused without exposing its source or signed
URL:

- CDN host: `v9-default.365yg.com`
- expected bytes: `110,691,390`
- safe descriptor SHA-256:
  `b7accd0d84c5057c7217d52ff9b82604ea9b4bf9dcac3198ac52ace9810c4799`

The historical fetch options received HTTP 403. Header isolation proved the CDN
rejects the page Referer; it does not reject CORS in general. Adding
`referrerPolicy: "no-referrer"` produced this real Chrome macOS result:

- HTTP 200 and a CORS-readable response body
- response acquisition in 483 ms
- first chunk in 485 ms
- complete browser fetch in 5,279 ms
- object-URL handoff in 5,342 ms total
- received, Blob, and saved bytes all exactly `110,691,390`
- no playback navigation
- zero browser console errors
- zero AnimeImages media requests and bytes

Chrome automation could not inspect the internal Downloads UI, but the complete
file appeared in the browser download directory and exactly matched both the
CDN Content-Length and browser byte accumulator.

Status:

`TIANTIAN_BROWSER_BLOB_RECOVERY_PASS`

Decision: proceed with a shared Single/Batch Browser Blob Recovery v1 primitive.
Do not automatically use Server Relay for ordinary media. Preserve
server-required paths for generated output only. Track
`BROWSER_BLOB_LARGE_FILE_LIMITATION` for 300 MiB and 500 MiB+ memory behavior;
the 100 MiB class is the current recovery gate.

## Shared Ordinary Download Candidate

Commit `530d0cc` implements the recovery primitive and is pushed to
`origin/stabilize/v1-autonomous-recovery-20260812`.

- `static/js/ordinary_media_download.js` is the only ordinary TianTianJieXi /
  KuKuTool browser-media implementation used by Single and Batch.
- It sends a credential-free, no-cache, no-Referer request, accepts only the
  declared ordinary-video content classes, enforces the configured 1 GiB Blob
  ceiling while reading even when `Content-Length` is absent, verifies exact
  response length when supplied, and saves through an object URL without a
  playback target.
- The server route validates the Provider manifest and capability, reports zero
  server egress, and blocks explicit proxy/server-single attempts for ordinary
  Provider videos. The prepare endpoint therefore cannot mint a stream ticket
  for that resource class.
- Browser failure stops with retry/reparse guidance. No automatic relay, ZIP,
  prepare, stream, or status request is permitted for the ordinary path.
- The Batch-only chunk progress UI was removed from this path and documented in
  `docs/research/v1-batch-blob-progress-archive.md` for possible reuse by work
  AnimeImages actually owns, such as ZIP or YouTube merge jobs.
- Automated candidate evidence: `xhs_download` 967/967, Provider Strategy
  118/118, and the shared primitive 14/14. A final full gate follows the
  documentation commit and real product-flow browser acceptance.

The completed 110,691,390-byte proof satisfies the 100 MiB memory gate. No 300
MiB or 500 MiB+ claim has been made; those sizes remain explicitly unverified
under `BROWSER_BLOB_LARGE_FILE_LIMITATION`.
