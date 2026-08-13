# Main Protection Audit v1

Date: 2026-08-14

Status: read-only audit; no GitHub setting changed.

## Observed Repository Settings

Repository: `Daiobs/AnimeImages` (private)

The repository API reports:

```text
default branch:          main
squash merge:            allowed
merge commit:            allowed
rebase merge:            allowed
delete branch on merge:  false
```

Both the branch-protection endpoint and repository-rulesets endpoint returned
HTTP 403 with GitHub's message that the private repository must upgrade its
plan or become public to enable the feature. Therefore this audit cannot prove
that main currently requires a PR, status checks, or conversation resolution.
It also cannot safely infer direct-push or deletion protection from absence of
API data.

## Recommended Protection

When the repository plan supports it, configure main to:

1. Require a pull request before merge.
2. Require the exact Django and Settings Guard checks after their current names
   are verified against a successful main/PR run.
3. Require conversation resolution.
4. Block force pushes.
5. Block branch deletion.
6. Keep a documented administrator recovery path.

Do not guess required-check names. A stale or incorrect context can prevent all
merges. Apply and verify protection in a separate, reversible governance task;
do not combine it with production deployment or runtime changes.

## Current Risk

Until enforceable protection is available, PR-only main changes are a process
rule rather than a server-enforced control. The immutable pre-reconciliation,
production, and converged checkpoints reduce recovery risk but do not replace
branch protection.
