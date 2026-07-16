# Current Version Status

Date: 2026-07-14

Last Audited Runtime Baseline:

```text
6b1787f Make release Git state read-only (#18)
```

This is the application runtime baseline used for this documentation audit. It is not a real-time pointer to `main`. Read the current remote baseline with:

```bash
git fetch origin
git log -1 --oneline origin/main
```

Production site:

```text
https://download.cosflow.icu/
```

## Unreleased Batch Parse Candidate

The `codex/batch-parse-stability-v1` development branch is not deployed. Its candidate behavior includes:

- Bounded Batch scheduling with no executor backlog beyond the configured worker count.
- A scheduling deadline for starting new rows and a fresh per-item deadline created only when a row actually starts.
- One response deadline calculated from the same `BatchParseBudget` value displayed as the response upper bound; the scheduler never performs an unbounded wait after it expires.
- Cooperative Provider deadlines remain the normal path. Because Python threads cannot be safely force-killed, an extreme non-cooperative worker may continue briefly after the request returns; the row is marked `timeout` and a non-sensitive `batch_parse_worker_deadline_overrun` warning identifies the missing deadline path.
- Separate `success`, `failed`, `timeout`, and `skipped` states.
- Separate row-duration totals and real Batch wall-clock duration.
- Interactive Batch runs reserve access and rate limits once, then issue one bounded item request at a time. Each completed shared result card is displayed before the next item starts; signed run tokens are client-bound, short-lived, one-use per index, and server-enforced in input order.
- Batch-only Douyin ultra-HD requests therefore reach TianTianJieXi serially, matching the upstream batch behavior and avoiding immediate concurrency-limit fallbacks. Single-link parsing is unchanged; the synchronous no-JavaScript fallback still uses the bounded worker scheduler.
- Each progressive item has its own response boundary. The synchronous fallback response budget remains capped at 55 seconds by default so an uncooperative worker cannot hold that HTTP request past the current 60-second Gunicorn timeout.
- A POST/CSRF-protected single-card retry that preserves the textarea and all other cards.
- Batch-only JavaScript in `static/js/batch_parse.js` and a shared result-card template.
- No optional missing-size CDN probe in the Batch display path; single-link parsing remains unchanged.
- Shared Batch and single-result size semantics: exact byte sources render normally, yt-dlp approximate or audio/video merge totals render with `预计`, and missing sizes render as `大小未知`.
- Progressive cards prioritize only their first preview image; other Batch previews remain lazy-loaded, including after single-card retry.

Do not treat these items as production behavior until the corresponding PR is reviewed, merged, and deployed.

## Production Runtime Topology

Current deployed topology:

```text
Gunicorn user:  www-data
Gunicorn bind:  127.0.0.1:8000
Nginx upstream: http://127.0.0.1:8000
Checkout owner: ubuntu:ubuntu
```

Production does not currently use `/run/animeimages/gunicorn.sock`. A systemd-managed Unix socket remains a future recommended hardening target and requires a separate reviewed server configuration change.

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
- Admin UI includes Provider settings, health tests, logs center, policy center, download runtime settings, batch parse runtime settings, and Release Management.
- PR #17 and PR #18 are merged into `main` and deployed. Release Management v1 is main-only, record-only by default, and restricted to read-only local Git refs in Web requests.
- The repository includes root-owned helper, systemd, and sudoers templates for a future Release Executor Wiring v1 rollout. They are not installed or enabled in production.
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
BATCH_PARSE_ULTRA_PER_LINK_TIMEOUT_SECONDS=12
BATCH_PARSE_TOTAL_TIMEOUT_SECONDS=60
BATCH_PARSE_PARALLEL_WORKERS=4
BATCH_PARSE_TIANTIAN_PARALLEL_WORKERS=1
BATCH_PARSE_MAX_RESPONSE_TIMEOUT_SECONDS=55
BATCH_PARSE_IP_INTERVAL_SECONDS=20
BATCH_PARSE_GLOBAL_MINUTE_LIMIT=30
BATCH_PARSE_DAILY_IP_LIMIT=100
```

Important behavior:

- `/batch/` defaults to Douyin original/ultra when runtime allows it.
- Interactive parsing uses `/batch/start/` once and `/batch/item/` sequentially, showing each card before starting the next item. The no-JavaScript fallback uses limited parallelism, with Batch Douyin ultra-HD capped to one TianTianJieXi worker.
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
/admin/release-management/
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

PR #17 added Admin Release Management v1 and PR #18 applied the read-only Git production fix. Both PRs are merged and deployed. The production release panel is intentionally narrow:

- It displays current Git state, the locally cached `origin/main`, tags, readiness, and deployment history.
- All Web requests read local Git refs only. The page cannot refresh remotes, and `www-data` must not run `git fetch`.
- The `ubuntu` deployment workflow or an external operations task refreshes local remote refs.
- Creating a main job reads local `origin/main` and records its exact `job.target_sha`.
- A database constraint permits only one `pending`/`running` job, while execution and cancellation use atomic status transitions.
- Production remains in record-only mode after the PR #17 and PR #18 rollout. `.env.example.production` does not enable the executor.
- Without `RELEASE_MANAGEMENT_EXECUTOR_COMMAND`, a release request remains `pending` as an audit record and does not deploy automatically.
- The only accepted executor command is `/usr/bin/sudo -n /usr/local/sbin/animeimages-release-submit {job_id}`. Django binds the current job ID and rejects direct systemd, shell, `deploy.sh`, alternate paths, wrong IDs, and extra arguments.
- `ops/release-executor/` contains versioned helper, systemd, sudoers, and installation documentation. The installed files must be root-owned and immutable by `ubuntu` and `www-data`.
- Configure the executor in `/srv/animeimages/.env.production` only after separately installing and validating the helper, systemd unit, sudoers rule, runtime user, application and virtualenv paths, filesystem permissions, and one controlled pending job.
- Configure `sudo git config --system --add safe.directory /srv/animeimages` so `www-data` can read the `ubuntu:ubuntu` repository without changing ownership. Do not use a wildcard safe directory.
- `deploy.sh` must deploy `job.target_sha`, verify it is reachable from `origin/main`, and verify final `HEAD == job.target_sha`.
- Tags remain visible, but v1 does not create or execute Tag/commit switching jobs.
- The full operating model and troubleshooting runbook live in `docs/release-management.md`.

## Health And Smoke Commands

Core checks:

```bash
python manage.py check
python manage.py makemigrations --check --dry-run
python manage.py test xhs_download --noinput
bash -n ops/release-executor/animeimages-release-submit
bash -n scripts/validate_release_executor.sh
./scripts/validate_release_executor.sh
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
7. Production may still show historical `mobile_feed` / 本地解析 fallbacks until the unreleased Batch candidate is merged and deployed. Local seven-link testing first reproduced two immediate TianTianJieXi concurrency rejections with four workers. The final progressive candidate completed the same seven links as 7/7 TianTianJieXi successes in about 34 seconds, while retaining targeted retry for genuine upstream timeouts.
8. Release Management v1 does not provide migration-aware Tag rollback. Do not treat the visible Tag list as an enabled version-switch workflow.
9. The release executor templates are repository artifacts only. Production remains record-only until a separate server installation and controlled-job validation are explicitly approved and completed.

## Next Priorities

1. Keep documentation current after major PRs; update this file and `docs/codex-handoff.md`.
2. Review the existing `xhs_download/services/batch_parse/` boundary from PR #13 and remove residual wrappers from `views.py`; do not restart the extraction from zero.
3. Move batch-page logic out of `static/js/app.js` into a focused module while preserving `/batch/` behavior.
4. Keep Release Management v1 record-only unless the root-owned helper, systemd one-shot unit, narrow sudoers rule, runtime user, paths, permissions, and a controlled pending job are independently validated on production. Keep Tag/commit switching disabled.
5. Keep Provider Strategy v1 in smoke/stabilization mode; defer new provider/platform expansion until the structural work is settled.
