# Scripts

这里存放本地实验脚本和生产运维脚本。生产运维脚本不属于 Django Web 服务运行路径，只能由服务器上的部署用户手动执行。

## Production deploy

- `pre_deploy_check.sh`：部署前安全检查，要求当前用户是 `ubuntu`、`/srv/animeimages` 目录本身归属为 `ubuntu:ubuntu`、分支是 `DEPLOY_BRANCH`（默认 `main`）、工作树干净、`djangoProject/settings.py` 没有未提交修改，并且 `.env.production` 存在。
- `deploy.sh`：标准生产部署脚本。拒绝 root 执行，检查 `/srv/animeimages` 目录本身归属为 `ubuntu:ubuntu`，安全清理 stale `.git/index.lock`，fetch 后固定目标 commit，校验目标可从 `origin/$DEPLOY_BRANCH` 到达，reset 到精确 SHA，再执行依赖安装、migration、collectstatic，并调用 `restart.sh` 重启服务和运行 readiness 检查。后台发布必须传入 `DEPLOY_TARGET_SHA=job.target_sha`；手工部署未传入时会固定本次 fetch 得到的远端 SHA。
- `restart.sh`：重启 `animeimages` systemd 服务并默认执行 `/health/ready/` 检查。可通过 `RUN_HEALTH_CHECK=0` 暂时跳过健康检查。
- `health_check.sh`：用 `curl` 检查 readiness endpoint。默认检查 `http://127.0.0.1:8000/health/ready/`，可用 `HEALTH_CHECK_URL` 覆盖。

生产服务器上推荐：

```bash
cd /srv/animeimages
./scripts/pre_deploy_check.sh
./scripts/deploy.sh
```

不要在 `/srv/animeimages` 中用 root 执行 Git 命令。部署脚本不会自动递归修复 ownership；如果 `/srv/animeimages` 目录 owner/group 不正确，先人工修复：

```bash
sudo chown -R ubuntu:ubuntu /srv/animeimages
```

`staticfiles/` 是 `collectstatic` 输出，保持可被 Nginx 读取即可；部署 ownership 检查只检查应用目录本身，不会因为 `staticfiles/` 内部归属不同而失败。

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

## Admin release jobs

`/admin/release-management/` 展示版本与 Tag，但 v1 只创建 `main` 发布任务。普通 GET 默认不 fetch GitHub；需要时使用页面刷新操作。Django 请求线程不直接执行 `deploy.sh`、迁移、重启或健康检查。v1 只接受 `systemctl start --no-block` 形式的非阻塞提交，直接配置 `deploy.sh` 会被拒绝。例如：

```text
RELEASE_MANAGEMENT_EXECUTOR_COMMAND="/usr/bin/sudo /bin/systemctl start --no-block animeimages-deploy@{job_id}.service"
```

systemd one-shot 服务再调用：

```bash
python manage.py run_release_deploy_job --job-id <job_id>
```

任务创建时会 fetch `origin/main` 并保存精确 `job.target_sha`。执行器把该值作为 `DEPLOY_TARGET_SHA` 传给 `deploy.sh`；脚本结束时 `HEAD` 必须与它一致。数据库最多允许一个 `pending`/`running` 任务。Tag/commit 在 v1 中只展示、不创建任务，需等迁移回滚策略完善后再开放。

## Local experiments

- `xhs_request_probe.py`：小红书页面请求调试。
- `bilibili_selenium_search.py`：Selenium 浏览器自动化实验，需要时单独安装 `selenium`。
- `delete_xml_files.py`：本地文件清理脚本，运行前必须手动确认目录。
