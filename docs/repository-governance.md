# Repository Governance

## Canonical Lines

AnimeImages uses four deliberately separate sources of truth:

| Concern | Authority |
| --- | --- |
| Code development | Latest fetched `origin/main` |
| Deployed code | Exact production SHA plus an immutable `checkpoint/production-*` ref |
| Runtime values | Production environment and persisted runtime settings |
| Human summary | `docs/current-version-status.md` |

`main` is not an automatic deployment trigger. A merge changes the canonical
development baseline only. Production changes require a separate exact-SHA
deployment and acceptance task.

## Development Workflow

1. Fetch and verify `origin/main` before a task starts.
2. Use one task branch and one pull request for one concrete task.
3. Keep only one active development branch in the retained product worktree.
4. After merge, require a clean tree, fetch, and switch the worktree to detached
   latest `origin/main` before creating the next branch.
5. Do not use an old feature, production, checkpoint, or research branch as a
   new development base.
6. Do not develop directly in the main synchronization worktree.

Every non-trivial PR follows the Extended Description rules in
`docs/codex-handoff.md`. A PR that changes a contract in
`docs/production-behavior-baseline-v1.md` must include a clearly labeled
`BEHAVIOR_CHANGE` section with user impact, runtime/configuration impact,
verification, deployment notes, and rollback.

## Production Line

- Record every production deployment as an exact SHA.
- Create or verify an immutable production checkpoint after acceptance.
- A production-only hotfix must be reconciled back through a reviewed main PR
  promptly; it must not become a second long-lived development line.
- Production-only work may exist only until the next reconciliation window.
- Never copy production secrets, `.env.production`, or persisted runtime rows
  into Git to simulate parity.
- Production deployment and main reconciliation are separate changes with
  separate rollback decisions.

## Production Drift

Set the repository state to `PRODUCTION_CODE_DRIFT` when either condition is
true:

- the deployed production SHA is not in current main ancestry; or
- production contains accepted code that has not been reconstructed in main.

`PRODUCTION_CODE_DRIFT` is a P0 governance backlog item. While it exists, avoid
accumulating unrelated product features. Create both a production checkpoint
and a pre-reconciliation main checkpoint, audit both sides, and reconstruct the
accepted behavior on top of main. Do not reset or force-push main to production.

## Checkpoints And Branches

- Checkpoint refs are recovery evidence and must not move.
- Feature and research branches are temporary workspaces, not sources of truth.
- Squash-merged branches require PR head/tree verification before deletion;
  ancestry alone is insufficient.
- Draft research PRs should be closed with an explanatory supersession comment
  once their conclusions are absorbed. Do not merge obsolete experiments just
  to preserve history.
- Branch deletion is a separate audited cleanup task. Never delete a branch as
  an incidental part of reconciliation or deployment.

## Runtime And Deployment Records

Every production deployment record must identify:

- exact deployed SHA and rollback SHA;
- checkpoint ref;
- environment/runtime values that changed, without secrets;
- migration state;
- service topology;
- local and public readiness;
- user-facing acceptance evidence.

Runtime configuration changes are operational changes even when code does not
change. Record them separately from code defaults and Admin runtime state.

## Main Protection

Recommended main protection:

- require a pull request before merge;
- require the exact relevant CI checks;
- require conversation resolution;
- block force pushes;
- block branch deletion;
- retain a documented administrator recovery path.

Check names must be audited before making them required. Protection changes are
separate, reversible governance work and must not be bundled with a product or
reconciliation PR. See `docs/research/main-protection-audit-v1.md`.
