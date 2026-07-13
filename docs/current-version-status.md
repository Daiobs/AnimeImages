# Current Version Status

Date: 2026-07-13

Main baseline:

```text
494b191 Document production follow-up and batch provider labels (#16)
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
- Admin Release Management v1 is being hardened on `codex/admin-release-management-v1`: it is main-only, pins an exact target SHA, and can delegate execution to a non-blocking external systemd job only after explicit production configuration.
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

Production observation from PR #15 deployment:

- In a six-link Douyin batch parse, one item used `selected_provider=mobile_feed`.
- `mobile_feed` is a native Douyin fallback path, not a separate external Provider and not KuKuTool.
- This can happen when TianTianJieXi does not return the selected result for that item within the foreground budget.
- User-facing batch UI should label this path as `本地解析`; logs should keep the raw `selected_provider=mobile_feed` value for diagnosis.
- Current decision: record and observe. Do not change batch provider order, retry behavior, or concurrency solely from this single sample.
- Likely contributing factors include batch concurrency, per-link foreground budget, TianTianJieXi transient latency, and candidate validation differences between attempts.
- If this becomes frequent after launch feedback or enough `ParseRequestLog` samples, inspect TianTianJieXi timeout/empty-candidate logs first, then consider lower ultra-HD batch concurrency or a targeted TianTianJieXi compensation retry before native fallback.

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

## Admin Release Management v1

The release panel is intentionally narrow:

- It displays current Git state, the locally cached `origin/main`, tags, readiness, and deployment history.
- Ordinary GET requests do not fetch GitHub by default (`RELEASE_MANAGEMENT_FETCH_ON_VIEW=False`).
- Creating a main job fetches `origin/main` and records its exact `job.target_sha`.
- A database constraint permits only one `pending`/`running` job, while execution and cancellation use atomic status transitions.
- Merge and the first production deployment leave v1 in record-only mode. `.env.example.production` does not enable the executor.
- Without `RELEASE_MANAGEMENT_EXECUTOR_COMMAND`, a release request remains `pending` as an audit record and does not deploy automatically.
- A direct systemd executor must use `systemctl start --no-block ...`; Django only submits the unit job.
- Configure the executor in `/srv/animeimages/.env.production` only after independently validating the systemd one-shot unit, sudoers rule, runtime user, application and virtualenv paths, and filesystem permissions.
- `deploy.sh` must deploy `job.target_sha`, verify it is reachable from `origin/main`, and verify final `HEAD == job.target_sha`.
- Tags remain visible, but v1 does not create or execute Tag/commit switching jobs.

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
7. Batch Douyin ultra may occasionally fall back to `mobile_feed` / 本地解析 even when the same link can return ultra-HD in single-link testing. Treat this as an observation item until production feedback or log volume shows it is frequent enough to justify lowering ultra-HD batch concurrency or adding a targeted retry.
8. Release Management v1 does not provide migration-aware Tag rollback. Do not treat the visible Tag list as an enabled version-switch workflow.

## Next Priorities

1. Keep documentation current after major PRs; update this file and `docs/codex-handoff.md`.
2. Review the existing `xhs_download/services/batch_parse/` boundary from PR #13 and remove residual wrappers from `views.py`; do not restart the extraction from zero.
3. Move batch-page logic out of `static/js/app.js` into a focused module while preserving `/batch/` behavior.
4. Finish Release Management v1 production wiring and smoke validation for pinned `main` deployment. Keep Tag/commit switching disabled.
5. Keep Provider Strategy v1 in smoke/stabilization mode; defer new provider/platform expansion until the structural work is settled.
