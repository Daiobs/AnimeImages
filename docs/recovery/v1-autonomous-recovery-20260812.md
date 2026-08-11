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
Provider/CDN -> browser native download task -> user disk
```

AnimeImages remains the parsing, quality-selection, policy, and handoff control
plane. Ordinary media must not use AnimeImages as a media relay. Server-required
outputs, such as a generated YouTube audio/video merge or ZIP, remain a separate
class.

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
| M1 TianTian native download | blocked | pending | not deployed | CDN lacks attachment semantics; all allowed standard primitives opened a video document instead of a download task |
| M2 Single/Batch unified direct download | pending | pending | not deployed | shared ordinary download primitive |
| M3 YouTube verification | pending | pending | not deployed | server-required merge remains separate |
| M4 Batch/Admin clarity | pending | pending | not deployed | begins only after ordinary download stability |

## Recovery Decisions

1. Do not merge or modify PR #32 during recovery.
2. Do not merge the recovery branch into GitHub main during the stabilization
   phase.
3. Every deployable candidate must be committed and pushed before a canary or
   production rollout.
4. A browser handoff is successful only when the browser creates a standard
   download task and the complete file lands on disk.
5. Playback tabs, Blob downloads, File System Access, Server Relay, and frontend
   progress completion are not substitutes for the ordinary download contract.

## Next Gate

Audit TianTianJieXi provider payload selection and historical native-download
implementations. Prove which provider field represents a download-capable URL
before changing the browser handoff.

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

### Contract blocker

With the currently returned CDN response, a normal web page cannot force a
cross-origin playable MP4 into a standard browser download task. Meeting that
goal requires at least one capability currently excluded by the recovery
contract:

- an upstream URL/endpoint that returns `Content-Disposition: attachment`
- Browser fetch followed by Blob/object-URL save
- Browser fetch streamed through File System Access
- a Service Worker/custom downloader
- a browser extension or manual browser save action
- an AnimeImages media relay that supplies attachment semantics

Implementing any of these without an explicit contract decision would redefine
the P0 requirement rather than satisfy it. No product code, deployment, runtime
configuration, database, systemd, Nginx, or environment change was made.

### Decision

Status: `STOP_AND_REPORT_CORE_CONTRACT_DECISION_REQUIRED`

Production remains on the known-good V1 baseline. M1 is blocked pending a
product decision about which browser capability may replace the unavailable
upstream attachment response. Batch unification and production canary/deploy
remain deferred because their prerequisite did not pass.
