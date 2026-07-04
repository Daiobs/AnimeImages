# Policy Intelligence v5

Policy Intelligence v5 adds a human-in-the-loop optimization queue on top of
the v4 recommendation and Safety Guard layer.

## Goal

Move from advisory suggestions to controlled optimization:

- low risk: may be auto-applied into the optimization queue
- medium risk: requires human approval
- high risk: rejected

v5 still keeps production safety first. Queue generation does not modify
Provider gates, Cost Guard, or Routing Engine. Runtime mutation is only possible
when explicitly requested by trusted code paths.

## Queue

`PolicyOptimizationProposal` records every optimization proposal:

- suggestion kind and policy area
- proposed change
- safety decision and risk
- drift score
- status
- whether automatic execution occurred
- whether runtime settings were mutated

Statuses:

- `auto_applied`: low-risk proposal accepted by the queue
- `needs_approval`: medium-risk or drift-sensitive proposal
- `rejected`: unsafe proposal
- `approved` / `applied`: reserved for future manual approval flows

## Drift Control

`evaluate_policy_drift()` prevents repeated low-risk changes from accumulating
into a high-risk behavior shift. If too many similar proposals were
auto-applied in the drift window, the next proposal is routed to human review.

## Runtime Mutation

By default:

- low-risk proposals can be marked `auto_applied`
- `runtime_mutated=False`
- production behavior is unchanged

When `allow_runtime_mutation=True`, the current MVP can enable
`PolicyRuntimeSetting.v4_auto_weights_enabled` for low-risk adaptive-weight
proposals. This is intentionally explicit so operators can separate queue
automation from production behavior changes.

## Admin

`/admin/policy-engine/` displays recent optimization queue records. The page
does not create new proposals while rendering.

## Rollback

1. Stop running v5 queue generation.
2. Set `PolicyRuntimeSetting.v4_auto_weights_enabled=False`.
3. Leave existing `PolicyOptimizationProposal` rows as audit history.
4. If necessary, delete queued proposals from Django admin as superuser.
