# Self-Healing System v0.3

Self-Healing System v0.3 upgrades the v0.2 fallback layer from passive fallback to an explicit failure detection and recovery record. It adds a bounded healing layer around existing routing and download execution, without replacing Routing v2/v3, Provider runtime gates, or Cost Guard.

## Core Structure

New modules:

- `xhs_download/services/self_healing_engine.py`
- `xhs_download/policy/healing_policy.py`

`SelfHealingEngine` is the single entry point. It provides:

- `detect_failure()`
- `classify_failure()`
- `decide_recovery_action()`
- `execute_recovery()`
- `record_healing_event()`

The engine does not download files. It observes a failure, asks the healing policy what should happen, records a `SelfHealingEvent`, and attaches healing metadata to the existing route info.

## Failure To Recovery Mapping

The v0.3 healing policy maps failures as follows:

- `provider_timeout` -> retry once, then switch provider.
- `provider_unavailable` -> switch provider.
- `merge_failed` -> switch provider.
- `routing_failed` -> ZIP fallback.
- `zip_failed` -> proxy fallback.
- `proxy_failed` -> direct fallback if the existing recovery layer says it is safe; otherwise blocked.
- `direct_failed` -> proxy fallback.
- `cost_blocked` -> wait/degrade mode and blocked response.
- unknown failure -> blocked gracefully.

Healing depth is capped at `3`. When the depth limit is reached, the engine records `block_gracefully` and does not continue fallback.

## Provider Switch Logic

Provider fallback is advisory and bounded:

```text
kukutool -> yt_dlp_generic -> native -> fallback
youtube_merge -> yt_dlp_generic -> fallback
yt_dlp_generic -> native -> fallback
gallery_dl_generic -> yt_dlp_generic -> fallback
native -> fallback
```

Provider health score influences the policy risk label:

- `< 0.5`: degraded
- `< 0.3`: temporary disable recommended
- `< 0.1`: quarantine

This does not automatically change `ProviderRuntimeSetting`. Admin runtime gates remain manually controlled.

## Trigger Points

Automatic triggers:

- routing exception
- Cost Guard block
- ZIP failure
- proxy failure
- provider timeout during download
- outer download exception

Manual trigger:

- `/admin/self-healing/run/`

Manual diagnostics create a `SelfHealingEvent` for a selected provider and are useful for checking the policy output without modifying Provider settings.

## Observability

New model:

- `SelfHealingEvent`

New `DownloadRequestLog` fields:

- `auto_recovery_count`
- `provider_auto_switch_count`
- `failure_to_recovery_ms`
- `healing_action`
- `healing_risk`
- `healing_confidence`

The Admin Console adds a `Self-Healing Dashboard` with:

- 24h auto recovery count
- 24h healing success rate
- 24h failure-to-recovery time
- 24h provider auto switch count
- top failure causes
- top healing actions
- recent healing events

## Routing And Policy Compatibility

v0.3 does not change Routing v2/v3 scoring, Policy Engine v1/v2/v3 decisions, Provider runtime gates, or Cost Guard limits.

The execution path remains:

```text
request -> existing routing/policy/cost guard -> existing download execution
                                    |
                                    v
                         self-healing on failure
```

Self-Healing records and annotates recovery. It does not become the main routing source.

## Production Safety

v0.3 does not:

- introduce AI or ML
- introduce Celery or background queues
- perform infinite retries
- automatically change Provider runtime settings
- automatically change public or gray rollout
- open any new platform

## Deployment

Deploy normally and run migrations:

```bash
cd /srv/animeimages
git fetch origin
git checkout codex/self-healing-system-v0.3
source .venv/bin/activate
python manage.py migrate
python manage.py collectstatic --noinput
sudo systemctl restart animeimages
```

This version adds migration:

- `xhs_download/migrations/0013_selfhealingevent_and_more.py`

## Verification

Recommended checks:

```bash
python manage.py check
python manage.py makemigrations --check --dry-run
python manage.py test xhs_download --noinput
python manage.py inspect_third_party_provider --health --json
```

Manual smoke:

1. Open `/admin/console/` and confirm the `Self-Healing Dashboard` renders.
2. Open `/admin/self-healing/run/`.
3. Run a manual diagnostic as a superuser or staff user with `add_selfhealingevent`.
4. Confirm a `SelfHealingEvent` row is created.
5. Confirm download failures still return the existing public error or fallback response.

## Rollback

Rollback code-first:

```bash
cd /srv/animeimages
git fetch origin
git reset --hard <previous-known-good-commit-or-tag>
source .venv/bin/activate
python manage.py migrate
python manage.py collectstatic --noinput
sudo systemctl restart animeimages
```

Because v0.3 fields are observability-only, leaving the new columns and `SelfHealingEvent` table in the database is usually safe during a short code rollback.

Only roll back the migration after confirming old code and operational dashboards no longer need v0.3 observability data:

```bash
python manage.py migrate xhs_download 0012
```
