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
- 项目启动时会自动读取根目录 `.env`；`.env.example` 只作为模板，不要写入真实密钥。

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
DOUYIN_PROVIDER_ORDER=tikhub,native_enhanced,native
XHS_PROVIDER_ORDER=native
TIKHUB_API_KEY=
TIKHUB_BASE_URL=
TIKHUB_REGION=CN
TIKHUB_CACHE_TTL_SECONDS=1800
TIKHUB_DAILY_LIMIT=20
```

`native` 是默认快路径，不打开“抖音超高清”时只走这一路，避免额外候选探测拖慢普通解析。`native_enhanced` 是本地增强探测 provider，只有明确开启 `ULTRA_HD_PROVIDER_ENABLED=True` 且请求打开超清时才会参与解析；`native` 会始终作为基础兜底。`tikhub` 是可选第三方 Provider，只有开启超高清开关、配置 `TIKHUB_API_KEY`，并且在右上角“设置”中开启“抖音超高清”后才会请求外部服务。开启后，TikHub 和本地 native 快路径会并发执行，TikHub 返回源再和本地结果按实际探测大小比较；TikHub 返回源小于本地 1080p 时会保留本地结果。Provider 失败、超限或返回不可用资源时也会自动回退本地解析。`TIKHUB_CACHE_TTL_SECONDS` 用于缓存同一作品的返回结果，`TIKHUB_DAILY_LIMIT` 可限制单个 Django 进程每天最多调用 TikHub 的次数；设置为 `0` 表示不限制。修改 `.env` 后需要重启 Django 服务。

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
