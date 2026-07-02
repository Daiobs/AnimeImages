# Policy Intelligence v3.5

Policy Intelligence v3.5 is the convergence layer for download decisions.
It keeps production behavior conservative by default, but makes Policy Engine
the common decision hook before download execution.

## Goal

Move the system from separate routing decisions toward one policy-aware
decision flow:

1. Build a route candidate with the existing routing layer.
2. Evaluate `decide_final_policy(context)`.
3. Keep the route unchanged in shadow mode.
4. Allow Policy Engine to override the route only when runtime mode enables it.
5. Execute the selected route with the existing download execution layer.

## Runtime Modes

`PolicyRuntimeSetting.v35_mode` controls whether v3.5 only observes or actively
controls routing:

- `shadow`: evaluate and log policy recommendation, do not change behavior.
- `staff`: staff requests may use policy-selected routes.
- `public_gray`: public rollout by `v35_rollout_percent`.
- `public`: all requests may use policy-selected routes.

## Current Download Flow

All routed download modes now pass through `apply_policy_decision_to_route()`:

- `auto`
- `browser`
- `proxy`
- `server_single`
- `zip`

Server-save mode remains behind its explicit server-save gate. When server-save
is disabled, requests fall back into the policy-aware `auto` path.

The legacy async task endpoints remain execution-only ZIP task helpers. They do
not choose between direct/proxy/zip routes, so v3.5 does not treat them as a
separate decision source.

## Boundaries

Policy Engine does not download files. It only returns a decision:

- route
- provider
- confidence
- reason chain
- policy scores

The existing execution layer still handles:

- direct browser route response
- single-file server download
- ZIP archive generation
- download limits
- provider manifest validation
- recovery and self-healing

Cost Guard remains a hard safety layer. Policy Engine can recommend a route, but
download preflight and runtime byte limits still block unsafe requests.

## Verification

The v3.5 branch includes tests that prove:

- Policy Engine can override explicit `zip` mode when v3.5 is active.
- Policy Engine can override explicit `proxy` mode when v3.5 is active.
- Policy Engine can override explicit `server_single` mode when v3.5 is active.
- Default shadow mode records policy fields without changing routes.
- Cost Guard still blocks unsafe video requests.

Rollback is operational:

1. Set `PolicyRuntimeSetting.v35_mode=shadow`.
2. Set `PolicyRuntimeSetting.v35_rollout_percent=0`.
3. Keep v4 adaptive weights disabled.
