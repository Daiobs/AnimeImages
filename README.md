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
- 项目启动时会自动读取根目录 `.env`；配置模板拆分为 `.env.example.local` 和 `.env.example.production`，不要写入真实密钥。

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
DOUYIN_PROVIDER_ORDER=tikhub,native
XHS_PROVIDER_ORDER=native
TIKHUB_API_KEY=
TIKHUB_BASE_URL=https://api.tikhub.io
TIKHUB_CHINA_BASE_URL=https://api.tikhub.dev
TIKHUB_REGION=CN
TIKHUB_DEFAULT_COUNTRY_CODE=CN
TIKHUB_TRUST_COUNTRY_HEADER=False
TIKHUB_COUNTRY_HEADER=HTTP_CF_IPCOUNTRY
TIKHUB_GEOIP_DATABASE_PATH=
TIKHUB_DAILY_LIMIT=20
DOUYIN_ULTRA_TOTAL_TIMEOUT_SECONDS=15.0
DOUYIN_ULTRA_NATIVE_TIMEOUT_SECONDS=5.0
DOUYIN_SHORT_LINK_TIMEOUT_SECONDS=2.0
DOUYIN_SHORT_LINK_MAX_REDIRECTS=4
TIKHUB_CONNECT_TIMEOUT_SECONDS=1.0
TIKHUB_READ_TIMEOUT_SECONDS=6.5
TIKHUB_MAX_ATTEMPTS=2
TIKHUB_PROBE_TIMEOUT_SECONDS=1.8
TIKHUB_MAX_PROBE_CANDIDATES=1
TIKHUB_VALIDATED_CACHE_TTL_SECONDS=600
```

`native` 是默认快路径，不打开“抖音超高清”时只走这一路。打开超高清后，TikHub 是结果权威来源：一旦返回合法候选就立即使用，不再与本地 1080p 比较，也不会等待尚未完成的 native。TikHub 明确返回合法媒体 URL、文件大小和视频类型时会直接采用并缓存，避免重复 CDN 探测；只有元数据不完整时才执行 Range 验证。缓存命中不会启动 native。TikHub 失败或没有合法候选时，才使用并发 native 或普通 native 兜底。

超高清请求会按访问者国家选择 TikHub 路由：中国大陆使用 `api.tikhub.dev` 和 `region=CN`，其他地区使用 `api.tikhub.io` 和对应 ISO 国家码。国家识别优先读取显式信任的反向代理国家头，其次使用本地 GeoIP2 Country 数据库，最后回退 `TIKHUB_DEFAULT_COUNTRY_CODE`。只有在 Cloudflare 或可信代理会覆盖并清理客户端同名请求头时，才能开启 `TIKHUB_TRUST_COUNTRY_HEADER=True`。直接部署可配置 `TIKHUB_GEOIP_DATABASE_PATH`；用户 IP 不会发送给 TikHub。

TikHub 偶尔会返回明确要求重试且不扣费的临时 400、网络超时，或 `text/html`/极小文件等不可用结果。超高清模式默认最多请求两次，并使用 15 秒总预算容纳一次重试。大陆用户的第二次请求仍使用 `api.tikhub.dev`，但临时改用 `region=HK` 避开异常 CN CDN；每日限额按每次真实出站尝试计数。该预算只影响用户主动开启的超高清解析，不影响普通本地解析。

抖音短链接采用受控重定向解析。任一跳转地址一旦出现 `aweme_id`，解析器会立即启动 native/TikHub 流程，不再请求后续桌面页面，以减少短链解析延迟和偶发 404/超时。

生产环境建议以 `.env.example.production` 为清单配置真实环境变量。`DJANGO_ENV=production` 或 `DJANGO_DEBUG=False` 时，项目会强制要求显式 `DJANGO_SECRET_KEY`、`DJANGO_ALLOWED_HOSTS` 和共享 Redis `DJANGO_CACHE_URL`。生产默认关闭内存异步下载任务，由前端回退同步 ZIP，并启用下载频率、并发、每日请求量和每日流量限制。

部署探针可使用 `/health/live/` 和 `/health/ready/`。后者会实际检查 Redis/cache 与数据库；图片预览代理也有独立的请求和流量限制，相关配置见 `.env.example.production`。

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
