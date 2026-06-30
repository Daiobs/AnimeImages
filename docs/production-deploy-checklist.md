# Production Deploy Checklist

Use this checklist for the first Hong Kong VPS deployment. Replace placeholders such as `/srv/animeimages`, `animeimages.example.com`, and commit hashes with the real server values.

## 1. Prepare Server

- [ ] Create application directory:

```bash
sudo mkdir -p /srv/animeimages
sudo chown -R "$USER":"$USER" /srv/animeimages
```

- [ ] Install OS packages:

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git nginx postgresql redis-server
```

- [ ] Create PostgreSQL database and user.
- [ ] Configure Redis password or bind Redis to a trusted local interface.
- [ ] Confirm firewall allows only SSH, HTTP, and HTTPS.

## 2. Fetch Code

- [ ] Clone or update repository:

```bash
cd /srv
git clone git@github.com:Daiobs/AnimeImages.git animeimages
cd /srv/animeimages
git fetch origin
git checkout main
git pull --ff-only origin main
```

- [ ] Pin the deployed version:

```bash
git checkout <deploy-commit>
git rev-parse HEAD
```

Record the commit in the deploy log.

## 3. Create Python Environment

- [ ] Create venv:

```bash
cd /srv/animeimages
python3 -m venv .venv
```

- [ ] Install dependencies:

```bash
/srv/animeimages/.venv/bin/python -m pip install --upgrade pip
/srv/animeimages/.venv/bin/pip install -r requirements.txt
/srv/animeimages/.venv/bin/pip install gunicorn
```

## 4. Configure Production Environment

- [ ] Create server-only environment file:

```bash
cp .env.example.production /srv/animeimages/.env.production
chmod 600 /srv/animeimages/.env.production
```

- [ ] Set real values in `.env.production`:

```text
DJANGO_ENV=production
DJANGO_SECRET_KEY=<real secret>
DJANGO_DEBUG=False
DJANGO_ALLOWED_HOSTS=animeimages.example.com
DJANGO_DATABASE_URL=postgresql://animeimages:<password>@127.0.0.1:5432/animeimages
DJANGO_CACHE_URL=redis://:<password>@127.0.0.1:6379/0
DJANGO_SECURE_SSL_REDIRECT=True
DJANGO_SESSION_COOKIE_SECURE=True
DJANGO_CSRF_COOKIE_SECURE=True
```

- [ ] Confirm first-launch safety switches:

```text
XHS_ALLOW_SERVER_SAVE=False
XHS_ALLOW_ABSOLUTE_SAVE_PATH=False
DOWNLOAD_ASYNC_TASKS_ENABLED=False
THIRD_PARTY_PROVIDER_ENABLED=True
THIRD_PARTY_PUBLIC_ACCESS_ENABLED=True
ULTRA_HD_PROVIDER_ENABLED=True
ULTRA_HD_PUBLIC_ACCESS_ENABLED=False
KUKUTOOL_PROVIDER_ENABLED=True
YTDLP_PROVIDER_ENABLED=True
YTDLP_MERGE_PROVIDER_ENABLED=True
GALLERYDL_PROVIDER_ENABLED=True
YOUTUBE_MERGE_PROVIDER_ENABLED=True
YOUTUBE_MERGE_PUBLIC_ACCESS_ENABLED=True
```

Provider capability can be configured at deploy time, but `ProviderRuntimeSetting` starts with all Provider modes disabled and `rollout_percent=0`; use the admin Provider console for staff/public-gray/public exposure.

Do not commit `.env.production`. Do not paste real secrets into issue comments, chat logs, or documentation.

## 5. Admin Observability

- [ ] Database migrations completed:

```bash
/srv/animeimages/.venv/bin/python manage.py migrate
```

- [ ] `/health/ready/` returns 200.
- [ ] `/admin/ops-dashboard/` can be opened by a superuser or staff with observability view permissions.
- [ ] `/admin/parse-logs/` can be opened by a user with `xhs_download.view_parserequestlog`.
- [ ] `/admin/provider-health-tests/` can be opened by a superuser or staff with `xhs_download.view_providerhealthtestrun`.
- [ ] A normal parse request writes a new `ParseRequestLog`.
- [ ] A normal download request writes a new `DownloadRequestLog`.
- [ ] Provider setting changes write a new `ProviderAdminAuditLog`.
- [ ] Running one Provider health test writes a new `ProviderHealthTestRun`.
- [ ] Optional Provider smoke URLs are configured from `/admin/provider-health-tests/`, not `.env.production`.
- [ ] Browser/network response headers include `X-Request-ID`.
- [ ] `journalctl` can find the same request id:

```bash
journalctl -u animeimages --since "1 hour ago" | grep '<request_id>'
```

- [ ] Nginx forwards request IDs and access logs include them:

```nginx
proxy_set_header X-Request-ID $request_id;

log_format animeimages '$remote_addr - $request_id [$time_local] '
                       '"$request" $status $body_bytes_sent '
                       '"$http_referer" "$http_user_agent"';
access_log /var/log/nginx/animeimages.access.log animeimages;
```

- [ ] `prune_observability_logs --dry-run` runs successfully:

```bash
/srv/animeimages/.venv/bin/python manage.py prune_observability_logs --days 30 --dry-run
```

- [ ] Old parse/download logs are pruned by cron:

```bash
0 3 * * * /srv/animeimages/.venv/bin/python /srv/animeimages/manage.py prune_observability_logs --days 30
```

See `docs/admin-observability.md` for request_id tracing, privacy guarantees, and daily checks.

## 6. Download Cost Guard

- [ ] `/admin/console/` can be opened by a superuser or staff with any observability view permission.
- [ ] `/admin/rollout-guard/` can be opened by a superuser or staff with any observability view permission.
- [ ] `/admin/console/` shows today's download count, success rate, actual traffic, failed count, blocked/rate-limited count, 24h traffic, and 7d traffic.
- [ ] `/admin/rollout-guard/` shows read-only Provider rollout advice and does not change Provider settings.
- [ ] Download settings can be opened from `/admin/console/`.
- [ ] Confirm initial download cost limits in `DownloadRuntimeSetting`:

```text
single_archive_bytes_limit = 536870912      # 500 MB, adjust for server bandwidth
single_video_bytes_limit = 314572800        # 300 MB, adjust for test phase
visitor_video_download_enabled = True       # set False if public video traffic is risky
staff_large_download_exempt = False
large_download_warning_bytes = 104857600    # 100 MB
```

- [ ] Confirm `global_daily_bytes_limit` is set to a value that protects the VPS bandwidth plan, or intentionally left as `0` for no limit.
- [ ] Trigger one normal ZIP download and confirm a `DownloadRequestLog` record appears with `actual_bytes`.
- [ ] Trigger one smart image download with `download_mode=auto` and confirm the response can return `route_type=direct`.
- [ ] Confirm `/admin/console/` shows the last 24h download routing section. If no smart downloads have happened yet, it should show `暂无路由数据`.
- [ ] Confirm `/admin/download-logs/` shows the `route_type` column and can filter by `route_type=direct` or `route_type=zip`.
- [ ] On a mobile user-agent smoke test, confirm the result page explains that ZIP files usually go to the browser download directory or system Files app and does not promise automatic album saving.
- [ ] If testing cost guard manually, lower `single_archive_bytes_limit` temporarily and confirm blocked downloads return `X-Error-Code: SINGLE_ARCHIVE_SIZE_LIMIT`.
- [ ] If public video traffic should be paused, set `visitor_video_download_enabled=False` and confirm visitor video downloads return `X-Error-Code: VISITOR_VIDEO_DOWNLOAD_DISABLED`.
- [ ] Confirm staff large-file exemption is still `False` for first launch.

See `docs/download-cost-guard.md` for traffic-cost behavior, error codes, warning thresholds, and rollback steps. See `docs/download-routing-engine.md` for route types, smart download activation, mobile notes, admin observability, and rollback.

## 7. Run Django Checks

Load the production environment before running these commands:

```bash
cd /srv/animeimages
set -a
. /srv/animeimages/.env.production
set +a
```

- [ ] Basic check:

```bash
/srv/animeimages/.venv/bin/python manage.py check
```

- [ ] Deployment check:

```bash
/srv/animeimages/.venv/bin/python manage.py check --deploy
```

- [ ] Migration dry-run:

```bash
/srv/animeimages/.venv/bin/python manage.py makemigrations --check --dry-run
```

- [ ] Apply migrations:

```bash
/srv/animeimages/.venv/bin/python manage.py migrate
```

- [ ] Collect static assets:

```bash
/srv/animeimages/.venv/bin/python manage.py collectstatic --noinput
```

- [ ] Run tests:

```bash
/srv/animeimages/.venv/bin/python manage.py test --noinput
```

- [ ] Provider health remains closed or staff-only:

```bash
/srv/animeimages/.venv/bin/python manage.py inspect_third_party_provider --health --json
```

Accept only `rollout.status=disabled` or `rollout.status=staff_only` for first launch. Do not treat `public_candidate` as automatic approval.

## 8. Configure Gunicorn and systemd

- [ ] Create `/etc/systemd/system/animeimages.service` using the sample from `docs/production-deploy-baseline.md`.
- [ ] Verify placeholders are replaced:

```text
/srv/animeimages
/srv/animeimages/.venv/bin/gunicorn
animeimages.example.com
```

- [ ] Start service:

```bash
sudo systemctl daemon-reload
sudo systemctl enable animeimages
sudo systemctl start animeimages
sudo systemctl status animeimages --no-pager
```

- [ ] Inspect logs:

```bash
journalctl -u animeimages -n 100 --no-pager
```

## 9. Configure Nginx and HTTPS

- [ ] Create `/etc/nginx/sites-available/animeimages`.
- [ ] Link it:

```bash
sudo ln -s /etc/nginx/sites-available/animeimages /etc/nginx/sites-enabled/animeimages
```

- [ ] Test Nginx config:

```bash
sudo nginx -t
```

- [ ] Reload Nginx:

```bash
sudo systemctl reload nginx
```

- [ ] Configure HTTPS certificate.
- [ ] Confirm HTTP redirects to HTTPS only after certificate works.

## 10. Health Checks

- [ ] Liveness:

```bash
curl -i https://animeimages.example.com/health/live/
```

- [ ] Readiness:

```bash
curl -i https://animeimages.example.com/health/ready/
```

Readiness must return HTTP 200 and include both cache and database components as true.

## 11. First Launch Smoke

- [ ] Home page loads.
- [ ] Xiaohongshu basic link parses.
- [ ] Douyin basic link parses.
- [ ] Bilibili basic link parses.
- [ ] Image preview loads.
- [ ] ZIP download succeeds.
- [ ] Server-side save controls are not visible or not usable for public users.
- [ ] Async task endpoints are not used when `DOWNLOAD_ASYNC_TASKS_ENABLED=False`.
- [ ] Third-party provider public entry remains disabled.
- [ ] Provider health does not expose a public launch path.
- [ ] Open `/admin/console/` and confirm traffic usage is normal after the first ZIP download.
- [ ] Open `/admin/rollout-guard/` and confirm Provider advice is `safe_for_staff`, `safe_for_1_percent_gray`, `caution`, or `do_not_public`.
- [ ] Open `/admin/provider-health-tests/` and run `health_ready`.
- [ ] Save stable smoke URLs in `/admin/provider-health-tests/`; these are database runtime settings, not `.env.production` values.
- [ ] Run "一键测试" and confirm each item is `success`, `skipped`, or intentionally `unavailable`.
- [ ] If a Douyin test URL is saved in the panel, confirm `douyin_native_parse` is not `failed`.
- [ ] Keep `douyin_kukutool_ultra`, `youtube_merge`, `third_party_ytdlp`, and `gallery_dl` staff-only until their test records are `success` or intentionally `skipped/unavailable`.
- [ ] Use the test run `request_id` to confirm related logs can be found in Django logs or `journalctl`.

## 12. Rollback

If deployment fails:

- [ ] Record failing commit and error.
- [ ] Switch to previous known-good commit:

```bash
cd /srv/animeimages
git checkout <previous-good-commit>
```

- [ ] Reinstall requirements if they changed:

```bash
/srv/animeimages/.venv/bin/pip install -r requirements.txt
```

- [ ] Run safe migrations only if required and reversible.
- [ ] Re-run `collectstatic --noinput`.
- [ ] Restart service:

```bash
sudo systemctl restart animeimages
sudo systemctl reload nginx
```

- [ ] Re-check `/health/live/`, `/health/ready/`, home page, and one stable adapter smoke.
