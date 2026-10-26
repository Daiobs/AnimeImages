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

[2026-10-26 01:54 UTC] M2/P3 #139 合并 | 已验证：Head 3a124054 六项必需检查全绿，protected-files 实际 L1，Ready 后复核通过且无 Review 阻塞；正常 Squash Merge 为 ae021cc7，父节点 b8552ad6，树 b0353e8b 与 PR Head 完全一致；实际 main Django CI 40255725532、Product Contracts CI 40255725607 成功，Django 3682 项（5 项既有跳过）、Worker 31 项、Node/check/迁移漂移及目标/前代 Runtime 兼容均通过；合并记录见 #139 评论 6406484071 | 剩余：按清单从此 main 进入 P4，YouTube 真实成功与 Worker raw 捕获的边界保持原说明 | 不阻塞：无用户可见行为变化，8000 仍为 834d331、PID 97825，未部署或修改配置；本条随下一项 PR 提交，不单开日志 PR

[2026-10-26 02:21 UTC] M2/P4 D-03 请求方法与正文缺口 | 已验证：纯 map 初稿复用两份完整历史 Itaku extractor 录制（2026-10-13，未截断、无未导出类型，SOURCE_ID_MATCH），不新增真实请求；完整 Django contracts 与适配器组合 269 项通过（本机无 Nginx 的 2 项跳过），原 gallery-dl/第三方回归 116 项、Node 契约 17 项及 check/迁移漂移通过，但不代表 P4 已验收；独立复核后用已安装 gallery-dl 1.32.4 的 UploadirFileExtractor.items()、仅替换最终 HTTP 返回离线确认：原生 Url 消息要求 POST，_http_data 含 authenticity_token/upload_id，当前 Variant 无法表达 method/body，初稿映射 validate_work=() 却被内核判为 browser_direct；这是契约事实缺口，不是本轮生产故障 | 剩余：请 Human 决定 D-03 的表达/拒绝规则，不伪造 required_headers、drm、segmented 或 browser_fetchable 绕过；另有四项适配器本地待修正（同 num 的视频与预览、明确 ytdl HLS、跨 album 身份、原生带引号的 HTTP 错误），不把初稿测试绿灯作为完整通过；Worker raw 捕获仍归 P12 | 阻塞：按 D 类暂停，P4 仅在 codex/pipeline-gallery-dl-adapter 保留未提交初稿和日志，未开 PR/推送；契约、内核、已有 contracts/pipeline、旧路径及 Worker 未改，真实 Provider/媒体请求为 0，8000 仍为 834d331、PID 97825，生产未操作

[2026-10-25 19:51 PDT] M2/P4 Human D-03/D-04 与 P7 范围决定 | 已验证：#140 已合并，最新 main=593939fc，已快进且保留 P4 初稿；契约第 9 节要求 request_method 按上游事实填写，非 GET 由第 0 条拒绝、原因 UNSUPPORTED_REQUEST，请求体只用于判断、值不进入结果，不丢资源、不新增 POST 交付；无 merge_with 的 video_only 不提供下载、原因 NEEDS_AUDIO_MERGE，不自行配音轨；#140 已同步 P3 的 request_method 默认来源与逐字段测试 | Human 修正 P7：videodl 纳入本步，与 ParseHub 同 PR，作为外部上游调用而非并入代码；map 输入为 Worker 观察点的 VideoInfo，先用既有 5 份缩减录制，不新增真实请求；分片/ffmpeg/N_m3u8DL-RE 标记按 D-02 写 segmented、不服务端处理；实际 backend 记入 field_sources，由后台显式选择，不隐藏回退；生产开放不影响适配器，影子只统计实际调用，先前“不纳入”的日志仅保留为历史 | 不再阻塞：继续 P4 并修正上述四项映射问题；不改契约、内核、已有 contracts/pipeline、旧路径、Worker 或配置；本条与上次未提交进度一并提交，未请求上游或操作生产

[2026-10-26 03:04 UTC] M2/P4 本地验收 | 已验证：gallery-dl 完整原生 Directory/Url 消息纯映射、2 份既有真实 Itaku 录制和 5 份明确标注的合成录制；同 num 视频/预览、明确 ytdl HLS、跨 album 身份及原生带引号 HttpError 四项反例修复前分别误拒绝/误接收/误分类，修复后正确分组、保留 SEGMENTED_STREAM、拒绝错配及映射 HTTP_404；D-03 POST 保留但不交付、正文值不进入 Work，GET 默认来源准确，D-04 不虚构音轨配对；P4 共 57 项测试，完整 contracts 与全部适配器组合 289 项通过（本机无 Nginx 的 2 项既有跳过），Node 契约 17 项、check、迁移漂移、7 份样本脱敏检查及 diff 检查通过；已安装 Uploadir 提取器的离线最终网络边界复现也确认 POST 被拒绝、无正文泄漏 | 剩余：准确 Head 的远端门禁；Worker raw 捕获/共享 Session 上下文仍归 P12，不把 CLI 过滤结果冒充完整原始输入，不宣称新平台真实验收 | 不阻塞：真实 Provider/媒体请求为 0，未改契约/内核/已有 contracts/pipeline、旧路径、Worker、配置或生产；日常 8000 仍为 834d331、PID 97825

[2026-10-26 03:29 UTC] M2/P4 #141 合并 | 已验证：Head 6839527b 六项必需检查全绿，protected-files 实际 L1（13 路径），Ready 后复核通过且无 Review 阻塞；正常 Squash Merge 为 b0bf4a95，父节点 593939fc，树 31f9de91 与验收 Head 完全一致；准确 main 的 Django CI 40263394941、Product Contracts CI 40263394995 均成功，Django 3745 项（5 项既有跳过）、Worker 31 项、Node/check/迁移漂移及 Runtime 兼容通过，远端 HTTP 契约 155 项、前端契约 17 项全过；独立限定范围复核无阻塞 | 剩余：下一项为 P5，尚未开始；P12 raw/Session 边界和无新增实时 gallery-dl 验收的范围保持，本条随下一项提交，不单开日志 PR | 不阻塞：未加 Human 标签，未修改契约/内核/Worker/旧路径、8000、配置或生产，真实 Provider/媒体请求为 0

[2026-10-26 04:14 UTC] M2/P5 当前边界与本地初稿 | 已验证：#142 已合并，基线 main=7974db45；常规请求头按 INV-03 不作为 required_headers，复用既有平台主机后缀清单，无清单才采用精确主机并注明来源；fetch 复用天天 client 的 observe 与既有调用，旧归一化缓存命中无 raw 返回 TIANTIAN_NO_RAW、不补请求；map 不接收旧结果/候选/令牌，unknown 轨道、明确 P 标签高度、共享文字拆分及业务码 1001/未知码 unavailable 已覆盖；4 份历史完整 raw、2 份本轮完整 raw 和 4 份明确合成输入已脱敏，P5 36 项及适配器基础/录制/pipeline 合计 151 项定向测试通过，包含空媒体异常与非对象 JSON 分类的红绿反例 | 剩余：实况配对事实缺口、完整复核及准确 Head CI；不能把本地初稿当作 P5 已完成，未开 PR、提交或推送 | 阻塞：配对规则待 Human/协调人决定；未修改契约、内核、已有 contracts/pipeline、旧路径、Worker、配置或生产，8000 仍为 834d331、PID 97825

[2026-10-26 04:14 UTC] M2/P5 天天小红书真实结构核验与暂停 | 已验证：Human 提供两个现成公开链接，第二次为额外明确授权；原本地配置及现有 client 各请求一次，均 HTTP 200/code 200，无重定向，分别返回 9 图片+9 视频及 6 图片+6 视频；data 只有 desc/images/videos，视频各有 url 与空 streams，无实况类型、配对 ID 或同项关联字段，不能用等长数组/位置推断 groups；脱敏录制保留全部 18/12 个独立内容项，groups 为空，不冒充配对验收；现有本地 Live Photo 源于 Native 同 imageList 项的 livePhoto/图片/stream 事实，结合 ParseHub 图片增强，不是天天配对规则，统一适配归 P10 | 剩余：请确认上游可证明的配对语义，或由 Human 明确允许 P5 先保留未配对资源并记录能力缺口；不借用 Native 对象、旧资产或令牌补 raw，不自行调整本轮 groups 要求 | 阻塞：按需求不确定暂停；本轮新增 Provider 调用 2、可观察 HTTP 2、媒体请求/播放/保存 0，上游内部流量未观测；未操作生产和日常 8000，源链接令牌仅留受限私有记录，仓库已脱敏

[2026-10-25 PDT] M2/P5 Human 实况边界确认 | 决定：P5 完整保留天天全部图片和视频，没有逐项关联事实就不生成 groups，数量相等不算配对事实；真实混合笔记可含 12 条实况和 5 张普通图，按位置配对会出错。Human 说明天天以封面图加一段小视频表现实况，此信息写入适配器注释，仅作以后配对规则背景；现有 Native 同项快照才可建立配对。P10 按 imageList 同一项的 livePhoto、图片和视频流建立 groups；Native 配对与天天媒体合成属于第 3 步 enrichment，影子遇到旧合成结果记 OLD_RESULT_MERGED | 原画规则：天天明确标签“原画”写 is_original=true，按内核成为可下载默认变体，不据此设 height；540p/1080p 等明确 P 标签继续用 height_from_label | 已解除配对决策阻塞：按此边界收口 P5，CI 全绿后合并，再继续 P6；不改计划正文、契约、内核、旧路径、配置、8000 或生产，本次不新增真实请求

[2026-10-25 21:28 PDT] M2/P5 本地收口 | 已验证：原生 JSON 纯映射及 observe fetch，6 份真实录制与 4 份合成录制保留全部格式；原画 is_original=true、height 未补造，两份 XHS 录制保留 18/12 内容项且 groups 为空；业务码 1001/未知码 unavailable，旧归一化缓存无 raw 不补请求；独立复核发现同址别名限制被覆盖和纯音频解包遗漏，红绿反例关闭，不改旧路径。修正前完整 HTTP contracts 与适配器组合 325 项通过（本机无 Nginx 的 2 项跳过）；修正后 P5/基础 55 项和 pipeline/录制/P5 138 项通过，P5 自身 39 项；Node 契约 17 项、系统检查、迁移漂移、10 份样本脱敏及 diff 检查通过 | 剩余：准确 Head 远端必需门禁；真实实况配对未声称通过，历史微博双井号话题按共享 helper 保留其已记录边界，raw 跨进程/影子接入不在 P5 | 不阻塞：本次续作新增 Provider/媒体请求 0，先前已授权 2 次真实 XHS 请求证据保留；8000/配置/生产不变

[2026-10-25 21:39 PDT] M2/P5 #143 合并 | 已验证：Head 4f6507e2 六项必需检查通过，protected-files 实际 L1（16 路径）；Ready 后重新核验门禁，无未解决 Review，正常 Squash Merge 为 c7665771，父节点 7974db45，树 39dd92bf 与验收 Head 完全一致；本地工作树干净后从此 main 建立 P6 分支 | 剩余：实际 main CI 40270179751/40270179776 运行中，将在下一检查点更新；真实实况逐项配对仍无事实，不冒充已验证 | 不阻塞：8000 保持 834d331、PID 97825，未修改旧路径、配置或生产，未加 Human 标签

[2026-10-25 21:42 PDT] M2/P6 Human sidecar 原始边界确认 | 决定：P6 实现原始 JSON 与未转换 sidecar envelope 的 map、录制测试及通用 helper fetch；run_kukutool_url_helper 在进程内返回解密业务对象，可作 raw。抖音 sidecar 在 run_kukutool_sidecar 中取得 JSON 后立即转换/过滤，P6 不消费转换结果、不改旧函数、不另发请求，转换前捕获钩子及 sidecar fetch 归 P12（L2） | 样本：真实录制与受控构造分开；现有 capture_upstream 的 kukutool_generic 分支只调用通用 helper，不能录到 sidecar envelope，故 sidecar 使用明确标注的受控构造，不新增真实请求。unknown 轨道、height_from_label、常规请求头非必须及现有 host_policy 清单继续遵守 | 不阻塞：按 Human 确认继续 P6，未改契约、内核、已有 contracts/pipeline、旧路径、Worker、后台或生产配置

[2026-10-25 21:50 PDT] M2/P5 合并后门禁完成 | 已验证：main c7665771 的 Django 40270179751（test、ytdlp-python312、nginx-baseline）及 Product Contracts 40270179776 全部成功；未绕过门禁，未改标签或仓库设置 | 不阻塞：P5 已完成，P6 按已确认原始数据边界继续；没有生产发布或新增上游请求

[2026-10-25 21:54 PDT] M2/P6 本地收口 | 已验证：通用解密 JSON 与未转换 sidecar envelope 的纯 map、通用旧 helper fetch/parse；2 份完整历史录制（AcFun 七档、微博九图）与 3 份明确受控构造（sidecar 两份、音频一份），逐字段、来源、默认项、不可变及无 I/O 检查通过。共用纯事实 helper 从 P5 原样提取；红绿反例关闭图片位置去重、同址标签丢失已知轨道和顶层 video_fullinfo 漏项；原生错误分类、缓存及 single-flight 不额外重试。最终 HTTP/pipeline 契约加全部适配器 376 项通过（本机无 Nginx 的 2 项跳过），P6 自身 48 项；Node 契约 17 项、check、迁移漂移、语法、15 份 P5/P6 录制脱敏及 diff 检查通过。一次测试命令曾写错 gallerydl 模块名，已更正并完整重跑，不算产品失败 | 剩余：准确 Head 的远端必要门禁；sidecar 仅 map 与受控构造证据，未声明真实抖音全字段或交付验收，原始捕获及 fetch 留 P12；没有实况逐项事实就不生成 groups | 不阻塞：P6 新增真实 Provider/媒体请求 0，不改旧路径、契约、内核、已有 contracts、Worker、8000、配置或生产

[2026-10-25 22:18 PDT] M2/P6 #144 合并完成 | 已验证：Head ecf18de417ee26c00fcbfe5744f1a99f2cd7b8d8 六项必需检查全绿，protected-files 实际 L1（13 路径）；Ready 后最新门禁通过、无未解决 Review，正常 Squash Merge 为 d88a65f9dffbb3714d5d564b1307396606a91096，父节点 c7665771，树 3f7837dd49c6410351654bf5e93c7c931a625c5c 与验收 Head 完全一致。PR CI 的 Django 3832 项（5 项条件跳过）、Worker 31 项及前端检查成功；契约 CI 的 155 项、Node 17 项及合并保护回放 19 项成功。合并后 main Django 40272598873 和 Product Contracts 40272598888 均成功 | 保留边界：sidecar 原始捕获和 fetch 留 P12，受控 envelope 不是真实抖音全字段证据；未新增真实 Provider/媒体请求，未接入用户路径或影子、未部署生产 | 不阻塞：开发目录 detached 在最终 main，8000 仍为 834d331/PID 97825；本条合并后日志留待下一计划 PR 一并提交，不新开日志 PR，不自动开始 P7

[2026-10-26 05:46 UTC] M2/P5-P6 A 类请求上下文修复 | Human/协调人确认：#145 已合并为 9bdc470，显式 required_headers 保留 Referer/Origin 等常规头；仅携带普通头的值不算必须。IP 绑定、requires_context 或 headers_present 缺少值写 browser_fetchable=false，缺少事实仍 unknown；沿用已有同资源上下文，不接入旧路径。新增反例在未修 main 上有 4 个失败测试、20 个失败子场景，修正后定向 41 项及全部适配器/HTTP/pipeline 契约 382 项通过（本机无 Nginx 的 2 项跳过），系统检查、迁移漂移及 diff 检查通过 | 剩余：独立 L1 A 类 PR 准确 Head 门禁，合并后再进入 P7；P7 三个 Worker 来源只做 RawMapper/录制，fetch/捕获留 P12，videodl backend 明示、分片/解密如实保留 | 不阻塞：未改契约、内核、已有 contracts、Worker、旧路径或配置；真实 Provider/媒体请求 0，日常 8000 和生产不变；P6 合并后日志本次一并提交

[2026-10-26 05:47 UTC] A 类复核补充 | 独立复核发现 headers_present=true 且头名称存在、值为 null/空串时仍为 unknown；8 个子场景红验证后修正为 false。最终相关 P5/P6、请求上下文与 pipeline 共 169 项通过；不影响默认无上下文的 unknown，也不输出任何头值 | 剩余：准确 Head CI | 不阻塞：无新增请求或旧路径改动

[2026-10-26 00:00 PDT] M2/A 类 #146 收口补记 | 已验证：Head b75a924 六项必需检查通过，protected-files 实际 L1；正常 Squash Merge 为 3f9a395，父节点 9bdc470，树 bc1acd91aa9e3e573ef583ef502bc3dae80b1819 与 PR 一致。main Django 40277300935 与 Product Contracts 40277300950 均成功；协调人已审查通过 | 剩余：继续 P7；本条合并后日志此前未提交，本次一并提交 | 不阻塞：未改旧路径、日常 8000、配置或生产

[2026-10-26 00:00 PDT] M2/P7 Markdown 依赖边界解除 | Human 批准单独 L2 依赖 PR #147，唯一变更为 requirements.txt 的 Markdown==3.10.3，与 ParseHub 隔离 Runtime 实装版本一致；准确 Head a4f2c2f 的 Python 3.12 CI 通过，Human 已批准并合并为 152ea56；实际 main Django 40280061960、Product Contracts 40280061959、Deploy Acceptance 40280061970 均成功 | Human 要求：P7 不改依赖，map 与 explicit_richtext_media 一致，只提取正文 img、保持顺序，跳过 # 占位和 data:image，按 canonical 解析相对地址、去重、沿用大小上限；同一真实录制能运行 Worker 函数时须对照，无扩展，不保存或输出生成 HTML | 不再阻塞：保留的 P7 初稿已基于最新 main 继续，测试依赖只装于独立本地目录，未改日常 Runtime；先前未提交进度随 P7 一并提交

[2026-10-26 00:00 PDT] M2/P7-P10 执行边界确认 | Human 要求：P7 三个 Worker 来源只交付 RawMapper 与录制测试，fetch/捕获归 P12；ParseHub 委托 dl.info_json 复用 P3，XHS groups 仅同 imageList 项的 livePhoto/图片/视频事实；videodl 复用五份缩减录制，分片标 segmented、解密标 drm、实际 backend 记入 field_sources，不隐藏回退。P7 合并后按序 P8/P9/P10；旧 Native 函数无观察点时仍只做 map/录制，fetch 归 P12；请求头遵守 #145，分轨如实记录且仅明确配对才 merge_with，抖音原声独立音频且不默认，小红书实况同项配对 | 剩余：P7 初稿修正与本地验收；未声称 P7-P10 已完成 | 不阻塞：预期 L1 实际以 protected-files 为准，L1 全绿可合并，P11 L2 开出后暂停；清单外工作、契约缺口或依赖/配置变化仍暂停请示

[2026-10-26 07:35 UTC] M2/P7 本地收口 | 已验证：ParseHub 通用/XHS 图片、videodl 三个 RawMapper；ParseHub 委托复用 P3，4 份完整历史录制按声明脱敏后与归档逐字段一致，两份真实 Markdown 录制与固定 Worker 离线对照分别保留 4/30 张图片，大小上限、顺序、占位过滤、相对地址与去重一致。videodl 复用 5 份缩减录制，按原生 VideoInfo 同对象音视频语义配对，真实 backend 由执行方提供，分片/解密及显式上下文保留。红绿关闭同址别名缺失上下文被放宽、最早期限丢失及同对象音轨遗漏；独立复审无剩余明确阻塞。最终 HTTP/pipeline 契约与全部适配器 480 项通过（本机缺少 Nginx 的 2 项跳过），P7 自身 98 项；Node 契约 17 项、check、迁移漂移、录制脱敏和 diff 检查通过 | 剩余：准确 Head 的远端门禁；XHS 同项配对及 ParseHub 内嵌 yt-dlp 使用受控构造，videodl 缩减录制不冒充完整真实录制；fetch/捕获仍归 P12 | 不阻塞：本次新增 Provider/媒体请求 0，不改依赖、旧路径、契约、内核、已有 contracts、Worker、配置、8000 或生产

[2026-10-26 07:52 UTC] M2/P7 #148 合并 | 已验证：Head 45967dd5 六项必需检查全绿，protected-files 实际 L1（17 路径）；Ready 后最新门禁通过，无未解决 Review，正常 Squash Merge 为 70aa6632，父节点 152ea562，树 799587f3942a2fa70330bff9fdab17049e9b795d 与 PR 完全一致。PR 的 Django 3936 项（5 项条件跳过）、Worker 31 项、契约及前端、Python 3.12/旧 Runtime 兼容检查通过 | 剩余：合并后 main Django 40288463919 与 Product Contracts 40288463863 运行中；下一项 P8 从此 main 建分支，本条随 P8 提交 | 不阻塞：没有新增真实请求、Human 标签或设置改动；8000、日常配置和生产保持不变

[2026-10-26 08:03 UTC] M2/P8 D-05 连续分段表达缺口 | 已验证：Native 普通视频在 api_get_json/_parse_url 内取得 view/playurl，尚无观察点，fetch/捕获归 P12；初始盘点把动态研究映射误写为生产可达，已按现有代码勘误，不新增动态能力。明确受控的两个 durl（order=1/2）经旧 get_play_items 判为 segmented_file；每条是连续片段，不是独立作品或可替换清晰度。当前 Variant 只有单 URL，逐条 Item/Variant 会改变语义，只留第一段会丢失事实，也没有约定的连续片段 Group。其它适配器保留的是格式自身的单 URL，不能据此授权用第一段代替整体 | 剩余：请 Human 决定修订契约表达完整分段资源，或明确本步对 multi-durl 的不支持范围与失败语义；不自行返回 unsupported/invalid_result 绕过 D-02。全未配对 DASH 则已有 1.4 第1/4条及末段明确规则，按 INVALID_RESULT 处理，不作为本次暂停原因。限定搜索未找到完整 Native view/playurl 历史录制；受控样本可作 map 测试但不称真实验收，已有天天/ParseHub 录制不能冒充 Native | 阻塞：按 D 类暂停在 P8，未创建 P8 产品代码或测试、未开 PR/推送，P9-P11 未开始；两份文档改动保留待续作，未新增真实 Provider/媒体请求，未改内核/契约/旧路径/Worker/依赖/配置，8000 和生产不变

[2026-10-26 08:04 UTC] M2/P7 合并后门禁完成 | 已验证：main 70aa6632 的 Django 40288463919（test、nginx-baseline、ytdlp-python312）及 Product Contracts 40288463863 均成功；合并树与验收 Head 相同，未绕过门禁 | 剩余：仅 P8 D-05 待 Human 决定，本条和 P8 盘点勘误保留在 codex/pipeline-bilibili-native-adapter，待获准续作时提交，不另开清单外日志 PR | 阻塞：P8 暂停；8000 仍为 834d331/PID 97825、工作树干净，未发布生产或新增上游请求

[2026-10-26 01:17 PDT] M2/P8 Human D-05 范围决定 | 决定：沿用旧 BilibiliIncompleteMediaError 与 INV-05，不修订契约。Native 播放数据没有完整文件时返回 Failure(UNSUPPORTED)：多段 durl 为 BILIBILI_SEGMENTED_DURL；单段分片地址为 BILIBILI_SEGMENTED_PATH；只有未配对分轨为 BILIBILI_SEPARATE_TRACKS_ONLY。作品形态不作为 Provider 故障，不用 unavailable/冷却，也不用 invalid_result；保留完整原始分段录制，不生成 Work，不拼接、不只留第一段、不把分段当清晰度。有完整 durl 和 DASH 时正常映射，完整文件默认，未配对轨道按 D-04 保留但不提供下载 | 后续（第 3 步/P14 报告）：其他平台没有任何可下载默认变体时，链路是否继续下一个 Provider？当前按契约可形成合法结果的此类情况算成功并停止，应由 Human 在第 3 步决定，本步不改路由。全未配对视频导致违反默认项不变式的情况与合法但不可下载结果应区分 | 不再阻塞：P7 #148 协调人审查通过；继续 P8/P9/P10，P11 开 L2 PR 后暂停；之前未提交日志与勘误随 P8 一并提交，不改旧路径、契约、内核、Worker、依赖、配置、8000 或生产

[2026-10-26 08:41 UTC] M2/P8 本地收口 | 已验证：普通视频原始 view/playurl 纯映射，3 份明确受控 API 夹具保留完整结构；25 项字段/身份/默认项/失败测试及 6 项真实 ChainRun + Native/TianTian mapper 组合测试通过。多段、分片地址和只有未配对分轨均 unsupported、无 Work、不冷却；完整 durl + DASH 保留完整默认及分轨，不猜配对。最终 HTTP/pipeline 契约与全部适配器 511 项通过（本机缺少 Nginx 的 2 项跳过），Node 17 项、系统检查、迁移漂移、脱敏检查和 diff 检查通过 | 剩余：准确 Head 的远端门禁；未取得完整 Native 真实录制，不把受控夹具称为真实上游验收，原始捕获与 fetch 留 P12 | 不阻塞：本次新增 Provider/媒体请求 0；未改契约、内核、已有 contracts、旧解析路径、Worker、依赖、配置、日常 8000 或生产；之前未提交日志和入口勘误一起提交

[2026-10-26 08:55 UTC] M2/P8 #149 合并 | 已验证：Head 71b9f148 六项必需检查全绿，protected-files 实际 L1（9 路径）；Ready 后最新门禁通过、无未解决 Review，正常 Squash Merge 为 549ca508，父节点 70aa6632，树 c325c75a22c1a9cdc0ed2d7ef6e311c141991f72 与 PR 一致。D05 三类 unsupported 及完整文件默认规则已完成，不修改契约或原解析行为 | 剩余：main Django 40295629422、Product Contracts 40295629555 运行中；P9 从此 main 继续，本条随 P9 提交 | 不阻塞：真实 Provider/媒体请求 0，未改标签、仓库设置、8000、配置或生产

[2026-10-26 09:08 UTC] M2/P8 合并后门禁完成 | 已验证：main 549ca508 的 Django 40295629422、Product Contracts 40295629555 全部成功；PR CI 为 Django 3967 项（5 项条件跳过）、Worker 31 项及既有前端检查通过。同 SHA 的定时 Deploy Acceptance 40296091651 也成功，属于隔离用户发布工具测试，不是生产发布 | 剩余：P9 按原始 aweme 边界推进 | 不阻塞：8000 与生产未变，未新增上游或媒体请求

[2026-10-26 09:18 UTC] M2/P9 本地收口 | 已验证：原始 aweme/envelope 的纯 map，图集与视频各格式保留，原声独立音频、无证据不配轨；同址不同格式保留自身事实，URL 作用域限制不扩散，不改写 CDN 地址或补造尺寸。独立复审关闭 URL 绑定音轨误分类及音乐时长冒充视频时长两项反例。P9 43 项、最终 HTTP/pipeline 契约与全部适配器 554 项通过（本机无 Nginx 的 2 项跳过），两份受控样本脱敏及 diff 检查通过；没有剩余确定性复审阻塞 | 剩余：准确 Head 远端门禁；两份夹具是 synthetic，不冒充真实 Native 全字段录制，原始捕获/fetch 归 P12；原声不是视频默认，不声称当前模型已有 Work 主选项 | 不阻塞：P8 合并后日志一并提交；新增真实 Provider/媒体请求 0，不改旧路径、契约、内核、已有 contracts、Worker、依赖、配置、8000 或生产

[2026-10-26 09:30 UTC] M2/P9 #150 合并 | 已验证：Head 00cf0a60 六项必需检查全绿，protected-files 实际 L1（8 路径）；Ready 后复核无未解决 Review，PR 描述更新触发的新检查完成后正常 Squash Merge 为 9c150103，父节点 549ca508，树 a932826c74e02410f86e502b549d28e65aad39ce 与 PR 完全一致。未使用管理员绕过或自动合并 | 剩余：准确 main 的 Django 40299991338 和 Product Contracts 40299991045 运行中；P10 从此 main 开始，本条随 P10 提交 | 不阻塞：未新增真实请求、审批标签或设置变更，未修改 8000、配置或生产

[2026-10-26 09:44 UTC] M2/P9 合并后门禁完成 | 已验证：main 9c150103 的 Django 40299991338、Product Contracts 40299991045 全部成功 | 剩余：P10 原始页面映射与离线回归推进中，未声称实时平台验收 | 不阻塞：8000、配置、旧路径和生产未变，新增真实请求 0

[2026-10-26 09:59 UTC] M2/P10 本地收口 | 已验证：XiaohongshuMapper 只读原始 HTML/page state，当前身份一致性、图文/视频/同项实况 groups、正文话题和 URL 作用域限制通过；独立复审两项反例（路径/查询身份冲突、Native 码率/编码/毫秒时长丢失）修复前失败、修复后通过。36 项定向、590 项契约及全部适配器组合通过（本机无 Nginx 的 2 项跳过），迁移漂移、三份样本脱敏及 diff 检查通过 | 剩余：准确 Head 远端门禁；三份 fixture 均为 synthetic，真实 Native 捕获/fetch 留 P12；不凭裸 originVideoKey 拼接或探测 URL | 不阻塞：没有修改契约、内核、已有 contracts、旧解析/下载、依赖或配置；真实 Provider/媒体请求 0，8000 与生产未变
