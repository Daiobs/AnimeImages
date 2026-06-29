# Download Cost Guard

AnimeImages 的 ZIP 下载、视频下载、YouTube merge 和 KuKuTool 超清下载都会经过服务器中转。用户下载一个 400MB 视频时，服务器通常会先从上游 CDN 拉取资源，再把文件返回给用户浏览器；这会消耗服务器带宽、出网流量、磁盘临时空间和 Python worker 时间。

本阶段的 Download Cost Guard 目标是先做最小可用的成本保护，不引入 Celery、CDN、支付、复杂队列或外部监控系统。

## 后台入口

运维首页：

```text
/admin/console/
```

该页面展示：

- 今日解析次数
- 今日下载次数、成功率、失败数、限流 / 阻止次数
- 今日下载实际流量和估算流量
- 最近 24 小时和 7 天下载实际流量
- 当前 `DownloadRuntimeSetting` 摘要
- 当前 `ProviderRuntimeSetting` 摘要
- 最近 Provider 健康测试结果
- 最近 24 小时失败原因 Top 10
- 最近 24 小时大文件下载 Top 10
- 最近 24 小时 blocked / rate_limited 记录
- Provider 设置、Download 设置、Provider 测试、日志页面快捷入口

## DownloadRuntimeSetting 字段

`/admin/download_console/downloadruntimesetting/` 中可以配置：

- `rate_limit_per_minute`：每分钟下载请求数 / IP，0 表示不限制。
- `daily_request_limit_per_ip`：每日下载请求数 / IP，0 表示不限制。
- `global_daily_request_limit`：每日全站下载请求数，0 表示不限制。
- `max_concurrent_per_ip`：并发下载数 / IP，0 表示不限制。
- `daily_bytes_limit_per_ip`：每日下载流量 / IP，单位 bytes，0 表示不限制。
- `global_daily_bytes_limit`：每日全站下载流量，单位 bytes，0 表示不限制。
- `single_archive_bytes_limit`：单次 ZIP 下载最大允许大小，单位 bytes，0 表示不限制。
- `single_video_bytes_limit`：单个视频资源最大允许大小，单位 bytes，0 表示不限制。
- `visitor_video_download_enabled`：是否允许非 staff 用户下载视频。
- `staff_large_download_exempt`：staff 是否豁免单次 ZIP / 单视频限制；不绕过每日流量限制。
- `large_download_warning_bytes`：大文件警告阈值，单位 bytes，0 表示不提示。

## 推荐生产初始值

这些值是上线初期建议，不强制写入代码：

```text
single_archive_bytes_limit = 536870912      # 500 MB
single_video_bytes_limit = 314572800        # 300 MB
visitor_video_download_enabled = True       # 如流量风险高，可先设 False
staff_large_download_exempt = False
large_download_warning_bytes = 104857600    # 100 MB
```

如果使用小规格云服务器，建议先降低 `global_daily_bytes_limit` 和 `max_concurrent_per_ip`，等 smoke test 稳定后再逐步放宽。

## 下载前保护

同步下载接口 `/media/download-images/` 会在真正下载资源前做 preflight：

- 访客视频下载关闭时，非 staff 下载 video 会被阻止。
- 已知 `size_bytes` 的单个视频超过 `single_video_bytes_limit` 会被阻止。
- 已知 `size_bytes` 的资源总和超过 `single_archive_bytes_limit` 会被阻止。

为了避免额外成本，preflight 不会主动对所有 URL 发送 HEAD 或 Range 请求。缺少 `size_bytes` 的资源不会在下载前被假定为 0 后强行通过，第一版只对已知大小做保护，真实 ZIP 大小仍会在打包后检查。

## 下载后保护

打包完成后会再次检查实际 ZIP 大小：

- 如果实际 ZIP 大小超过 `single_archive_bytes_limit`，且当前用户不是被允许豁免的 staff，则关闭临时文件并返回错误。
- 每日 IP / 全站流量限制仍继续生效。
- 即使 `staff_large_download_exempt=True`，staff 也不能绕过每日 IP / 全站流量限制。

## 错误码

下载成本保护会写入 `DownloadRequestLog.error_code`，响应头也会带 `X-Error-Code`：

- `VISITOR_VIDEO_DOWNLOAD_DISABLED`
- `SINGLE_VIDEO_SIZE_LIMIT`
- `SINGLE_ARCHIVE_SIZE_LIMIT`
- `DOWNLOAD_DAILY_BYTES_LIMIT_IP`
- `DOWNLOAD_DAILY_BYTES_LIMIT_GLOBAL`
- `DOWNLOAD_MINUTE_RATE_LIMIT`
- `DOWNLOAD_CONCURRENCY_LIMIT`

排查时先复制响应头里的 `X-Request-ID`，然后到 `/admin/download-logs/` 搜索 request_id。

## 流量告警处理

打开 `/admin/console/` 查看“今日全站流量使用率”：

- `global_daily_bytes_limit=0`：显示“未限制”。
- 超过 70%：黄色提示，应暂停扩大公开范围。
- 超过 90%：红色提示，应立即降低下载限额或关闭高风险 Provider。

建议优先处理：

1. 降低 `single_archive_bytes_limit`
2. 降低 `single_video_bytes_limit`
3. 将 `visitor_video_download_enabled` 设为 False
4. 降低 `global_daily_bytes_limit`
5. 将高风险 Provider runtime mode 调整为 `staff` 或 `disabled`

## 回退配置

如果上线后出现误拦截：

1. 在 Download 设置中把 `single_archive_bytes_limit` 或 `single_video_bytes_limit` 临时设为 0。
2. 如需恢复访客视频下载，将 `visitor_video_download_enabled` 设为 True。
3. 在 `/admin/download-logs/` 搜索对应 `error_code` 和 request_id，确认是否为 size_bytes 误判。
4. 保留每日流量限制，不建议直接关闭 `global_daily_bytes_limit`。

不要通过打开 `DOWNLOAD_ASYNC_TASKS_ENABLED`、`XHS_ALLOW_SERVER_SAVE` 或 `XHS_ALLOW_ABSOLUTE_SAVE_PATH` 来绕过成本保护。
