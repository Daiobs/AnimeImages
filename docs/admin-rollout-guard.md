# Admin Rollout Guard

`/admin/rollout-guard/` 是只读的 Provider 放量建议页面。它根据最近解析日志、下载日志、Provider 健康测试和下载流量使用率，帮助管理员判断是否可以从 `staff` 推进到 `public_gray`，或者是否应该暂缓/回退。

Rollout Guard 不会：

- 自动放量
- 自动修改 `ProviderRuntimeSetting`
- 自动调整 `rollout_percent`
- 自动关闭 Provider
- 触发真实下载测试

## 后台入口

```text
/admin/rollout-guard/
```

相关页面也提供入口：

- `/admin/console/`
- `/admin/provider-settings/`
- `/admin/provider-health-tests/`
- `/admin/ops-dashboard/`

权限与 observability 页面一致：superuser 可访问，或 staff 拥有任一 observability view 权限。

## 建议状态

- `safe_for_staff`：适合 staff-only 内测
- `safe_for_1_percent_gray`：可考虑 1% 公开灰度
- `caution`：谨慎，建议继续观察
- `do_not_public`：不建议公开

这些状态只是建议，不会阻止你在 Provider 设置页保存配置。

## 判断依据

页面覆盖三个现有 Provider 维度：

- 抖音 KuKuTool 超高清：`douyin_ultra`
- YouTube 高清合并：`youtube_merge`
- 通用第三方 Provider：`third_party`

主要参考：

- capability gate 是否开启
- 当前 runtime mode 和 rollout percent
- 最近 ProviderHealthTestRun
- 最近 24h ParseRequestLog 成功率、失败数、平均耗时
- 最近 24h DownloadRequestLog 下载次数、实际流量、blocked/rate_limited
- 今日全站下载流量使用率
- third-party 公开 allowlist 是否为空

## 推荐灰度节奏

```text
staff
public_gray 1%
public_gray 5%
public_gray 10%
public
```

建议每一步至少观察 24 小时。长尾平台和高带宽视频 Provider 可以观察更久。

## 可以考虑 1% 灰度

通常需要同时满足：

- capability gate 已开启
- 最近健康测试成功
- 24h 解析样本数不少于 5
- 24h 成功率不低于 90%
- 平均耗时不超过 8000ms
- 失败数不高于 10
- 今日下载流量没有接近上限

## 应该暂缓或回退

出现以下情况时，建议保持 staff-only、回退到 disabled，或暂停公开灰度：

- 健康测试失败或不可用
- 24h 成功率低于 90%
- 24h 成功率低于 70%
- 平均耗时超过 8000ms，尤其超过 15000ms
- 今日下载流量超过 70%
- 当前已 public/public_gray 且今日下载流量超过 90%
- 第三方公开 allowlist 为空
- blocked/rate_limited 突增

## 上线使用方式

1. 部署代码并执行 `python manage.py migrate`。
2. 打开 `/admin/provider-health-tests/`，先运行 Provider 测试。
3. 打开 `/admin/rollout-guard/` 查看建议。
4. 如果建议是 `safe_for_staff`，保持 staff-only 继续观察真实样本。
5. 如果建议是 `safe_for_1_percent_gray`，可在 Provider 设置页手动改为 `public_gray` 且 rollout 设置为 1。
6. 继续观察 `/admin/console/` 和 `/admin/rollout-guard/`。

## 回滚

Rollout Guard 本身只读，回滚通常不需要数据库操作。

如果放量后出现风险：

1. 到 `/admin/provider-settings/` 将对应 Provider 改回 `staff` 或 `disabled`。
2. 到 `/admin/console/` 检查下载流量和 blocked/rate_limited。
3. 到 `/admin/parse-logs/` 和 `/admin/download-logs/` 按 request_id 或 error_code 排查。
4. 如果只是页面代码需要回滚，切回上一版代码并重启服务即可。
