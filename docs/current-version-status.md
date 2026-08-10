# Current Version Status

Date: 2026-08-10

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

## Unreleased Browser Direct Restoration Candidate

The `codex/browser-direct-restoration-v1` branch adopts this download
architecture north star:

> Server is the download control plane, not the default media data plane.

AnimeImages continues to parse, authorize, validate Provider manifests, choose
the route, and issue the handoff. For a manifest-validated TianTianJieXi video,
and for KuKuTool when its runtime delivery mode is `browser_first`, the normal
media-byte path becomes Provider/CDN to browser rather than Provider/CDN to
AnimeImages to browser. The final signed Provider/CDN URL may therefore be
visible in browser Network tools; public raw-link UI and exports remain
staff-only.

The candidate separates two capabilities that CORS cannot safely conflate:

- Native handoff uses browser navigation/download management and does not
  require JavaScript fetch CORS permission.
- Fetch/Blob is limited to explicitly CORS-safe resources with a known size no
  greater than 32 MiB. Large and unknown-size videos are never Blob-first.

Direct routing still requires the existing Provider identity, resource ID,
cache key, signed manifest, allowed host and scheme, public address, media type,
and expected-resource validation. Cookie-bound, merge-required, server-only,
or otherwise non-handoff-safe resources retain the bounded server path. Single
and Batch pages consume the same server route decision.

Direct observability means `route_selected` on the control plane; it does not
claim that the browser completed the file. A native handoff therefore remains
visibly pending in the page while the browser download manager owns the real
transfer. For the retained relay path, `DownloadRequestLog.success` is written
only after the stream iterator exhausts normally, and `actual_bytes` is the
number of bytes actually yielded. Early close and generator/upstream errors are
failures, never successful completion.

This candidate does not change Gunicorn, Provider parsing order or timeouts,
permissions, migrations, Nginx, systemd, or production configuration. It has
not been deployed.

## Unreleased Web Concurrency And Provider Isolation Candidate

The stacked `codex/web-concurrency-provider-isolation-v1` branch adds a
repository-controlled Gunicorn candidate at `ops/systemd/animeimages.service`:

```text
worker_class = gthread
workers = 2
threads = 4
timeout = 60
```

This increases synchronous Django I/O capacity without changing the existing
request timeout. It does not increase TianTianJieXi capacity. Every real
TianTianJieXi parse from Single, Batch, or a Provider race must acquire the
same Redis-backed global lease, whose effective maximum is fixed at one.
Payload cache hits do not acquire it. A busy caller waits no more than the
configured short interval, capped at 250 ms, records `provider_busy` without a
source URL, and continues through the existing Provider fallback chain.

The lease uses atomic cache add, an owner token, Redis compare-and-delete on
release, and a bounded TTL. A crashed worker therefore cannot retain the slot
forever, and an expired old owner cannot delete a newer lease. Cold account
status refresh has a separate lease and cache; it never consumes the parse
slot. Production TianTianJieXi configuration requires the shared Redis cache
backend and fails closed when that boundary is unavailable.

The tracked systemd unit is a candidate, not evidence of the current server
unit. Until a separately approved rollout compares and installs it,
`PRODUCTION_UNIT_DRIFT` remains expected. No production unit, Nginx setting,
environment, database, or Provider timeout has been changed by this branch.

## Unreleased Batch Runtime Admin Clarity Candidate

The `codex/batch-runtime-admin-clarity-v1` branch improves only the Django
Admin control surface for `BatchParseRuntimeSetting`:

- The singleton changelist redirects directly to the one change form, removes
  Admin actions, denies deletion, and offers the add flow only while no row
  exists. Opening the empty changelist/add flow does not create the row.
- A read-only panel separates the saved/effective database state from the form
  draft and shows the access mode, internal value, anonymous/staff effect,
  rollout percentage, source, and `updated_at` value.
- An existing database singleton remains authoritative. Environment values are
  first-row defaults or database-failure fallback, not an override for a saved
  row.
- Save is labeled `保存并生效`; successful changes are read by subsequent
  requests without a restart. Unsaved edits are visibly marked.
- Public-expansion transitions require explicit UI confirmation and the same
  confirmation marker is enforced by the server. Access contractions and
  ordinary limit changes do not add confirmation friction.
- An `updated_at` version token plus a locked fresh read rejects stale Admin
  forms instead of silently overwriting a newer save.
- Successful changes emit a structured application log containing only the
  actor, request ID, changed field names, old/new access and rollout values,
  and public-expansion classification.

This candidate adds no migration or parallel settings page and does not change
Batch access semantics, rollout, parsing, scheduling, downloads, Provider,
Policy, or infrastructure. It has not been deployed.

## Unreleased Batch Public Download Candidate

The `codex/batch-public-download-visibility-v1` branch aligns the existing
`BatchParseRuntimeSetting.access_mode` with all Batch product surfaces:

- `disabled` hides the navigation and returns 404 for Batch page/API requests,
  including staff website requests; Django Admin remains available for changing
  the runtime setting.
- `staff` exposes Batch only to active staff users.
- `public_gray` uses the existing deterministic rollout helper for navigation,
  page access, and start/item/retry access; staff remains allowed.
- `public` exposes parsing and downloads to visitors and staff.
- Allowed public users can download individual video and image results through
  the same server-validated route decision used by the single-result page.
  Provider single-file routes use the existing prepare, same-origin stream, and
  bounded status-polling contract. A 100% state means browser handoff was
  confirmed, not that the browser finished receiving the file.
- Public download cards carry only the existing allowlisted client media fields
  required by that contract. Copy/open raw URLs, raw exports, aria2 exports, and
  the advanced strategy matrix remain staff-only.

This candidate adds no migration and does not change Provider order, Provider
timeouts, Batch deadlines, Nginx, systemd, or production configuration. It has
not been deployed.

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

## Unreleased Shared Parse Preferences Candidate

The `codex/shared-parse-preferences-v1` branch keeps two browser-only parsing preferences consistent across the single-link and Batch pages:

- `animeimages.parsePreferences.v1` stores only versioned Boolean values for Douyin original/ultra and YouTube highest-quality merge.
- Existing values migrate per field from `xhsDownloadSettings`, then `animeimages.batchParseDraft.v1`, then the server-rendered page defaults. Valid values already present in the new key always win.
- `animeimages.batchParseDraft.v2` stores only the Batch links text. It never stores parse results, Provider data, manifests, tokens, or media URLs.
- Disabled controls remain off for submission while the browser may retain the user's preference for a later page where the server makes that capability available.
- Provider Strategy, Batch Provider order, timeouts, access gates, and server-side rollout decisions are unchanged.

This is browser state only, adds no migration, and is not production behavior until its PR is reviewed, merged, and deployed.

## Unreleased XHS Media Fidelity Candidate

The `codex/xhs-media-fidelity-v1` development branch improves the dedicated Xiaohongshu adapter without changing Provider Strategy or production infrastructure:

- Bounded parsing reads only valid JSON hydration data or a valid current-note JSON object from a known state assignment. It never executes page JavaScript, replaces JavaScript expressions, or imports a browser session.
- The current note is selected by a validated 24-character note ID from the final page URL. A multi-note payload is not allowed to select recommendation content by traversal order.
- Parsed JSON is diagnostic only until it contains a note-like candidate. Unrelated JSON-LD, application JSON, and hydration configuration do not suppress compatible safe fallbacks.
- Title and author use structured current-note data first, then explicit meta fields, then current-note DOM selectors.
- A matched structured image list becomes authoritative only after normalization leaves at least one valid resource. Missing, empty, or wholly invalid lists may use meta, current-note DOM, and safe broad fallback; when only another note is found, broad image/video scanning is suppressed to prevent recommendation leakage.
- Video covers prefer an explicit current-note poster/cover, then the first structured note image, then meta and current-note DOM images.
- XHS video size is exact-known-or-unknown. Only a validated integer byte field attached to the selected structured stream is accepted; no HEAD, Range, partial-download, bitrate, or duration estimate is added.
- Explicit login-required, private, deleted, and unavailable states precede meta, DOM, and broad media fallbacks. Verified structured public media from the matched current note may override only a generic page-level marker; a terminal state declared by the current note itself always wins.
- Single-link and Batch display contexts share the same normalized XHS title, author, cover, gallery order, and size semantics.

The candidate adds no migration, Cookie import, CAPTCHA bypass, third-party Provider, Provider-order change, timeout change, or production deployment. Synthetic fixtures contain no real signed media URLs.

## Unreleased Douyin Reliability Child Candidate

The `codex/douyin-parse-download-reliability-v1` branch is a child of the
`codex/xhs-media-fidelity-v1` V1 integration branch. It is not deployed. Its
candidate behavior includes:

- Single-link Douyin ultra requests can return a short-lived
  `continuation_pending` state when an already-started race Provider outlives
  the foreground budget. The browser performs at most five cache-only polls
  over a 15-second window; polling cannot call a Provider or submit the
  original parse again.
- A weak or native fallback can be rendered immediately while that bounded
  continuation remains active. It does not cancel a still-running external
  Provider, and a later strong result replaces the fallback in the same
  request lifecycle. Provider provenance comes from the actual parsed result,
  so an inner native fallback is not relabeled as KuKuTool or TianTianJieXi.
- Ultra Douyin result rendering does not start a separate missing-size media
  probe after parsing. Unknown fallback sizes remain unknown rather than
  consuming response time outside the Provider budget.
- A valid TianTianJieXi result with a usable Provider title and cover no longer
  starts an additional native metadata request before returning. Existing
  cached/direct native metadata is still reused, and native enrichment remains
  available when the Provider metadata is incomplete. This reduces avoidable
  Batch deadline overruns without increasing the 12-second item budget or
  changing the Batch Provider order.
- The existing one-second response-completion grace may retain a fully
  completed successful Batch row when a cooperative fallback returns just
  after its Provider deadline. The Provider still receives only the configured
  12-second deadline; unfinished work, explicit Provider timeouts, and results
  outside the bounded grace remain `timeout`.
- Provider video downloads first use a local-only JSON prepare request.
  Prepare performs no upstream media request and produces separate
  client-bound, 60-second stream and status URLs that contain no raw Provider
  URL. The stream URL is one-use; the status URL is repeatable within the
  original TTL and exposes only fixed states and safe errors. The opaque token
  points to a server-private, allowlisted cache record. That record contains
  only the typed fields needed for download and recovery; it currently retains
  one manifest-validated minimal media URL because Provider identity alone
  cannot reliably reconstruct the initial media request.
- The browser opens the same-origin stream URL and polls the status URL at a
  bounded interval until `streaming`, `failed`, `expired`, or a client-side
  15-second ceiling. Status polling is cache-only and cannot contact a
  Provider, extend the handoff TTL, or read media bytes.
- Constructing the streaming response leaves `stream_started` false. The first
  actual stream iteration marks it true immediately before yielding the
  already-validated first chunk. It remains true if the connection later
  closes. Closing before iteration leaves it false; the UI therefore
  distinguishes a confirmed browser handoff from a pre-stream failure.
- After the same-origin request is handed to browser download management, the
  page progress bar remains in its connecting state until status confirms
  `stream_started`. It then reaches 100% to represent confirmed browser
  handoff, not byte progress for the proxied media body. Browser download UI
  owns actual byte progress. URL-free telemetry separates the first and second
  upstream connection attempts, Provider refresh, and time to first chunk.
- The normal stream path opens one upstream media GET, validates its first
  non-empty chunk, then yields that chunk and the remaining iterator from the
  same response. The response object, socket, and generator are never stored
  in cache; the download concurrency slot is released when streaming ends.
- Stream startup has one absolute 50-second deadline shared by the first media
  GET, one allowed Provider refresh, the recovery GET, and the first non-empty
  chunk. Each operation receives only the remaining budget. Exhaustion returns
  `DOWNLOAD_STREAM_START_TIMEOUT` with a safe retry message, closes any open
  upstream, and releases concurrency. Once the first chunk is yielded, this
  startup deadline no longer limits normal file transfer.
- Browser fetch is permitted only for an explicitly declared CORS-safe
  Provider capability. TianTianJieXi and KuKuTool signed resources currently
  use server prepare/stream; ordinary public native URLs retain their direct
  path.
- A recoverable TianTianJieXi signed-resource failure can trigger exactly one
  server-side refresh through TianTianJieXi and exactly one download retry,
  capping the recovery path at two upstream media GETs.
  The refreshed item must retain the original Douyin resource identity and
  pass the existing manifest, media-host, content-type, and size checks.
- The candidate does not increase Provider timeouts, change Provider order,
  modify Batch scheduling, add a migration, use Cookie state, disable TLS
  verification, or add a Provider.

These changes are not production behavior until the child PR is reviewed and
merged into the V1 integration branch, the integration PR is reviewed and
merged into `main`, and a separate deployment is completed.

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
- `/batch/` supports runtime-gated batch parsing with limited parallelism; public download capability and staff-only raw-link tools are separate permissions.
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
| Batch parse | `/batch/` + `BatchParseRuntimeSetting` | Access mode controls navigation/page/API; allowed public users may download, while raw-link tools remain staff-only. |

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
- Allowed public users can use validated video/image download controls.
- Admin/staff can additionally see copy/open/raw-export controls.
- Anonymous/public users must not receive raw-link UI or exports; only the existing allowlisted client media fields needed for validated routing may be rendered.
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
