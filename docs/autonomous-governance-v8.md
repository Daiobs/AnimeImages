# Autonomous Governance System v8

v8 adds a governance and risk control layer on top of the v7 autonomous
optimization system.

v7 can detect optimization opportunities, run shadow experiments, and produce
mutation decisions. v8 does not replace v7. It governs v7 output before any
future production change can be considered.

## Module

`xhs_download/services/governance_engine.py`

Main components:

- `governance_engine()`
  - receives v7 evolution proposals, shadow results, mutation decisions, system
    metrics, and policy control history.
  - returns `allow`, `deny`, or `partial_allow` decisions.

- `risk_governor()`
  - scores cost spike risk, routing instability, provider volatility, and
    traffic anomaly.
  - classifies risk as `low`, `medium`, `high`, or `critical`.

- `evolution_limiter()`
  - caps a single policy change to `10%`.
  - enforces the daily mutation limit.
  - blocks when rollback failure rate crosses the threshold.

- `policy_guard()`
  - denies high or critical risk.
  - denies non-better shadow results.
  - denies proposals not allowed by the v7 mutation controller.
  - returns `partial_allow` when the change is safe but must be capped.

- `record_governance_audit_decision()`
  - writes an explicit audit record to `PolicyControlRecord`.
  - does not mutate production routing, provider gates, or cost settings.

## Admin

`/admin/governance-v8/` shows:

- governance decisions
- risk scores
- blocked optimizations
- partial applied changes
- system safety status
- system autonomy level
- risk exposure
- mutation rate
- stability index
- governance audit records

## v7 to v8

- v7 = autonomous proposal and shadow evaluation.
- v8 = governed autonomy.

The v8 layer makes v7 output controllable by adding:

- risk scoring
- mutation boundaries
- policy guard rules
- audit-ready decisions
- rollback-aware denial rules

## Safety

- no AI or ML model
- no automatic production mutation
- no unlimited mutation
- no Routing Engine core change
- no Provider gate change
- no Cost Guard change
- all v7 behavior is evaluated through governance decisions
- audit writes are explicit, not triggered by page rendering

## Deploy

No v8 migration is required. The feature uses existing `PolicyControlRecord`
for explicit governance audit records.

Verify:

```bash
python manage.py check
python manage.py makemigrations --check --dry-run
python manage.py test xhs_download.tests.PolicyEngineEvolutionTests --noinput
```

Open:

```text
/admin/governance-v8/
```

## Rollback

Switch back to the v7 branch or remove the v8 admin URL/template/service.
Because v8 does not mutate production settings and introduces no new table,
rollback requires no data migration.
