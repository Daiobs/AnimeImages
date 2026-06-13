# Deployment Guide

## Recommended Target

This is a Django backend project. Deploy it to a Python WSGI/ASGI host such as Render, Railway, Fly.io, or a VPS with Gunicorn and Nginx.

Netlify can host a future static frontend, but it is not the right target for the full Django backend.

## Required Environment Variables

- `DJANGO_SECRET_KEY`: long random production secret
- `DJANGO_DEBUG=False`
- `DJANGO_ALLOWED_HOSTS`: comma-separated hostnames
- `DJANGO_CACHE_URL`: shared Redis URL used by rate limits and validated provider caches
- `DJANGO_SECURE_SSL_REDIRECT=True` when HTTPS is available
- `DJANGO_SESSION_COOKIE_SECURE=True` when HTTPS is available
- `DJANGO_CSRF_COOKIE_SECURE=True` when HTTPS is available
- `DJANGO_SECURE_HSTS_SECONDS=31536000` only after confirming HTTPS for the full site
- `DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS=True` only if all subdomains are HTTPS-only
- `DJANGO_SECURE_HSTS_PRELOAD=True` only if the domain is ready for browser preload

## Build Commands

```bash
python -m pip install -r requirements.txt
python manage.py migrate
python manage.py collectstatic --noinput
python manage.py check --deploy
```

## Runtime

Use a production server instead of `runserver`.

Example:

```bash
gunicorn djangoProject.wsgi:application
```

Add `gunicorn` to dependencies when a deployment target is chosen.

Configure the deployment platform or load balancer with:

- `/health/live/` for process liveness. It does not contact external services.
- `/health/ready/` for traffic readiness. It verifies the configured Django cache/Redis and database with a real round trip.

Both endpoints disable response caching and expose only component availability, not connection strings or exception text. A failed dependency makes readiness return HTTP `503`.

## Database

SQLite is acceptable for local development. Production should use PostgreSQL or another managed relational database.

Before switching databases:

1. Add database configuration via environment variables.
2. Run migrations on a staging database.
3. Verify upload and media paths.

## Media Files

User uploads and downloaded files live under `media/` locally. For production, use persistent storage or object storage. Do not rely on ephemeral container disk for important uploads.

Django only appends the development `MEDIA_URL` route while `DEBUG=True`. In production, configure Nginx, private object storage, or another explicit media delivery layer. Do not expose the whole `media/` directory as a public fallback route.

The Xiaohongshu download endpoint returns a browser ZIP download instead of writing to arbitrary server paths. Keep this boundary in production; do not allow untrusted users to choose absolute server filesystem paths.

Recommended production settings:

```text
XHS_ALLOW_SERVER_SAVE=False
XHS_ALLOW_ABSOLUTE_SAVE_PATH=False
DOWNLOAD_ASYNC_TASKS_ENABLED=False
```

The in-memory background task implementation is intentionally local-only. With production async tasks disabled, the browser automatically uses the synchronous temporary ZIP response. Before re-enabling asynchronous progress in production, replace task state and execution with Redis plus Celery/RQ and store completed archives in a controlled temporary/object-storage location.

Public download endpoints use shared-cache counters for per-IP frequency, per-IP concurrency, daily request count, and daily output bytes. Configure the limits from `.env.example.production`. `DOWNLOAD_TRUST_X_FORWARDED_FOR` must remain false unless a trusted reverse proxy overwrites that header; otherwise clients can spoof their limiter identity.

The image preview proxy is also an outbound bandwidth path and has separate controls:

```text
PREVIEW_RATE_LIMIT_PER_MINUTE=60
PREVIEW_DAILY_REQUEST_LIMIT_PER_IP=1000
PREVIEW_GLOBAL_DAILY_REQUEST_LIMIT=10000
PREVIEW_DAILY_BYTES_LIMIT_PER_IP=1073741824
PREVIEW_GLOBAL_DAILY_BYTES_LIMIT=21474836480
```

These are initial safety defaults, not capacity guarantees. Tune them from observed traffic and server bandwidth. Redis failure makes protected preview requests fail closed with HTTP `503`.

Ultra-HD access has two operator switches:

```text
ULTRA_HD_PROVIDER_ENABLED=False
ULTRA_HD_PUBLIC_ACCESS_ENABLED=False
```

Keep both `ULTRA_HD_PUBLIC_ACCESS_ENABLED` and `KUKUTOOL_PROVIDER_ENABLED` disabled until KuKuTool latency, limits, download recovery, and protocol stability have been validated in staging. Production must provide Node.js 18 or newer through an explicit `KUKUTOOL_NODE_BINARY`; it does not search the system `PATH` or a user's Codex runtime.

Production Redis is also used for KuKuTool single-flight, short failure caching, validated results, and the global upstream concurrency counter. Keep `DJANGO_CACHE_SOCKET_CONNECT_TIMEOUT` and `DJANGO_CACHE_SOCKET_TIMEOUT` below the three-second foreground budget so a cache outage cannot pin a request. The current bounded thread pools are an interim deployment model. For sustained traffic, run the helper as a long-lived internal Node sidecar or move resolution to Celery/RQ so Django workers never own subprocess lifecycles.

TikHub and country-aware API routing are retained only as dormant research code. They do not affect the current synchronous ultra-HD path.

For local development only, these can be enabled to save files from the Django process to a server-side directory:

```text
XHS_ALLOW_SERVER_SAVE=True
XHS_ALLOW_ABSOLUTE_SAVE_PATH=True
XHS_DEFAULT_SERVER_SAVE_DIR=/Users/your-name/Downloads/xhs
```

## Rollback

Keep each deployed version tied to a Git commit. If deployment fails:

1. Revert to the previous known-good commit.
2. Re-run migrations only if rollback migrations are safe.
3. Confirm `check`, smoke URLs, and logs.
