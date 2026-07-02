# Feedback Loop MVP

Feedback Loop MVP turns production logs into a read-only learning loop:

```text
Observation -> Feedback -> Insight -> Suggestion
```

It does not modify production behavior. It does not change the routing engine,
cost guard, provider gates, download execution, or runtime settings.

## Admin Page

```text
/admin/feedback-loop/
```

The page is available to superusers or staff with observability view
permissions. It shows:

- signals
- insights
- suggestions
- provider trend
- routing trend

## Feedback Engine

Source data:

- `DownloadRequestLog`
- `ParseRequestLog`
- `ProviderHealthTestRun`
- `RoutingDecisionLog`

Output shape:

```json
{
  "provider": "all",
  "signals": {},
  "routes": {}
}
```

Signals include provider reliability, route success/fallback, cost accuracy,
server/browser egress, failure distribution, and health-test results.

## Insight Engine

The insight layer detects:

- failure increase
- provider degradation
- cost spike
- route inefficiency

Output shape:

```json
{
  "insights": []
}
```

## Policy Suggestion Engine

The suggestion layer maps insights into advisory policy suggestions. It always
sets:

```json
{
  "automatic_execution": false
}
```

Suggestions are for humans. The system never writes runtime settings or changes
production decisions automatically.

## Deploy

Run:

```bash
python manage.py check
python manage.py makemigrations --check --dry-run
python manage.py test xhs_download --noinput
python manage.py collectstatic --noinput
```

No migration is required.

## Rollback

Rollback is code-only:

1. Deploy the previous branch or commit.
2. No database rollback is needed.
3. Existing logs remain valid because Feedback Loop reads current log tables
   without adding columns.
