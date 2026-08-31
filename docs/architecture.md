# Architecture

本文描述 documented application baseline `457139b98d67d6e5f79b099f9a4e91d60a26904f` 的稳定结构。该 SHA 是文档快照，不是永久的 `main` 指针；当前 development baseline 必须从 latest fetched `origin/main` 解析。它也不是生产部署证明；last recorded Production Runtime 仍为 `38992e955f2f268c2a6d4ecd81d900feba83f048`。

## 系统定位

AnimeImages 是一个 Django monolith。正式请求在固定平台路线中完成识别、Provider 调用、候选准入、结果归一化、预览和下载授权。

Provider Lab 也是 monolith 内的模块，但它可以启动隔离的 external repository Runtime。它不是微服务、自动 Broker 或正式路由层。

```mermaid
flowchart LR
    U[Browser] --> D[Django monolith]
    D --> R[Platform Registry]
    R --> X[XHS fixed route]
    R --> Y[Douyin fixed route]
    R --> B[Bilibili Native]
    R --> G[Optional generic providers]
    D --> L[Staff Provider Lab]
    L --> W[Isolated repository workers]
```

## Apps 与页面

主要 Django apps：

- `xhs_download`：平台解析、Provider 策略、预览、Batch、下载授权、Staff Lab 和运维页面。
- `search_app`：本地图片库搜索和展示。
- `submissions`：保留历史数据模型与 migration，但旧 Public Submissions 产品页面已退休。

当前用户反馈使用 `xhs_download.UserFeedback` 与 `/feedback/`，不等同于历史 `submissions` 模型。

主要路由：

| 路径 | 所有者 | 说明 |
| --- | --- | --- |
| `/` | `xhs_download` | 通用单条解析 |
| `/xhs/` | `xhs_download` | 小红书专用入口 |
| `/tiktok/` | `xhs_download` | 抖音专用入口 |
| `/bilibili/` | `xhs_download` | Bilibili 视频入口 |
| `/batch/` | `xhs_download` | 有界渐进 Batch |
| `/library/` | `search_app` | 本地图片库 |
| `/feedback/` | `xhs_download` | 轻量问题反馈 |
| `/admin/` | Django / `xhs_download` | Staff 控制面 |
| `/admin/provider-lab/` | `xhs_download` | Staff-only Provider Lab |

旧 `/submissions/submit/` 和 `/submissions/list` 不再是公开产品入口。历史模型保留不代表路由仍公开。

## 请求主流程

```text
HTTP input
  -> URL/text normalization
  -> platform registry
  -> fixed adapter/provider route
  -> candidate admission
  -> ParseResult / MediaAsset normalization
  -> display context
  -> browser or server-generated output contract
```

职责边界：

- Views：HTTP 输入输出、身份与 CSRF、模板或 JSON response。
- Platform adapters：Source identity、平台 payload、基础媒体语义。
- Provider strategy：固定顺序、预算、fallback、单飞和短期缓存。
- Candidate admission：scheme、host、redirect、type、magic、identity 和 relationship。
- Result builders：平台结果归一化为 `ParseResult`、`MediaAsset` 或专用 Rich Media view model。
- Download governance：签发受约束 descriptor/manifest/token；不接受任意客户端 URL。
- Browser controllers：普通媒体传输、归档、进度和保存交付。
- Management commands：诊断与有界离线检查，不进入普通页面请求。

## Platform Registry

正式 Registry 包含专用 adapters：

- Xiaohongshu
- Douyin
- Bilibili
- Generic third-party

专用平台优先使用经过实证的固定路线。Generic provider 只扩展受控长尾能力，不覆盖专用 adapter，也不代表所有上游宣称的平台都已稳定支持。

Provider Lab 使用独立的固定 Lab Registry。Lab Provider 不会被正式 Platform Registry 自动发现或调用。

## Xiaohongshu

### Source Authority

XHS Native 负责：

- 短链解析后的目标 note identity；
- exact note payload ownership；
- 标题、作者、封面和文件名；
- image item 数量与源顺序；
- Live Photo same-item relationship；
- terminal / invalid note fail-closed。

推荐内容、其它 note payload、generic carousel 和 broad page scan 不能覆盖 current-note authority。

### 普通视频路线

```mermaid
flowchart TD
    I[XHS input] --> N[Native exact identity and metadata]
    N --> U{Ultra enabled?}
    U -->|No| V[Native video]
    U -->|Yes| T[TianTian ordinary-video candidate]
    T -->|admitted| H[High-quality video result]
    T -->|failed or rejected| V
```

图片和 Live Photo 分支不会调用 TianTian。TianTian 只替换普通视频媒体候选，不替代 Native note identity。

### 图片与图集路线

```mermaid
flowchart TD
    N[Native identity and source order] --> P[ParseHub raw original candidate]
    P -->|same-item and admitted| O[Original download asset]
    P -->|missing or rejected| F[Native exact same-item fallback]
    O --> Q[Shared static preview]
    F --> Q
```

ParseHub XHS Runtime 是正式 XHS 图片路线的一个可选供应方。它与 ParseHub Provider Lab Worker 不是同一个开关、进程入口或产品用途。

图片候选必须通过 exact-note/item correspondence、HTTPS host/final-host、文件头和尺寸等门禁。原图支持 JPEG、PNG、GIF、WebP、AVIF 和 HEIC；预览根据浏览器展示边界生成或选择，不改变 original asset authority。

### Live Photo

Live Photo relationship 只接受同一 item 内的明确 still、motion 和 marker/owner 证据，不使用数组位置、URL 相似性或 `firstNoteId` 猜测。

Active Staff Single 数据流：

```text
Native exact-note snapshot
  -> Live Photo relationship mapper
  -> admitted still + motion Asset Specs
  -> Staff-only result view
  -> click-to-load one-shot motion
  -> ended restore to still
```

下载数据流：

```text
selected ordinary stills and/or Live Photo pairs
  -> browser fetches admitted still/motion bytes
  -> browser archive with stable order and extensions
  -> disk
```

边界：

- Anonymous Single：flat static images，无 motion URL、badge 或 controller。
- Active Staff Single：badge、one-shot playback、still + motion archive。
- Batch：flat static images，无 motion item、pair token 或 pair archive。
- Provider Health：不启用 Staff Live Photo pipeline。

## Douyin

Douyin Normal 使用 Native baseline。Source Lifecycle 将短链、标准链接、modal 链接和分享文本归一为 canonical content identity。

Single Ultra 固定路线：

```text
TianTian preferred
  -> KuKuTool fallback / race reserve
  -> Native fallback
```

Batch Ultra 固定路线：

```text
TianTian
  -> Native fallback
```

两条路线共享安全候选和下载契约，但预算、Provider 顺序与渐进协议各自固定。不要从 Single 配置推断 Batch 顺序。

Ultra Provider 工作有独立 foreground deadline、短期 payload/failure cache、single-flight 和请求级 attempt accounting。最终媒体 redirect 只接受 source-bound 的受审查 host policy。

## Bilibili

正式路线保持 Native video-only：

- 识别 BV、AV 和 `b23.tv`；
- 支持 `p=` part；
- 使用官方 View/PlayURL；
- 从官方 View 读取标题、UP 主名称和封面；
- 标题与 UP 主名称进入当前下载文件名语义，封面进入媒体结果；
- 独立结构化 `ParseResult.metadata.author` 尚未形成稳定契约；
- 优先完整 DURL 媒体；
- 在没有 DURL 时才保留受限 DASH video candidate，不承诺音视频 merge 升级。

Provider Lab 的三个真实样本显示 Native、TianTian、videodl 与官方 DURL 的完整媒体字节一致。ParseHub 只增加格式元数据信号，未产生可交付媒体增量。该证据支持保持 Native，而不是让 Lab 自动改写路线。

Dynamic、Opus、Live Photo、member-only quality 和 DASH merge 仍是独立后续范围。

## Generic Providers

Generic third-party adapter 通过显式 platform rules、Provider allowlist、runtime gate 和安全预算提供可选长尾能力。

当前主要工具：

- yt-dlp：通用视频 metadata/candidate，YouTube 可形成 server-required merge task。
- gallery-dl：通用图片/gallery metadata/candidate。
- KuKuTool generic：与 Douyin Ultra 的 KuKuTool capability 分离。

外部工具运行在有界子进程中，使用临时 HOME/CWD、输出上限和进程树清理。它们是 breadth tools，不是专用平台 adapter 的替代品。

Provider capability、public exposure、platform allowlist、rollout 和 safety limit 分层，见 [Provider Runtime Gates](provider-runtime-gates.md)。

## 下载架构

### Ordinary Video

```mermaid
flowchart LR
    C[Provider or CDN] --> S[Browser Service Worker]
    S --> M[Browser Download Manager]
    M --> K[Disk]
    A[AnimeImages control plane] -. descriptor and validation .-> S
```

普通视频的目标是 Browser Zero Relay：AnimeImages 不传输普通媒体 body，也不在 direct failure 后自动切入 Server Relay。

### Images And Archives

普通图片：

```text
Image CDN -> browser image fetch -> validated filename/format -> disk
```

多图：

```text
ordered image assets -> browser archive implementation -> ZIP -> disk
```

浏览器根据实际文件头保留扩展名，归档条目顺序与 source order 对齐。静态 preview 不得替代 original bytes。

### Server-Generated Outputs

服务器仍可处理天然需要生成新文件的任务，例如：

- YouTube video/audio merge；
- 显式 ZIP/merge compatibility output；
- 必须由服务器产生的新文件。

这些是不同资源类型，不是普通 Browser Direct 的自动 fallback。

## Preview 与 Resource Tokens

图片 preview proxy 只放行经过安全 raster signature 识别的 JPEG、PNG、GIF、WebP 和 AVIF；上游声明 MIME 不能作为唯一准入依据，SVG/HTML/JSON/XML/video/unknown binary fail closed。

正式下载 manifest 和 Provider Lab resource token 都是 opaque、短期且受范围约束，但用途不同：

- 正式 manifest/descriptor：授权当前产品下载动作。
- Lab resource token：Staff-only 的受限浏览器检查，不是下载 authority。

客户端提交的任意媒体 URL 或文件系统路径不能直接获得服务器下载能力。

## Batch

Batch 使用 progressive token 和有界 item 调度：

- 逐条显示 success、failed、timeout 或 skipped；
- 单条 timeout 不阻塞后续链接；
- retry 只替换目标卡片；
- response deadline 与页面预算一致；
- fail-safe response boundary 防止不协作任务无限占用请求线程；
- XHS Live Photo 在 Batch 中保持 flat static images。

Batch Runtime Setting 控制 access、并发和预算，不改变固定 Provider 顺序。

## Provider Lab

Provider Lab 的请求流：

```mermaid
flowchart TD
    B[Staff Browser] -->|max 3 concurrent POSTs| V[Access CSRF rate single-flight]
    V --> R[Fixed Lab Registry]
    R --> I[Existing wrappers]
    R --> W[Isolated whole-repository workers]
    I --> N[LabProviderResultV1]
    W --> N
    N --> S[Sanitized cards]
    N --> T[Short-lived Staff-bound tokens]
    T --> X[Browser resource inspection]
```

Whole-repository Workers：

- 使用 exact repository pin 和独立 venv；
- 一请求一进程；
- JSON stdin/stdout handshake；
- 临时 HOME/XDG/CWD；
- Cookie、proxy inheritance 和 Provider downloader 禁用；
- timeout、输出上限及进程树清理；
- 归一为 `LabProviderResultV1`；
- 不持久化结果。

Lab 不自动 fan out、不选赢家、不修改平台路线、不做完整媒体 Relay。操作和许可证边界见 [Provider Lab](provider-lab.md)。

videodl 是一个局部例外：浏览器仍然只对显式 Provider 并发，Django
只在单个 `videodl_repo` 请求内部执行 AnimeImages-owned、顺序且有界的
exact-backend orchestration。每个 backend attempt 使用独立子进程；
backend category 不作为共享或安全判定。与 exact pin 绑定的局部 policy
逐项审核 70 个 dedicated 和 37 个 common backend，并仅让 dedicated
`allowed` 项进入 `reviewed_auto`。Common 不存在直接 `allowed` 状态：21 个
exact-pin IDs 只有在 Staff 明确选择并确认 source sharing 后才能执行，16 个
具有额外风险的 IDs 以及未知 IDs 均阻止。共享确认不授权 browser、TLS
bypass、interactive/provider-owned downloader、embedded-cookie/credential
行为。inventory 的 registry presence 与 controlled eligibility 分开，完整
registry ID 集合必须与 policy 相等，并按 pin、policy identity 和 runtime
identity 有界缓存。
该局部 policy 和 attempt contract 不扩展为全局 Observation Kernel 或
Backend Catalog。见
[Asset 1/2 outcome](research/videodl/ASSET_1_2_IMPLEMENTATION_OUTCOME_V1.md)。

## Runtime Gate 分层

正式 Provider 使用：

1. Capability Gate：部署是否准备了能力和依赖。
2. Runtime Gate：`disabled/staff/public_gray/public`。
3. Platform Gate：Generic 平台/host allowlist 与 blocklist。
4. Safety Limits：timeout、频率、并发、输出字节。
5. Rollout Gate：`public_gray` 的稳定 bucket。

Provider Lab 使用另一套边界：

- `UPSTREAM_PROVIDER_LAB_ENABLED`；
- `VIDEODL_PROVIDER_LAB_ENABLED`；
- active Staff；
- CSRF；
- rate limit；
- single-flight。

Lab 没有 public rollout，也不能通过正式 Provider Settings 开放匿名访问。

## Static 与 Media

- `static/`：源 CSS、JavaScript 和应用静态资产。
- `staticfiles/`：`collectstatic` 生成物，不手工编辑。
- `media/`：本地上传/生成文件；生产交付由受控 Web 层或对象存储负责。
- Staff controller 必须进入共享 static asset version source，避免新模板配旧 JavaScript。

普通 Provider 媒体不会作为用户解析历史保存到 `media/`。

## Health 与观测

- Liveness：证明 Django 进程能够应答。
- Readiness：执行 cache/database round trip。
- Provider Health：依赖、配置和受控 Provider readiness。
- Daily Provider Validation：复用 Provider Health registry，以一个局部 Suite
  聚合顺序执行、single-flight、总预算、状态变化通知和 retention；它不是
  Provider score、planner、自动 route switch 或全局 Observation Kernel。
- Provider Audit Log：记录正式 Runtime Setting 变更。
- Structured logs：只包含固定状态、阶段、耗时和非敏感 host facts。

日志不得包含用户输入、Cookie、token、Provider body 或 signed media URL。

Asset 3 仅增加一个 Suite 模型并扩展现有 Setting/Run。daily producer 是固定
management command，consumer 是同一 Admin 报告和聚合邮件。videodl 每日
边界只包含 exact-pin inventory 与一个显式 allowed Dedicated backend；Common、
legacy/reviewed auto、grabber、browser 和正式平台路线均不在该能力中。

## 开发与生产

开发仓库和运行中的生产 checkout 是两个事实源：

```text
current development baseline = latest fetched origin/main
documented application baseline = 457139b98d67d6e5f79b099f9a4e91d60a26904f
recorded production runtime = 38992e955f2f268c2a6d4ecd81d900feba83f048
```

PR #74、#75 和 #76 已合并 development main，但本次文档任务没有访问生产，也没有记录它们已部署。部署需要独立授权、exact-SHA gate、迁移/static/infrastructure 检查和验收。

PR #62 仍为 OPEN / Draft / deferred，不属于当前架构能力。

## 扩展原则

新增正式平台能力时：

1. 先建立真实 payload/transport 证据。
2. 明确 source identity、candidate 和 relationship authority。
3. 在专用 adapter 或固定 Provider route 中实现。
4. 保持 Public、Staff、Batch 和 Health 边界。
5. 定义浏览器或 server-generated download contract。
6. 增加 fail-closed tests 和真实浏览器 gate。
7. 单独执行 rollout，不让 Lab 自动晋升 Provider。

新增 Lab Provider 时遵循 [Provider Lab 指南](provider-lab.md)，并将 Lab 集成与正式产品路线拆成不同审查阶段。
