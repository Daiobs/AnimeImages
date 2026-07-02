# Production Intelligence System v1

Production Intelligence v1 connects the existing Policy Engine and admin
observability data into one read-only feedback loop:

```text
logs -> metrics -> policy insights -> recommendations
```

It does not change the download execution layer. Routing Engine, Cost Guard,
Provider runtime gates, ZIP/proxy/direct execution, and existing download
limits remain the production authority unless Policy Engine v3.5 is explicitly
enabled through its existing runtime setting.

## Policy Engine v3.5 -> v4 Convergence

The unified decision entry is:

```python
decide_final_policy(context)
```

v3.5 already converges these inputs:

- CostPolicy: size limits, visitor video limits, server-cost constraints.
- ProviderPolicy: native / yt-dlp / gallery-dl / KuKuTool / YouTube merge
  server-side requirements.
- RoutingPolicy: Routing Engine v3 recommendation and v2 fallback behavior.

The conflict resolver applies weighted scoring:

- cost: 0.4
- provider: 0.4
- routing: 0.2

The output contains:

- final route: direct / proxy / zip / blocked
- provider
- cost level
- confidence
- reason chain
- policy scores
- weighted route scores

v4 is currently recommendation mode. Observability v2 reads production logs and
generates adaptive weight suggestions and operational insights, but it does not
write runtime settings or automatically modify production strategy.

## Observability v2

Admin page:

```text
/admin/observability-v2/
```

The page is available to superusers, or staff with observability view
permissions such as `xhs_download.view_downloadrequestlog`.

Views:

- Routing: direct / proxy / zip / blocked distribution, success rate, failure
  rate, fallback rate.
- Provider: success rate, latency, stability score, failure heatmap.
- Cost: server egress trend, browser egress trend, estimated savings, ZIP
  ratio trend.
- Policy: decision consistency, confidence distribution, route recommendation
  accuracy, shadow vs actual diff.

Core metrics:

- `policy_conflict_rate`: Policy recommendation differs from actual route.
- `routing_efficiency`: success rate adjusted by fallback frequency.
- `cost_accuracy`: actual bytes vs estimated bytes accuracy.
- `provider_reliability`: provider success rate in the selected window.
- `fallback_frequency`: fallback count divided by download request count.

## Data Flow

1. Download requests write `DownloadRequestLog`.
2. Policy shadow evaluation writes policy scores, final policy route,
   confidence, active/shadow source, and estimated savings.
3. Observability v2 aggregates the logs into production metrics.
4. Metrics produce Policy insights and v4 recommendation-only output.
5. Operators inspect recommendations and decide whether to change runtime
   settings manually.

No part of Observability v2 automatically changes:

- Provider runtime gates
- Policy weights
- Download limits
- Routing behavior
- Cost guard behavior

## Deploy

Run the normal deployment checks:

```bash
python manage.py check
python manage.py makemigrations --check --dry-run
python manage.py test xhs_download --noinput
python manage.py collectstatic --noinput
```

No new database migration is required for v1 because it reuses existing
observability and policy log fields.

After deploy:

1. Open `/admin/observability-v2/`.
2. Confirm the page renders for a superuser.
3. Trigger one parse and one download.
4. Confirm Routing, Provider, Cost, and Policy sections update.
5. Keep Policy Engine in shadow/staff mode until the dashboard shows stable
   conflict and fallback metrics.

## Rollback

Rollback is low risk:

1. Revert this code branch or deploy the previous tag/commit.
2. No migration rollback is needed.
3. Existing `DownloadRequestLog` policy fields can remain in the database.
4. Existing Policy Engine runtime settings are unchanged by Observability v2.
