# Policy Intelligence v4

Policy Intelligence v4 is the safe self-optimizing recommendation layer.
It analyzes existing logs and produces policy suggestions with safety review
metadata, but it never changes production behavior by itself.

## Goal

Move from passive policy feedback to safe advisory optimization:

1. Read `DownloadRequestLog`, `ParseRequestLog`, routing logs, and provider
   feedback through the existing learning/adaptive pipeline.
2. Generate policy suggestions such as adaptive Policy Engine weights.
3. Validate each suggestion through Safety Guard.
4. Return `approve`, `review`, or `reject` for operators.
5. Keep production behavior unchanged unless a human changes runtime settings.

## Core API

`build_policy_evolution_recommendation(window_hours=24)` returns:

- `version`: `v4`
- `mode`: `recommendation_only`
- `suggestions`: advisory policy suggestions
- `safety_summary`: counts of `approve`, `review`, and `reject`
- `automatic_execution`: always `False`
- `production_behavior_changed`: always `False`

Each suggestion includes:

- `kind`
- `policy_area`
- `proposed_change`
- `confidence`
- `sample_count`
- `reason_chain`
- `safety.decision`
- `safety.risk`
- `safety.reason_chain`

## Safety Guard

`validate_policy_suggestion()` rejects automatic execution in v4. It also
checks that adaptive weights have the expected shape, sufficient samples,
enough confidence, and a bounded change size.

Decision meanings:

- `approve`: low-risk advisory suggestion; still not automatically applied.
- `review`: needs human review due to insufficient samples, low confidence, or
  large weight change.
- `reject`: invalid or unsafe suggestion, such as automatic execution.

## Admin

`/admin/policy-engine/` now shows v4 Safety Guard suggestions beside the
existing adaptive weight diagnostics.

## Production Impact

None by default.

v4 does not modify:

- Routing Engine
- Cost Guard
- Provider runtime gate
- Download execution
- PolicyRuntimeSetting

## Rollback

Rollback is operational and does not require a database migration:

1. Ignore the v4 Safety Guard panel.
2. Keep `PolicyRuntimeSetting.v4_auto_weights_enabled=False`.
3. Keep `PolicyRuntimeSetting.v35_mode=shadow` if you want pure observation.
