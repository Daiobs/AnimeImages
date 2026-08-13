# Production Behavior Baseline v1

Date: 2026-08-14

This document is the behavior manifest for the reconciliation baseline. A pull
request that changes an item below must label it `BEHAVIOR_CHANGE` and explain
user impact, runtime configuration impact, verification and rollback.

## Source Of Truth

```text
Deployed code checkpoint: checkpoint/production-douyin-ultra-retry-v1
Deployed code SHA:        87afba321a2dc459b7daebcd1cc9eea6735b60ba
Documentation head:       8b81041059839691b08c472b0a4ad9586431ec4c
```

Production environment values and database runtime settings are intentionally
external to Git. This manifest records behavior, not credentials or a copy of
production configuration.

## Douyin Ultra

- A fresh Ultra attempt starts TianTianJieXi, KuKuTool and Native at T=0.
- Native remains hidden during the six-second foreground window.
- A strong Ultra result that completes within the foreground window is the
  first visible result.
- After the window, a prepared Native result may be displayed while bounded
  continuation observes the already-started external attempts.
- A late strong result upgrades the same result; continuation does not submit a
  new Provider attempt.
- The production user allowance is 10 fresh Ultra attempts per minute.
- The tenth fresh attempt is allowed and the eleventh is rejected.
- Payload-cache hits, failure-cache/cooldown hits, pending single-flight waits,
  continuation attachment/polls, Native fallback and automatic upgrades do not
  consume another fresh attempt.
- TianTianJieXi and KuKuTool transient cooldown targets are eight seconds.
- The default order remains TianTianJieXi, KuKuTool, Native. Runtime gates may
  disable an unavailable Provider without changing the order contract.

## Ordinary Media Download

The ordinary TianTianJieXi/KuKuTool data plane is:

```text
Provider/CDN -> Browser Service Worker -> Browser Download Manager -> Disk
```

- `BROWSER_STREAMING` is the default for validated ordinary Provider video.
- `BROWSER_BLOB` is a capability fallback only before Streaming starts.
- After an upstream fetch or browser download task starts, Blob fallback is
  forbidden to avoid duplicate requests/files.
- Ordinary media never falls back to AnimeImages Server Relay.
- The ordinary path does not call `/media/download-prepare/`,
  `/media/download-stream/` or `/media/download-status/`.
- The service worker streams data and does not buffer the complete media body.
- Refresh reattaches to the opaque active download ID without reparsing or
  restarting the CDN request.
- Site cancellation and Browser Download Manager cancellation propagate to the
  upstream fetch.
- Single and Batch use the same JavaScript transport primitive.
- Signed media URLs are not written to logs, browser storage, documentation or
  reports. They are necessarily visible to the receiving browser at handoff.
- Ordinary AnimeImages media request count and media bytes remain zero.

## Server-Required Output

- YouTube 1080p+ may require separate video/audio acquisition and ffmpeg merge.
- ZIP archives, generated media and resources that require server-held
  Cookie/header context are separate server-required output classes.
- These paths are not fallbacks for an ordinary Streaming-capable video.

## Provider Lifecycles

Three clocks are independent:

1. A successful TianTianJieXi payload is cached for 120 seconds by default.
2. AnimeImages Provider manifests are signed and expire after 3600 seconds by
   default.
3. CDN URL lifetime is Provider-controlled and remains
   `CDN_URL_LIFETIME_UNKNOWN` unless separately proven.

A successful parse can be a payload-cache hit. It does not prove a new upstream
request or a newly issued CDN URL. Failure caches and single-flight coordination
must not be described as successful payload reuse.

## Runtime Gates And Concurrency

- Environment configuration supplies capability and safe defaults.
- Database/Admin runtime settings control live availability and rollout when a
  persisted setting exists.
- Explicit production environment values may override code defaults according
  to the documented setting precedence.
- Production Gunicorn topology is 2 workers x 4 gthreads.
- The TianTianJieXi effective concurrency intent is two, not the historical
  fixed-at-one lease from PR #30.
- Provider-global limits remain separate from per-user fresh-attempt limits.

## Batch

- Batch page/API access is controlled by `BatchParseRuntimeSetting.access_mode`.
- Staff-only raw-link copy/open/export and strategy diagnostics remain
  staff-only.
- A public user allowed by the Batch runtime gate may use the validated shared
  download action after the reconciled main is deliberately deployed.
- Batch Provider order, per-link deadlines, response boundary and progressive
  protocol are unchanged by the access/Admin reconciliation.

The final bullet above is a main-only retained capability from PR #26 and is not
evidence that production at `87afba3` already exposes it.

## Production And Main

- `main` is the canonical development baseline after reconciliation.
- The exact production SHA remains the deployment fact until a separate deploy.
- Merging main does not imply production deployment.
- A production hotfix that is not in main is `PRODUCTION_CODE_DRIFT` and is a P0
  governance item until reconciled.

