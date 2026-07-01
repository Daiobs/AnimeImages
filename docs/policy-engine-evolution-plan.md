# Policy Engine Evolution Plan

AnimeImages is moving from scattered download decisions to a single policy brain.

Current production still uses:

- Routing Engine v2/v3 for execution route selection
- Download Cost Guard for hard safety limits
- Provider runtime gates for provider access
- Observability logs for feedback

This plan introduces Policy Engine gradually. The default mode is shadow-only.

## Target Architecture

```text
Request
  -> Policy Engine
      -> Routing Engine as execution layer
      -> Cost Guard as hard constraint layer
      -> Provider system as input/data source
```

Policy Engine does not download files. It decides and explains:

- allow or block
- route: direct / proxy / zip / blocked
- provider preference
- cost level
- confidence
- reason chain

## Phase 1: Policy Engine v3

Status: shadow learning.

Purpose:

- read `DownloadRequestLog`, `ParseRequestLog`, and `RoutingDecisionLog`
- standardize 24h and 7d learning windows
- produce `shadow_recommendation`, `confidence`, and `estimated_savings_bytes`
- persist learning snapshots in `PolicyLearningRun`

Command:

```bash
python manage.py inspect_policy_engine --window-hours 24 --json
python manage.py inspect_policy_engine --all-windows --record --json
```

Production impact:

- none
- no route override
- no provider setting changes
- no Cost Guard changes

## Phase 2: Policy Engine v3.5

Status: implemented as a single decision API, default shadow.

Core API:

```python
decide_final_policy(context)
```

Output:

```json
{
  "route": "direct|proxy|zip|blocked",
  "provider": "native|yt_dlp|kukutool|...",
  "cost_level": "low|medium|high",
  "confidence": 0.0,
  "reason_chain": [],
  "policy_scores": {
    "cost": 0.0,
    "provider": 0.0,
    "routing": 0.0
  }
}
```

Policies:

- `CostPolicy`: cost level and hard block signal
- `ProviderPolicy`: provider constraints such as server-side merge or signed resource validation
- `RoutingPolicy`: wraps Routing Engine v3 feedback

Conflict resolver:

- cost weight: `0.4`
- provider weight: `0.4`
- routing weight: `0.2`
- Cost Guard block always wins
- otherwise highest weighted route wins

Current activation:

- `DownloadRequestLog` records `policy_cost_score`, `policy_provider_score`, `policy_routing_score`, `final_policy_route`, and `policy_confidence`
- execution still follows existing routing
- policy decisions are advisory

Future activation:

- set deployment/runtime flag `POLICY_ENGINE_MODE=v3_5` or `POLICY_ENGINE_V35_ENABLED=True`
- then route override can be introduced behind a small rollout
- target: 80% policy decisions, 20% v2 fallback

## Phase 3: Policy Engine v4

Status: shadow adaptive recommendation.

Purpose:

- analyze success rate, server cost, provider diversity, and route failure
- recommend adaptive weights
- keep humans/admin gates in control

Command:

```bash
python manage.py inspect_policy_engine --window-hours 24 --adaptive --json
```

Constraints:

- no AI model
- no external ML service
- no Celery/queue dependency for decisions
- no automatic ProviderRuntimeSetting changes
- no Cost Guard behavior changes

## Fallback

Fallback is built into v3.5:

- if policy mode is shadow, production route remains the current Routing v2/v3 route
- if policy evaluation fails, download continues and logs `policy_shadow_evaluation_failed`
- if Cost Guard blocks, policy also returns `blocked`

## Rollback

Rollback is low-risk:

1. Keep `POLICY_ENGINE_MODE=shadow` or unset.
2. Ignore policy fields in `DownloadRequestLog`.
3. Stop running `inspect_policy_engine --record`.
4. If necessary, revert the migration adding policy log fields and `PolicyLearningRun`.

Existing download execution does not depend on the new policy tables.
