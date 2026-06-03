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

- 图库搜索：http://127.0.0.1:8000/
- 小红书素材下载：http://127.0.0.1:8000/xhs_download/
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
