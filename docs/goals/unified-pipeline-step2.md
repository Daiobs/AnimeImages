# 目标：统一管线第 2 步，Provider 适配器加影子比较（2026-10-25，协调人：Claude）

## 一句话目标

把生产上能走到的每一个 Provider 都映射到统一结果模型，并且在不改变任何用户可见行为、不增加上游请求的前提下，持续比较“旧路径的结果”和“统一管线的结果”。最后给出每个平台能否切换的证据报告。

## 为什么做这件事（Human 原话）

Human 在 2026-10-25 描述的设计：

> 1.后台有“provider管理”，“provider健康测试”两个模块负责接入上游provider，provider对应解析的平台，不同平台设置url链接，让provider作批量测试
> 2.平台链接通过provider解析后，平台通用能力统一处理provider返回的解析结果，呈现给用户
> 3.用户选择自己需要的清晰度，主要通过浏览器下载，不消耗服务器流量（blob备用/blob用于youtube音频视频合成）

> 但是这个通用能力我修改了一百多个PR，当前仍在反复绕圈子中

本目标的委托原话：“写第 2 步给 GPT 的目标文件：适配器加影子比较，按契约执行。”

第 1 步已经完成（#134，main `26bf3c1`）：契约、内核和契约测试都在 main 上。本目标只做第 2 步。第 3 步（按平台切换、删除旧路径）另立目标，不在这里做。

## 完成条件（全部满足才算完成）

1. 盘点清单里每个“生产可达”的 Provider × 平台组合：
   - 要么有已合并的适配器；
   - 要么在清单里写明不做的原因，例如已停用或生产不可达。
2. 影子比较已合并、已发布到生产，并且在健康测试和每日验证里运行。至少有 3 次每日验证运行带有影子结果。
3. 如果 Human 打开了生产解析的影子开关，至少积累 7 天的计数；如果没打开，在报告里写明。
4. 统一授权（契约第 5 节）的内核实现已合并，暂不接入。
5. `docs/goals/unified-pipeline-step2-report.md` 已写完：按平台列出适配器覆盖、差异统计、每条差异的归类，以及“能否切换”的结论和依据。
6. 最终 main 的必需检查全绿，`python manage.py test contracts` 通过。
7. 从头到尾没有用户可见的行为变化：页面、解析结果、下载方式、文件名都与基线相同。

## 先读

- `AGENTS.md`，尤其是“统一管线迁移”一节：冻结规则、退回规则、发布门槛。
- `docs/architecture/unified-pipeline.md`：契约本身。本目标的一切以它为准。
- `xhs_download/services/pipeline/`：内核。`Work`、`Item`、`Variant`、`validate_work`、`field_coverage`、`decide_delivery`、`decide_archive`、`file_name`、`ChainRun`。
- `contracts/pipeline/` 和 `contracts/README.md`。
- `docs/product-invariants.md`：INV-01～09、A1/A2、G1～G3。
- 现有入口：
  - 解析：`xhs_download/services/registry.py`（`parse_media_page`）和 `platforms/` 下各平台适配器；
  - 第三方：`upstream_routes.py`、`quality_execution.py`、`upstream_candidates.py`；
  - 旧交付决定：`provider_strategy.py` 的 `build_download_strategy`；
  - 健康测试：`provider_health_tests.py`、`provider_daily_validation.py`、`provider_download_smoke.py`，以及 `ProviderHealthTestSetting` 的各平台样本 URL。
- 已有录制样本：`xhs_download/fixtures/native_replay`、`xhs_download/test_fixtures`、`tools/provider_lab/fixtures`、`tools/parsehub_lab/fixtures`。

## 设计要点（不要自行改变）

### 适配器的形状

- 放在 `xhs_download/services/provider_adapters/`，每个 Provider 家族一个模块。这个目录是 L1，不放进内核目录。
- 每个适配器有两部分：
  - `fetch(source_url, budget)`：调用上游，返回上游的原始数据或 `Failure`。**复用现有的上游调用代码**，不另写一套 HTTP 客户端。
  - `map(raw, source_url)`：纯函数，把原始数据映射成 `Work` 或 `Failure`。没有网络、数据库、缓存、时间依赖。
  - `parse(source_url, budget)` 就是 `fetch` 加 `map`，满足契约 2.1。
- `map` 的输入是**能找到的最底层、仍然保留全部上游事实的数据**：
  - 有上游 JSON、yt-dlp info dict、gallery-dl 元数据、页面状态时，就用它们；
  - 不得以旧的 `ParseResult`、`MediaAsset`、候选记录、manifest 或令牌作为输入，因为它们已经混入了旧路径的决定。
  - 如果某个 Provider 的现有代码拿不到这样的数据，就在盘点里写明，并暂停请 Human 决定。
- 平台差异只能写在适配器里（契约第 0 节规则 1）。同一个上游服务多个平台时（例如天天同时服务抖音、B 站、小红书和通用链接），可以是一个适配器内部按平台分别映射。
- 适配器只填事实，不做决定：
  - `tracks`、`merge_with`、`container`、`height` 等按上游给的填；
  - `required_headers` 只写请求头名称，值留在服务端；
  - `browser_fetchable`：只有已经有证据时才写 `true` 或 `false`，例如 CORS 实测、每日冒烟记录、现有契约。没有证据就写 `unknown`；
  - `host_policy`：用现有的主机白名单知识；
  - `is_original`：只有上游明确给出原画或源文件时才写 `true`；
  - 拿不到的字段留空，不要编，例如不能把“原始文件”当清晰度，不能把平台名当标题。
- 失败映射：每个适配器有一张表，把上游的状态码或业务码映射到五种失败之一，并给出安全的诊断码。例如：
  - 天天 `1001` → `unavailable`；
  - HTTP 412、429 → `unavailable`；
  - 作品已删除 → `not_found`。

### 影子比较的形状

- **不增加上游请求。** 旧路径照常调用上游。在旧路径已经拿到原始数据的地方，用一个“捕获”钩子把原始数据交给影子：
  - 没有开启影子时，钩子什么都不做；
  - 钩子不改变旧路径的任何返回值、时序或异常；
  - 旧路径在线程里调用上游时（例如抖音 Native 和超清并行），捕获也要能拿到。
- 影子拿到原始数据后：
  1. 用对应适配器的 `map` 得到 `Work`，再按内核规则算出默认变体、交付方式和文件名；
  2. 与旧路径最终给用户的结果逐项比较；
  3. 输出差异码。
- 差异码至少包括：
  - `ADAPTER_MISSING`：没有对应适配器；
  - `ADAPTER_INVALID_<违反项>`：`validate_work` 不通过；
  - `TITLE_DIFF`；
  - `ITEM_COUNT_DIFF`、`ITEM_KIND_DIFF`；
  - `DEFAULT_QUALITY_DIFF`：默认清晰度的高度或显示名不同；
  - `DEFAULT_URL_DIFF`；
  - `SIZE_DIFF`；
  - `DELIVERY_DIFF_<旧方式>_TO_<新方式>`：旧方式取 `build_download_strategy` 的结果，新方式取规则表的结果；
  - `FILENAME_DIFF`；
  - `RULE7_HIT`：新规则判为“不提供下载”；
  - `OLD_RESULT_MERGED`：旧路径把多个 Provider 的数据拼成一个结果。这种情况下，逐字段比较时只比这个 Provider 自己提供的字段（按 `field_sources`）；
  - `SHADOW_ERROR`：影子本身出错，已吞掉。
- **运行位置：**
  - 健康测试和每日验证：样本链接是后台数据，可以保存明细，包括差异码、两边的值、统一结果的字段覆盖和不变式检查结果。
  - 生产公开解析：只保存计数，维度是日期、平台、Provider 和差异码。不保存链接、标题、作品 ID 或用户信息。由后台开关控制，默认关闭；开不开由 Human 决定。
- 影子除了写明细或计数，不得有其他 I/O，尤其不得联网；不得延长前台预算；出错只计 `SHADOW_ERROR`，不影响请求。

### 差异的归类（报告里每条差异都要归类）

| 类别 | 含义 | 怎么处理 |
|---|---|---|
| A 适配器错 | 适配器的映射或事实不对 | 修适配器（允许单独的 L1 修复 PR，描述里写差异码） |
| B 旧路径错 | 旧路径的行为违反 INV 或契约 | 只记录，不修旧路径（冻结规则）；如果是相对基线的回归，按冻结规则的例外处理 |
| C 契约有意的变化 | 例如按事实从服务端改到浏览器（INV-03），或文件名按第 4 节变化 | 记录，切换前给 Human 看清单 |
| D 契约缺口 | 模型表达不了某个事实，或规则表没有覆盖 | 暂停，写明缺口，请 Human 决定；不得自行改契约或内核 |

## PR 清单（只做这些；清单外的工作先暂停请示）

按顺序进行。前一项合并后再开下一项，A 类修复 PR 除外。

| # | 内容 | 预期等级 |
|---|---|---|
| P1 | 本目标文件提交为 `docs/goals/unified-pipeline-step2.md`，加上 `docs/architecture/provider-inventory.md` 盘点（见检查点 M0） | L0 |
| P2 | 适配器基础：协议、失败映射工具、录制样本的格式和脱敏工具、适配器边界测试 | L1 |
| P3 | yt-dlp 通用适配器，包括 YouTube 音视频分离（`video_only` + `merge_with`） | L1 |
| P4 | gallery-dl 通用适配器 | L1 |
| P5 | 天天适配器：通用链接、抖音超清、B 站兜底、小红书 | L1 |
| P6 | KuKuTool 适配器：通用链接和抖音 | L1 |
| P7 | ParseHub 适配器，包括小红书图片；如果 videodl 在生产可达，也在这里 | L1 |
| P8 | B 站 Native 适配器 | L1 |
| P9 | 抖音 Native 适配器，包括原声作为音频内容项（契约决定 4） | L1 |
| P10 | 小红书 Native 适配器：图文、视频、实况照片（`groups`）、富文本 | L1 |
| P11 | 影子数据的存储：健康测试明细字段、生产计数表、后台开关（含迁移） | L2，暂停等待审阅 |
| P12 | 捕获钩子、比较器，以及在健康测试、每日验证和公开解析里的接入，附契约测试（见 M3） | L2，暂停等待审阅 |
| P13 | 统一授权的内核实现（见 M4） | L2，暂停等待审阅 |
| P14 | 报告 `docs/goals/unified-pipeline-step2-report.md` | L0 |

风险等级以 PR 上 `protected-files` 的判定为准。上表只是预期。

## 检查点

### M0 · 盘点（P1）

`docs/architecture/provider-inventory.md` 写清：

1. **Provider × 平台清单。** 每一行写：
   - Provider ID；
   - 平台；
   - 生产是否可达，以及依据（开关、路由、健康测试）；
   - 上游调用位置（文件:函数）；
   - 原始数据形式；
   - 旧路径中把它变成 `ParseResult` 的位置；
   - 计划用哪个 PR 覆盖。
2. **旧路径里按平台名、Provider 名或资源 ID 前缀做判断的位置。** 用 grep 列出文件:行，只做清单，这一步不改。它们是第 3 步的删除清单。
3. **现有样本。** 哪些 Provider × 平台已经有录制样本，哪些还需要录制。

### M1 · 适配器基础（P2）

- 协议：`name`、`platforms`、`fetch`、`map`、`parse`。用内核的 `invoke` 包住异常。
- 录制样本：
  - 放在 `xhs_download/fixtures/provider_raw/<provider>/<case>.json`；
  - 脱敏工具要把 Cookie、令牌和签名类查询参数的值替换为 `REDACTED`，保留主机和路径；
  - 提交前用工具检查，确保没有密钥形态的值。
- 边界测试（放在适配器测试里，不放 `contracts/`）：
  - 适配器模块不得直接导入旧的交付、展示和令牌模块，包括：
    - `provider_strategy`、`quality`、`downloads`、`download_handoff`、`provider_manifest`；
    - `xhs_livephoto_pair_handoff`、`upstream_delivery`；
    - 任何 `*views`；
    - `platforms/` 下名字含 `policy`、`authority`、`admission` 的模块。
  - `map` 不得进行网络调用：测试中把网络替换为“调用即失败”。

### M2 · 各家适配器（P3～P10）

每个 PR 包含：

1. 适配器代码；
2. 映射测试：每个录制样本都检查 `Work` 的每个字段、`validate_work(work) == ()`、`field_sources`，以及默认变体是否符合契约 1.4 第 2 条；
3. 失败映射测试：每种上游失败形态各一个用例；
4. 一段说明：每个事实字段（`browser_fetchable`、`required_headers`、`host_policy`、`is_original`）的依据。

如果需要新的录制样本，优先复用已有样本。确实需要真实请求时，每个 Provider × 平台最多 1 次，使用本地日常配置，不打印密钥，录制后立即脱敏。

### M3 · 影子比较（P11、P12）

- P11 只放存储，即模型、迁移和后台开关，并在 Django admin 里提供只读查看。开出后暂停等待审阅。
- P12 放钩子、比较器和接入，并附带契约测试（放在 `contracts/`，所以是 L2）：
  1. 影子开和关，公开解析的响应内容、状态码和上游请求次数完全相同；
  2. 影子抛出异常时，请求照常成功，`SHADOW_ERROR` 计数加 1；
  3. 生产计数里没有链接、标题或作品 ID；
  4. 健康测试明细里包含差异码、字段覆盖和不变式检查结果。
  开出后暂停等待审阅。
- 合并后按发布门槛发布，然后等每日验证（服务器时间 03:30）积累结果。请 Human 决定是否打开生产解析的影子开关。

### M4 · 统一授权（P13）

在内核里新增 `xhs_download/services/pipeline/grant.py`，并加 `contracts/pipeline/test_grant.py`：

- 只用标准库（`hmac`、`hashlib`），密钥由调用方传入。内核边界测试不允许导入 Django。
- 签发：
  - 绑定 `url`、`host_policy`、平台、`work_id`、`variant_id` 和过期时间；
  - 过期时间取变体 `expires_at` 与签发时间加短期有效期两者中较早的一个；
  - 只记录 `required_headers` 的名称，不包含任何请求头的值。
- 校验：
  - 篡改任何字段都要拒绝；
  - 过期要拒绝；
  - 换一把密钥要拒绝。
- 主机检查：
  - 按后缀匹配，而且必须在点号边界上：`evil-cdn.com` 不能匹配 `cdn.com`；
  - 只认 HTTPS，不接受 IP 字面量；
  - 逐跳调用由第 3 步的出站代码负责。
- 每条规则都做反向验证，记进 `contracts/README.md`。
- 开出后暂停等待审阅。

### M5 · 报告（P14）

报告按平台写清：

- 适配器覆盖；
- 每日验证的影子结果，以及生产计数（如果开了）；
- 每个差异码的数量和归类（A～D）；
- `RULE7_HIT` 的情况；
- A 类是否都已修复；
- B、C 两类的清单；
- D 类的缺口；
- 结论：哪些平台满足切换条件，哪些不满足、差什么。

建议的切换条件（Human 最终决定）：

- 该平台当前链上的 Provider 都有适配器；
- 连续 3 次每日验证没有未归类的差异；
- 生产计数（如果开了）7 天内 A 类为 0。

## 授权

- **合并：** L0、L1 PR 在 CI 全绿后可以自己合并。L2 PR 开出后暂停，不要添加 `human-approved`，合并前不要再推送新提交。
- **发布：** 可以用现有托管发布流程发布已合并的 main，遵守 `AGENTS.md` 的发布门槛：
  - 发布前必需检查和 `python manage.py test contracts` 通过；
  - 发布后跑 `tools/production_smoke.py`；
  - 结果比上一个发布点差，就回滚到该发布点并暂停。
- **不包括：** 修改生产配置、`.env`、数据库内容（包括后台开关）、systemd、nginx、仓库设置或分支保护。

## 约束

- **契约就是规格。** 不得修改契约文档、内核（M4 新增授权模块除外）或 `contracts/pipeline/` 里已有的测试来迁就实现。
- **冻结规则照常适用：**
  - 旧路径只允许加“捕获”钩子，而且必须证明行为不变；
  - 不修旧路径的问题（B 类只记录）；
  - 不新增交付方式或结果格式；
  - 不新增按平台名、Provider 名或资源 ID 前缀的判断（适配器内部除外）。
- **INV 契约就是规格：** 不得削弱、回退或绕开；不新增“失败一次就永久停用”的状态（INV-02）。
- **禁止改动：** 两个 Worker 协议文件（G1）。
- **PR 规则：**
  - 一个 PR 只做清单里的一项；
  - 描述第一段写“用户可见行为：之前 → 之后 → 不变量编号”，本目标里应当都是“无变化”；
  - 写明风险等级和对应的契约章节。
- **推送：** 先在本地跑完受影响的测试再推送，不要反复推送半成品。
- **范围：** 发现清单外的问题，只记进进度日志的“后续”部分，不开新工作。
- **敏感信息：** PR、日志、录制样本和进度记录里都不写密钥、Cookie、令牌或签名。

## 暂停条件（暂停并写明原因）

- 需要 L2 审阅（P11、P12、P13）。
- 需要 Human 打开生产影子开关，或修改后台、生产配置。
- 出现 D 类契约缺口，或某个 Provider 的现有代码拿不到原始数据。
- 想做清单之外的 PR。
- 同一个检查点连续 3 次修复尝试仍然失败。
- 发布后冒烟变差，已经回滚。
- 需要登录 Staff、会明显增加上游请求，或者需求本身不确定。

## 不在本目标内（记作后续）

- 第 3 步：
  - 后台按平台配置 Provider 链（契约 2.3，默认值放在数据迁移里）；
  - 按平台切换到统一管线；
  - 切换稳定后删除旧路径（M0 盘点出的判断位置、63 个交付相关函数、各类旧令牌）。
- 工作台按统一结果显示字段覆盖（契约 2.4 的界面部分）。
- 2026-10-28 之后关于 N2、N3 的建议（每日冒烟目标的后续）。

## 汇报格式

每完成一个检查点或一个 PR，在进度日志里追加一行：

`[时间] 检查点/PR | 已验证（证据） | 剩余 | 是否阻塞`

## 进度日志

（由执行者追加）

[2026-10-25 18:07 UTC] M0/P1 初始盘点 | 已验证：基线 26bf3c1；专用及通用入口、原始取数点、现有样本、37 文件/499 行条件分支索引已记录；现有 57 项 pipeline 契约通过；目标正文原样保留 | 剩余：当前生产准入未新取证，videodl 生产可达性及跨进程 raw 捕获的 PR 边界待明确；后续：D-01 轨道未知无法由现有 tracks 枚举如实表达 | 阻塞：按 D 类暂停，P2 未开始；未改契约/内核/旧路径，未请求上游或操作生产

[2026-10-25 18:53 UTC] M0/P1 Human 决定与恢复 | 已验证：#136 已合并为 a9cb418，契约第 9 节解除 D-01；tracks 默认 unknown，上游或平台未说明时按完整文件交付、不进服务端合并，明确轨道仍填写 av/video_only/audio_only；height_from_label 只解释明确 P 标签（如 1080P、2160P、1080P+），原画/高清/4K 不设 height；健康测试和影子以 fact_gaps 计数 TRACKS_UNKNOWN | 范围：yt-dlp/gallery-dl/ParseHub 等 raw 仅在子进程内时，P3～P10 只交付 map、录制样本测试及进程内可取得 raw 的 fetch，跨进程 raw 传递归 P12（L2）；videodl 不纳入第 2 步，报告记“生产可达性未确认”；缓存命中无 raw 时计 SHADOW_NO_RAW，不重新请求 | 不再阻塞：Human 已通过 #135，按 Ready、全绿、合并顺序收口后继续 P2；此前盘点的暂停及 P7 待确认描述仅为历史状态

[2026-10-25 19:18 UTC] M0/P1 合并 | 已验证：#135 在 L0、六项必需检查通过后正常 Squash Merge；main=9878051，父节点=a9cb418，文件树与预期合并树一致；合并后的 Django 和 Product Contracts CI 均成功 | 剩余：按清单继续 P2 | 不阻塞：未修改审批标签、契约、内核、Worker、8000 或生产

[2026-10-25 19:27 UTC] M1/P2 本地验收 | 已验证：新增内核外的 fetch/map/parse 协议与组合、失败映射、完整原始样本外壳及本地脱敏/检查 CLI；38 项适配器/录制工具测试和现有 63 项 pipeline 契约通过，共 101 项；包含单次 fetch、预算耗尽、异常隔离、map 无 I/O、禁止旧交付导入、敏感样本拒绝及 URL/HTML 编码保真反例；CLI 实际读写合成临时样本，不冒充真实上游录制；独立复核发现的预算、失败分类与四项脱敏边界问题已在本地修复 | 剩余：P2 当前 Head 的远端门禁及后续各家映射；不接旧请求链、不实现 P12 捕获 | 不阻塞：真实 Provider/媒体请求为 0，用户流程及日常 8000 不变

[2026-10-25 19:33 UTC] M1/P2 #137 补充反例 | 已验证：普通方括号/引号开头的标题不应被当作损坏 JSON；3 个子用例修复前被拒绝、修复后原样保留，严格嵌入 JSON 与敏感值检查仍通过；最终本地组合 102 项通过 | 剩余：集中推送修正后的准确 Head CI，旧 Head 绿灯不复用 | 不阻塞：只改 P2 录制工具、测试和说明，不影响旧请求链

[2026-10-25 19:58 UTC] M1/P2 #137 合并 | 已验证：准确 Head ecee5e15 的六项必需检查通过，protected-files 判为 L1，Ready 后正常 Squash Merge；main=60680788，父节点=9878051a，树 aa083327 与批准的 PR Head 一致；main Django CI 40227263549 与 Product Contracts CI 40227263556 均成功，Django 3637 项（5 项既有跳过）、Worker 31 项及 Node/check 检查通过 | 剩余：按清单进入 P3，未接入任何旧请求路径 | 不阻塞：未修改 Human 标签、契约、内核、Worker、日常 8000 或生产

[2026-10-25 19:58 UTC] M2/P3 取数与 D-02 契约边界 | 已验证：Variant 没有 protocol/清单分段/DRM 事实，交付表也没有相应条件；现有真实缩减录制 421dcc02aca139ef0727c86a 含 m3u8_native、manifest_url、has_drm=false，合成 Worker 夹具覆盖 http_dash_segments，旧选择器流媒体/DRM 拒绝回归 1 项通过；无网络内核反例把普通 HTTPS 文件、HLS 清单及合成 DRM 文件的可表达事实分别映射后，三者 validate_work 均为 ()，交付均命中规则 6 browser_streaming，说明不能如实保留这些格式的限制（不是当前生产回归）；未找到完整真实 yt-dlp info_dict，现有视频/音频录制均为注明字段省略的历史缩减样本，不冒充完整录制 | 剩余：请 Human/协调人决定清单、分段及 DRM 的事实表达、默认选择与交付规则，再继续 P3；跨进程 raw 仍归 P12，不以旧候选补 raw，不丢格式或伪造 browser_fetchable/请求头来绕过缺口 | 阻塞：按 D 类暂停；P3 仅建立 codex/pipeline-ytdlp-adapter 分支，未写适配器、未修改契约/内核/已有测试，Provider/媒体请求为 0，8000 仍为 834d331、PID 97825，生产未操作

[2026-10-25 18:07 PDT] M2/P3 Human D-02 决定与恢复 | 已验证：#138 已合并为 b8552ad，契约第 9 节及内核按上游事实保留 drm/segmented 变体，由第 0 条不提供下载并显示原因；URL 路径以 .m3u8/.mpd/.m4s 结尾却未标 segmented 时结果无效；默认变体及晚到比较均优先可下载变体；服务端拼接分片不在范围内 | 剩余：继续 P3 的原始 yt-dlp 映射、录制样本及回归，raw 跨进程仍归 P12；上次两条未提交日志一起提交 | 不再阻塞：D-02 已由 Human 决定，不改契约、内核或已有 contracts/pipeline 测试；日常 8000、配置和生产不变

[2026-10-26 01:34 UTC] M2/P3 本地验收 | 已验证：内核外 yt-dlp 适配器复用原有有界 CLI，纯 map 保留原始格式、明确分轨配对、DRM/分段与请求上下文事实；147 项适配器/录制/pipeline 定向测试通过，完整 Django contracts 149 项通过（本机 Nginx 缺失的 2 项跳过），原 yt-dlp Runtime 兼容回归 10 项通过，check/迁移漂移/样本脱敏/diff 检查通过；真实 PeerTube 完整 info dict 保留 10 格式（5 HTTPS、5 HLS），未知 codec 不因派生 audio_ext=none 误判静音；重复资源限制、根选中资源凭据隔离及泛化 unavailable 错误分类已完成红绿反例 | 剩余：准确 Head 的远端门禁；YouTube 唯一一次元数据取数被登录/风控拒绝，分轨仅有明确标注的合成测试，不冒充真实成功；Worker raw 传递及影子接入仍归 P12 | 不阻塞：真实 Provider 调用共 2 次（YouTube、PeerTube 各 1 次），无完整媒体保存，禁用格式试下载，内部 HTTP/清单字节未完整观测；未接旧路径，未改契约/内核/Worker、配置、8000 或生产
