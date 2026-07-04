# Policy Production Control Layer

This layer upgrades Policy Intelligence v5 from an advisory optimization queue
into a controlled production decision surface.

## Goal

The control layer receives v5 policy suggestions, scores risk, sends the result
through an approval gate, and records controlled proposals.

It does not modify production routing, provider gates, or cost limits without an
explicit approval path.

## Modules

- `xhs_download/services/policy_risk_engine.py`
  - scores cost, provider, routing, policy-weight, historical instability, and
    repeated-change pressure.
- `xhs_download/services/approval_gate.py`
  - returns `auto_apply`, `require_approval`, or `reject`.
  - supports admin approve, reject, and rollback.
- `xhs_download/services/policy_control_layer.py`
  - combines suggestions, risk scoring, and approval gate output.
  - persists controlled proposals in `PolicyOptimizationProposal`.

## Output

`evaluate_policy_control_layer()` returns:

```json
{
  "decision": "auto_apply | require_approval | reject",
  "reason": "",
  "risk_score": 0.0,
  "impact_area": "routing|cost|provider|policy_weights",
  "approved_changes": [],
  "blocked_changes": []
}
```

## Safety Rules

- Provider changes are high risk.
- Routing changes are medium risk.
- Cost changes are medium or high risk depending on estimated impact.
- Historical instability increases risk.
- Repeated similar changes increase drift pressure.
- Low-risk changes can be auto-applied to the proposal queue.
- Medium-risk changes require human approval.
- Unsafe or extreme-risk changes are rejected.

## Admin

`/admin/policy-control/` shows:

- current policy suggestions
- risk score
- approval gate decision
- pending approvals
- approved changes
- rejected changes
- rolled-back changes

Available actions:

- generate controlled proposals
- approve
- reject
- rollback

`/admin/control-convergence/` extends this view with lifecycle and execution
feedback:

- control state distribution
- execution success rate
- approval rate
- failure patterns
- policy drift score
- policy consistency score

## Production Mutation

Generating controlled proposals does not mutate runtime settings.

Rollback only restores explicitly recorded runtime mutations from
`application_result.before`. The current MVP only allows controlled restoration
of Policy Runtime fields already used by the v5 optimization queue.

## Deployment

Run migrations:

```bash
python manage.py migrate
```

Rollback:

1. Stop using `/admin/policy-control/`.
2. Reject pending proposals.
3. Roll back any proposal with `runtime_mutated=True`.
4. Set `PolicyRuntimeSetting.v4_auto_weights_enabled=False`.
