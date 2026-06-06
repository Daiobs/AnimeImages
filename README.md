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
- Environment variables 可参考 `.env.example`

## 小红书下载模式

默认推荐使用“生产安全模式”：后端打包 ZIP，浏览器保存到默认下载目录；支持 File System Access API 的浏览器可以在下载时选择保存位置。

图片默认以“原始文件”模式保存，尽量保留平台返回的原始字节，不在下载流程中做 JPG、PNG、WEBP 等二次转码。这样可以避免无意义的体积变大和重复压缩损耗。ZIP 在服务端通过临时文件生成，避免大视频下载时把整个压缩包放进内存。

解析层已预留平台适配器结构。小红书当前作为稳定 adapter 运行，抖音 adapter 已接入页面识别、移动分享页回退和嵌入 JSON 图片/视频解析。当前版本以稳定无登录源为主，超高清源枚举和质量探测会在下一阶段通过独立 Provider 层研究，不直接在 view 中追加平台逻辑。

本地开发/测试时，可以在环境变量中开启服务端保存：

```text
XHS_ALLOW_SERVER_SAVE=True
XHS_ALLOW_ABSOLUTE_SAVE_PATH=True
XHS_DEFAULT_SERVER_SAVE_DIR=/Users/your-name/Downloads/xhs
```

开启后，小红书页面设置里会出现“开发保存模式”，可以像之前一样填写服务端绝对路径。

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
- `docs/image-library.md`：图库素材和 padded 初始化规则
