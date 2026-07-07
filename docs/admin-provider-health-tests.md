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

URL 为空时，对应测试会显示“未配置测试 URL”，执行结果为 `skipped`，不算失败。保存配置时，明显无效的 URL，例如没有 host、不是 `http/https` 协议，会被清空并显示提示。

只要数据库不被重建或清空，更新代码、执行 `migrate`、重启 Gunicorn、重新拉取分支都不会覆盖这些 URL。生产环境应使用持久化 PostgreSQL，并把数据库备份纳入上线流程；不要依赖临时 SQLite 文件或容器内非持久卷保存这类运行期配置。

建议测试 URL 选择：

- 抖音测试 URL：一条稳定的公开视频链接，用于普通 native 和 KuKuTool 超清 smoke。
- YouTube 测试 URL：一条普通 1080p 音画分离视频，用于验证 YouTube merge planning。
- 第三方 yt-dlp 测试 URL：一条 yt-dlp 支持的轻量公开视频。
- gallery-dl 测试 URL：一条 Imgur 或 Flickr 图片/图集链接。

不要把私密链接、带 token 的链接或临时 CDN 直链配置为测试 URL。

## 测试项

当前面板包含 9 个测试项：

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
