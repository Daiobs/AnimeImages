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

Status: implemented as a single decision API plus a guarded execution hook.

Default behavior is still shadow-only. Production routing is unchanged until
`PolicyRuntimeSetting.v35_mode` is moved away from `shadow`.

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

Current activation and feedback:

- `DownloadRequestLog` records `policy_cost_score`, `policy_provider_score`, `policy_routing_score`, `final_policy_route`, and `policy_confidence`
- `DownloadRequestLog.route_type` remains the actual executed route
- `DownloadRequestLog.final_policy_route` is the recommended route
- `DownloadRequestLog.policy_active` records whether v3.5 actually controlled the route
- `DownloadRequestLog.policy_decision_source` records shadow/fallback/active source
- `DownloadRequestLog.policy_estimated_savings_bytes` stores the estimated server-cost delta
- execution follows existing routing while `v35_mode=shadow`

Runtime gate:

- `PolicyRuntimeSetting.v35_mode=shadow`: observe only
- `PolicyRuntimeSetting.v35_mode=staff`: staff requests can use policy decisions
- `PolicyRuntimeSetting.v35_mode=public_gray`: public rollout by percent
- `PolicyRuntimeSetting.v35_mode=public`: all requests can use policy decisions

80/20 target:

- set `v35_mode=public_gray`
- set `v35_rollout_percent=80`
- 80% of rollout buckets use Policy Engine v3.5
- 20% stay on Routing v2/v3 fallback

Execution safety:

- Routing Engine still builds the executable route payload.
- Policy v3.5 can only override to routes the current execution layer can run safely.
- If a policy route is not executable, the system keeps the existing route and records `policy_fallback_reason`.
- Cost Guard remains a hard final constraint.

## Phase 3: Policy Engine v4

Status: adaptive feedback loop implemented with safe runtime gate.

Default behavior is still shadow-only. v4 does not change Provider settings and
does not change Cost Guard behavior.

Purpose:

- analyze success rate, server cost, provider diversity, and route failure
- recommend adaptive weights
- keep humans/admin gates in control
- optionally feed adaptive weights into v3.5 conflict resolution when explicitly enabled

Command:

```bash
python manage.py inspect_policy_engine --window-hours 24 --adaptive --json
```

Feedback inputs:

- actual route: `DownloadRequestLog.route_type`
- recommended route: `DownloadRequestLog.final_policy_route`
- actual success/failure: `DownloadRequestLog.status`
- server cost: `actual_bytes`, `server_egress_estimated_bytes`
- latency: `duration_ms`
- provider failures by `provider`

Runtime gate:

- `PolicyRuntimeSetting.v4_adaptive_shadow_enabled=True`: keep reporting adaptive recommendations
- `PolicyRuntimeSetting.v4_auto_weights_enabled=False`: default; do not affect real decisions
- `PolicyRuntimeSetting.v4_auto_weights_enabled=True`: use feedback-derived weights in Policy Engine conflict resolution
- `PolicyRuntimeSetting.v4_adaptive_min_confidence`: minimum confidence required before using adaptive weights
- if samples or confidence are insufficient, the engine falls back to static weights

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
- if policy active rollout misses, production route remains the current Routing v2/v3 route
- if policy route cannot be mapped safely to the current execution layer, existing routing stays in place
- if Cost Guard blocks, policy also returns `blocked`

## Rollback

Rollback is low-risk:

1. Set `PolicyRuntimeSetting.v35_mode=shadow`.
2. Set `PolicyRuntimeSetting.v35_rollout_percent=0`.
3. Set `PolicyRuntimeSetting.v4_auto_weights_enabled=False`.
4. Ignore policy fields in `DownloadRequestLog`.
5. Stop running `inspect_policy_engine --record`.
6. If necessary, revert the migrations adding policy runtime/log fields and `PolicyLearningRun`.

Existing download execution does not depend on the new policy tables.
