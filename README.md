# Django Project

本项目已升级为 Python 3.14.5 + Django 5.2.x 运行基线。

## 本地运行

建议在 PyCharm 中打开 `manage.py` 所在目录，然后选择项目虚拟环境：

```text
.venv/bin/python
```

命令行运行：

```bash
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python manage.py check
.venv/bin/python manage.py migrate
.venv/bin/python manage.py runserver
```

如果已经激活 `.venv`，也可以继续使用 `python manage.py ...`。

常用页面：

- 下载小站首页：http://127.0.0.1:8000/
- 小红书素材下载：http://127.0.0.1:8000/xhs/
- 抖音素材下载：http://127.0.0.1:8000/tiktok/
- 图库搜索：http://127.0.0.1:8000/library/
- 投稿表单：http://127.0.0.1:8000/submissions/submit/
- 投稿列表：http://127.0.0.1:8000/submissions/list

## 图库素材

图库素材当前存放在 `static/images`。原始素材按分类和角色组织，`padded` 目录是生成产物，用于统一搜索结果图片展示比例。

`static/images` 当前是本地素材库，体积较大，不进入普通 Git 提交。需要迁移或共享素材时，建议使用单独备份、Git LFS、对象存储或压缩素材包。

新增或替换素材后运行：

```bash
.venv/bin/python manage.py init_image_library
```

常用参数：

```bash
.venv/bin/python manage.py init_image_library --dry-run
.venv/bin/python manage.py init_image_library --force
```

命令会跳过已有 `padded` 目录中的图片，避免生成 `padded/padded` 这类嵌套产物。

## PyCharm

Run Configuration 可使用普通 Python 配置：

- Script path: `manage.py`
- Parameters: `runserver`
- Working directory: 当前目录
- 项目启动时会自动读取根目录 `.env`；实际运行只使用这一份 `.env`。仓库中的 `.env.example.local` 和 `.env.example.production` 是本地/生产配置模板，可按需复制为 `.env` 后修改。

## 小红书下载模式

默认推荐使用“生产安全模式”：后端打包 ZIP，浏览器保存到默认下载目录；支持 File System Access API 的浏览器可以在下载时选择保存位置。

图片默认以“原始文件”模式保存，尽量保留平台返回的原始字节，不在下载流程中做 JPG、PNG、WEBP 等二次转码。这样可以避免无意义的体积变大和重复压缩损耗。ZIP 在服务端通过临时文件生成，避免大视频下载时把整个压缩包放进内存。

解析结果区默认保持轻量展示：图片资源不额外显示质量和大小，视频资源显示平台接口或请求探测得到的文件大小。点击“保存所有资源”后，浏览器下载模式会创建后台打包任务并显示百分比进度条，单个大文件会按响应字节推进，最后再显示浏览器接收 ZIP 的进度。

解析层已预留平台适配器结构。小红书当前作为稳定 adapter 运行；抖音 adapter 已接入页面识别、移动 feed/share 原生 provider、候选视频源排序、候选源诊断和嵌入 JSON 图片/视频解析；哔哩哔哩 adapter 已接入公开视频接口的基础 mp4 解析，并复用候选 URL 去重。当前版本以稳定无登录源为主，超高清源枚举、B 站 DASH 音视频合流和质量探测会通过独立 Provider 层研究，不直接在 view 中追加平台逻辑。

本地开发/测试时，可以在环境变量中开启服务端保存：

```text
XHS_ALLOW_SERVER_SAVE=True
XHS_ALLOW_ABSOLUTE_SAVE_PATH=True
XHS_DEFAULT_SERVER_SAVE_DIR=/Users/your-name/Downloads/xhs
```

开启后，小红书页面设置里会出现“开发保存模式”，可以像之前一样填写服务端绝对路径。

Provider 的 `.env` 配置表示服务器具备对应底层能力，不等于公网开放。开发环境可以在项目根目录 `.env` 中准备常规 Provider 能力，日常开放/关闭仍通过 `/admin/provider-settings/` 控制：

```text
ULTRA_HD_PROVIDER_ENABLED=False
ULTRA_HD_PUBLIC_ACCESS_ENABLED=True
XHS_PROVIDER_ORDER=native
TIKHUB_API_KEY=
TIKHUB_BASE_URL=https://api.tikhub.io
TIKHUB_CHINA_BASE_URL=https://api.tikhub.dev
TIKHUB_REGION=CN
TIKHUB_DEFAULT_COUNTRY_CODE=CN
TIKHUB_TRUST_COUNTRY_HEADER=False
TIKHUB_COUNTRY_HEADER=HTTP_CF_IPCOUNTRY
TIKHUB_GEOIP_DATABASE_PATH=
TIKHUB_DAILY_LIMIT=0
TIKHUB_CONNECT_TIMEOUT_SECONDS=1.0
TIKHUB_READ_TIMEOUT_SECONDS=6.5
TIKHUB_MAX_ATTEMPTS=2
TIKHUB_PROBE_TIMEOUT_SECONDS=1.8
TIKHUB_MAX_PROBE_CANDIDATES=1
TIKHUB_VALIDATED_CACHE_TTL_SECONDS=600
DOUYIN_ULTRA_TOTAL_TIMEOUT_SECONDS=4.0
DOUYIN_ULTRA_NATIVE_TIMEOUT_SECONDS=3.0
DOUYIN_ULTRA_KUKUTOOL_GRACE_SECONDS=1.0
DOUYIN_NATIVE_EXECUTOR_WORKERS=4
DOUYIN_NATIVE_MAX_PENDING_TASKS=8
DOUYIN_SHORT_LINK_TIMEOUT_SECONDS=2.0
DOUYIN_SHORT_LINK_MAX_REDIRECTS=4
KUKUTOOL_PROVIDER_ENABLED=True
KUKUTOOL_NODE_BINARY=
KUKUTOOL_SIDECAR_BASE_URL=
KUKUTOOL_SIDECAR_TIMEOUT_SECONDS=3.0
KUKUTOOL_SIDECAR_HEALTH_TIMEOUT_SECONDS=0.5
KUKUTOOL_TIMEOUT_SECONDS=12.0
KUKUTOOL_PROBE_TIMEOUT_SECONDS=2.0
KUKUTOOL_MAX_PROBE_CANDIDATES=2
KUKUTOOL_MAX_CONCURRENCY=2
KUKUTOOL_EXECUTOR_WORKERS=2
KUKUTOOL_MAX_PENDING_TASKS=4
KUKUTOOL_GLOBAL_MAX_CONCURRENCY=4
KUKUTOOL_SINGLE_FLIGHT_TTL_SECONDS=30
KUKUTOOL_FAILURE_CACHE_TTL_SECONDS=15
KUKUTOOL_CACHE_POLL_INTERVAL_SECONDS=0.1
KUKUTOOL_GLOBAL_REQUEST_LIMIT_PER_MINUTE=0
KUKUTOOL_CIRCUIT_FAILURE_THRESHOLD=5
KUKUTOOL_CIRCUIT_WINDOW_SECONDS=60
KUKUTOOL_CIRCUIT_OPEN_SECONDS=120
KUKUTOOL_PROFILE_CACHE_TTL_SECONDS=86400
KUKUTOOL_VALIDATED_CACHE_TTL_SECONDS=300
KUKUTOOL_REFERENCE_WAIT_SECONDS=0.5
THIRD_PARTY_PROVIDER_ENABLED=True
THIRD_PARTY_INTERNAL_STAFF_ALLOW_ADULT=False
THIRD_PARTY_RATE_LIMIT_PER_MINUTE=0
THIRD_PARTY_GLOBAL_REQUEST_LIMIT_PER_MINUTE=0
THIRD_PARTY_FRONTEND_TIMEOUT_SECONDS=3.0
THIRD_PARTY_EXTERNAL_OUTPUT_MAX_BYTES=8388608
THIRD_PARTY_KUKUTOOL_PROVIDER_ENABLED=False
THIRD_PARTY_KUKUTOOL_TIMEOUT_SECONDS=12.0
THIRD_PARTY_KUKUTOOL_RETRIES=2
THIRD_PARTY_KUKUTOOL_EXECUTOR_WORKERS=2
THIRD_PARTY_KUKUTOOL_MAX_PENDING_TASKS=4
THIRD_PARTY_KUKUTOOL_GLOBAL_MAX_CONCURRENCY=4
THIRD_PARTY_KUKUTOOL_SINGLE_FLIGHT_TTL_SECONDS=30
THIRD_PARTY_KUKUTOOL_FAILURE_CACHE_TTL_SECONDS=15
THIRD_PARTY_KUKUTOOL_CACHE_POLL_INTERVAL_SECONDS=0.1
THIRD_PARTY_KUKUTOOL_CIRCUIT_FAILURE_THRESHOLD=5
THIRD_PARTY_KUKUTOOL_CIRCUIT_WINDOW_SECONDS=60
THIRD_PARTY_KUKUTOOL_CIRCUIT_OPEN_SECONDS=120
THIRD_PARTY_KUKUTOOL_PROFILE_CACHE_TTL_SECONDS=86400
THIRD_PARTY_KUKUTOOL_PAYLOAD_CACHE_TTL_SECONDS=120
YTDLP_PROVIDER_ENABLED=True
YTDLP_BINARY=
YTDLP_TIMEOUT_SECONDS=15.0
YTDLP_EXECUTOR_WORKERS=1
YTDLP_MAX_PENDING_TASKS=2
YTDLP_PAYLOAD_CACHE_TTL_SECONDS=300
YTDLP_FAILURE_CACHE_TTL_SECONDS=30
YTDLP_MERGE_PROVIDER_ENABLED=True
YTDLP_MERGE_DEFAULT_MAX_HEIGHT=1080
YTDLP_MERGE_HIGH_QUALITY_MAX_HEIGHT=4320
YTDLP_MERGE_MAX_FILE_SIZE_BYTES=1073741824
YOUTUBE_MERGE_PROVIDER_ENABLED=True
GALLERYDL_PROVIDER_ENABLED=True
GALLERYDL_BINARY=
GALLERYDL_TIMEOUT_SECONDS=15.0
GALLERYDL_EXECUTOR_WORKERS=1
GALLERYDL_MAX_PENDING_TASKS=2
GALLERYDL_MAX_ITEMS=30
GALLERYDL_PAYLOAD_CACHE_TTL_SECONDS=300
GALLERYDL_FAILURE_CACHE_TTL_SECONDS=30
DOUYIN_METADATA_CACHE_TTL_SECONDS=1800
PROVIDER_MANIFEST_MAX_AGE_SECONDS=3600
```

`native` 是默认快路径，不打开“抖音超高清”时只走这一路。当前阶段打开超高清后，只调用 KuKuTool 上游；TikHub 和 `native_enhanced` 不进入同步网页请求链路。TikHub 配置仅保留给研究和诊断命令，不建议作为当前公网超高清路径。KuKuTool 返回的 `video_fullinfo` 会按码率、大小和质量参数排序，并以稳定的 TOS 对象路径去重。具备合法字节 CDN 域名、正文件大小、稳定对象路径和未到期签名的最高档候选会直接采用；元数据不完整时才执行 `Range: bytes=0-0` 校验。结果不再与本地 1080p 比较。

超高清请求会在独立线程池中同时启动 KuKuTool 和 native，并只占用最多 `DOUYIN_ULTRA_TOTAL_TIMEOUT_SECONDS` 的主要前台等待预算。KuKuTool 在预算内成功时返回超高清；到达 deadline 时若 native 已完成，会额外使用 `DOUYIN_ULTRA_KUKUTOOL_GRACE_SECONDS` 做一个短暂尾部等待，尽量接住刚好完成的 KuKuTool 4K 结果；仍未完成则返回 native，而 KuKuTool 在受控后台继续运行并回填缓存。两条路径都未完成时返回可重试的超时信息。验证后的签名 URL 使用 Django cache 缓存，TTL 同时受 `KUKUTOOL_VALIDATED_CACHE_TTL_SECONDS` 和 CDN URL 自身到期时间约束。

该 Provider 可以直接启动 Node helper，也可以优先调用内部 `douyin-source-resolver` sidecar。直接启动 helper 时需要 Node.js 18 或更高版本，因为上游请求使用 Node 内置的 `fetch` 和 `crypto`；生产环境必须显式配置 `KUKUTOOL_NODE_BINARY` 或 `KUKUTOOL_SIDECAR_BASE_URL`，不会搜索用户 Home 下的 Codex runtime。本地 `DEBUG=True` 时才允许回退到系统 `node` 或开发机的 Codex runtime。找不到可用运行时或可用 sidecar 时，页面不会开放超高清开关。sidecar 在预算内失败时才回退本地 helper，helper 只使用剩余预算，避免突破前台解析 deadline。线程池、待处理任务数、单进程子进程数、Redis 全局并发和每分钟上游请求量均受配置限制；Redis single-flight 让相同 `aweme_id` 的并发请求共享一次上游任务。连续失败会打开临时熔断，staff 可通过 `/admin/provider-health/` 查看状态，并通过 `/admin/provider-health/control/` 紧急停用或恢复。Node 子进程只继承最小化环境，不会获得 Django 密钥、数据库 URL 或其他 API key。长驻 sidecar 是正式上线后的推荐形态，Django 只消费 `douyin-source-resolver.v1` 响应契约。

验证后的 KuKuTool URL 和作品标题、作者、文件名、封面会分别缓存。完整热缓存命中时不再启动 native，因此不会固定等待 `KUKUTOOL_REFERENCE_WAIT_SECONDS`。下载清单保留 Provider、`aweme_id` 和缓存键，并带有短期 Django 签名；浏览器伪造的清单不能触发上游刷新。若缓存 URL 返回 403/404、HTML 风控页或错误 Content-Type，服务端会删除对应验证缓存，刷新 KuKuTool 一次，仍失败则自动回退 native。重定向后的域名和媒体类型在每次尝试中都会重新校验。

抖音短链接采用受控重定向解析。任一跳转地址一旦出现 `aweme_id`，解析器会立即启动 native/KuKuTool 流程，不再请求后续桌面页面，以减少短链解析延迟和偶发 404/超时。

第三方通用解析采用 capability gate、runtime gate 和 platform gate 分层控制。`.env` 中的 `THIRD_PARTY_PROVIDER_ENABLED`、`YTDLP_PROVIDER_ENABLED`、`GALLERYDL_PROVIDER_ENABLED`、`YOUTUBE_MERGE_PROVIDER_ENABLED` 表示服务器具备对应底层能力，不代表公网开放；日常的 disabled / staff / public_gray / public 由 `/admin/provider-settings/` 的 `ProviderRuntimeSetting` 控制。第三方公开访问还必须命中后台配置的 `third_party_allowed_public_platforms` 或 `third_party_allowed_public_hosts`，allowlist 为空时不会向访客开放。`third_party_blocked_platforms`、`third_party_blocked_hosts` 和内置高风险 host 阻止列表优先级最高，即使命中 allowlist 也会拒绝。YouTube、Flickr 这类 safe 候选平台也不会自动公开；Instagram、Facebook、X、Reddit、Tumblr、DeviantArt 等 normal/sensitive 平台默认只适合 staff smoke；adult、age-restricted、login_required 平台默认不向访客开放，也不通过 Cookie、登录或风控绕过提供公开解析入口。`THIRD_PARTY_KUKUTOOL_PROVIDER_ENABLED` 只控制通用长尾平台里的 KuKuTool 上游，和抖音超高清的 `KUKUTOOL_PROVIDER_ENABLED` 分开。KuKuTool generic 在本地和生产都默认关闭，必须显式开启；先灰度 `yt-dlp` 或 `gallery-dl` 时可以不触发 KuKuTool Node helper。

第三方通用解析已预留 `yt-dlp` 兜底 Provider。它不会替代小红书、抖音或哔哩哔哩的专用 adapter，也不参与抖音超高清主线；只在显式开启 `YTDLP_PROVIDER_ENABLED=True` 时，对通用第三方视频平台作为白名单兜底。生产环境应先安装并固定 `yt-dlp` 可执行命令，再设置 `YTDLP_BINARY`。调用时会带 `--ignore-config`，并在最小化子进程环境中设置 `YTDLP_NO_PLUGINS=1` 和 `PYTHONNOUSERSITE=1`，同时使用临时工作目录和临时 `HOME`，避免开发机或服务器用户级 yt-dlp 配置、插件、缓存或 user-site Python 包悄悄改变解析行为。该路径只采用直接 HTTP 的完整单文件视频；HLS/DASH、音视频分离格式，以及缺少明确音频/视频 codec 标记的直链候选都会被拒绝，避免返回无声视频或需要转码合并的资源。

第三方图片/图集平台预留 `gallery-dl` 兜底 Provider。它同样默认关闭，只在显式开启 `GALLERYDL_PROVIDER_ENABLED=True` 后用于 Imgur、Reddit、Tumblr、Flickr、DeviantArt 等白名单平台。该路径只接受平台规则允许的直连媒体 URL，并复用下载签名与最终域名校验；静态图按图片处理，Imgur 动图这类 `.mp4/.webm` 资源会按视频处理。若同一图集条目里同时存在原图和 `thumbnail/preview` 字段，默认优先保留原图候选；如果另一个条目只有缩略图可用，仍会把它作为该条目的兜底素材保留。不处理 HLS/DASH、视频合并或需要登录 Cookie 的私有资源。

通用第三方 Provider 会记录结构化解析诊断。每次 Provider 尝试都会在日志中写入 `third_party_provider_failed` 或 `third_party_provider_selected`，并把 `selected_provider`、`selected_provider_duration_ms`、`provider_attempts` 和 `provider_fallback_count` 写入 `ParseResult.metadata`，便于排查某个平台是上游超时、输出结构变化，还是媒体 URL 被白名单拒绝。`inspect_third_party_provider --json` 的成功和失败输出都会在顶层返回 `attempts`、规范化后的 `source_url` 和匹配到的 `platform_id`；输入或配置阶段失败时 `attempts` 为空列表，错误状态为 `rejected`，例如缺少 URL、不支持的 URL、未启用 Provider 或无效的 `--binary` 覆盖。终端文本输出会直接列出规范化 URL、平台 ID、每个 Provider 的状态和耗时，方便灰度排障。

只打开 `THIRD_PARTY_PROVIDER_ENABLED` 不会自动启用任何具体上游，也不会自动公开所有 yt-dlp/gallery-dl 支持站点。若当前平台规则下没有启用的 provider，解析会在调用外部工具前返回“没有启用可用 Provider”，并记录 `third_party_provider_unavailable` 日志，方便灰度配置排查。第三方 capability 打开但具体 provider 全部关闭时，`manage.py check` 也会报 `xhs_download.E007`。如果 runtime 已设为 public/public_gray 但 URL 未命中 allowlist，Web 入口会返回“该平台暂未进入公开灰度”；命中 blocked 或高风险策略时会返回更明确的拒绝原因。

第三方通用解析入口同时有 IP 级分钟限流和全站分钟限流。`THIRD_PARTY_RATE_LIMIT_PER_MINUTE` 限制单个访客，`THIRD_PARTY_GLOBAL_REQUEST_LIMIT_PER_MINUTE` 在 Web 入口层限制所有访客合计请求，因此会覆盖 KuKuTool generic、`yt-dlp` 和 `gallery-dl`，避免灰度阶段被长尾平台请求打满 worker 或外部上游。

`THIRD_PARTY_FRONTEND_TIMEOUT_SECONDS` 是通用第三方同步解析的前端预算上限。KuKuTool generic 会按该预算等待缓存或后台任务；`yt-dlp` 和 `gallery-dl` 会把实际子进程 timeout 限制为 `min(自身 Provider timeout, THIRD_PARTY_FRONTEND_TIMEOUT_SECONDS)`，避免页面请求被单个外部命令拖到十几秒。多个 Provider 串行 fallback 时也共享这个总预算，预算耗尽后不会继续调用下一个外部工具。外部命令运行在临时工作目录和临时 `HOME` 下；`THIRD_PARTY_EXTERNAL_OUTPUT_MAX_BYTES` 限制 `yt-dlp` / `gallery-dl` 子进程 JSON 输出大小，输出会先进临时文件并在解析前校验，避免异常 extractor 输出直接占满 Django worker 内存。

`yt-dlp` 和 `gallery-dl` 的健康状态会暴露可执行文件检查结果，包括 `binary_configured`、`executable_found`、`resolved_executable`、`version_probe_ok` 和 `ready`。生产环境启用这两个 Provider 时必须显式配置对应 binary；启用后健康状态和 `manage.py check` 都会执行短 `--version` 探测，并对探测结果做短缓存，binary 缺失、不可执行或探测失败时会直接报错，避免上线后才在用户请求中失败。

通用 KuKuTool、`yt-dlp` 和 `gallery-dl` 解析结果会按规范化后的源链接做 best-effort 短期 payload cache，重复解析同一链接时不会反复启动上游任务或外部子进程。失败也会写入短期 failure cache，避免短时间内对同一失效链接持续打满本机 worker；缓存读写失败会记录日志并继续走真实解析路径。若签名预览或下载时遇到 HTTP 错误、HTML 风控页或 Content-Type 异常，服务端会删除对应 provider/source 的 payload cache 和 failure cache，下一次解析会重新获取新直链。

`yt-dlp` 和 `gallery-dl` 不属于基础运行依赖，默认不会随 `requirements.txt` 安装。外部命令会禁用 Python user-site，并把 `HOME` 指向每次调用的临时目录，因此不要依赖用户目录里的临时安装、配置文件或缓存；开发机测试时可以二选一：

```bash
.venv/bin/python -m pip install yt-dlp gallery-dl
YTDLP_BINARY=".venv/bin/python -m yt_dlp"
GALLERYDL_BINARY=".venv/bin/python -m gallery_dl"
```

或使用系统/容器内固定的可执行命令：

```text
YTDLP_BINARY=/usr/local/bin/yt-dlp
GALLERYDL_BINARY=/usr/local/bin/gallery-dl
```

第三方外部工具只作为独立命令或未来 sidecar 使用，不应把上游项目源码直接复制进本仓库。上线前需要固定工具版本并复核许可证与部署方式：yt-dlp 源码当前为 Unlicense，但官方打包产物可能包含额外第三方许可；gallery-dl 当前为 GPL-2.0，且 GitHub Releases 已提示活跃开发迁往 Codeberg，生产升级源需要固定；MeTube 当前为 AGPL-3.0 且只作为队列/进度/部署形态参考，不作为本项目依赖引入。

生产环境请基于根目录 `.env` 配置真实环境变量。`DJANGO_ENV=production` 或 `DJANGO_DEBUG=False` 时，项目会强制要求显式 `DJANGO_SECRET_KEY`、`DJANGO_ALLOWED_HOSTS` 和共享 Redis `DJANGO_CACHE_URL`。生产默认关闭内存异步下载任务，由前端回退同步 ZIP，并启用下载频率、并发、每日请求量和每日流量限制。

部署探针可使用 `/health/live/` 和 `/health/ready/`。后者会实际检查 Redis/cache 与数据库；图片预览代理也有独立的请求和流量限制，相关配置见根目录 `.env` 或部署环境变量。

抖音候选源诊断可以通过管理命令运行：

```bash
.venv/bin/python manage.py inspect_douyin_candidates "https://v.douyin.com/xxxx/" --json
.venv/bin/python manage.py inspect_douyin_candidates --health --json
```

该命令会枚举 direct page、mobile feed、mobile share 中可见的视频候选源，并使用 HEAD/Range 探测真实大小，便于对比 480p/1080p/超高清差异。`--health` 不需要链接，会输出抖音超高清开关、KuKuTool runtime、sidecar、熔断和配置问题，适合在打开页面前确认 `KUKUTOOL_NODE_BINARY` 或 `KUKUTOOL_SIDECAR_BASE_URL` 是否可用。

第三方通用 Provider 诊断可以运行：

```bash
.venv/bin/python manage.py inspect_third_party_provider --health --json
```

`--health` 不需要传 URL，会输出第三方总开关、公共入口开关、各 Provider 的可执行文件/版本探测状态、平台规则以及当前 issue 列表，适合在 PyCharm 本地启动或生产灰度前做配置自检。文本模式会列出平台覆盖、允许 Provider 顺序、当前启用 Provider 和 ready Provider；JSON 模式会把这些字段拆成 `provider_ids`、`enabled_provider_ids` 和 `ready_provider_ids`，方便脚本检查。需要临时验证外部命令路径时，可以配合显式 Provider 使用，例如 `--health --provider ytdlp --binary ".venv/bin/python -m yt_dlp" --json`，此时即使 Provider 尚未启用，也会对该 binary 执行一次短版本探测。

```bash
.venv/bin/python manage.py inspect_third_party_provider "https://www.youtube.com/watch?v=..." --provider ytdlp --binary ".venv/bin/python -m yt_dlp" --json
```

该命令会复用平台白名单、Provider normalizer 和 `ParseResult`/`MediaAsset` 输出契约，适合在打开 Web 入口前验证 `yt-dlp` 或 `gallery-dl` 解析结果。`--provider auto` 会尊重当前 Provider 启用开关并模拟 Web 解析顺序；显式指定 `--provider ytdlp`、`--provider gallerydl` 或 `--provider kukutool` 则会忽略启用开关，用于单独诊断该 Provider。`--timeout` 会同时覆盖命令诊断的前端预算和对应 Provider timeout，避免诊断时仍被默认 3 秒页面预算截断。JSON 诊断默认只输出脱敏媒体直链和脱敏错误 URL；本地需要复制完整 CDN URL 做人工验证时，再显式增加 `--show-urls`。`gallery-dl` 冷启动和站点访问通常比 `yt-dlp` 更慢，本地诊断或灰度图片平台时建议先用 8 秒左右预算验证。图片平台可用：

```bash
.venv/bin/python manage.py inspect_third_party_provider "https://imgur.com/..." --provider gallerydl --binary ".venv/bin/python -m gallery_dl" --timeout 8 --json
```

## 部署说明

Django 后端建议部署到支持 Python WSGI/ASGI 的平台，例如 Render、Railway、Fly.io 或 VPS + Gunicorn/Nginx。

上线前至少执行：

```bash
.venv/bin/python manage.py check
DJANGO_DEBUG=False \
DJANGO_SECRET_KEY=local-production-check-7f4e2d9c8b1a6e5d3c0f9a8b7e6d5c4b3a2f1e0d9c8b7a6f5e4d3c2b1a0f9e8 \
DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1,testserver \
DJANGO_CACHE_URL=redis://localhost:6379/0 \
ULTRA_HD_PROVIDER_ENABLED=False \
KUKUTOOL_PROVIDER_ENABLED=False \
THIRD_PARTY_PROVIDER_ENABLED=True \
YTDLP_PROVIDER_ENABLED=True \
YTDLP_MERGE_PROVIDER_ENABLED=True \
YOUTUBE_MERGE_PROVIDER_ENABLED=True \
GALLERYDL_PROVIDER_ENABLED=True \
DJANGO_SECURE_SSL_REDIRECT=True \
DJANGO_SESSION_COOKIE_SECURE=True \
DJANGO_CSRF_COOKIE_SECURE=True \
DJANGO_SECURE_HSTS_SECONDS=31536000 \
DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS=True \
DJANGO_SECURE_HSTS_PRELOAD=True \
.venv/bin/python manage.py check --deploy
.venv/bin/python manage.py collectstatic --noinput
```

生产环境应设置长随机 `DJANGO_SECRET_KEY`、`DJANGO_DEBUG=False`、线上域名 `DJANGO_ALLOWED_HOSTS`，并在确认全站 HTTPS 后开启安全 Cookie、HTTPS redirect 和 HSTS。
如果要在生产打开 KuKuTool 超高清，需要显式配置 `KUKUTOOL_NODE_BINARY` 或 `KUKUTOOL_SIDECAR_BASE_URL`，并把 `KUKUTOOL_PROVIDER_ENABLED=True` 加入同一组检查命令。

更多文档：

- `docs/development.md`：开发流程和 Git 规范
- `docs/deployment.md`：上线部署清单
- `docs/architecture.md`：模块结构说明
- `docs/current-version-status.md`：当前版本状态、超清限制和下一步诊断方向
- `docs/image-library.md`：图库素材和 padded 初始化规则
