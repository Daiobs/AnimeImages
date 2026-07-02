# Admin Observability MVP

AnimeImages 上线后可以通过后台查看解析、下载和 Provider 操作审计。第一版只使用 Django ORM、模板和管理命令，不引入 Grafana、Prometheus、Celery 或前端图表库。

## Request ID

`RequestIDMiddleware` 会为每个请求写入 `request.request_id`，并在响应头返回：

```http
X-Request-ID: <request id>
```

如果 Nginx 已传入合法 `X-Request-ID`，Django 会复用；否则生成 UUID。业务日志、数据库事件和 journalctl 日志都带有 request_id，方便串联一次请求。

建议 Nginx 在 `server` 或对应 `location` 中转发 request id：

```nginx
location / {
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Request-ID $request_id;
    proxy_pass http://127.0.0.1:8000;
}
```

建议 access log 也包含同一个 request id：

```nginx
log_format animeimages '$remote_addr - $request_id [$time_local] '
                       '"$request" $status $body_bytes_sent '
                       '"$http_referer" "$http_user_agent"';
access_log /var/log/nginx/animeimages.access.log animeimages;
```

Django 会读取 `X-Request-ID`，并在响应头再次返回 `X-Request-ID`。用户反馈问题时，可先让用户提供响应头里的 request id，再用它搜索后台解析/下载日志、journalctl 和 Nginx access log。

## 后台入口

需要具备对应 view 权限的 staff 用户，或 superuser 登录后台后访问：

```text
/admin/ops-dashboard/
/admin/parse-logs/
/admin/download-logs/
/admin/provider-audit-logs/
/admin/provider-settings/
/admin/provider-health-tests/
/admin/console/
/admin/control-plane/
```

`/admin/ops-dashboard/` 需要 superuser 或任意一个 observability view 权限。单独日志页面需要对应权限：

- `xhs_download.view_parserequestlog`
- `xhs_download.view_downloadrequestlog`
- `xhs_download.view_provideradminauditlog`
- `xhs_download.view_providerhealthtestrun`

运营看板显示今日解析、下载、成功率、失败原因 Top 10、平台 Top 10、Provider Top 10 和最近 Provider 审计记录。

`/admin/console/` 是新的运维首页，聚合下载成本、流量风险、Provider runtime 状态和常用后台入口。它会显示今日下载实际流量、估算流量、24h/7d 流量、今日全站流量使用率、最近大文件下载、blocked/rate_limited 记录和 DownloadRuntimeSetting 摘要。详细说明见 `docs/download-cost-guard.md`。

`/admin/control-plane/` 是统一生产控制平面，建议作为日常巡检第一入口。它聚合 health ready、核心解析/下载指标、Provider 状态、下载成本、Policy / Feedback / Governance 状态和 Action Center。页面只读，不运行 Provider 测试、不触发外部请求、不修改生产配置。详细说明见 `docs/production-control-plane.md`。

`/admin/provider-health-tests/` 用于上线后执行 Provider smoke。页面会展示环境 capability、runtime gate、下载限流摘要和最近 50 条测试记录。执行测试需要 superuser 或 `xhs_download.add_providerhealthtestrun` 权限。详细说明见 `docs/admin-provider-health-tests.md`。

修改 Provider 测试 URL 需要 superuser 或 `xhs_download.change_providerhealthtestsetting` 权限。测试 URL 保存在数据库配置中，不放入 `.env`。

## 落库日志

`ParseRequestLog` 记录解析请求的 request_id、host、平台、Provider、状态、失败原因、耗时、素材数量和 runtime/rollout 信息。

`DownloadRequestLog` 记录下载请求的 request_id、状态、下载类型、素材数量、估算/实际字节数、Provider、失败原因和耗时。

`ProviderAdminAuditLog` 记录 Provider 设置修改的 actor、action、changed_fields、before_snapshot 和 after_snapshot。

日志写入是 best effort。写入失败只会进入 `logger.exception`，不会中断正常解析或下载。

错误短文本会先脱敏再落库或写入结构化日志。完整 URL 会替换成 `[url-redacted]`，`provider_manifest_token` 和常见签名参数会替换成 `[redacted]`，并截断到 300 字。

## 不保存的敏感信息

业务日志不会保存：

- 完整 URL
- 真实 IP
- 完整 User-Agent
- provider signed URL
- manifest token
- cookie
- Authorization
- API key
- 短期 CDN 链接

IP、User-Agent 和限流 identity 会使用 `sha256 + SECRET_KEY salt` 后保存 hash。解析日志只保存 `query_host`，不保存完整 query。

## 通过 request_id 排查

1. 用户反馈失败时，让用户提供响应头或页面错误对应的 request_id。
2. 先打开 `/admin/control-plane/` 查看当前系统风险等级和 Action Center。
3. 在 `/admin/parse-logs/` 或 `/admin/download-logs/` 搜索 request_id。
4. 查看 status、error_code、error_type、duration_ms、selected_provider。
5. 如果是下载流量或大文件问题，打开 `/admin/console/` 查看今日全站流量使用率、最近大文件下载 Top 10 和 blocked/rate_limited 记录。
6. 到服务器用 journalctl 继续串联：

```bash
journalctl -u animeimages --since "1 hour ago" | grep '<request_id>'
```

7. 如需串联 Nginx，搜索 access log 中同一个 request_id：

```bash
grep '<request_id>' /var/log/nginx/animeimages.access.log
```

## 清理旧日志

解析和下载日志默认建议保留 30 天。先 dry-run 看将删除的数量：

```bash
python manage.py prune_observability_logs --days 30 --dry-run
python manage.py prune_observability_logs --days 30
```

生产 cron 示例：

```bash
0 3 * * * /srv/animeimages/.venv/bin/python /srv/animeimages/manage.py prune_observability_logs --days 30
```

`ProviderAdminAuditLog` 默认保留更久，不会被普通 prune 命令删除。

## 每日巡检

1. 打开 `/health/ready/`
2. 打开 `/admin/control-plane/`
3. 查看风险等级和 Action Center
4. 查看 Provider 状态、下载流量和 route 分布
5. 查看 pending approvals / governance decisions
6. 如果控制平面提示流量风险，再打开 `/admin/console/`
7. 如果控制平面提示 Provider 风险，再打开 `/admin/provider-health-tests/`
8. 如果需要排查具体请求，再打开 `/admin/ops-dashboard/`、`/admin/parse-logs/` 或 `/admin/download-logs/`
9. 用 `journalctl -u animeimages --since today` 检查异常堆栈
