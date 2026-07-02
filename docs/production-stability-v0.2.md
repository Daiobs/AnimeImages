# Production Stability v0.2

Production Stability v0.2 adds a small reliability layer on top of the existing routing, policy, provider, and cost guard systems. It does not add platforms, queues, or a new routing architecture.

## Recovery Flow

All download recovery follows the same shape:

1. Detect the failing stage.
2. Classify the failure type.
3. Apply one bounded fallback.
4. Record the recovery decision in `DownloadRequestLog`.

Failure types include:

- `provider_timeout`
- `provider_unavailable`
- `merge_failed`
- `zip_failed`
- `proxy_failed`
- `direct_failed`
- `cost_blocked`
- `routing_failed`

## Fallback Rules

The first version intentionally keeps fallback shallow:

- Routing exception -> safe ZIP fallback.
- Provider timeout during ZIP for a single recoverable item -> single-file proxy.
- ZIP failure for one recoverable item -> single-file proxy.
- ZIP failure for multi-resource or unsafe item -> blocked response.
- Proxy failure for a browser-safe single video -> browser direct route.
- Proxy failure without a safe direct route -> blocked response.

The system does not retry forever and does not start background queues. Existing provider-specific retry settings still apply inside each provider.

## Observability Fields

`DownloadRequestLog` now records:

- `recovery_count`
- `fallback_count`
- `recovery_action`
- `recovery_reason`
- `fallback_from_route`
- `fallback_to_route`
- `failure_type`

These fields let the admin pages answer: did the system recover, what failed, and which route was selected after fallback?

## Provider Stability Score

The admin console computes a 7-day stability score from download logs:

- success rate
- timeout rate
- failure rate
- average latency

Status mapping:

- `stable`: score >= 0.8
- `degraded`: score >= 0.55
- `unstable`: score < 0.55

This score is advisory. It does not change provider runtime gates automatically.

## Admin Console

`/admin/console/` now includes a `System Stability Panel` with:

- 24h download failure rate
- 24h routing failure rate
- 24h provider failure rate
- 24h recovery count
- 24h fallback count
- top failure types
- provider stability score
- unstable provider list

## Deployment

Deploy the branch as usual:

```bash
cd /srv/animeimages
DEPLOY_BRANCH=codex/production-stability-v0.2 ./scripts/deploy.sh
```

This version adds a migration:

```bash
python manage.py migrate
```

The deploy script already runs migrations.

## Rollback

Rollback remains code-first:

```bash
cd /srv/animeimages
git fetch origin
git reset --hard <previous-known-good-commit-or-tag>
source .venv/bin/activate
python manage.py migrate
python manage.py collectstatic --noinput
./scripts/restart.sh
```

If the rollback target predates migration `0012`, use normal Django migration rollback only after confirming no newer log fields are needed:

```bash
python manage.py migrate xhs_download 0011
```

Because v0.2 fields are observability-only, leaving the columns in the database during code rollback is usually safe.
