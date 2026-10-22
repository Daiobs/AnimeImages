# 产品不变量、架构原则与执行规则

以下文字来自 Human 于 2026-10-20 确认的决定，逐字保留。实现与测试应引用编号，不得自行修改决定；修改本文件属于 L2。

## 产品不变量

| 编号 | 不变量 |
| --- | --- |
| INV-01 | 抖音超清解析按后台配置的 Provider 顺序执行（当前天天解析首选；TikHub 已停用）。Native 从解析一开始就并行运行。在后台设置的前台总预算内（当前 5 秒），首选 Provider 返回超清结果就直接展示；预算到了还没有超清结果，先展示 Native 的结果，之后超清结果返回时自动替换页面上的结果。拿到超清结果后，其余上游 Provider 的工作即结束，不再互相比较。 |
| INV-02 | 任何 Provider 的单次失败、超时或未知结果，都不能让它在没有人工操作时永久停用；冷却时间有上限，并在控制面可见。 |
| INV-03 | 解析结果本身包含多种清晰度，默认选最高，用户可以改选。选择清晰度只决定下载哪一个文件，下载流程不变：解析 → 选择清晰度 → 下载。普通完整媒体优先由浏览器直接从源站获取，支持时使用现有 Service Worker 流式交付，不经项目服务器中转；浏览器 Blob 是在浏览器收完整个文件后再交付保存的兼容方式，不等于服务端下载。只有实际访问条件不允许浏览器直连时才保留已有服务端兜底；音视频确实分离、需要合并的资源由服务器处理后交付。不得仅因平台、Provider、候选绑定或清晰度决定改走服务端，也不将 B 站或某一分辨率一概视为必须服务端交付。 |
| INV-04 | 服务端附件下载在 Safari 和 Chrome 都能保存；文件真正完成前，页面不显示"已保存"。 |
| INV-05 | B 站：Native 优先；Native 返回 412/429/5xx、超时或媒体不完整时，用已准入的天天兜底。 |
| INV-06 | 下载文件名使用作品短标题和真实扩展名，不出现 `.bin` 这类占位扩展名。 |
| INV-07 | 公开结果页不展示 Provider、候选 ID 等内部技术细节；Staff 诊断保留。 |
| INV-08 | 每个公开的 `/media/` 接口都能经 nginx 到达 Django。 |
| INV-09 | 按 IP 的限额和匿名灰度分桶，都按真实客户端计算，伪造请求头无效。 |

## 架构原则

| 编号 | 原则 |
| --- | --- |
| A1 | 平台 → Provider 的路由由后台统一管理：所有平台（包括小红书、抖音、B 站）都进入 Provider 管理和上游测试工作台；Provider 顺序、预算、兜底和是否比较质量都来自后台配置，代码不得写死，也不得覆盖后台配置。 |
| A2 | 解析结果统一处理：所有 Provider 的结果先映射到同一个结果模型（作品信息、资源、清晰度和格式），页面展示和下载输出只读这个模型；工作台评估 Provider 时使用同一个模型，并显示每个 Provider 能提供哪些字段（标题、封面、清晰度、大小）。 |

## 执行规则

| 编号 | 规则 |
| --- | --- |
| G1 | Worker 协议冻结：`ordinary_media_download_sw.js`、`ordinary_media_streaming_protocol.js` 未经你批准不得修改；必须改动时攒进一个 PR。遥测连续 14 天没有旧版本客户端后，再用一个 PR 删除兼容代码。 |
| G2 | 风险分级按改动文件判定，取最高等级：L2 安全边界需要你批准；L1 用户流程需要契约测试加一次真实验收；L0 展示有单测即可，PR 描述不超过一屏。 |
| G3 | 合并保护（不建机器用户）：规则写在 `AGENTS.md`；main 开启分支保护，必需检查不通过就不能合并；改到 L2 文件时，受保护文件检查会一直失败，直到你打上 `human-approved` 标签，有新提交时标签自动移除。agent 不得自己打这个标签。 |

## 契约与风险检查

- `contracts/` 是独立的行为契约目录，CI job 为 `contract-tests`。本轮覆盖 INV-02～09；INV-01 的实现与契约归 T12，本轮不改变其现行调度。
- 路径与等级的唯一清单是 [risk-tiers.json](../.github/risk-tiers.json)。`scripts/risk_tier.py` 对所有命中项取最高等级；没有命中明确清单的路径按 L1 处理，新增安全边界时必须同时更新 L2 清单。
- `protected-files` 由 `pull_request_target` 运行，只检出 main 的规则和检查脚本，不检出或执行 PR 代码。文件列表包含重命名前后的路径，读取不全或请求期间 Head 变化时不放行。
- Human 的 `labeled` 事件通过 GitHub commit status 将批准绑定到该 PR 编号及该次事件的 Head。仅有遗留标签不能批准另一个 Head，其他 PR 的批准也不能复用；状态只由可信工作流写入，不是 Agent 手动授权。
- `synchronize` 会先撤销 `human-approved`，再核对 Head/base 并重新判定；新 Head 不继承旧 Head 的批准状态，不依赖由 `GITHUB_TOKEN` 删除标签触发另一次工作流。检查不设置会替换 pending 事件的并发组。Agent 不得手动添加或移除标签。
- 这是首次引入检查的 PR：新工作流合并到 main 后才成为可信规则。该 PR 的离线事件回放不能证明线上标签、分支保护已经生效。

## Human 合并后启用

1. 在仓库创建 `human-approved` 标签。后续 L2 PR 由 Human 审阅当前 Head 后添加；新提交须重新审阅。
2. 先用一个实际 PR 确认 `protected-files` 出现在该 PR 的检查列表中（挂在 PR 的 Head 上）：改到 L2 文件且未加标签时应为失败。若没有出现，不要把它设为必需检查，先修正检查的上报方式。
3. 再为 main 开启分支保护：必须通过 PR 合并（必需审批数 0）；将 `protected-files`、`contract-tests`、`test`、`nginx-baseline`、`ytdlp-python312`、`protect-settings` 设为必需检查；启用“不允许绕过上述设置”（包括管理员），禁止强推与删除。`protected-files/human-approval/pr-N` 是 L2 批准事实，不单独设为所有 PR 的必需检查。不要把检查跳过或失败当作通过。
4. `Require branches to be up to date before merging`（`required_status_checks.strict=true`）为可选。开启后，L2 PR 每次同步 main 都会产生新提交，标签被自动撤销，需要 Human 重新审阅；PR 较少时可以先不开。
5. 用实际 PR 核验：模板变更通过；L2 变更未批准时失败、Human 加标签后通过、新提交后标签被自动移除并失败；修改检查工作流的 PR 仍受 main 规则约束。确认失败时普通合并和管理员合并均不可用。

本轮 Agent 只提供代码与隔离回放，不创建标签、不改变仓库或分支保护设置、不执行合并。标签方案无法从同一个账号区分 Human 与 Agent，行为约束依照 G3；不在本轮新增机器账号。

GitHub 的权限与事件语义依据：[pull_request_target 安全说明](https://docs.github.com/en/actions/reference/security/securely-using-pull_request_target)、[GITHUB_TOKEN 触发限制](https://docs.github.com/en/actions/concepts/security/github_token)、[必需检查说明](https://docs.github.com/en/pull-requests/how-tos/merge-and-close-pull-requests/troubleshooting-required-status-checks)。
