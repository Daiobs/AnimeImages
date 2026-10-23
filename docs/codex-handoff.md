# DokiTool 开发交接

核对日期：2026-10-19 UTC。这是后续开发的快速入口，部署证据集中在 [当前版本状态](current-version-status.md)，历史任务不作为新的永久制度。

## 从哪里继续

- 本次应用快照：main `c2b8ac51cfb87d4c030d321bf763080708db9593`，tree `1befb9f3c27a880d91da26305a27f4a671faccce`。
- #96–#100 已顺序合并，不再向这些已合并功能分支追加工作。新任务从实际最新 main 建立分支，除非任务明确指定其他基线。
- 最后已记录的生产应用：`bf9f1255b006ea93b7a0cf766d28f1db6138113f`。本次没有访问生产；应用合并不代表 Runtime、开关、账号或部署已更新。

开始前确认工作目录、远端和并行工作：

```bash
pwd
git status --short --branch
git fetch origin
git rev-parse HEAD
git rev-parse origin/main
git worktree list
```

这些 SHA 是文档快照，不追逐文档自身提交。保护日常 8000 所在工作树、未提交修改、原 `.env`、解释器、数据库、管理员、历史及正式路线；不要为普通开发切换其他任务的服务。已有修改先读清楚，不覆盖或强推。

## 当前用户主流程

粘贴作品链接 → 获取素材 → 查看原标题、平台、资源数量和预览 → 有多档时选择清晰度 → 原下载按钮保存所选文件。

视频格式全部展开并按清晰度降序显示，桌面在预览右侧、窄屏上下排列。只合并已证实为同一资源且事实相容的别名；原始候选保留。不同视频、图集与 Live Photo 不能机械合成清晰度组。格式选择不是 Staff 特权，但匿名来源/Provider/下载准入仍独立核验。

当前已接通 yt-dlp、天天/Kukutool 通用服务以及抖音专用服务候选。无共享候选或无法可靠分组的旧结果保留原路径。所选动作绑定原候选、作品、资源、文件名与有效期；适用的旧资产/抖音专用路径另核验准确 manifest，不把通用候选和专用资产授权混为一套。播放、保存和 Batch 导出不能偷换成默认资源。

中央播放按钮在提供新标签页操作的路径上核验并打开当时选中的 CDN 视频，不切换勾选或触发保存；保留的内嵌预览路径不因此改写。下载不主动启动播放器，浏览器自身的自动打开偏好不是应用保存成功的依据。

## 路线与缓存

- 成熟平台策略与通用上游路线并存。适用来源可使用已配置、获准的默认 Provider 顺序，不必先逐站保存人工路线，也不对每条链接全通道竞赛。
- 人工路线保存首选/备选与质量偏好，作用于精确 hostname；内容分组建议不是内容类型路由。成熟路线保护、来源准入、权限、Runtime 和安全检查仍生效。
- 固定 videodl Common 使用已审查的具体 Backend 和类别。保存时的未来共享确认与 host/Backend/执行范围绑定，执行时重新核验；单次 Lab 测试和历史导入不授予未来权限。许可及使用范围不因代码接通而放宽。
- **当前抖音相关服务链**采用天天优先：有效缓存直接复用；缺失或过期先请求天天，Kukutool 缓存不能提前截断；天天失败、不可用、限流或达到既定期限才进入获准兜底。天天成功不预热 Kukutool。不能把这条规则写成全站所有来源都先请求天天。
- Kukutool 与 Native 属于同一兜底档，不是三级串行；首选等待与兜底各有有界窗口，显式调用方 deadline 仍限制整体执行。保留本地账号串行、期限、请求上限和 single-flight；普通超时不再形成无限期账号阻断或人工恢复前置条件，历史未知任务不自动重放。异步 Single 可续查原在途任务，晚到结果只更新同次解析且无活动下载的页面；不重发 Provider，不替换已开始下载的资源。停止等待不证明远端 HTTP 已停止。
- 缓存到期正常重新解析，不从历史档案跨过期拼接旧标题、封面或媒体授权。本次响应取得的作品信息正常保留；缺失则如实显示。

## 下载与旧 Worker

清晰度选择只改变资源及相应授权，不能仅因 Provider 名称、候选绑定或新旧卡片身份改变传输优先级。

符合当前访问策略和授权的普通完整视频优先原生附件导航；资格来自已有无应用请求头探测最终采用的短期附件响应事实，并要求按有效 `filename*` / `filename` 解码后的文件名与响应 MIME 扩展名相符，不从 URL 的 `.mp4` 后缀猜测。允许 `12345.mp4` 等合理源站基名；带 Referer 等上下文的成功、`.bin`、错后缀或无有效附件名都不能作为原生资格。后续否定或安全拒绝撤销旧资格，普通网络失败按既有可达 HEAD 回退处理。没有该事实时复用已有适用的 SW 流式附件，不把打开播放器称为保存。原生进度和最终文件名由浏览器/源站决定，页面只报告请求已移交；可控响应路径继续使用作品短标题和真实扩展名（INV-06）。SW 路径的协议、上下文、主机策略、CORS、MIME 和大小限制保留。音频、图片、明确的服务端合流及其他特殊输出保持各自路径，不把所有 HLS 当成完整 MP4。

Blob 只在既有策略明确允许的兼容条件下使用；启动后失败、未知状态或取消不能自动再全量下载或转服务器。`provider_strategy`、动作描述符和前端共用入口须保持一致，不能前面选择 streaming、后面因 bound 标记改回 Blob。

所选候选及 manifest 在正常动作接口核验，页面冻结资源并在移交前复核有效期。Worker 只接收普通传输字段，不接收 Provider、候选 token 或 manifest；备用下载使用 `/__animeimages-stream-download/` 专用注册的 active Worker，不依赖首页的旧根作用域 controller，不要求精确业务版本握手。

浏览器按正常生命周期更新 Worker，不强制接管或打断在途下载，也不因存在 waiting Worker 自动改走 Blob。旧绑定消息仅为已经打开的旧页面保留兼容，不另行维护两套下载系统。文件名在移交前保留既有资源命名和明确后缀，不能把命名修复变成必须升级 Worker 的条件。

## 关键入口

| 范围 | 代码 / 指南 |
| --- | --- |
| 页面和动作路由 | `djangoProject/urls.py`、`xhs_download/views.py`、`xhs_download/ytdlp_selection_views.py` |
| 共享执行与 Runtime | `services/quality_execution.py`、`services/upstream_matching.py`、`tools/upstream_match_worker.py`（services 路径均在 `xhs_download/` 下） |
| 候选与选择 | `services/upstream_candidates.py`、`services/ytdlp_selection.py`、`services/service_format_selection.py`、[格式选择指南](ytdlp-result-picker.md) |
| 抖音服务协调 | `services/platforms/douyin_providers.py`、`services/platforms/douyin.py` |
| 下载策略与前端 | `services/provider_strategy.py`、`static/js/ordinary_media_download.js`、`static/js/ordinary_media_streaming.js`、`static/js/ordinary_media_download_sw.js`、`static/js/ytdlp_result_picker.js` |
| 首页与 Batch 消费者 | `templates/partials/xhs_results_region.html`、`templates/partials/batch_result_card.html`、`static/js/batch_parse.js` |
| 台账 / 路线 | `services/provider_lab/history.py`、`services/provider_lab/ledger.py`、`services/upstream_routes.py`、`services/saved_videodl.py`、[Lab 指南](provider-lab.md) |
| 环境 / 架构 | [开发指南](development.md)、[架构](architecture.md)、[Provider 准入](provider-runtime-gates.md) |

Lab 页面、持久执行历史和精确 host 路线仍由后端权限控制。短期资源检查与持久脱敏证据不同；历史导入不能签发下载权限，浏览和草稿不能触发解析。未确认身份、离线回放和真实媒体成功分别表达。

## 证据与验证

已有证据须保留原 Head、Provider、用户身份、配置范围和实际 transport：

- #99 的匿名 PeerTube 1080p/720p 证明该代表作品的选择与落盘，不自动代表全部网站或 SW 交付。
- [正常 Chrome](https://github.com/Daiobs/AnimeImages/pull/100#issuecomment-6251827401)：`58f79d57`，日常页面的抖音专用天天非默认 720p，真实 CDN 增量交付与文件证据。
- [Safari 与旧 Worker 升级](https://github.com/Daiobs/AnimeImages/pull/100#issuecomment-6252702886)：`e7c9d9f`，原 Safari 日常会话从 v2 正常换代到 v4，所选 720p 的浏览器任务早于流结束。同一记录中“不打断已有下载”的生命周期证据来自持久 Chrome 上的真实旧 v2/新 v4 与本地慢流夹具，不冒称 Safari 的在途升级实测。不要据此推断所有 Safari/来源都通过。
- 应用内浏览器同类 SW 流在首块后取消的内部原因仍不完整，不能把它等同用户取消或用 Chrome 成功代替。早期 Blob 文件不追认为 SW 成功。
- [F1–F5 修复及复审](https://github.com/Daiobs/AnimeImages/pull/100#issuecomment-6257613265)和[最终合并记录](https://github.com/Daiobs/AnimeImages/pull/100#issuecomment-6259776450)记录后续定向回归与最终同树验证。

按改动风险运行现有 Django、Node、Worker 入口；先复用对应版本的有效证据，再补具体缺口。自动测试使用脱敏输入和有界本地边界，不把替身或页面回调写成真实媒体成功。独立音频、全部 HLS 合流与全浏览器覆盖尚未全面验收，不自动成为每个任务的前置条件。

## 发布交接

发布使用已有 [固定 SHA 的 SSH 发布通道](../ops/managed-release/README.md#approved-release)，从独立、已核验的工具 checkout 执行；须另行获得明确授权并重新核对现场 SHA、配置、迁移和服务状态。[Admin Release Management](release-management.md) 是另一项可选执行入口，最近生产记录仍为 record-only，不能据页面存在推断 Web 执行器已安装。本交接不授权部署、生产重启或权限变更；最新记录不等于本次实时核验。
