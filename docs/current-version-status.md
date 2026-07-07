# Current Version Status

Date: 2026-07-07

Main baseline:

```text
c732e72 Fix batch parse ultra default and runtime controls (#11)
```

Production site:

```text
https://download.cosflow.icu/
```

## Summary

- Universal input routes Xiaohongshu, Douyin, Bilibili and supported long-tail links through platform-aware adapters/providers.
- Xiaohongshu remains a dedicated adapter path for original images, multi-image notes, and baseline video downloads.
- Douyin normal mode remains native/baseline first.
- Douyin ultra mode now uses Provider Strategy v1:
  - TianTianJieXi official API is the preferred ultra provider.
  - KuKuTool remains fallback/race backup.
  - Native remains fallback.
- Bilibili remains baseline-only. Do not pursue cookie/member/DASH work unless explicitly requested.
- `/batch/` supports batch parsing with runtime settings, limited parallelism, and staff-only/raw-link controls.
- yt-dlp and gallery-dl are optional generic long-tail providers behind capability, runtime, platform and safety gates.
- Admin UI includes Provider settings, health tests, logs center, policy center, download runtime settings, and batch parse runtime settings.
- TikHub and older native-enhanced experiments remain historical/diagnostic only; they are not the active Douyin ultra path.

## Current Route Map

| Area | Current path | Notes |
| --- | --- | --- |
| Xiaohongshu images | Dedicated adapter | Preserve original bytes; no needless transcode. |
| Xiaohongshu video | Dedicated adapter + download strategy | Server fallback remains important for reliability. |
| Douyin normal | Native adapter/provider path | Expected baseline is stable 1080p/public source when available. |
| Douyin ultra | TianTianJieXi + KuKuTool + native race/fallback | TianTianJieXi should get fair race budget; KuKuTool is not disabled. |
| Bilibili | Dedicated baseline adapter | High-quality DASH/cookie path deferred. |
| YouTube | yt-dlp / merge provider | 1080p+ often requires server-side ffmpeg merge; keep gated. |
| Image galleries | gallery-dl generic provider | Only via runtime/platform gates. |
| Batch parse | `/batch/` + `BatchParseRuntimeSetting` | Staff can see raw controls; public/raw exposure must stay restricted. |

## Provider Strategy v1

Douyin ultra parsing uses provider order:

```text
DOUYIN_ULTRA_PROVIDER_ORDER=tiantianjiexi,kukutool,native
DOUYIN_ULTRA_RACE_ENABLED=True
```

Expected behavior:

1. Extract `aweme_id`.
2. Run enabled ultra providers under race/fallback budget.
3. TianTianJieXi receives fair remaining budget in race mode and does not reserve KuKuTool grace.
4. Normalize nested stream candidates, not just parent video URL.
5. Accept only allowed ByteDance media hosts such as `365yg.com` / `douyinvod.com`.
6. Rank by explicit quality label, size, bitrate parameters, and stable object identity.
7. Use signed provider manifests for preview/download.
8. Never log TianTianJieXi phone/password or full signed CDN URLs.

Validated production behavior from PR #10 / #11 rollout:

- TianTianJieXi account health succeeds when credentials are configured.
- Douyin ultra can select `selected_provider=tiantianjiexi`.
- `source_provider` and `ultra_race_winner` can both be `tiantianjiexi`.
- KuKuTool remains available as fallback.
- Batch parse can parse multiple Douyin links through TianTianJieXi / ultra race.

## Batch Parse Runtime

Batch parsing is controlled by `BatchParseRuntimeSetting` and `.env` fallback values.

Current defaults:

```text
BATCH_PARSE_MAX_LINKS=20
BATCH_PARSE_STAFF_MAX_LINKS=20
BATCH_PARSE_PUBLIC_MAX_LINKS=3
BATCH_PARSE_PER_LINK_TIMEOUT_SECONDS=8
BATCH_PARSE_TOTAL_TIMEOUT_SECONDS=60
BATCH_PARSE_PARALLEL_WORKERS=4
BATCH_PARSE_IP_INTERVAL_SECONDS=20
BATCH_PARSE_GLOBAL_MINUTE_LIMIT=30
BATCH_PARSE_DAILY_IP_LIMIT=100
```

Important behavior:

- `/batch/` defaults to Douyin original/ultra when runtime allows it.
- Parsing uses limited parallelism, not one giant synchronous loop.
- Admin/staff can see copy/open/browser-download controls.
- Anonymous/public users must not see raw signed direct links.
- Download strategy matrix is folded at the bottom.

Runtime page:

```text
/admin/xhs_download/batchparseruntimesetting/
```

## Admin And Observability

Important pages:

```text
/admin/provider-settings/
/admin/provider-health/
/admin/provider-health-tests/
/admin/console/
/admin/logs/
/admin/policy-center/
/admin/ops-dashboard/
/admin/xhs_download/batchparseruntimesetting/
```

Important log/diagnostic models:

- `ParseRequestLog`
- `DownloadRequestLog`
- `ProviderAdminAuditLog`
- `ProviderHealthTestRun`
- `RoutingDecisionLog`

Request tracing:

- Django reads `X-Request-ID`.
- Response headers include `X-Request-ID`.
- Use the same request id across admin logs and `journalctl`.

## Configuration State

Provider `*_ENABLED` env values are capability gates. They say the deployment is allowed/prepared to run the capability.

Admin runtime settings control user exposure:

```text
disabled
staff
public_gray
public
```

Do not use `.env` as the daily product switch if an admin runtime model exists.

Examples:

- Douyin ultra runtime: `/admin/provider-settings/`
- Third-party runtime: `/admin/provider-settings/`
- YouTube merge runtime: `/admin/provider-settings/`
- Batch parse runtime: `/admin/xhs_download/batchparseruntimesetting/`
- Download limits/runtime: `/admin/console/`

## Health And Smoke Commands

Core checks:

```bash
python manage.py check
python manage.py makemigrations --check --dry-run
python manage.py test xhs_download --noinput
```

Provider checks:

```bash
python manage.py inspect_third_party_provider --health --json
python manage.py smoke_tiantianjiexi_provider --url "<douyin test url>" --mode api
python manage.py smoke_tiantianjiexi_provider --url "<douyin test url>" --mode parse-result
python manage.py smoke_tiantianjiexi_provider --url "<douyin test url>" --mode douyin-ultra
```

Production health:

```bash
curl -i http://127.0.0.1:8000/health/ready/
curl -i https://download.cosflow.icu/health/ready/
```

## Known Issues

1. Browser-first/provider download behavior differs between localhost and HTTPS production. Always verify both if touching download handoff code.
2. Server attachment fallback is stable but consumes VPS egress and can be slower than third-party browser handoff.
3. yt-dlp/YouTube merge can be slow and large; public exposure should remain gated until queue/progress behavior is proven.
4. Bilibili high quality remains deferred because it commonly requires Cookie, DASH merge, and entitlements.
5. Some historical docs describe policy/autonomous/KuKuTool/TikHub plans that are not the active production path. Use `docs/codex-handoff.md` and this file as the source of truth.
6. Unapplied migrations are the most common cause of admin `OperationalError`.

## Next Priorities

1. Keep documentation current after major PRs; update this file and `docs/codex-handoff.md`.
2. Continue Provider Strategy v1 stabilization:
   - TianTianJieXi smoke fixtures.
   - KuKuTool fallback verification.
   - Batch parse public/staff product rules.
3. Measure browser blob / browser-first / server attachment speed on production and decide the default per platform.
4. Decide when a durable queue is required for YouTube merge or large provider downloads.
5. Expand long-tail platforms only after Provider Health Tests and download strategy are reliable.
