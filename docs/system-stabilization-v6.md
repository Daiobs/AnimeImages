# System Stabilization Layer v6

v6 turns the policy/control/feedback stack into a single stable decision view.

The goal is not to optimize aggressively. The goal is to reduce oscillation,
merge conflicting signals, and keep production behavior predictable.

## Module

`xhs_download/services/stabilization_engine.py`

The module contains:

- `policy_convergence_engine()`
  - merges multiple policy recommendations into one stable route.
  - reports `policy_drift_rate` and `decision_fluctuation_rate`.

- `control_simplifier()`
  - groups duplicate control decisions.
  - reports `control_conflict_rate` and approval redundancy.

- `feedback_stabilizer()`
  - smooths feedback volatility.
  - caps extreme failure delta movement.
  - reports `feedback_volatility` and `provider_instability`.

- `stabilization_engine()`
  - combines policy, control, and feedback results.
  - returns a stable route, stability score, conflict level, and reason chain.

## Output

```json
{
  "stable_route": "direct|proxy|zip",
  "stability_score": 0.0,
  "conflict_level": "low|medium|high",
  "reason": [],
  "metrics": {
    "stability_score": 0.0,
    "policy_drift_rate": 0.0,
    "decision_fluctuation_rate": 0.0,
    "execution_consistency_score": 0.0
  }
}
```

## Admin

`/admin/stability-v6/` shows:

- system stability trend
- routing fluctuation
- provider instability
- control conflict rate
- feedback volatility
- stable route

The page is read-only.

## Safety Boundary

v6 does not:

- change production policy
- change routing engine behavior
- change provider runtime gates
- change cost guard limits
- add Celery or queues
- add AI or ML
- add platforms
- auto rollout anything

## Deploy

No migration is required for v6 itself.

Verify:

```bash
python manage.py check
python manage.py test xhs_download.tests.PolicyEngineEvolutionTests --noinput
```

Open:

```text
/admin/stability-v6/
```

## Rollback

Revert the branch or remove the admin URL/template. The engine is read-only and
does not own production state, so rollback does not require data migration.
