# Repository Branch Archive And Cleanup v1

Audit date: 2026-08-14

Status: execution authorization. Remote deletion is allowed only after this
document is merged, main CI passes, all recorded tips are unchanged, and the
open-PR count is rechecked.

## Baseline And Method

- Audit base: `d88eca8768790e690d7f10b48e7af8b027cedd72`
- Production application: `fc1f1368431fb2ff063231dfaa4f35a9c8154430`
- Production checkpoint: `checkpoint/production-main-converged-v2` at
  `fc1f1368431fb2ff063231dfaa4f35a9c8154430`
- Initial remote refs: 60 (`main` 1, `checkpoint` 11, `codex` 37, `feat` 4,
  `infra` 1, `reconcile` 1, `research` 4, `stabilize` 1)
- Initial local refs: 59 before creating the cleanup branch
- Initial worktrees: 4
- Initial open or Draft PRs: 0
- Structured initial snapshot:
  `research/evidence/repository-branch-audit-snapshot-v1.json`

The audit used exact remote tips, all GitHub PR metadata, merge bases,
`main..branch` counts, tree OIDs, per-path diffs, worktree state, and repository
automation references. A non-ancestor squash branch is deletable only when its
PR head tree equals its merge tree or later main/checkpoint evidence captures
the branch content. No branch is authorized by name pattern or prefix.

The structured snapshot covers all 60 initial remote refs and records each
tip's date, subject, author, committer, tree, merge base, ahead/behind counts,
main containment, associated PR metadata, worktree use, and automation result.
The category, archive evidence, decision, and deletion authorization for those
same refs are recorded in the tables below, joined by exact branch name and
tip SHA.

## Protected Canonical And Checkpoint Refs

`main` is `KEEP_CANONICAL`. These 11 refs are `KEEP_CHECKPOINT` and their SHAs
form the before/after integrity snapshot:

| Ref | SHA |
| --- | --- |
| `checkpoint/blob-progress-streaming-start-20260812` | `2736c180e5d28b1de5c8057a0d22caa51c491af8` |
| `checkpoint/douyin-ultra-retry-start-20260813` | `e43b59f71cbb23db19a2980bf4ecfbbd05d8d008` |
| `checkpoint/main-pre-production-reconciliation-v1` | `d98b3098bd55031c859c125e44c5d84b1f458f17` |
| `checkpoint/main-production-converged-v1` | `11642b0194b052f9368bf0024900e845fc6609c7` |
| `checkpoint/native-archaeology-start-20260812` | `df28dac1d7020f382add0b15b216a0c9ca043bfe` |
| `checkpoint/production-browser-blob-recovery-v1` | `a5e0254e001d56429008178ce39364be071da24a` |
| `checkpoint/production-douyin-ultra-retry-v1` | `87afba321a2dc459b7daebcd1cc9eea6735b60ba` |
| `checkpoint/production-main-converged-v2` | `fc1f1368431fb2ff063231dfaa4f35a9c8154430` |
| `checkpoint/production-service-worker-streaming-v1` | `5af0892bc7f01ec73a5a3c1ba1b55814364e5e6a` |
| `checkpoint/sw-streaming-productionization-start-20260812` | `2667f1e2f980d9fb22f1b54ac9db2fffb8a68ca9` |
| `checkpoint/v1-autonomy-start-20260812` | `bbbf87b24c7d5fe40d9eaf9aa36eff3cb3992791` |

No tag is part of this cleanup.

## Squash-Merged Equivalent Branches

For every row below, the remote tip equals the final PR head and the PR head
tree equals the merge commit tree. Each is
`DELETE_SQUASH_MERGED_EQUIVALENT`.

| Branch | Tip SHA | Ahead | PR | Squash SHA |
| --- | --- | ---: | ---: | --- |
| `codex/admin-release-management-v1` | `3902788ee1bf246104479fae6f3c2b2583010eca` | 6 | #17 | `465a425754ab19f4eaac7ccfe6b23f23d2acf500` |
| `codex/batch-parse-stability-v1` | `ebb5ac2ef20cc7d9015b0cfac00bb66018b4e44a` | 6 | #21 | `a07d4abce6a12f57da8c1c2ab6cca03ca66ceee8` |
| `codex/batch-public-download-visibility-v1` | `a8706c7f2062e1d302212a86b1c0f8e11040740d` | 2 | #26 | `27c68b94710eeede6d0318d0cfe55cee87d39198` |
| `codex/batch-runtime-admin-clarity-v1` | `752d73845a93327c0c58dc55e2e5fc41ee3a53c7` | 1 | #28 | `de5f38fe61521137b7357f3f9cd8573ee886b346` |
| `codex/browser-direct-native-launch-hotfix-v1` | `8c22a42593cdd2c05943c4184a3100ae050e919e` | 2 | #31 | `23bcd9edb9a925f96350794e8c342372b4ddba26` |
| `codex/browser-direct-restoration-v1` | `c04f51fbcd2dae038c68ec60812097a325bdf475` | 1 | #29 | `e485fa98ce531d395ac897f7e1894e2797c403d4` |
| `codex/douyin-parse-download-reliability-v1` | `58466c675d5094e554971972e547a3fc2d52b276` | 12 | #24 | `9d97684cf17ea8c219ccf4e2789424b8f626087f` |
| `codex/fix-production-deploy-hardening` | `9599afb1a194014c59652cfd7ffa1caef7e05608` | 1 | #14 | `2438a73dbb66fc6def108d37f9ffb865f7af3cb3` |
| `codex/fix-release-readonly-git-state` | `0c8fac88b5d9cc0e99d5dd8c4fe0e3da09c3ab6b` | 1 | #18 | `6b1787ff827ac967b009f7ecf3d0caa83f2673ee` |
| `codex/production-followup-batch-provider-labels` | `db04c175a2d4dbf0483b1a083c4b52d9d7082ade` | 2 | #16 | `494b191163d3ff7370eba442bfd6c2bfd3be3909` |
| `codex/provider-result-lifecycle-cleanup-v1` | `4efa75c1e1d38fc526ed8eda5ec242a76cd3f35f` | 1 | #33 | `d98b3098bd55031c859c125e44c5d84b1f458f17` |
| `codex/refactor-batch-parse-service` | `eb8957a3e011b61eede9358fd3cc3dccdcc79fdd` | 1 | #13 | `242ed0df98cefbcac9e0fd5e19d74d393d3b8b47` |
| `codex/release-executor-wiring-v1` | `01539d3aad069caf8d2f437266ad6b2c99f28d28` | 1 | #20 | `eef0b9bc22b98775a26929e3c072d8c82596386c` |
| `codex/shared-parse-preferences-v1` | `76ded2e20ad6c0993cfd1f8af84f364b045d9acb` | 2 | #22 | `faf71f69c70ac4a1b38a27f089b3b76ebd734f30` |
| `codex/web-concurrency-provider-isolation-v1` | `0f89a0c8207c3f5752026d69656cb715631cccae` | 1 | #30 | `0cf897bcf1741f05a7cd20d8f3cbffea7cb29abc` |
| `codex/xhs-fallback-authority-v1` | `48034ab8a2260c541bcb17b58faf6e478390b08b` | 4 | #25 | `5cc5c05ca473dc9ae95894dfc0ea2a030c7bd94e` |
| `codex/xhs-media-fidelity-v1` | `5cc5c05ca473dc9ae95894dfc0ea2a030c7bd94e` | 3 | #23 | `485951b1f962c4279ef9b78903f0170e05baae5c` |
| `feat/sw-fetch-failure-telemetry-v1` | `c9733446536e1c4fe552c99b54842635be0e70d9` | 4 | #35 | `fc1f1368431fb2ff063231dfaa4f35a9c8154430` |

PR #24 and PR #25 were child PRs into the V1 integration branch; PR #23 then
carried their final tree into main. Their head/merge tree identities still pass.

## Superseded Or Captured Branches

These refs are not authorized by ancestry alone. Their necessary facts are
captured by the listed main document, immutable checkpoint, or later PR.

| Branch | Tip SHA | Ahead | Evidence | Decision |
| --- | --- | ---: | --- | --- |
| `codex/fix-batch-parse-issues` | `3e002b641590c84f880fbb8df8655eda73971949` | 5 | PR #15 head tree matched its squash; the one later tip commit's behavior is in PR #16, with only superseded status text differing | `DELETE_SUPERSEDED_CAPTURED` |
| `codex/batch-parse-ui-polish-v1` | `7496ddf01bec02ace282727fd9acefecbfb42956` | 5 | `v1-batch-blob-progress-archive.md` and `v1-download-transport-archaeology.md` record this UI lineage; later Batch PRs supersede it | `DELETE_SUPERSEDED_CAPTURED` |
| `codex/browser-blob-download-provider` | `a7adce5504dc9b3b33c36ba557381835f392da40` | 9 | `v1-download-transport-archaeology.md` and the recovery record name this Blob-plus-relay behavior; current Streaming supersedes it | `DELETE_SUPERSEDED_CAPTURED` |
| `codex/browser-streaming-direct-download-v2` | `d93977ec383538871f1199ff0a825de28b99a988` | 1 | Closed PR #32; File System Access design and commit are recorded in recovery and Streaming productionization docs | `DELETE_SUPERSEDED_CAPTURED` |
| `codex/douyin-tiantianjiexi-ultra-primary` | `0c353bfe0951ca08ef50690fd831ddd95b414929` | 6 | Provider/download lineage is recorded by exact commit in `v1-download-transport-archaeology.md`; current strategy supersedes it | `DELETE_SUPERSEDED_CAPTURED` |
| `codex/provider-browser-direct-download-handoff` | `8a4dffa2909346242c33607ea711ae1890654117` | 8 | Mixed direct/server fallback is recorded by exact commit in the archaeology and recovery docs | `DELETE_SUPERSEDED_CAPTURED` |
| `feat/blob-progress-ux-v1` | `e9b5001a3054ffb060950825d253d234ebad775e` | 13 | Ancestor of `checkpoint/sw-streaming-productionization-start-20260812`; three Blob/progress research docs are byte-identical in main | `DELETE_RESEARCH_CAPTURED` |
| `feat/service-worker-streaming-production-v1` | `e43b59f71cbb23db19a2980bf4ecfbbd05d8d008` | 16 | Exact `checkpoint/douyin-ultra-retry-start-20260813`; productionization report is byte-identical in main | `DELETE_RESEARCH_CAPTURED` |
| `research/admin-runtime-control-audit-v1` | `e07c8e60315eba11b992649bd3eb2d865a536c49` | 1 | Closed PR #27; this audit PR archives the exact report and structured evidence, and PR #28 implemented the scoped correction | `DELETE_RESEARCH_CAPTURED` |
| `research/native-download-archaeology-v2` | `2736c180e5d28b1de5c8057a0d22caa51c491af8` | 12 | Exact `checkpoint/blob-progress-streaming-start-20260812`; conclusions are in main archaeology docs | `DELETE_RESEARCH_CAPTURED` |
| `research/sw-upstream-fetch-failure-v1` | `d4ba8932b3f303d3264ee67719ebf72347dbaf91` | 1 | Report and JSON evidence are byte-identical in main | `DELETE_RESEARCH_CAPTURED` |
| `research/zero-server-browser-streaming-v1` | `2667f1e2f980d9fb22f1b54ac9db2fffb8a68ca9` | 14 | Exact `checkpoint/sw-streaming-productionization-start-20260812`; conclusions are in the productionization report and raw POC remains in the immutable checkpoint | `DELETE_RESEARCH_CAPTURED` |
| `feat/douyin-ultra-retry-continuation-v1` | `8b81041059839691b08c472b0a4ad9586431ec4c` | 21 | Named production documentation head in the main reconciliation report; its deploy audit is byte-identical in main | `DELETE_RECONCILED` |
| `stabilize/v1-autonomous-recovery-20260812` | `df28dac1d7020f382add0b15b216a0c9ca043bfe` | 11 | Exact `checkpoint/native-archaeology-start-20260812`; recovery conclusions are in main | `DELETE_RECONCILED` |
| `reconcile/production-stable-main-v1` | `83f7faa3863ec32b3e8ccc0ca6128fd1be79ef20` | 4 | PR #34 head/merge trees match; merge `11642b0194b052f9368bf0024900e845fc6609c7` is checkpointed | `DELETE_RECONCILED` |
| `infra/production-nginx-baseline-v1` | `255a44f170eef9285228258560f9a6ac58bb8146` | 3 | PR #36 head/merge trees match; merge `d88eca8768790e690d7f10b48e7af8b027cedd72` is current audit base | `DELETE_INFRA_MERGED` |

The Admin report is `docs/research/admin-runtime-control-audit-v1.md`; its
structured evidence is `research/evidence/admin-runtime-control-audit-v1.json`.
Neither contains credentials or production mutation data.

## Retained Unique Historical Refs

These branches have no merged PR and contain unique policy, governance,
control-plane, migration, template, or mixed provider-history changes that are
not independently archived. They are `KEEP_UNIQUE_HISTORICAL`. No binary
artifact was found, but absence of a binary does not make the code disposable.

| Branch | Tip SHA | Ahead | Reason retained |
| --- | --- | ---: | --- |
| `codex/autonomous-governance-v8` | `d4877bf63b24328b247c937877f96fb5557868f4` | 8 | Unique governance runtime and Admin lineage |
| `codex/autonomous-optimization-v7` | `4f4cb3c7ff1e033221cfe986a481027c34c9c860` | 7 | Unique optimization runtime lineage |
| `codex/download-handoff-strategy-research` | `8355ce3d5001e89ec4e3b0edddd12401340c65c5` | 14 | Mixed policy history plus unarchived handoff/runtime commits |
| `codex/policy-control-convergence-system` | `ded8dc138a1b3e2ef7fc56bb2f485d7bb5b3ed86` | 5 | Unique convergence models, migrations, and Admin flow |
| `codex/policy-engine-v1` | `1adb820d7f28a4152ecacebeab76ab78af5cbc0f` | 1 | Earliest unique policy engine implementation |
| `codex/policy-engine-v2` | `c584c6ca8ffca9d7a4c01208c5c007e25835a1b3` | 2 | Unique scoring/model evolution |
| `codex/policy-engine-v3-adaptive-weights` | `a3a5a96c5099f61da794d038cbbd02bce6f61f65` | 3 | Unique adaptive-weight learning implementation |
| `codex/policy-intelligence-v3.5` | `9f13f995131ff518416f70a992fe0353809d9acc` | 1 | Unique routed-policy implementation |
| `codex/policy-intelligence-v4` | `44fce866f50a59c0662e66c4e87cd23a89a05e03` | 2 | Unique evolution safety guard |
| `codex/policy-intelligence-v5` | `ba0f57ddcc40e75a5480a43ac65fb2f2cab47372` | 3 | Unique optimization queue and model changes |
| `codex/policy-production-control-layer` | `6c70861346f80078a16bc9c6b2e08bda7bde4b8d` | 4 | Unique production-control workflow |
| `codex/production-control-plane-v1` | `fab0a0b52597ad4ac5b1e3a81882c57d3a259844` | 11 | Unique combined control-plane history |
| `codex/provider-runtime-budget-controls` | `b57f89525a26a4f0b4f26ffb9ce8444b3fb0b0d0` | 14 | Mixed policy lineage plus unarchived provider-budget commits |
| `codex/system-stabilization-v6` | `d130f7c40d6e33fdb973967494b9a01be8663a9e` | 6 | Unique stabilization models and Admin surface |

A later `Repository Historical Architecture Archive` task may extract and
reassess these refs. This cleanup does not guess that they are obsolete.

## Worktree And Local-Only Protection

| Ref | State | Decision |
| --- | --- | --- |
| `main` | Clean main synchronization worktree | `KEEP_CANONICAL` |
| `chore/repository-branch-cleanup-v1` | Active clean task worktree | `KEEP_ACTIVE`, then `DELETE_CLEANUP_BRANCH_AFTER_MERGE` |
| `research/zero-server-browser-streaming-v1` | Clean scratch worktree, no process, exact protected checkpoint | Remove the worktree only after the remote ref is deleted |
| `codex/platform-lab-auto-test-system` | Dirty user worktree with modified and untracked evidence | `KEEP_WORKTREE_PROTECTED`; do not touch |

Five other local-only task refs point at a main commit or checkpoint but do not
have a current remote ref: `codex/browser-native-download-restoration-v1`,
`codex/ordinary-video-download-contract-recovery-v1`,
`codex/production-download-usability-recovery-v1`,
`codex/provider-browser-request-context-audit-v1`, and
`codex/v1-production-single-stream-failure-v1`. They are `UNKNOWN` for this
remote allowlist and remain untouched. A later local-only ref audit may remove
them without conflating that decision with remote authorization.

## Automation Dependency Audit

No workflow, deployment script, release helper, or runtime configuration
checks out a deletion candidate by exact name. CI namespace patterns such as
`codex/**` and `feat/**` are triggers, not branch dependencies. Historical
documentation references remain valid after ref deletion because this manifest
records the branch tips and their archive evidence.

## Safe Remote Delete Allowlist

The 34 existing remote branches in the two deletion tables above are the exact
pre-PR allowlist. Before deletion, each name must still resolve to its recorded
SHA. A changed or missing SHA invalidates authorization for that branch.

After this audit PR is squash-merged and main CI passes,
`chore/repository-branch-cleanup-v1` is separately authorized as
`DELETE_CLEANUP_BRANCH_AFTER_MERGE`. Its exact PR head and squash SHA cannot be
self-recorded in its own commit; they must be verified from immutable PR
metadata and included in the execution report before deleting the ref.

Deletion commands must name one exact branch at a time, in batches of at most
10. Wildcards, tags, checkpoints, main, open-PR heads, changed tips, and
unexplained refs are never included.

## Expected Post-Cleanup Shape

If every authorization remains valid, the remote will retain 26 heads:

- `main`: 1
- `checkpoint/*`: 11
- retained unique historical `codex/*`: 14

The actual post-delete inventory, failed or invalidated deletions, checkpoint
SHA comparison, and cleanup-branch PR head/merge SHA are recorded in the final
execution report. Branch deletion must not change the main tree or deploy the
governance-only commit to production.
