# Admin Provider Health Tests

`/admin/provider-health-tests/` 是上线后的 Provider smoke 面板，用于管理员快速判断 Provider 问题来自环境、runtime gate、下载限流还是具体解析链路。

这个页面不是公网入口，也不会自动开放第三方 Provider。它只做管理员诊断。

## 入口

后台入口：

```text
/admin/provider-health-tests/
```

也可以从 Django 后台首页的 `Provider` 模块进入：

```text
Provider 健康测试
```

Provider 设置页和运营看板也保留了入口。

## 权限

查看页面：

- superuser 可以访问；
- 或 staff 且拥有 `xhs_download.view_providerhealthtestrun` 权限。

执行测试：

- superuser 可以执行；
- 或 staff 且拥有 `xhs_download.add_providerhealthtestrun` 权限。

无权限用户返回 404。

## 测试 URL 配置

测试 URL 不写死在代码里，也不放进 `.env`。它们属于运行期可变配置，应在后台页面直接保存：

```text
/admin/provider-health-tests/
```

页面中的“测试 URL 配置”会保存到数据库 `ProviderHealthTestSetting`。保存后立即生效，不需要重启服务。

URL 为空时，对应测试会显示“未配置测试 URL”，执行结果为 `skipped`，不算失败。Admin 保存是第一道结构校验：只接受 `http/https`、无 username/password、端口为 80/443、literal host 非本机或特殊用途地址，并要求平台字段命中对应的现有公开页面 host。无效值会被清空并显示不含原 URL 的固定提示，其它有效字段仍按原行为保存。

每次手动或 scheduled execution 都会在调用 Provider、Worker、yt-dlp、gallery-dl、TianTian 或 videodl 之前重新执行结构校验和 DNS 解析。所有 DNS 结果都必须是公网地址；任一结果为 loopback、private、link-local、multicast、reserved 或 unspecified 时，测试以 `SAMPLE_URL_POLICY_REJECTED` fail closed，记录只保留安全 host。Staff 权限不授权访问私网或 metadata endpoint。该局部门禁不是完整 egress sandbox；各 Provider 仍必须对后续 redirect 和媒体 admission 执行自身现有策略。

只要数据库不被重建或清空，更新代码、执行 `migrate`、重启 Gunicorn、重新拉取分支都不会覆盖这些 URL。生产环境应使用持久化 PostgreSQL，并把数据库备份纳入上线流程；不要依赖临时 SQLite 文件或容器内非持久卷保存这类运行期配置。

建议测试 URL 选择：

- 抖音测试 URL：一条稳定的公开视频链接，用于 `douyin_native_parse`、`douyin_kukutool_ultra`、`douyin_tiantianjiexi_ultra`。
- YouTube 测试 URL：一条普通 1080p 音画分离视频，用于验证 YouTube merge planning。
- 第三方 yt-dlp 测试 URL：一条 yt-dlp 支持的轻量公开视频。
- gallery-dl 测试 URL：一条 Imgur 或 Flickr 图片/图集链接。
- 小红书视频测试 URL：一条稳定公开的普通视频笔记。
- 小红书图集测试 URL：一条稳定公开的普通图集笔记。
- 哔哩哔哩测试 URL：一条无需登录的公开视频。
- videodl 测试 URL：只用于已审核 Dedicated backend 的非商业内部轻量解析。

不要把私密链接、带 token 的链接或临时 CDN 直链配置为测试 URL。

TianTianJieXi 相关测试分两类：`tiantianjiexi_account_status` 只查账号/配置状态，不消耗真实视频解析；`douyin_tiantianjiexi_ultra` 和 `third_party_tiantianjiexi` 会调用真实解析接口，可能消耗账号额度或触发上游限流。

## 测试项

当前面板包含 14 个测试项。原有 9 项保持不变，Asset 3 增加 5 项：

| 测试项 | 目标 | 说明 |
| --- | --- | --- |
| `health_ready` | 数据库和缓存 readiness | 复用现有 readiness 逻辑。 |
| `douyin_native_parse` | 抖音普通解析 | 不强制 KuKuTool，只验证基础链路。 |
| `douyin_kukutool_ultra` | 抖音 KuKuTool 超清 | capability 未启用时返回 `unavailable`。 |
| `tiantianjiexi_account_status` | 天天解析账号状态 | 只查账号/配置状态，缓存结果，不消耗真实视频解析。 |
| `douyin_tiantianjiexi_ultra` | 抖音天天解析超清 | 使用测试抖音 URL 跑 TianTianJieXi ultra path，会消耗真实解析。 |
| `youtube_merge` | YouTube 合并规划 | 验证 yt-dlp + ffmpeg health 和候选规划，不下载大文件。 |
| `third_party_ytdlp` | 通用 yt-dlp | 验证 yt-dlp health 和轻量解析。 |
| `third_party_tiantianjiexi` | 通用天天解析 | 验证 TianTianJieXi generic normalizer 和签名 manifest。 |
| `gallery_dl` | gallery-dl | 验证 gallery-dl health 和轻量解析。 |
| `xhs_video_current` | 当前 XHS 视频路线 | 验证平台身份和至少一个视频候选，不下载媒体。 |
| `xhs_gallery_current` | 当前 XHS 图集路线 | 验证 exact item 和至少一个图片候选，不下载图集。 |
| `bilibili_native_parse` | Bilibili Native | 验证当前正式 Native View/PlayURL 路线和视频候选。 |
| `videodl_policy_inventory` | videodl exact-pin policy | 验证独立环境、70 Dedicated、37 Common 和默认拒绝边界，不运行 Backend。 |
| `videodl_specific_backend` | videodl Dedicated sample | 只允许 policy 明确 `allowed`、不需 source sharing 的 Dedicated backend；不使用 Common、auto、grabber 或 browser。 |

页面也提供“一键测试”，会顺序执行所有测试项。未配置 URL 的项目会写入 `skipped`，不会被当作失败。因为 YouTube、gallery-dl 或 KuKuTool 可能访问外部服务，一键测试适合管理员 smoke，不应放到普通用户路径里。

## 结果状态

每次执行会写入 `ProviderHealthTestRun`：

- `success`：测试成功。
- `failed`：测试执行异常或解析失败。
- `skipped`：缺少测试 URL 等可预期跳过。
- `unavailable`：capability、依赖、runtime health 不满足。

记录中只保存测试 URL 的 host，不保存完整 URL。

错误文本会经过脱敏：

- 完整 URL 替换为 `[url-redacted]`。
- `provider_manifest_token` 和常见签名参数替换为 `[redacted]`。
- 文本截断到 300 字。

## 每日验证批次

Asset 3 复用同一 registry 和 `ProviderHealthTestRun`，没有新增平行样本平台。
`ProviderValidationSuiteRun` 只聚合一次顺序批次的 test keys、固定状态、计数、
耗时、通知状态和非敏感操作者信息。它不保存 sample/media URL、Payload、
Cookie、Header、SMTP 配置或原始异常。

安全默认值：

```text
daily_enabled = false
daily_test_keys = ["health_ready"]
email_notifications_enabled = false
PROVIDER_VALIDATION_EMAIL_DELIVERY_ENABLED = false
```

即使候选 timer 被误装，每日外部样本也不会自动执行。实际执行固定按 14 项
registry 顺序串行运行，single-flight 使用共享 Django cache，总预算默认 300
秒且只允许 30-900 秒。预算耗尽前尚未开始的项会留下
`SUITE_DEADLINE_EXCEEDED` skipped 记录，不会伪装成已执行。过期 running 批次
会以 `STALE_SUITE_RECOVERED` 安全关闭。

`videodl_backend_id` 在 Admin 保存时始终严格校验；运行 daily suite 时仅当
`videodl_specific_backend` 被选中才作为启动门禁。未选择该测试时，数据库中
遗留的无效 Backend ID 不会阻断 `health_ready`、XHS 或 Bilibili 等其它测试，
也不会启动 videodl Worker。

命令入口：

```bash
python manage.py run_daily_provider_validation --dry-run
python manage.py run_daily_provider_validation --force --no-email
python manage.py run_daily_provider_validation --force --no-email --test-key health_ready
```

`--dry-run` 不写数据库、不调用外部服务、不发邮件、不做 retention。
普通 Provider failed/unavailable 会形成 degraded 批次，但默认不把命令误报为
基础设施崩溃；外部监控可显式使用 `--fail-on-degraded`。

## 通知与报告

通知是状态变化邮件，不发送每日成功日报：

- 默认连续两次 failed/unavailable 后发送 failure；
- 同 fingerprint 在冷却期内去重，冷却后发送 reminder；
- fingerprint 变化可以立即形成新的 failure；
- 只有此前 failure/reminder 已成功投递，后续 success 才发送 recovery；
- skipped 不增加失败、不清空失败、不触发 recovery；
- 每个 Suite 最多一封纯文本聚合邮件；
- 邮件失败不回滚验证结果，后续批次可以重试。

邮件必须同时通过数据库开关、环境投递 Gate、非空收件人和有效 Django Email
Backend。密码、用户名和收件人只来自环境；后台只显示 Gate、收件人数和
Backend 类，不显示地址或 SMTP Secret。邮件只包含 test key、公开名称、
provider、status、固定 error code、连续次数、事件和 host。

生产配置中 console、dummy、locmem 和 filebased Backend 均不构成真实投递，
系统检查会固定报错且运行时 `configured=false`；不可导入的 Backend 同样
fail closed。开发和自动测试仍可使用 locmem。`SENT` 与 `notified_at` 仅在允许
的 Backend 完成发送且 `send()` 明确返回 1 后写入，配置被拒绝时不会调用
`EmailMessage.send()`。明确配置且可导入的 SMTP 或自定义真实投递 Backend
不受此禁用清单影响。

后台同一页面展示最新 scheduled Suite、计数、逐项最近状态、连续不健康、
7/30 天批次趋势以及可分页/筛选的 Suite 历史。手动逐项和“一键测试”仍然
工作，但不会触发 Asset 3 邮件。

## Retention

`retention_days` 默认 90 天，仅清理已结束且过期的 Suite 及其 FK 关联 daily
test runs。未关联 Suite 的既有手动 `ProviderHealthTestRun` 不会自动删除。

## 生产激活前清单

本候选 PR 不执行以下动作。后续独立授权任务必须按顺序完成：

1. 确认生产 checkout exact SHA，并备份数据库。
2. 执行 migrate，确认 Admin 页面与权限。
3. 在 Admin 录入 Human-approved 稳定公开样本并选择 daily keys，同时保持 `daily_enabled=false`。
4. 从环境配置 SMTP Secret、发件人和收件人；使用与每日验证相同的 Django
   `get_connection` 受控 canary 验证。该局部 env adapter 不修改全局
   `settings.py`，因此不要用通用 `sendtestemail` 代替此链路验证。
5. 配置邮件 delivery env Gate，先运行 `--dry-run`。
6. 运行 `--force --no-email`，人工检查 Suite、逐项状态、host-only 记录和日志。
7. 运行一次受控带邮件 canary，确认正文不含完整 URL 或 Secret。
8. 核对服务器 timezone，运行 `systemd-analyze verify`。
9. 审核后复制 service/timer，执行 daemon-reload 和 `enable --now`。
10. 检查 timer next run、首次 scheduled Suite 和 journal。
11. 最后才在 Admin 设置 `daily_enabled=true`。

回滚优先关闭数据库 `daily_enabled` 和环境 email delivery Gate；已安装 timer
需在单独运维任务中 disable。迁移反向执行只在明确评估数据影响后进行。

```text
ASSET_3_CODE = IMPLEMENTED_IN_CANDIDATE_PR
ASSET_3_PRODUCTION_ACTIVATION = NOT_PERFORMED
MERGE != DEPLOYMENT
SYSTEMD_FILE_IN_REPOSITORY != INSTALLED
EMAIL_CODE != SMTP_CONFIGURED
SAMPLE_FIELDS != PRODUCTION_SAMPLES_CONFIGURED
```

## 当前本地基线

2026-06-29 本地检查：

```bash
.venv/bin/python manage.py check
.venv/bin/python manage.py inspect_third_party_provider --health --json
```

结果：

- Django system check：通过。
- `yt_dlp_generic`：enabled 且 ready，版本 `2026.03.17`。
- `gallery_dl_generic`：enabled 且 ready，版本 `1.32.4`。
- `youtube_merge`：ready，ffmpeg 版本 `8.1.2`。
- `kukutool_generic`：provider disabled。
- 本地数据库未配置测试 URL，所以后台测试面板中的真实 URL 测试会保持 `skipped`，直到在页面中保存测试 URL。

## 上线建议

首次部署后按顺序执行：

1. 确认 `/health/ready/` 返回 200。
2. 打开 `/admin/provider-health-tests/`。
3. 先执行 `health_ready`，确认数据库和缓存正常。
4. 在页面中保存测试 URL。
5. 点击“一键测试”，得到完整 smoke 记录。
6. staff-only 打开对应 provider runtime gate 后，再复测 `youtube_merge`、`third_party_ytdlp`、`gallery_dl`。
7. TianTianJieXi account status 可以作为抖音超清上游的低成本预检。
8. `douyin_tiantianjiexi_ultra` 和 `third_party_tiantianjiexi` 会消耗真实解析额度，只在需要 smoke 时运行。
9. KuKuTool 超清只在确认 capability、runtime gate、上游稳定后执行，当前定位是抖音超清 fallback/race backup。
10. 若出现 `failed`，用页面上的 `request_id` 去 `/admin/parse-logs/`、`/admin/download-logs/` 和 `journalctl` 关联排查。

不要因为 health 命令显示 `public_candidate` 就直接全站开放。公网开放仍应通过 Provider 管理后台的 runtime gate 逐步从 `staff` 到 `public_gray` 再到 `public`。
