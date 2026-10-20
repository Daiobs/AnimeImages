## 用户可见行为：之前 → 之后

| 之前 | 之后 | 不变量编号 |
| --- | --- | --- |
| | | |

风险等级以 `protected-files` 的路径判定为准。L0 提供单测，描述不超过一屏；
L1 提供契约测试与一次真实验收；L2 由 Human 审阅当前 Head 并批准。
Agent 不得添加或移除 `human-approved`，不得修改仓库设置或绕过检查。

## Summary

- What does this PR change?
- Which modules, scripts, documents, or runtime surfaces are affected?

## Why

- Why is the change needed?
- What problem or production finding does it address?

## Design / Implementation

- Core design and implementation approach:
- Important architecture decisions:

## Scope

- Included:
- Not included:

## Safety

- Permission or security impact:
- Migration impact:
- Provider / Download / Runtime behavior impact:

## Documentation Impact

Choose exactly one:

- [ ] Updated: `<files>`
- [ ] Not needed: `<reason>`

## Verification

- [ ] `git diff --check`
- [ ] `python manage.py check`
- [ ] `python manage.py makemigrations --check --dry-run`
- [ ] Relevant automated tests:
- [ ] Relevant manual smoke tests:
- [ ] Changed file list reviewed with `git status --short --branch`
- [ ] CI passed

## Deployment Notes

- Environment / systemd / Nginx / migration / server-config impact:
- Default state and rollout steps:

## Rollback

- Revert or rollback steps:
- Database handling, if any:

## Non-goals

- Explicitly excluded work:

<!--
Squash merge rule: keep the full design in the PR body. The squash commit body
must retain concise Summary, Validation, Deployment, and Rollback information.
-->
