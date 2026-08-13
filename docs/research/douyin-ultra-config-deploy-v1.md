# Douyin Ultra Production Config Alignment And Deploy v1

Date: 2026-08-13

This report intentionally contains no source URL, signed media URL, Provider
credential, cookie, token, or request/response body.

## Status

```text
DOUYIN_ULTRA_RETRY_PRODUCTION_STABLE
```

| Item | Value |
| --- | --- |
| Previous production SHA | `5af0892bc7f01ec73a5a3c1ba1b55814364e5e6a` |
| Deployed production SHA | `87afba321a2dc459b7daebcd1cc9eea6735b60ba` |
| Production checkpoint | `checkpoint/production-douyin-ultra-retry-v1` |
| Production branch | `feat/douyin-ultra-retry-continuation-v1` |
| Rollback performed | No |
| Migration | None |

## Configuration Transaction

The production environment was backed up before any write. Each target key had
at most one active definition. The five keys were updated atomically, preserving
the original `ubuntu:ubuntu` ownership and mode `0600`. The Provider Runtime
model was updated through Django ORM inside a database transaction. Redis was
not flushed.

| Setting | Before | Effective after |
| --- | ---: | ---: |
| `DOUYIN_ULTRA_TOTAL_TIMEOUT_SECONDS` | 4.0 | 6.0 |
| `ULTRA_HD_RATE_LIMIT_PER_MINUTE` | 2 | 10 |
| `THIRD_PARTY_RATE_LIMIT_PER_MINUTE` | 2 | 10 |
| `TIANTIANJIEXI_FAILURE_CACHE_TTL_SECONDS` | Missing | 8 |
| `KUKUTOOL_FAILURE_CACHE_TTL_SECONDS` | 15 | 8 |
| Runtime `douyin_ultra_total_timeout_seconds` | 5.0 | 6.0 |

The environment SHA-256 changed from
`3469959c65aa709ff02658c863cffaf5c84000d3dca335fbaee92d56ed6a7b31`
to
`df30fbd8347b105233069d3f9833fc4d15f7716ad6c7706689c60b3cc02011de`.
No other environment value was intentionally changed.

Existing Provider-global protections remained unchanged:

```text
TianTianJieXi global limit: 20/min
TianTianJieXi global concurrency: 2
KuKuTool global limit: 30/min
KuKuTool global concurrency: 4
Single-link Ultra race: tiantianjiexi,kukutool,native
Batch-specific provider order: tiantianjiexi,native
```

## Deployment

The production checkout was clean before and after deployment. The exact
candidate was fetched and checked out by full SHA; no branch merge, rebase, or
floating pull was used. Dependency files were unchanged, so dependencies were
not reinstalled.

Before restart, Django `check` and migration dry-run passed with PostgreSQL.
All migrations through `0023` were already applied and no new migration was
generated. `collectstatic --noinput` completed with two files copied and 146
unchanged. The existing service was restarted at 2026-08-13 21:58:05 HKT and
returned active/running as `www-data` on `127.0.0.1:8000`.

Nginx and systemd hashes remained unchanged. Gunicorn topology, Redis
configuration, database schema, credentials, DNS, and Provider accounts were
not modified.

## Production Parse Acceptance

One fresh production Douyin Ultra attempt was used. TianTianJieXi completed and
won in 1,691 ms. The first visible result was the original/Ultra resource;
Native was not visible first. The result contained one video with an exact
size of 110,691,390 bytes.

The result arrived inside the six-second foreground window, so continuation
was not needed. No unexpected 429 occurred. The task did not manufacture an
upstream failure or send repeated requests to force cooldown/rate-limit
behavior; those paths remain covered by the candidate's automated and Linux
gates.

## Browser Streaming Regression

The accepted Ultra result used `browser_streaming`:

```text
Provider/CDN
  -> browser Service Worker
  -> Chrome Download Manager
  -> disk
```

Chrome displayed real byte progress, speed, and ETA, then completed an exact
110,691,390-byte file. Its SHA-256 matched an independently saved prior copy of
the same approved resource. No playback page opened and no Server Relay was
used.

During the download window:

| Gate | Result |
| --- | ---: |
| `/media/download-prepare/` requests | 0 |
| `/media/download-stream/` requests | 0 |
| `/media/download-status/` requests | 0 |
| Download audit route | `direct/direct` |
| AnimeImages actual media bytes | 0 |
| AnimeImages estimated server-egress bytes | 0 |
| Browser-egress bytes | 110,691,390 |

This confirms the existing Service Worker Streaming data plane was preserved.

## Health And Logs

After parse and download acceptance:

```text
service: active/running
local readiness: 200
public readiness: 200
homepage: 200
```

The acceptance window contained no 5xx response, traceback, Redis error storm,
continuation error, unexpected 429, or service restart loop.

## Rollback And Checkpoint

The complete rollback snapshot covered the old code SHA, exact environment,
Runtime timeout, static tree, and service state. It was not needed. The old
known-good code SHA remains `5af0892bc7f01ec73a5a3c1ba1b55814364e5e6a`.
After all production gates and the new checkpoint passed, the temporary
server-local maintenance backup was securely removed; no environment backup
was copied into the repository or another host.

The new production checkpoint is:

```text
checkpoint/production-douyin-ultra-retry-v1
  -> 87afba321a2dc459b7daebcd1cc9eea6735b60ba
```

This checkpoint deliberately points to the deployed code SHA, not to the later
documentation-only branch commit. Production was not restarted for the
documentation update.
