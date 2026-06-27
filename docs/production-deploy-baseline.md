# Production Deploy Baseline

This document defines the first production baseline for deploying AnimeImages to a Hong Kong VPS. The goal is a small, stable launch that serves the existing dedicated adapters and keeps all third-party provider expansion disabled by default.

## Target Architecture

Recommended first production shape:

- Ubuntu LTS VPS in Hong Kong.
- Python virtual environment under `/srv/animeimages/.venv`.
- Gunicorn serving `djangoProject.wsgi:application`.
- Nginx as the public reverse proxy and static file server.
- PostgreSQL as the relational database.
- Redis as Django cache, rate-limit state, provider cache state, and readiness dependency.
- HTTPS through Certbot, a managed certificate, or the VPS provider's certificate workflow.

Netlify is not a good target for the current full Django backend. Netlify can host a future static frontend, but this project currently needs a long-running Python process, Django views, database connections, Redis-backed limits, file downloads, and server-side ZIP responses.

## First Launch Scope

First launch should expose only the stable dedicated adapter paths:

- Home page and shared parse form.
- Xiaohongshu basic image/video parsing.
- Douyin basic native parsing.
- Bilibili basic public parsing.
- ZIP download response.
- Image preview proxy with production limits.
- `/health/live/` and `/health/ready/`.

Do not open public third-party provider entrypoints for the first launch. Provider smoke remains a staging/staff task after the VPS baseline is healthy.

## Development vs Production

Local development can keep convenience defaults such as SQLite, `DEBUG=True`, server-side save paths, local async download tasks, and optional local smoke toggles.

Production must use explicit infrastructure configuration. The production process should read a server-side environment file such as `/srv/animeimages/.env.production` or systemd `EnvironmentFile`, not a committed secret file.

Important production defaults:

```text
DJANGO_DEBUG=False
XHS_ALLOW_SERVER_SAVE=False
XHS_ALLOW_ABSOLUTE_SAVE_PATH=False
DOWNLOAD_ASYNC_TASKS_ENABLED=False
THIRD_PARTY_PROVIDER_ENABLED=False
THIRD_PARTY_PUBLIC_ACCESS_ENABLED=False
THIRD_PARTY_STAFF_ONLY=True
ULTRA_HD_PROVIDER_ENABLED=False
ULTRA_HD_PUBLIC_ACCESS_ENABLED=False
KUKUTOOL_PROVIDER_ENABLED=False
YTDLP_PROVIDER_ENABLED=False
YTDLP_MERGE_PROVIDER_ENABLED=False
GALLERYDL_PROVIDER_ENABLED=False
YOUTUBE_MERGE_PROVIDER_ENABLED=False
YOUTUBE_MERGE_PUBLIC_ACCESS_ENABLED=False
```

The provider layer can be enabled only after staging or staff-only smoke proves the exact runtime, timeout, output-size, final-host validation, and recovery behavior. A health result of `public_candidate` is technical evidence only; it is not approval to expose anonymous public traffic.

## Required Production Environment

Start from `.env.example.production`, then create a real server-side file with secrets replaced.

Minimum values that must be set manually before launch:

```text
DJANGO_ENV=production
DJANGO_SECRET_KEY=<long random secret>
DJANGO_DEBUG=False
DJANGO_ALLOWED_HOSTS=animeimages.example.com
DJANGO_DATABASE_URL=postgresql://animeimages:<db-password>@127.0.0.1:5432/animeimages
DJANGO_CACHE_URL=redis://:<redis-password>@127.0.0.1:6379/0
DJANGO_SECURE_SSL_REDIRECT=True
DJANGO_SESSION_COOKIE_SECURE=True
DJANGO_CSRF_COOKIE_SECURE=True
DJANGO_SECURE_HSTS_SECONDS=31536000
DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS=False
DJANGO_SECURE_HSTS_PRELOAD=False
```

Never commit the real `DJANGO_SECRET_KEY`, PostgreSQL password, Redis password, API keys, cookies, or provider credentials.

## Gunicorn Example

Install Gunicorn in the virtual environment:

```bash
/srv/animeimages/.venv/bin/pip install gunicorn
```

Example command:

```bash
cd /srv/animeimages
set -a
. /srv/animeimages/.env.production
set +a
/srv/animeimages/.venv/bin/gunicorn djangoProject.wsgi:application \
  --bind 127.0.0.1:8000 \
  --workers 2 \
  --timeout 60 \
  --access-logfile - \
  --error-logfile -
```

Keep the worker count conservative at first. Increase only after observing CPU, memory, Redis latency, and download behavior.

## systemd Example

Example unit file path:

`/etc/systemd/system/animeimages.service`

```ini
[Unit]
Description=AnimeImages Django application
After=network.target postgresql.service redis-server.service

[Service]
Type=simple
User=www-data
Group=www-data
WorkingDirectory=/srv/animeimages
EnvironmentFile=/srv/animeimages/.env.production
ExecStart=/srv/animeimages/.venv/bin/gunicorn djangoProject.wsgi:application --bind 127.0.0.1:8000 --workers 2 --timeout 60
Restart=on-failure
RestartSec=5
PrivateTmp=true
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
```

Operational commands:

```bash
sudo systemctl daemon-reload
sudo systemctl enable animeimages
sudo systemctl start animeimages
sudo systemctl status animeimages --no-pager
journalctl -u animeimages -f
```

## Nginx Example

Example site path:

`/etc/nginx/sites-available/animeimages`

```nginx
server {
    listen 80;
    server_name animeimages.example.com;

    location /.well-known/acme-challenge/ {
        root /var/www/html;
    }

    location /static/ {
        alias /srv/animeimages/staticfiles/;
        access_log off;
        expires 7d;
    }

    location /media/ {
        return 404;
    }

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 120s;
        client_max_body_size 20m;
    }
}
```

After HTTPS is configured, redirect HTTP to HTTPS and keep Django secure cookie settings enabled. Do not expose the whole `media/` directory as a public static folder. ZIP downloads should be returned through controlled Django responses, not long-lived public media URLs.

## Static, Media, and Downloads

Run `collectstatic` during deployment and let Nginx serve `/static/` from `/srv/animeimages/staticfiles/`.

For first launch:

- Keep server-side save disabled.
- Keep in-memory async download tasks disabled.
- Use synchronous temporary ZIP downloads.
- Keep preview and download rate limits enabled.
- Keep `DOWNLOAD_TRUST_X_FORWARDED_FOR=False` unless Nginx is the only trusted proxy and overwrites the header.

Long-running background work is a later phase. Celery/RQ plus Redis can be introduced after production traffic shows where progress reporting and durable task state are needed.

## Provider Policy

Production first launch must keep these disabled:

```text
THIRD_PARTY_PROVIDER_ENABLED=False
THIRD_PARTY_PUBLIC_ACCESS_ENABLED=False
THIRD_PARTY_KUKUTOOL_PROVIDER_ENABLED=False
YTDLP_PROVIDER_ENABLED=False
YTDLP_MERGE_PROVIDER_ENABLED=False
GALLERYDL_PROVIDER_ENABLED=False
YOUTUBE_MERGE_PROVIDER_ENABLED=False
YOUTUBE_MERGE_PUBLIC_ACCESS_ENABLED=False
ULTRA_HD_PROVIDER_ENABLED=False
ULTRA_HD_PUBLIC_ACCESS_ENABLED=False
KUKUTOOL_PROVIDER_ENABLED=False
```

After launch, provider work can continue in staging or staff-only smoke:

1. Enable one provider at a time.
2. Verify `inspect_third_party_provider --health --json`.
3. Verify parse, preview, signed manifest download, content type, final host, output size, and failure recovery.
4. Keep `THIRD_PARTY_STAFF_ONLY=True`.
5. Promote only after logs show stable latency and failure behavior.

## Launch Smoke Test

Run these after deployment:

- Home page loads over HTTPS.
- Xiaohongshu basic parsing returns expected image/video assets.
- Douyin basic parsing returns native 1080p or platform-public quality assets.
- Bilibili basic parsing returns public video/cover assets.
- ZIP download works and contains selected assets.
- Image preview loads through the preview proxy.
- `/health/live/` returns HTTP 200.
- `/health/ready/` returns HTTP 200 with cache and database components true.
- `inspect_third_party_provider --health --json` reports rollout `disabled` or `staff_only`.
- No provider health result is treated as public launch approval when rollout is `public_candidate`.

## Rollback Baseline

Every deployment should record:

- Git commit.
- Python version.
- Installed package snapshot.
- Migration state.
- Static asset collection time.
- Environment file checksum without secret values.

Rollback steps:

1. Switch the server checkout to the previous known-good commit.
2. Reinstall requirements if needed.
3. Run migrations only if the rollback path is safe.
4. Run `collectstatic --noinput`.
5. Restart Gunicorn/systemd.
6. Confirm `/health/live/`, `/health/ready/`, home page, and one stable adapter smoke.
