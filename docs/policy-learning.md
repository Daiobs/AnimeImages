# Policy Learning

Policy Learning 是 Policy Engine v3 的只读学习层。它读取历史 `DownloadRequestLog`，用聚合数据生成推荐权重和 shadow outcome，帮助管理员判断是否需要人工调整策略。

第一版只做：

```text
历史日志 -> 聚合指标 -> 推荐权重 -> 影子决策 -> 后台展示
```

它不会自动修改生产权重、下载路线、ProviderRuntimeSetting 或 DownloadRuntimeSetting。

## 数据来源

学习数据来自 `DownloadRequestLog`，主要使用：

- `route_type`
- `final_policy_route`
- `policy_cost_score`
- `policy_provider_score`
- `policy_routing_score`
- `policy_confidence`
- `server_egress_estimated_bytes`
- `browser_egress_estimated_bytes`
- `actual_bytes`
- `status`
- `provider`
- `source_platform`
- `duration_ms`

`PolicyLearningRun.details_json` 只保存聚合数据，例如 route 计数、provider 计数、成功率、失败率、字节总量、权重和推荐原因。

不会保存完整 URL、真实 IP、User-Agent、cookie、Authorization、token 或 `provider_manifest_token`。

## 推荐权重

当前静态权重默认是：

```text
cost = 0.4
provider = 0.4
routing = 0.2
```

推荐权重使用透明规则生成：

- 成本压力高时，提高 cost weight。
- Provider 失败率高于 30% 时，提高 provider weight。
- direct route 成功率 >= 90% 且样本 >= 20 时，提高 routing weight。
- 样本不足 20 条时不推荐调整，状态为 `insufficient_data`。

所有推荐权重都会归一化，满足：

```text
cost + provider + routing = 1.0
每个权重在 0.1 到 0.7 之间
```

## Confidence

`confidence` 表示这次推荐的可信度，范围是 0 到 1。

它会受到样本数量、成本压力、Provider 失败、route 稳定性等因素影响。样本不足时 confidence 不超过 0.3。

建议至少满足以下条件，再考虑人工调整生产权重：

- 至少 7 天数据。
- confidence >= 0.7。
- 推荐方向连续多次稳定。
- 下载失败率没有同步上升。

## Shadow Outcome

Shadow outcome 会用推荐权重重新计算历史日志中的理论 route，输出：

- 实际 route 分布。
- shadow 推荐 route 分布。
- mismatch count。
- estimated shadow server egress。
- estimated savings bytes。
- recommended shadow success rate。

它不会修改原始 `DownloadRequestLog`，也不会改变真实下载流程。

`estimated_savings_bytes` 是理论节省的服务器出网估算，只能作为趋势参考，不等于实际账单节省。

## 后台入口

访问：

```text
/admin/policy-learning/
```

入口也会出现在：

- `/admin/console/`
- `/admin/rollout-guard/`
- `/admin/provider-settings/`

页面展示：

- 最近一次学习时间。
- 学习窗口。
- 样本数量。
- 推荐置信度。
- 预计节省服务器流量。
- 当前权重和推荐权重。
- Route 分布。
- Provider 指标。
- 推荐原因。
- 最近学习记录。

运行学习按钮只会创建一条 `PolicyLearningRun` 记录，不会应用权重。

## 权限

查看页面：

- superuser。
- 或 staff 且拥有任意 observability view 权限。

执行学习：

- superuser。
- 或 staff 且拥有 `xhs_download.add_policylearningrun`。

## Management Command

只输出推荐结果，不写数据库：

```bash
python manage.py run_policy_learning --window-hours 24 --dry-run
```

写入一条 `PolicyLearningRun`：

```bash
python manage.py run_policy_learning --window-hours 24
```

也可以使用 7 天窗口：

```bash
python manage.py run_policy_learning --window-hours 168 --dry-run
```

本任务不自动配置 cron。后续如果需要定期生成建议，可以由管理员手动加 cron，但仍然不应自动应用推荐权重。

## 上线

上线步骤：

1. 部署代码。
2. 执行 `python manage.py migrate` 创建 `PolicyLearningRun` 表。
3. 执行 `python manage.py check`。
4. 打开 `/admin/policy-learning/`。
5. 运行一次 24h 学习，确认 `PolicyLearningRun` 新增记录。
6. 使用 `--dry-run` 验证命令输出。

## 回滚

如果需要回滚功能：

1. 回滚代码到上一版本。
2. 不需要清理生产下载数据。
3. `PolicyLearningRun` 是独立学习记录表，不影响下载流程。
4. 如需删除表，可在确认没有依赖后回滚对应 migration。

