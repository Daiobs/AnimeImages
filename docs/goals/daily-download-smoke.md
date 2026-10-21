# 目标：每日真实样本下载冒烟（T11），并清理生产恢复留下的三个小问题（2026-10-21，协调人：Claude）

## 一句话目标

生产环境每天自动对小红书图文、小红书视频、抖音、B 站各跑一次“解析 → 下载前 1KB”的冒烟，记录每个结果的字段覆盖。任一平台失败时，按现有规则发通知。

## 完成条件（全部满足才算完成）

1. M0 的三个小项都已合并。
2. T11 的改动已合并，并用托管发布流程发布到生产。
3. 生产上至少有一次真实的每日验证运行，报告里四个平台都有下载冒烟结果和字段覆盖。全部通过，或者每个失败都附有证据和原因。
4. 最终 main 上必需的 CI 检查全绿，`python manage.py test contracts` 也通过。
5. 本文件末尾的进度日志写清楚：做了什么、证据是什么、哪些没做以及原因。

## 先读

- `AGENTS.md`
- `docs/product-invariants.md`：INV-01…09、A1/A2、G1–G3
- `contracts/README.md`
- `.github/risk-tiers.json`：风险等级以 PR 上 `protected-files` 检查的结果为准
- 每日验证：`xhs_download/services/provider_daily_validation.py`、`xhs_download/services/provider_health_tests.py`、`xhs_download/management/commands/run_daily_provider_validation.py`、`ops/systemd/animeimages-provider-validation.timer`（服务器时间 03:30）
- 模型：`ProviderHealthTestSetting`（各平台样本 URL、`daily_enabled`、`daily_test_keys`、邮件通知）和 `ProviderHealthTestRun`
- 现成的公开冒烟脚本：`tools/production_smoke.py`（#118）。它做完整下载，作发布后人工验收用，不放进每日任务。
- `docs/goals/production-recovery.md` 和 PR #121 的最终记录

## M0 · 三个小项（每项一个小 PR，按风险等级处理）

1. **补齐上一目标的进度日志。**
   - 把本地已追加、但还没推送的最终日志提交到 `docs/goals/production-recovery.md`。
   - 只改这个文件（L0），CI 全绿后合并。
2. **天天业务码显示完整。**
   - 现状：`tiantianjiexi_generic.py` 里的 `ensure_api_success` 只认 1–3 位业务码，所以 1001 被记成 `SERVICE_BUSINESS_CODE_UNKNOWN`。
   - 改法：放宽为最多 5 位，例如 `-?[0-9]{1,5}`。同时确认 `safe_error_detail` 会放行新格式，让 #119 的日志能直接显示 `SERVICE_BUSINESS_CODE_1001`。
   - 带回归测试，预期 L1。
3. **复用 B 站匿名上下文。**
   - 现状：#120 和 #121 之后，每次解析都要多请求首页、spi 和 nav 三次。
   - 改法：
     - 匿名 Cookie（buvid3、b_nut、buvid4）在进程内复用约 10 分钟；
     - WBI 密钥在进程内复用约 1 小时；
     - 遇到 412 或签名失败时，立即作废并重建。
   - 限制：只放在进程内存里，不写共享缓存或数据库；不发给媒体主机；不记录 Cookie 的值。
   - 回归测试：
     - 有效期内的第二次解析不再请求首页、spi 和 nav；
     - 遇到 412 或签名失败后会重建；
     - 过期后会重建。
   - 预期 L1。

## M1 · 实现每日下载冒烟

1. **只读核对生产后台（不修改）：**
   - `xhs_gallery_url`、`xhs_video_url`、`douyin_url`、`bilibili_url` 是否已填；
   - `daily_enabled` 是否开启，邮件通知是否开启、收件人是否已配置。

   有任何一项缺失，就暂停，请 Human 在后台设置。代码里不写死样本（A1）。
2. **新增四个健康测试项。** 例如 `download_smoke_xhs_gallery`、`download_smoke_xhs_video`、`download_smoke_douyin`、`download_smoke_bilibili`，样本 URL 读上面四个字段。它们是 `daily_test_keys` 的可选项，是否加入每日清单由 Human 在后台决定。
3. **每一项的检查：**
   - 解析：用与用户相同的内部解析入口，抖音按用户默认设置打开超清。
   - 下载：取默认清晰度的第一个资源，走与 download-prepare/stream 相同的服务端取数 helper，只取前 1024 字节（用 Range 或字节上限），不保存。
   - 记录：HTTP 状态、最终主机和端口、跳转次数、跨源跳转时是否丢了 Referer、Content-Type、是否支持 Range。这些是 N2/N3 的证据。
4. **字段覆盖：**
   - 记录标题、封面、清晰度、文件大小是否齐全，以及由哪个 Provider 提供。
   - 直接读现有的 `ParseResult` / `MediaAsset`，不新增并行的结果模型（T14 前的约束）。
5. **存储和报告：**
   - 给 `ProviderHealthTestRun` 加一个可空的 JSON 字段，存上面的明细。这一改动涉及迁移，属于 L2，单独开 PR 后暂停，等待审阅。
   - 现有的每日报告和邮件按平台显示这些结果；失败沿用现有的降级/恢复通知规则。
6. **测试：**
   - 人为制造的失败要在报告里被标出来，例如端口被策略拒绝、Referer 丢失、HTTP 403。
   - 不发真实网络请求，用 `contracts/network.py` 风格的替身。
7. **预算：** 每个平台每天 1 次解析加 1 次不超过 1KB 的请求。不重放旧的签名 URL。

## M2 · 发布并在生产上验证

1. 用托管发布流程发布包含 M1 的 main。发布前确认 CI 全绿、回滚点可用。发布后跑一轮 `tools/production_smoke.py`，结果不得比 6517a38 时差；变差就回滚并暂停。
2. 等下一次定时运行（服务器时间 03:30），或者请 Human 在后台手动触发一次。不得为此修改 systemd 或配置。
3. 用只读方式读取那次运行的报告，确认四个平台都有下载冒烟结果和字段覆盖，把结论记进进度日志。如果读不到报告，暂停，请 Human 提供。

## M3 · 收尾

在进度日志里写最终总结，列出范围外的后续事项。

## 授权

沿用生产恢复目标的授权：可以用现有托管发布流程发布已合并的 main，并按流程回滚。授权不包括修改生产配置、`.env`、数据库内容、systemd、nginx、仓库设置或分支保护。

## 约束

- **不变量和契约就是规格：**
  - 不得削弱、回退或绕开。
  - 不新增“失败一次就永久停用、只能人工恢复”的状态（INV-02）。
- **禁止改动：** 两个 Worker 协议文件（G1）。
- **PR 规则：**
  - 一个问题一个 PR。描述第一段写“用户可见行为：之前 → 之后 → 不变量编号”，并写明风险等级。
  - L0/L1 在 CI 全绿后可以自己合并。
  - L2 开出 PR 后，这一项暂停，等待审阅；不要添加 `human-approved` 标签；合并前不要再推送新提交。
- **推送：** 先在本地跑完受影响的测试再推送，不要反复推送半成品（CI 按分钟计费）。
- **范围：**
  - 发现范围外的问题，只记进进度日志的“后续”部分，不开新工作。
  - 不改天天的路由逻辑，也不改下载交付方式（INV-03、INV-04）。
- **敏感信息：** PR、日志和进度记录里都不写密钥、Cookie 或令牌。

## 暂停条件（暂停并写明原因）

- 需要 L2 审阅，或者需要 Human 修改后台、生产配置。
- 同一个检查点连续 3 次修复尝试仍然失败。冒烟轮数不算修复尝试。
- 发布后冒烟变差，已经回滚。
- 需要登录 Staff、会明显多消耗付费额度，或者需求本身不确定。

## 不在本目标内（记作后续）

- 积累一周数据后（2026-10-28 起），根据报告里的端口和 Referer 证据，对 N2、N3 给出保留或调整的建议。
- T14 统一结果模型、T9 后台管理 Provider 链、T15 成熟平台进入工作台。

## 汇报格式

每完成一个检查点，在进度日志里追加一行：

`[时间] 检查点 | 已验证（证据） | 剩余 | 是否阻塞`

## 进度日志

（由执行者追加）

[2026-10-21 14:43 UTC] 准备与 M0 第 1 项完成 | 已读取 AGENTS.md、产品不变量及契约说明；协调人计划原样入库，提交时 cmp 和 SHA-256 一致，正文未改写。按 Human 授权与上一目标的 12 行最终进度合并为 [PR #122](https://github.com/Daiobs/AnimeImages/pull/122)，实际 protected-files 判定 L0，Head `d9365dd` 六项检查全绿后正常 Squash Merge；main `733e3680cd79cf5a3ed8b0756bd4f622eb27ed97` 的父节点、两份文档差异及 tree `ec1c9f7d3912153a6c0a2fd2bb24fe7115fef1ce` 均核验，main Django 与 Product Contracts CI 通过 | 本行先保留本地，后续检查点一并提交；未开始 M0 第 2/3 项或 M1–M3，未访问生产、未触发 Daily、未改变本地 8000，等待 Human 的 /goal 指令 | 否，准备完成，目标尚未启动

[2026-10-21 14:55 UTC] M0 第 2 项本地验证完成 | /goal 已启动；解析器与 safe_error_detail 同步接受最多 5 位 ASCII 业务码，正负 1001/99999 可到达 Lab 和公开 B站失败日志，六位、非数字、全角数字及附加私密文本仍拒绝。3 项反例在旧代码产生 4 个预期断言失败；修复后相关 69 项（含公开 HTTP 契约）和 Django check 通过。首次扩展测试因未指定已有 ParseHub 测试解释器失败，指定原有 Runtime 后通过，未安装依赖或改变日常 Runtime。使用原本地配置的隔离数据库副本，经匿名 /results/ 真实解析固定抖音样本一次，天天 SUCCESS_FRESH、4 个候选、约 1.6 秒；并行 Native 最后超时，未改变已返回结果，未启动 Kukutool。仅真实解析，不保存媒体；1001 错误展示是离线边界回放，不冒充本次上游失败 | 待同一问题的 L1 PR 实际检查与合并；未改路由、下载、生产或日常 8000，M0 第 3 项尚未实现 | 否

[2026-10-21 15:10 UTC] M0 第 2 项合并 | [PR #123](https://github.com/Daiobs/AnimeImages/pull/123) 实际 L1，Head `71a5106` 六项 CI 全绿、无未解决线程，正常 Squash Merge 为 main `2f541651fa0e988cd01ed977cec55af5b3f61886`；父节点 `733e368`，tree `d6c6ccd92975246cdcbd16dc3ead92879ff615c4` 与 PR 相同。首次文件保护工作流已成功结束而 GitHub 检查项仍挂起，只重跑该既有门禁后恢复，未改门禁或重复跑 Django | 新 main CI 自动运行中；继续 M0 第 3 项，不部署或触发 Daily | 否

[2026-10-21 15:12 UTC] M0 第 3 项本地验证 | Cookie 进程内 TTL 600 秒，WBI 材料 TTL 3600 秒，签名每次重算；缓存拒绝立即失效，原期限内最多一次新上下文重建，冷启动即失败仍沿用原兜底。并发初始化、TTL 分离、晚到响应/失败、同代旧快照、最后真实异常及媒体不带 Cookie 均有离线回归；102 项既有 B站回归、10 项新增缓存测试、22 项契约（本机 Nginx 两项跳过）、check/diff 检查通过。原配置隔离数据库的匿名 /results/ 对固定 B站作品真实解析一次，约 0.76 秒返回一份 Native 视频、匿名 Cookie 3 项，未调用天天、未保存媒体；缓存 TTL/并发证据是离线受控时钟，不冒充等待一小时的实时验证 | 待独立复审与单独 L1 PR 的实际 CI/合并；未写共享缓存、数据库或 Cookie 值日志，未改 Worker、媒体交付、日常 8000、生产或配置 | 否

[2026-10-21 15:22 UTC] M0 第 3 项复审与最终本地回归 | 独立复审定位并修正四个边界：重建失败仍保留首次 HTTP 412 的兜底资格并保留末次异常原因；同代晚到 Cookie 撤销不丢失；Cookie TTL 换代后旧失败不能撤销新上下文；最初创建者同样服从撤销。13 项新增测试及既有 B站/HTTP 契约合计 137 项通过（本机 Nginx 两项跳过），未改契约或冻结文件；上述真实冷启动解析证据继续保留，新增边界仅离线验证。M0 第 2 项新 main 的 Django、Product Contracts CI 已成功 | 提交本项单独 PR，等待实际风险门禁与 CI；尚未进入 M1 或访问生产 | 否
