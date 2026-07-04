# Policy Control Convergence System

This stage upgrades the Policy Production Control Layer into a traceable
control lifecycle.

It does not change production routing, provider gates, cost guard limits, or
download execution behavior. It records control decisions, execution outcomes,
feedback, and convergence metrics so operators can decide what to approve next.

## Modules

- `xhs_download/services/control_state_machine.py`
  - owns the control lifecycle state transitions.
  - valid states: `pending`, `approved`, `rejected`, `auto_applied`,
    `executed`, `failed`.
  - rejects invalid jumps such as `executed -> approved`.

- `xhs_download/services/control_execution_tracker.py`
  - records execution outcomes from download observability.
  - binds `request_id`, `route`, `provider`, `cost`, and result to a
    `PolicyControlRecord`.
  - writes `executed` or `failed` state without changing runtime behavior.

- `xhs_download/services/control_convergence_engine.py`
  - aggregates control history.
  - writes feedback summaries back to control records.
  - reports state distribution, execution success rate, approval rate,
    failure patterns, drift score, policy consistency score, and stability.

## Data Model

`PolicyControlRecord` is the control lifecycle record:

```json
{
  "request_id": "",
  "policy_decision": "",
  "control_decision": "",
  "state": "pending|approved|rejected|auto_applied|executed|failed",
  "execution_result": "",
  "provider": "",
  "route": "",
  "cost": 0,
  "created_at": ""
}
```

The record may be linked to `PolicyOptimizationProposal`, but it can also store
observed download decisions that do not have a proposal.

## Feedback Loop

The existing Feedback Loop now calls `apply_control_feedback_to_records()`.

The feedback writeback updates:

- `feedback_summary`
- `control_confidence`
- `drift_score`
- `consistency_score`
- `failure_pattern`

This is an observability writeback only. It does not mutate production
configuration.

## Admin

`/admin/control-convergence/` shows:

- control state distribution
- execution success rate
- approval rate
- failure patterns
- control drift score
- policy consistency score
- recent control records
- non-executing suggestions

Access uses the same staff permission style as the existing observability
pages. Superusers can view it; staff need the relevant view permission.

## Safety Boundary

The convergence system is intentionally read-mostly:

- no automatic policy execution
- no routing engine changes
- no provider gate changes
- no cost guard changes
- no Celery or queue
- no AI/ML model

## Deploy

Run migrations:

```bash
python manage.py migrate
```

Then verify:

```bash
python manage.py check
python manage.py test xhs_download.tests.PolicyEngineEvolutionTests --noinput
```

Open:

```text
/admin/control-convergence/
```

## Rollback

1. Stop using `/admin/control-convergence/`.
2. Revert the code branch or deploy the previous release.
3. The new table is observational. Existing routing/download behavior does not
   depend on it.
4. If needed, keep `PolicyControlRecord` rows as audit history or drop the
   migration during a database rollback window.
