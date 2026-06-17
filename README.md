# Django Project

本项目已升级为 Python 3.14.5 + Django 5.2.x 运行基线。

## 本地运行

建议在 PyCharm 中打开 `manage.py` 所在目录，然后选择项目虚拟环境：

```text
.venv/bin/python
```

命令行运行：

```bash
python -m pip install -r requirements.txt
python manage.py check
python manage.py migrate
python manage.py runserver
```

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
python manage.py init_image_library
```

常用参数：

```bash
python manage.py init_image_library --dry-run
python manage.py init_image_library --force
```

命令会跳过已有 `padded` 目录中的图片，避免生成 `padded/padded` 这类嵌套产物。

## PyCharm

Run Configuration 可使用普通 Python 配置：

- Script path: `manage.py`
- Parameters: `runserver`
- Working directory: 当前目录
- 项目启动时会自动读取根目录 `.env`；本项目统一使用这一份 `.env` 作为本地和生产环境配置来源，不再维护拆分的本地/生产模板文件。

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

超高清 Provider 相关配置默认关闭，当前只预留基础设施。开发环境可以在项目根目录 `.env` 中配置：

```text
ULTRA_HD_PROVIDER_ENABLED=False
ULTRA_HD_PUBLIC_ACCESS_ENABLED=True
XHS_PROVIDER_ORDER=native
DOUYIN_ULTRA_TOTAL_TIMEOUT_SECONDS=3.0
DOUYIN_ULTRA_NATIVE_TIMEOUT_SECONDS=3.0
DOUYIN_NATIVE_EXECUTOR_WORKERS=4
DOUYIN_NATIVE_MAX_PENDING_TASKS=8
DOUYIN_SHORT_LINK_TIMEOUT_SECONDS=2.0
DOUYIN_SHORT_LINK_MAX_REDIRECTS=4
KUKUTOOL_PROVIDER_ENABLED=True
KUKUTOOL_NODE_BINARY=
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
DOUYIN_METADATA_CACHE_TTL_SECONDS=1800
PROVIDER_MANIFEST_MAX_AGE_SECONDS=3600
```

`native` 是默认快路径，不打开“抖音超高清”时只走这一路。当前阶段打开超高清后，只调用 KuKuTool 上游；TikHub 和 `native_enhanced` 不进入同步网页请求链路。KuKuTool 返回的 `video_fullinfo` 会按码率、大小和质量参数排序，并以稳定的 TOS 对象路径去重。具备合法字节 CDN 域名、正文件大小、稳定对象路径和未到期签名的最高档候选会直接采用；元数据不完整时才执行 `Range: bytes=0-0` 校验。结果不再与本地 1080p 比较。

超高清请求会在独立线程池中同时启动 KuKuTool 和 native，并只占用最多 `DOUYIN_ULTRA_TOTAL_TIMEOUT_SECONDS` 的前台等待预算。KuKuTool 在预算内成功时返回超高清；到达 deadline 时若 native 已完成则立即返回 native，而 KuKuTool 在受控后台继续运行并回填缓存；两条路径都未完成时返回可重试的超时信息。验证后的签名 URL 使用 Django cache 缓存，TTL 同时受 `KUKUTOOL_VALIDATED_CACHE_TTL_SECONDS` 和 CDN URL 自身到期时间约束。

该 Provider 需要 Node.js 18 或更高版本，因为上游请求使用 Node 内置的 `fetch` 和 `crypto`。运行时只在 Django 进程启动阶段检查；生产环境必须显式配置 `KUKUTOOL_NODE_BINARY`，不会搜索用户 Home 下的 Codex runtime。本地 `DEBUG=True` 时才允许回退到系统 `node` 或开发机的 Codex runtime。找不到可用运行时时，页面不会开放超高清开关。线程池、待处理任务数、单进程子进程数、Redis 全局并发和每分钟上游请求量均受配置限制；Redis single-flight 让相同 `aweme_id` 的并发请求共享一次上游任务。连续失败会打开临时熔断，staff 可通过 `/admin/provider-health/` 查看状态，并通过 `/admin/provider-health/control/` 紧急停用或恢复。Node 子进程只继承最小化环境，不会获得 Django 密钥、数据库 URL 或其他 API key。长驻 Node sidecar 仍是正式上线后的推荐形态。

验证后的 KuKuTool URL 和作品标题、作者、文件名、封面会分别缓存。完整热缓存命中时不再启动 native，因此不会固定等待 `KUKUTOOL_REFERENCE_WAIT_SECONDS`。下载清单保留 Provider、`aweme_id` 和缓存键，并带有短期 Django 签名；浏览器伪造的清单不能触发上游刷新。若缓存 URL 返回 403/404、HTML 风控页或错误 Content-Type，服务端会删除对应验证缓存，刷新 KuKuTool 一次，仍失败则自动回退 native。重定向后的域名和媒体类型在每次尝试中都会重新校验。

抖音短链接采用受控重定向解析。任一跳转地址一旦出现 `aweme_id`，解析器会立即启动 native/KuKuTool 流程，不再请求后续桌面页面，以减少短链解析延迟和偶发 404/超时。

生产环境请基于根目录 `.env` 配置真实环境变量。`DJANGO_ENV=production` 或 `DJANGO_DEBUG=False` 时，项目会强制要求显式 `DJANGO_SECRET_KEY`、`DJANGO_ALLOWED_HOSTS` 和共享 Redis `DJANGO_CACHE_URL`。生产默认关闭内存异步下载任务，由前端回退同步 ZIP，并启用下载频率、并发、每日请求量和每日流量限制。

部署探针可使用 `/health/live/` 和 `/health/ready/`。后者会实际检查 Redis/cache 与数据库；图片预览代理也有独立的请求和流量限制，相关配置见根目录 `.env` 或部署环境变量。

抖音候选源诊断可以通过管理命令运行：

```bash
python manage.py inspect_douyin_candidates "https://v.douyin.com/xxxx/" --json
```

该命令会枚举 direct page、mobile feed、mobile share 中可见的视频候选源，并使用 HEAD/Range 探测真实大小，便于对比 480p/1080p/超高清差异。

## 部署说明

Django 后端建议部署到支持 Python WSGI/ASGI 的平台，例如 Render、Railway、Fly.io 或 VPS + Gunicorn/Nginx。

上线前至少执行：

```bash
python manage.py check
python manage.py check --deploy
python manage.py collectstatic --noinput
```

生产环境应设置长随机 `DJANGO_SECRET_KEY`、`DJANGO_DEBUG=False`、线上域名 `DJANGO_ALLOWED_HOSTS`，并在确认全站 HTTPS 后开启安全 Cookie、HTTPS redirect 和 HSTS。

更多文档：

- `docs/development.md`：开发流程和 Git 规范
- `docs/deployment.md`：上线部署清单
- `docs/architecture.md`：模块结构说明
- `docs/current-version-status.md`：当前版本状态、超清限制和下一步诊断方向
- `docs/image-library.md`：图库素材和 padded 初始化规则
