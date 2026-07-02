# Scripts

这里存放本地实验脚本和生产运维脚本。生产运维脚本不属于 Django Web 服务运行路径，只能由服务器上的部署用户手动执行。

## Production deploy

- `pre_deploy_check.sh`：部署前安全检查，要求当前用户是 `ubuntu`、分支是 `DEPLOY_BRANCH`（默认 `main`）、工作树干净、`djangoProject/settings.py` 没有未提交修改，并且 `.env.production` 存在。
- `deploy.sh`：标准生产部署脚本。拒绝 root 执行，修正 `/srv/animeimages` 归属为 `ubuntu:ubuntu`，安全清理 stale `.git/index.lock`，执行 `git fetch origin $DEPLOY_BRANCH`、`git reset --hard origin/$DEPLOY_BRANCH`、依赖安装、migration、collectstatic，并调用 `restart.sh` 重启服务和运行 readiness 检查。
- `restart.sh`：重启 `animeimages` systemd 服务并默认执行 `/health/ready/` 检查。可通过 `RUN_HEALTH_CHECK=0` 暂时跳过健康检查。
- `health_check.sh`：用 `curl` 检查 readiness endpoint。默认检查 `http://127.0.0.1:8000/health/ready/`，可用 `HEALTH_CHECK_URL` 覆盖。

生产服务器上推荐：

```bash
cd /srv/animeimages
./scripts/pre_deploy_check.sh
./scripts/deploy.sh
```

不要在 `/srv/animeimages` 中用 root 执行 Git 命令。

临时 staging 分支部署可以显式指定分支，生产默认仍使用 `main`：

```bash
DEPLOY_BRANCH=codex/production-stabilization-v0.1 ./scripts/deploy.sh
```

单独重启或验证：

```bash
cd /srv/animeimages
./scripts/restart.sh
./scripts/health_check.sh
```

## Local experiments

- `xhs_request_probe.py`：小红书页面请求调试。
- `bilibili_selenium_search.py`：Selenium 浏览器自动化实验，需要时单独安装 `selenium`。
- `delete_xml_files.py`：本地文件清理脚本，运行前必须手动确认目录。
