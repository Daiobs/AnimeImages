# Branch Cleanup Candidates v1

Date: 2026-08-14

Status: `SUPERSEDED_BY_EXECUTION_AUDIT`. No deletion was authorized by this
inventory. The execution evidence and exact-ref allowlist are maintained in
`docs/research/repository-branch-archive-cleanup-v1.md`.

This inventory was built from fetched `origin/*` refs and GitHub PR metadata.
Squash-merged branches are not necessarily ancestors of main, so a future
cleanup must also verify PR head OID and merge-tree equivalence before deletion.

## KEEP_CHECKPOINT

These refs are immutable recovery/audit points and are not cleanup candidates:

- `checkpoint/main-pre-production-reconciliation-v1`
- `checkpoint/production-douyin-ultra-retry-v1`
- `checkpoint/production-service-worker-streaming-v1`
- `checkpoint/production-browser-blob-recovery-v1`
- `checkpoint/douyin-ultra-retry-start-20260813`
- `checkpoint/sw-streaming-productionization-start-20260812`
- `checkpoint/blob-progress-streaming-start-20260812`
- `checkpoint/native-archaeology-start-20260812`
- `checkpoint/v1-autonomy-start-20260812`
- `checkpoint/main-production-converged-v1` after reconciliation merge

## ACTIVE

- `reconcile/production-stable-main-v1`: current reconciliation branch until
  its PR is merged and audited.

## RESEARCH_SUPERSEDED

- `research/admin-runtime-control-audit-v1` / PR #27: conclusions absorbed by
  PR #28 and the current Admin documentation; close the Draft PR after
  reconciliation, keep the branch as research history until a cleanup task.
- `codex/browser-streaming-direct-download-v2` / PR #32: File System Access
  experiment superseded by accepted Service Worker Streaming; close after
  reconciliation and retain its research branch for later audited cleanup.
- `research/native-download-archaeology-v2`
- `research/zero-server-browser-streaming-v1`
- `stabilize/v1-autonomous-recovery-20260812`
- `feat/blob-progress-ux-v1`
- `feat/service-worker-streaming-production-v1`
- `feat/douyin-ultra-retry-continuation-v1`

The last three feature branches include production/recovery lineage. Preserve
them until the converged main checkpoint and a later production deployment are
both established.

## MERGED_SAFE_TO_REVIEW_FOR_DELETION

These branches have merged PRs. They are candidates only after a future task
checks PR head OID and tree identity:

- `codex/admin-release-management-v1` (#17)
- `codex/batch-parse-stability-v1` (#21)
- `codex/batch-public-download-visibility-v1` (#26)
- `codex/batch-runtime-admin-clarity-v1` (#28)
- `codex/browser-direct-native-launch-hotfix-v1` (#31)
- `codex/browser-direct-restoration-v1` (#29)
- `codex/douyin-parse-download-reliability-v1` (#24)
- `codex/fix-batch-parse-issues` (#15)
- `codex/fix-production-deploy-hardening` (#14)
- `codex/fix-release-readonly-git-state` (#18)
- `codex/production-followup-batch-provider-labels` (#16)
- `codex/provider-result-lifecycle-cleanup-v1` (#33)
- `codex/refactor-batch-parse-service` (#13)
- `codex/release-executor-wiring-v1` (#20)
- `codex/shared-parse-preferences-v1` (#22)
- `codex/web-concurrency-provider-isolation-v1` (#30)
- `codex/xhs-fallback-authority-v1` (#25)
- `codex/xhs-media-fidelity-v1` (#23)

## UNKNOWN_OR_HISTORICAL

These branches have no directly associated merged PR in the current GitHub
query. Keep them until ownership, unique commits, and historical value are
reviewed individually:

- `codex/autonomous-governance-v8`
- `codex/autonomous-optimization-v7`
- `codex/batch-parse-ui-polish-v1`
- `codex/browser-blob-download-provider`
- `codex/douyin-tiantianjiexi-ultra-primary`
- `codex/download-handoff-strategy-research`
- `codex/policy-control-convergence-system`
- `codex/policy-engine-v1`
- `codex/policy-engine-v2`
- `codex/policy-engine-v3-adaptive-weights`
- `codex/policy-intelligence-v3.5`
- `codex/policy-intelligence-v4`
- `codex/policy-intelligence-v5`
- `codex/policy-production-control-layer`
- `codex/production-control-plane-v1`
- `codex/provider-browser-direct-download-handoff`
- `codex/provider-runtime-budget-controls`
- `codex/system-stabilization-v6`

No branch in this document is authorized for deletion by this reconciliation.
