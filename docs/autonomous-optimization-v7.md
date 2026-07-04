# Autonomous Optimization System v7

v7 adds a safe autonomous evolution layer on top of v6 stabilization.

The system can discover optimization opportunities, run shadow experiments,
evaluate mutation gates, and produce rollback plans. It does not directly mutate
production routing, provider gates, or cost settings.

## Module

`xhs_download/services/autonomous_evolution_engine.py`

Main components:

- `detect_optimization_opportunities()`
  - reads `DownloadRequestLog` history, Feedback signals, provider stats, and
    v6 stability signals.
  - creates routing adjustment proposals with expected gain and confidence.
  - lowers proposal confidence when current Feedback risk is high.

- `shadow_experiment()`
  - clones historical traffic samples in memory.
  - compares route metrics without changing production traffic.

- `policy_mutation_controller()`
  - allows apply only when confidence is high and shadow result is better.
  - otherwise rejects or delays the proposal.

- `rollback_engine()`
  - checks success drop, cost increase, latency increase, and error spike.
  - returns a previous stable policy restore plan when rollback is needed.

- `policy_execution_history()`
  - summarizes `PolicyControlRecord` history so v7 can reason about previous
    policy execution outcomes without writing new state.

## Safety Rules

- no direct production mutation
- no AI or ML model
- no queue or Celery
- no new platform
- every proposal must have shadow output
- every allowed mutation must be explainable
- rollback is represented as a plan and does not bypass approval gates

## Admin

`/admin/autonomous-evolution/` shows:

- evolution proposals
- shadow experiment results
- mutation gate decisions
- applied/rejected/delayed policy decisions
- rollback history
- system gain metrics
- provider stats
- policy execution history

## v6 Compatibility

v7 consumes the v6 stability snapshot and treats it as a guardrail. It does not
replace or modify v6. Production behavior remains controlled by existing
Routing, Provider, Cost Guard, and Approval Gate logic.

## Deploy

No migration is required for v7.

Verify:

```bash
python manage.py check
python manage.py test xhs_download.tests.PolicyEngineEvolutionTests --noinput
```

Open:

```text
/admin/autonomous-evolution/
```

## Rollback

Remove the branch or the admin URL/template. Because v7 is read-only and stores
no runtime state, rollback requires no data migration.
