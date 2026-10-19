# TianTianJieXi API Notes

## Current local account policy

Membership terms were confirmed by Human on 2026-09-26. The local execution
policy below was updated on 2026-10-19 at Human's request.

The configured TianTian account has lifetime membership, paid once; subsequent
parses have no per-call fee. Authentication is still required. Kukutool has no
practical request-count bottleneck at the current development scale. Neither
requires repeated fee approval for public-source tests within an explicitly
authorized task. External service/source sharing consent remains mandatory;
this does not authorize purchases, new recipients, private content or new
credential use. Historical approvals and request ledgers remain unchanged.

All TianTian application entry points use same-host, same-OS-user account file
locking, independent of Django cache namespace and worker process. Queue wait
is bounded to 30 seconds (or the remaining caller deadline), separate from
execution time. Admission expiry is unexecuted, not a parse failure. An active
HTTP call retains the slot until it exits even if a browser stops waiting.
Timeouts and network failures are reported for that request, not automatically
replayed or turned into a persistent account-wide block. Once the local call
exits, a new explicit request may run subject to existing deadlines, quotas,
rate limits, bounded failure cooldown and single-flight. Legacy unknown markers
and event files remain historical evidence but no longer control admission.
No new outcome markers are written and no manual recovery is required.
`manage.py tiantian_account_state` is inspection-only and identifies legacy
records separately from the current local lock state.

Normal completion, exceptions and process exit release the OS lock; cancelled
waiters never acquire it. The lock file itself is retained at the same inode.
Local termination does not prove that a remote request has finished; a later
explicit request can overlap unknown remote work. This is not a cross-machine
lock and cannot serialize website
operations, containers with separate temporary filesystems, or other OS users.
The current local deployment is a single host/OS user. Do not claim account-wide
serialization across those external environments.

Batch keeps its configured worker count even when ultra quality is requested;
only actual TianTian work enters the local account serialization lock. The legacy whole-batch
TianTian worker cap no longer serializes unrelated provider rows.

Kukutool retains existing execution concurrency, timeout and real rate-limit
handling. No new low call quota or remote limit probing is introduced.

## Service-entry diagnosis (2026-09-29, local only)

A single original Coolapk representative from the closed Scout batch returned
HTTP 200, JSON business code 451 and the explicit message that the link is not
currently supported. It contained no media. The request used the documented
POST endpoint, form encoding and `phone`, `password`, `link` fields, with no
redirect and an unchanged source. Configured credentials were present; values
were not exported. This is evidence of rejection of this particular source,
not HTTP 451, expired membership or a concurrency diagnosis.

Only code 451 together with that explicit unsupported-link message maps to
`PROVIDER_UNSUPPORTED`. Unknown 451 messages remain upstream failures. Explicit
unsupported responses do not enter the client retry loop. Existing account
serialization and uncertain-outcome handling are unchanged. Historical batch
codes alone do not establish the messages for all seven earlier failures.

The companion Kukutool diagnosis is recorded in
[platform coverage](upstream-platform-coverage.md#service-entry-repair-2026-09-29-local-only).

Historical API integration notes below: 2026-07-06.

This document summarizes the public TianTianJieXi API information and the
response shape observed while integrating it as a Douyin ultra-HD provider.
Do not store account credentials in source control.

## Official Public Information

TianTianJieXi publishes a lightweight API note at:

- https://www.tiantianjiexi.com/api.txt

The official note says API usage requires an activated member account. The API
response JSON contains video URLs for different qualities, audio URL, cover URL,
title, and description.

The TianTianJieXi batch page FAQ also states that parsed videos/images are
returned at the highest original quality without compression.

## Endpoints

### Parse Link

Request:

```http
POST https://tiantianjiexi.com/ajax/detail.php
Content-Type: application/x-www-form-urlencoded
```

Form fields:

| Field | Required | Description |
| --- | --- | --- |
| `phone` | yes | TianTianJieXi account phone number. |
| `password` | yes | TianTianJieXi account password. |
| `link` | yes | Source URL to parse, such as a Douyin share/detail URL. |

Example with curl:

```bash
curl -X POST "https://tiantianjiexi.com/ajax/detail.php" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  --data-urlencode "phone=$TIANTIANJIEXI_PHONE" \
  --data-urlencode "password=$TIANTIANJIEXI_PASSWORD" \
  --data-urlencode "link=https://www.douyin.com/user/self?modal_id=7611146072161047526"
```

### Query Account Status

Request:

```http
GET https://tiantianjiexi.com/ajax/detail.php?action=query&phone={phone}&password={password}
```

Use this only for health checks or admin diagnostics. Cache the result to avoid
unnecessary account-status calls.

## Observed Parse Response Shape

The response is JSON. For successful Douyin video parsing, the shape observed in
local integration is approximately:

```json
{
  "code": 200,
  "msg": "请求成功",
  "data": {
    "title": "",
    "desc": "description text",
    "images": [
      "https://p3-sign.douyinpic.com/..."
    ],
    "videos": [
      {
        "url": "https://v11-default.365yg.com/...&br=1999&bt=1999&qs=0...",
        "streams": [
          {
            "url": "https://v11-default.365yg.com/...&br=1264&bt=1264&qs=1...",
            "sizeInBytes": 1871762,
            "qualityLabel": "540p"
          },
          {
            "url": "https://v11-default.365yg.com/...&br=1542&bt=1542&qs=0...",
            "sizeInBytes": 2283894,
            "qualityLabel": "720p"
          },
          {
            "url": "https://v11-default.365yg.com/...&br=1999&bt=1999&qs=0...",
            "sizeInBytes": 2960282,
            "qualityLabel": "1080p"
          },
          {
            "url": "https://v6-default.365yg.com/...&br=58919&bt=58919&qs=13...",
            "sizeInBytes": 86980828,
            "qualityLabel": "原画"
          }
        ]
      }
    ],
    "audioUrl": "https://sf11-cdn-tos.douyinstatic.com/obj/ies-music/..."
  }
}
```

Important integration detail:

- Do not only read `data.videos[0].url`.
- The best/original source can be inside `data.videos[0].streams[]`.
- Sort and select across both parent video URLs and nested stream URLs.

## Candidate Ranking Rules

Recommended ranking order:

1. Explicit quality label:
   - `原画`, `无压缩`, `source`, `original`, `ultra`, `超高清`, `超清`
   - `4K` / `2160`
   - `2K` / `1440`
   - `1080`
   - `720`
   - `480`
2. File size:
   - Prefer larger `sizeInBytes`, `size`, `file_size`, `fileSize`, or `filesize`.
3. CDN URL parameters:
   - `qs=13` is a strong original/high-quality signal.
   - High `br` or `bt` values are strong bitrate signals.
   - `br >= 20000` or `bt >= 20000` can be treated as original/high quality.
4. URL validity:
   - Only accept allowed ByteDance media hosts such as `365yg.com` and
     `douyinvod.com` for Douyin video downloads.

Current project inference:

| URL parameter signal | Suggested label |
| --- | --- |
| `qs=13` or `br/bt >= 20000` | `原画` |
| `br/bt >= 10000` | `2K` |
| `br/bt >= 2500` | `1080P` |
| `br/bt >= 1500` | `720P` |

## Error Handling

Common failure groups:

| Failure | Recommended handling |
| --- | --- |
| Auth/account error | Mark provider unavailable and show admin-facing setup warning. |
| Rate/limit/quota error | Short failure cache and avoid repeated calls. |
| Timeout | Fall back to KuKuTool/native provider. |
| Empty media result | Treat as provider failure; do not expose raw response to users. |
| Non-JSON response | Treat as upstream API failure. |

## Security Requirements

- Never expose `phone` or `password` to the frontend.
- Store credentials only in local/server environment configuration.
- Redact account fields from logs, admin pages, and health responses.
- Validate final media URLs before download.
- Reject localhost/private IPs and unexpected domains.
- Do not trust frontend-submitted provider URLs unless protected by provider
  manifest tokens.

## Recommended Runtime Settings

Suggested development values:

```env
THIRD_PARTY_PROVIDER_ENABLED=True
TIANTIANJIEXI_PROVIDER_ENABLED=True
TIANTIANJIEXI_API_BASE_URL=https://tiantianjiexi.com/ajax/detail.php
TIANTIANJIEXI_PHONE=
TIANTIANJIEXI_PASSWORD=
TIANTIANJIEXI_TIMEOUT_SECONDS=15
TIANTIANJIEXI_RETRIES=2
TIANTIANJIEXI_PAYLOAD_CACHE_TTL_SECONDS=120
TIANTIANJIEXI_FAILURE_CACHE_TTL_SECONDS=8
TIANTIANJIEXI_ACCOUNT_STATUS_CACHE_TTL_SECONDS=300
TIANTIANJIEXI_HEALTH_QUERY_ACCOUNT_STATUS_ENABLED=True
TIANTIANJIEXI_GLOBAL_MAX_CONCURRENCY=2
TIANTIANJIEXI_REQUEST_LIMIT_PER_MINUTE=20
BATCH_PARSE_TIANTIAN_PARALLEL_WORKERS=1
BATCH_PARSE_MAX_RESPONSE_TIMEOUT_SECONDS=55
THIRD_PARTY_PROVIDER_ORDER=kukutool_generic,tiantianjiexi_generic,yt_dlp_generic,gallery_dl_generic
THIRD_PARTY_PROVIDER_ORDER_DEFAULT=kukutool_generic,tiantianjiexi_generic,yt_dlp_generic,gallery_dl_generic
THIRD_PARTY_PROVIDER_ORDER_YOUTUBE=yt_dlp_generic,kukutool_generic,tiantianjiexi_generic
THIRD_PARTY_PROVIDER_ORDER_SHORT_VIDEO=kukutool_generic,tiantianjiexi_generic,yt_dlp_generic,gallery_dl_generic
DOUYIN_ULTRA_PROVIDER_ORDER=tiantianjiexi,kukutool,native
DOUYIN_ULTRA_RACE_ENABLED=True
```

For user-facing Douyin ultra-HD parsing, keep a strict frontend budget and race
TianTianJieXi against the fallback provider so a slow upstream call does not
block the page.

## Project Integration Notes

Current AnimeImages integration uses TianTianJieXi as the primary Douyin
ultra-HD provider and KuKuTool as fallback in the single-link path, where both
providers can race. The Batch path is intentionally different: it excludes
KuKuTool and the interactive page submits one independently bounded item at a
time, displaying its card before starting the next item. The no-JavaScript
fallback also caps TianTianJieXi scheduling to one worker and can fall back to
native parsing.
All real TianTianJieXi parse calls reserve a Redis-backed global concurrency
counter shared by Single, Batch, worker processes, and Provider-race threads.
The effective production target is two. Payload-cache hits return before the
single-flight key and concurrency counter; same-source concurrent misses remain
coordinated by the single-flight key. A busy concurrency counter follows the
existing bounded fallback path and records a URL-free diagnostic. This counter
supersedes the historical fixed-at-one lease from PR #30.
TianTianJieXi results must be normalized by expanding nested `streams[]`
candidates before ranking.

## Smoke Test

After credentials are configured in `.env` or `.env.production`, run:

```bash
python manage.py smoke_tiantianjiexi_provider --url "<douyin test url>" --mode api
python manage.py smoke_tiantianjiexi_provider --url "<douyin test url>" --mode parse-result
python manage.py smoke_tiantianjiexi_provider --url "<douyin test url>" --mode douyin-ultra
```

The command prints provider availability, asset counts, selected quality,
selected media host, and duration. It does not print phone/password or complete
media URLs.

Provider health tests also include:

- `tiantianjiexi_account_status`: account status only, cached, no real video
  parse and no parse quota consumption.
- `douyin_tiantianjiexi_ultra`: manual Douyin smoke, consumes a real parse.
- `third_party_tiantianjiexi`: manual third-party smoke, consumes a real parse.

`TIANTIANJIEXI_HEALTH_QUERY_ACCOUNT_STATUS_ENABLED=False` disables account
status requests from health checks. In that mode, health only reports local
configuration, cache, circuit and failure-cache state.

For the sample Douyin item `modal_id=7611146072161047526`, the parent
`videos[0].url` was low bitrate, while the nested `streams[]` entry with
`qualityLabel=原画`, `sizeInBytes=86980828`, `br=58919`, and `qs=13` was the
actual original-quality video.
