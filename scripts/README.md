# Scripts

这里存放本地实验脚本和生产运维脚本。生产运维脚本不属于 Django Web 服务运行路径，只能由服务器上的部署用户手动执行。

## Production deploy

- `pre_deploy_check.sh`：部署前安全检查，要求当前用户是 `ubuntu`、分支是 `main`、工作树干净、`djangoProject/settings.py` 没有未提交修改，并且 `.env.production` 存在。
- `deploy.sh`：标准生产部署脚本。拒绝 root 执行，修正 `/srv/animeimages` 归属为 `ubuntu:ubuntu`，安全清理 stale `.git/index.lock`，执行 `git fetch origin`、`git reset --hard origin/main`、依赖安装、migration、collectstatic，并重启 `animeimages` systemd 服务。

生产服务器上推荐：

```bash
cd /srv/animeimages
./scripts/pre_deploy_check.sh
./scripts/deploy.sh
```

不要在 `/srv/animeimages` 中用 root 执行 Git 命令。

## Local experiments

- `xhs_request_probe.py`：小红书页面请求调试。
- `bilibili_selenium_search.py`：Selenium 浏览器自动化实验，需要时单独安装 `selenium`。
- `delete_xml_files.py`：本地文件清理脚本，运行前必须手动确认目录。
