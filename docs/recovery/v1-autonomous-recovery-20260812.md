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
| M1 TianTian native download | in progress | pending | not deployed | requires >=100 MiB real browser download and zero AnimeImages media bytes |
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
