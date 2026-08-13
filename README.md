# AnimeImages

AnimeImages 是一个 Django 素材解析下载项目，当前主线目标是把“小红书 / 抖音 / 哔哩哔哩下载工具”推进成可灰度、可观测、可扩展的多平台高清素材解析系统。

如果你是在新的 Codex 项目里接手，请先读：

- [Codex 接手指南](docs/codex-handoff.md)
- [当前版本状态](docs/current-version-status.md)
- [生产行为基线](docs/production-behavior-baseline-v1.md)
- [仓库治理规范](docs/repository-governance.md)
- [Release Management 运维指南](docs/release-management.md)
- [生产部署检查清单](docs/production-deploy-checklist.md)
- [Provider Runtime Gates](docs/provider-runtime-gates.md)
- [天天解析 API 说明](docs/tiantianjiexi-api.md)

## 当前能力

核心页面：

- 首页通用解析：http://127.0.0.1:8000/
- 小红书：http://127.0.0.1:8000/xhs/
- 抖音：http://127.0.0.1:8000/tiktok/
- 批量解析：http://127.0.0.1:8000/batch/
- 图库：http://127.0.0.1:8000/library/
- Django 后台：http://127.0.0.1:8000/admin/

当前生产主线：

- 小红书：专用 adapter，负责原图、多图、视频基础下载。
- 抖音：专用 adapter 负责普通 1080p / 基础解析；超清链路优先 TianTianJieXi，KuKuTool 作为 fallback/race 备选，native 兜底。
- 哔哩哔哩：保留公开视频基础 mp4 / 封面解析，短期不做大会员、Cookie、DASH 音视频合流。
- 批量解析：`/batch/` 支持多链接解析，运行参数由 `BatchParseRuntimeSetting` 和 `.env` fallback 控制。
- 通用 Provider：yt-dlp、gallery-dl 作为长尾平台能力，必须经过 capability gate、runtime gate、platform gate。
- 下载策略：普通 TianTianJieXi / KuKuTool 视频由用户浏览器获取并保存，AnimeImages 不传输媒体正文；YouTube 合并、ZIP 等服务器生成文件继续走独立服务端流程。
- 后台运维：Provider 设置、Provider Health Tests、下载运行配置、批量解析运行配置、日志中心、策略中心和 Release Management 均已接入后台。

## 本地运行

建议在 PyCharm 中打开 `manage.py` 所在目录，并选择项目虚拟环境：

```text
.venv/bin/python
```

命令行运行：

```bash
cd /Users/xingkong/Documents/Codex/2026-06-03/django-python-pycharm-codex-netlify/djangoProject
source .venv/bin/activate
python -m pip install -r requirements.txt
python manage.py check
python manage.py migrate
python manage.py runserver
```

如果本机没有虚拟环境：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

本地 `.env` 可从 `.env.example.local` 复制。生产配置可参考 `.env.example.production`，但不要提交真实 `.env` 或 `.env.production`。

## 配置原则

项目目前把 Provider 配置拆成几层：

- capability gate：`.env` 中的 `*_ENABLED` 表示服务器是否具备运行能力。
- runtime gate：`/admin/provider-settings/` 中的 disabled / staff / public_gray / public 决定当前开放状态。
- platform gate：第三方通用 Provider 还需要平台或 host allowlist。
- safety limits：超时、并发、下载流量、批量解析频率等限制来自 `.env` fallback 或后台运行配置。

日常开关不要直接改代码。常规流程是：

1. `.env` 准备底层能力和依赖。
2. `python manage.py migrate`。
3. 后台打开对应运行配置。
4. Provider Health Tests 做 smoke。
5. 再从 staff 到 public_gray 到 public。

## 常用后台入口

- Provider 设置：`/admin/provider-settings/`
- Provider 健康状态：`/admin/provider-health/`
- Provider 健康测试：`/admin/provider-health-tests/`
- 下载运行配置：`/admin/console/`
- 批量解析运行配置：`/admin/xhs_download/batchparseruntimesetting/`
- 日志中心：`/admin/logs/`
- Policy Center：`/admin/policy-center/`
- 运维首页：`/admin/ops-dashboard/`
- Release Management：`/admin/release-management/`

Release Management v1 只读取本地 Git refs，默认处于 record-only 状态；未配置外部执行器时只记录 `pending` 发布任务，不会自动部署。权限边界、systemd 接线和故障排查见 [Release Management 运维指南](docs/release-management.md)。

## 常用检查命令

```bash
python manage.py check
python manage.py makemigrations --check --dry-run
python manage.py test xhs_download --noinput
python manage.py inspect_third_party_provider --health --json
python manage.py smoke_tiantianjiexi_provider --url "<douyin test url>" --mode douyin-ultra
```

生产部署后：

```bash
curl -i https://download.cosflow.icu/health/ready/
python manage.py prune_observability_logs --days 30 --dry-run
```

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

## 部署

当前服务器部署在：

- https://download.cosflow.icu/

常规 VPS 部署使用 PostgreSQL、Redis、Gunicorn、Nginx。完整流程见 [生产部署检查清单](docs/production-deploy-checklist.md)。

服务器更新主线代码时，通常在 `/srv/animeimages` 执行：

```bash
git fetch origin
git reset --hard origin/main
source .venv/bin/activate
set -a
. /srv/animeimages/.env.production
set +a
python manage.py migrate
python manage.py collectstatic --noinput
sudo systemctl restart animeimages
curl -i http://127.0.0.1:8000/health/ready/
```

如果 `.env.production` 出现 `-m: command not found`，通常是类似 `YTDLP_BINARY=/path/python -m yt_dlp` 没有加引号，应写成：

```text
YTDLP_BINARY="/srv/animeimages/.venv/bin/python -m yt_dlp"
GALLERYDL_BINARY="/srv/animeimages/.venv/bin/python -m gallery_dl"
```

## 文档索引

- [Codex 接手指南](docs/codex-handoff.md)
- [当前版本状态](docs/current-version-status.md)
- [Provider Runtime Gates](docs/provider-runtime-gates.md)
- [Provider 健康测试](docs/admin-provider-health-tests.md)
- [天天解析 API 说明](docs/tiantianjiexi-api.md)
- [下载路由引擎](docs/download-routing-engine.md)
- [下载成本守卫](docs/download-cost-guard.md)
- [Admin Observability](docs/admin-observability.md)
- [生产部署检查清单](docs/production-deploy-checklist.md)
- [Release Management 运维指南](docs/release-management.md)
- [开发流程](docs/development.md)
- [架构说明](docs/architecture.md)
- [图库素材](docs/image-library.md)

历史方案和实验路线，例如 Policy Intelligence、自愈系统、自治治理等，仍保留在 `docs/` 中作为设计记录。新任务接手时以本 README、`docs/codex-handoff.md` 和 `docs/current-version-status.md` 为准。

`origin/main` 是唯一开发基线，生产则由精确部署 SHA 和 checkpoint
记录。合并 main 不等于部署；生产 hotfix 若尚未进入 main，应标记为
`PRODUCTION_CODE_DRIFT` 并尽快通过审查 PR 收敛。
