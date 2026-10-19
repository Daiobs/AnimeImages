# Current Version Status

核对日期：2026-10-19 UTC。本文区分**已合并的应用能力、最后已记录的生产发布、实际运行配置与历史验收**。

## 当前结论

| 项目 | 本次核对结果 |
| --- | --- |
| 应用功能快照 | main `c2b8ac51cfb87d4c030d321bf763080708db9593` |
| 应用文件树 | `1befb9f3c27a880d91da26305a27f4a671faccce` |
| #96–#100 | 已按顺序 Squash Merge，最终树等于冻结审查树 |
| 开发与生产差异 | #96–#100 的应用增量已在 main，尚无本次增量的部署记录 |
| 最后已记录的生产应用 | `bf9f1255b006ea93b7a0cf766d28f1db6138113f` |
| 本文档任务访问 / 部署生产 | 均未执行 |
| 8000、配置、账号、正式路线和历史 | 本文档任务不切换、不重启、不修改 |

依据：[最终合并与本地同步记录](https://github.com/Daiobs/AnimeImages/pull/100#issuecomment-6259776450)、[最后已记录的生产发布](https://github.com/Daiobs/AnimeImages/pull/95#issuecomment-6211550835)。

应用 SHA 是本次功能快照，不是永久 main 指针。本文档 PR 自身会产生新提交，不为追逐文档自身 SHA 反复改写快照。获取最新代码应重新读取远端：

```bash
git fetch origin
git rev-parse origin/main
git rev-parse origin/main^{tree}
```

## 事实来源

| 问题 | 核对依据 |
| --- | --- |
| 当前代码支持什么 | 实际 main 的代码、模板、配置定义与测试 |
| 哪些改动已合并 | 合并提交、最终文件树及 PR 记录 |
| 上一次部署了什么 | 明确记录的应用 SHA 与部署验证 |
| 生产此刻运行什么 | 需要另行授权的现场核验；本文未执行 |
| 谁能调用或下载 | 环境能力开关、Admin Runtime、来源/Provider 准入及资源授权 |
| 某项验收通过意味着什么 | 原 Head、浏览器、用户身份、样本、配置范围和实际 transport |

合并不能推导部署；代码支持不能推导当前公众开放。一个样本成功不能推导全平台、全部格式或全浏览器通过。

## 已合并的应用能力

| 增量 | main 中的实际价值 | 仍需区分的边界 |
| --- | --- | --- |
| #96：yt-dlp Runtime | 共享路径兼容已验证的 2026.3.17 / 2026.6.9，保留同次请求真实版本一致性和 Runtime 缓存指纹；CI 含 Python 3.12 隔离路径 | 不是自动接受所有新版本，不替生产升级 Runtime |
| #97：平台台账 | 按精确 host 聚合跨批次能力、Scout/Lab 来源、作品/候选与路线参考；未确认身份和离线证据单列 | 行数、历史导入和样本成功不等于产品新增上线 |
| #98：已审查 Common 路线 | 明确固定 Backend、正常 API 保存、与 host/Backend 绑定的未来共享确认，普通解析读取并再次核验 | 不是遍历所有 Common；不覆盖成熟路线或自动扩大许可/匿名准入 |
| #99：yt-dlp 格式选择 | 真实候选、音视频类型、大小/语言/格式表达，按需检查所选 HTTPS MP4，卡片独立选择与 Batch 快照 | 不根据后缀猜轨道，不自动合流所有 HLS/DASH |
| #100：服务格式与交付 | 天天/Kukutool 通用及抖音专用结果接入原结果页；全部视频清晰度展开、保留标题/分组、同资源别名处理、逐候选操作绑定 | 通用服务与专用来源准入不同；没有返回的档位不补造 |
| #100：抖音服务与播放 | 天天有效缓存优先，过期先重新请求天天，失败/期限后兜底；新标签页播放点击时所选视频 | 不把天天优先推广到所有来源，不跨过期拼接旧作品信息 |
| #100：下载与更新 | 符合条件的绑定视频复用共用流式路径；旧 Worker 更新等待不静默降级 Blob，接管后重新核验 | 不是所有 HTTPS/CDN 都具备 SW 条件；不能打断已有任务 |

没有人工路线的适用通用来源，可按默认顺序使用已配置且获准的 Provider，包括服务通道；人工路线不是启用许可证。既有 Native/成熟平台路径、资源安全条件、Runtime 与公开准入仍是独立约束。管理员配置详情见 [Provider Lab](provider-lab.md)，普通用户操作见 [README](../README.md)及[格式选择指南](ytdlp-result-picker.md)。

### F1–F5 合并前修复

[五项修复与复审记录](https://github.com/Daiobs/AnimeImages/pull/100#issuecomment-6257613265)确认以下内容保留在最终树：

- F1：台账/历史只把严格确认的目标媒体作为已验证参考，离线与证据不足不冒称成功。
- F2：旧卡片被替换或取消后，晚到的下载准备响应不能继续触发保存；已经接受的远端任务不伪称已撤销。
- F3：未知大小的空 200 流报失败，不报下载完成，不自动重试或改 Blob。
- F4：元数据读取先验证 Range 与剩余预算，避免畸形长度导致超预算读取。
- F5：原始签发记录与派生检查投影分开；合法格式检查不破坏仍有效的解析缓存，真实期限和关键身份检查不放宽。

最终 main 的实际 CI 已在[合并记录](https://github.com/Daiobs/AnimeImages/pull/100#issuecomment-6259776450)逐项核对。本文档同步没有再次开展整仓功能或真实上游验收。

## 已有浏览器证据与限制

| 证据 | 版本 / 环境 | 已确认与未确认 |
| --- | --- | --- |
| PeerTube 两档视频 | #99 历史记录，正常 Chrome 独立匿名会话 | 1080p 与 720p 选择及实际落盘；不把早期文件追认为 SW 交付，也不推广为所有来源匿名开放 |
| 服务多档文件 | #100 初始服务接入记录，Staff、隔离单通道配置 | 天天/Kukutool 代表资源实际文件；不替代日常默认路线或匿名准入证据 |
| Chrome 真实流式 | `58f79d57bcdf6cdd8ef234e59f91d02355f0626a`，原日常页面/配置、抖音专用天天 | 所选非默认 720p 的下载任务及部分文件增长早于流结束；不是只看到 transport 或文件最终存在 |
| Safari 旧用户升级与流式 | `e7c9d9f73155ef5de9f99f3e09c912335ced29c1`，原 Safari 日常会话 | 正常关闭/重开完成 v2→v4；720p SW 任务开始早于流结束，文件对应所选资源 |
| 应用内浏览器取消 | Chrome 对照任务中的同一当前 SW/小型慢流 | 首块后下游取消仍有内部原因缺口，不能解释为用户主动取消，也不能用正常 Chrome 成功替代 |
| 最终组合树 | `1befb9f3c27a880d91da26305a27f4a671faccce` | 后续 F1–F5 定向回归及完整 CI 保留；没有重新下载所有历史样本 |

来源：[格式选择历史记录](ytdlp-result-picker.md)、[Chrome 对照](https://github.com/Daiobs/AnimeImages/pull/100#issuecomment-6251827401)、[Safari 升级](https://github.com/Daiobs/AnimeImages/pull/100#issuecomment-6252702886)。Chrome/Safari 记录未单独声明匿名身份的，不补写为匿名通过。

当前保留的能力/证据限制：

- 独立音频真实保存、全部 HLS 合流、所有历史样本视觉效果与所有浏览器尚未全面验收。
- 上游有候选但受上下文、协议、过期或访问限制，不等于“平台不支持”，也不等于文件可保存。
- HLS/分离轨候选保留与特定服务器合流能力不是全量下载保证。
- XHS Live Photo 的 Staff 单作品与匿名/Batch 静态边界仍分别处理；原始配对 ZIP 不是自动 iOS Photos 导入。
- videodl 许可及既有授权用途继续保留；本地固定路线的代码能力不授权生产或商业使用。其他用途如缺少独立许可证据，保持待核验。

## 最后已记录的生产发布

[2026-10-16 发布记录](https://github.com/Daiobs/AnimeImages/pull/95#issuecomment-6211550835)明确记载：

| 项目 | 当时记录 |
| --- | --- |
| 应用 SHA | `bf9f1255b006ea93b7a0cf766d28f1db6138113f` |
| 应用 tree | `8364ff8f7b0fbac01a2419376acbe5ea7a0a94bf` |
| 独立发布工具 SHA | `6f28bcefa0701c1eca9ce068a7b2e03eb3b01639` |
| 发布方式 | 既有受控固定 SHA 通道，正式 apply / verify 成功 |
| 数据库 | 0001–0028 已应用；当次 migrate 无新增变更 |
| Runtime | 保留 yt-dlp 2026.6.9；应用共享版本门禁当时仍只接受 2026.3.17 |
| 开关与业务状态 | Lab 关闭；账号、路线、邮件/Daily 和既有权限未因当次发布改变 |
| 实际业务验收 | 当次未执行真实 Provider 解析、媒体下载、发信或手动 Daily |

#96 修复了上述共享 Runtime 兼容限制，但它目前属于已合并、未记录部署的后续代码。不能因为 main 已更新，就声称生产该路径已通过或公众能力已开放。生产工具、开关、账号与 Runtime 的**当前值**未由本文档任务重新核验。

后续发布需按当时数据变化决定备份与验证，使用已有 [固定 SHA 发布通道](../ops/managed-release/README.md#approved-release)。首次安装资料不是重复发布命令；不能从线上旧 checkout 直接调用旧部署脚本。本文不授权生产操作，也不要求为版本一致性回滚迁移或重建数据库。

## 较早历史记录

以下仍是当时的有效记录，**都不是最新生产版本声明**：

| 时间 / 阶段 | 历史记录与范围 |
| --- | --- |
| 2026-09-07 邮件能力 | 应用 `003f75b83e164fc19a043c8213e887ddf9879459`，迁移 0026 与邮件加密能力已部署；当时真实邮件未配置、浏览器验收待补。见[邮件部署记录](admin-mail-settings.md#production-deployment-2026-09-07) |
| 2026-09-04 Asset 3 | 应用 `509ef74b76a4424f907bbf6152a824b5a9b2d633`，Daily 指定检查启用；当时 SMTP/通知与浏览器有保留缺口。见[生产激活记录](research/production/ASSET_3_PRODUCTION_ACTIVATION_V1.md) |
| Browser Photo Archive | `e70848c8b89225beba0838e9b681aaf5b7fdf789`，图片字节、扩展、ZIP 与浏览器路径验收。见[原始记录](research/browser-photo-archive-format-reliability-v1.md) |
| Download UI / Batch Actions | #42 的界面与权限验收，见[原始记录](research/download-ui-batch-actions-production-acceptance-v1.md) |
| 更早平台/发布里程碑 | 保留在 [CHANGELOG](../CHANGELOG.md) 与 `docs/research/`，原 Head、范围和失败事实不追改 |

更早的 Native/Hybrid、竞速、Blob 恢复和旧 Worker 说明只代表对应阶段。当前运行行为以本页快照的代码及现行指南为准；旧成功不重新标成新的 transport 或全部平台验收。
