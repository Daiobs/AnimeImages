# Production Deploy Baseline

This document defines the first production baseline for deploying AnimeImages to a Hong Kong VPS. The goal is a small, stable launch that serves the existing dedicated adapters and keeps Provider exposure controlled through the admin runtime gate.

## Target Architecture

Recommended first production shape:

- Ubuntu LTS VPS in Hong Kong.
- Python virtual environment under `/srv/animeimages/.venv`.
- Gunicorn serving `djangoProject.wsgi:application`.
- Nginx as the public reverse proxy and static file server.
- PostgreSQL as the relational database.
- Redis as Django cache, rate-limit state, provider cache state, and readiness dependency.
- HTTPS through Certbot, a managed certificate, or the VPS provider's certificate workflow.

Current production topology audited for this runbook:

```text
Gunicorn user:  www-data
Gunicorn bind:  127.0.0.1:8000
Nginx upstream: http://127.0.0.1:8000
Checkout owner: ubuntu:ubuntu
```

Production does not currently use `/run/animeimages/gunicorn.sock`. The Unix-socket model remains a future recommended hardening target, not the deployed topology.

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

Do not open public third-party provider entrypoints for the first launch. Provider smoke remains a staging/staff task after the VPS baseline is healthy. `.env` Provider values should describe capability and dependencies; `/admin/provider-settings/` decides whether a capability is disabled, staff-only, gray, or public.

## Development vs Production

Local development can keep convenience defaults such as SQLite, `DEBUG=True`, server-side save paths, local async download tasks, and optional local smoke toggles.

Production must use explicit infrastructure configuration. The production process should read a server-side environment file such as `/srv/animeimages/.env.production` or systemd `EnvironmentFile`, not a committed secret file.

## Git Ownership and Deployment Hygiene

The production Git working tree must have one owner:

```text
/srv/animeimages -> ubuntu:ubuntu
```

Do not run `git fetch`, `git pull`, `git reset`, `git checkout`, or other Git commands as `root` inside `/srv/animeimages`. Mixing `root` and `ubuntu` ownership can leave `.git/index.lock` or index files unreadable by the deploy user.

### Pinned Tools and Permission Gates

The permission-fix deployment contract below requires the reviewed fixed tools;
an older copy of `deploy.sh` does not implement these gates. This draft is not
authorization to deploy and does not claim production validation.

Run as `ubuntu` from a separate, clean Git checkout outside `APP_DIR`. Require
`DEPLOY_TOOL_SHA` to match that tool checkout's HEAD and `DEPLOY_TARGET_SHA` to
be the full intended application commit SHA. Record both; never execute the
deployment controller from the application tree it will replace.

Preparation example (replace the paths and both SHA placeholders):

```bash
cd /path/to/reviewed-deploy-tools
APP_DIR=/srv/animeimages \
DEPLOY_TOOL_SHA=<full-reviewed-tool-commit> \
DEPLOY_TARGET_SHA=<full-release-commit> \
DEPLOY_STATE_DIR=/path/to/existing-private-release-state \
WEB_USER=www-data STATIC_USER=<actual-nginx-worker-user> \
STATIC_GROUP=<actual-nginx-worker-group> \
CODE_UMASK=0022 DEPLOY_PREPARE_ONLY=1 ./scripts/deploy.sh
```

Keep the existing ownership, branch, clean-tree, settings and readable-env
preconditions. Additionally:

- `WEB_USER` must match the systemd service's actual `User`, be non-root and
  differ from the deploy user. Set `STATIC_USER` explicitly to the actual Nginx
  worker identity; do not infer it from the deploy or Web account. The Web probe
  uses systemd `Group`, falling back to the Web user's primary group when unset.
  Set `STATIC_GROUP` to the actual Nginx worker group; its default is the static
  user's primary group. Operator verification of the Nginx identity is required;
  the script does not discover it from Nginx configuration. Supplemental groups
  and service sandbox restrictions still need to match the intended deployment.
- Permission probes run as those real identities using `sudo -n -u ... -g ...` and require
  existing, approved identity-execution privileges for the actual probe command.
  The previously observed restart-only passwordless permissions are insufficient;
  the next release needs approved Web/static identity execution before scheduling
  maintenance. Do not expand sudoers or substitute deploy-user checks to bypass
  failures. Preparation exercises both identities: Web against candidate and
  existing code/runtime paths, and the static identity against the candidate's
  `static/` directory. Authorization failure blocks before checkout replacement.
- `DEPLOY_STATE_DIR` must already exist, be deploy-owned and mode `0700`.
  Keep recovery logs and secret artifacts private (`0600` files). Never apply
  the application's permissions policy to this directory or the env/key files.
  The script creates a private release journal recording tool, target and previous
  SHA, plus `code-before.tar.gz`. This limited code backup uses current HEAD's
  tracked-path list and preserves those paths' actual working-tree bytes and
  modes; it is not a reconstructed Git archive. It runs before candidate checks
  or checkout replacement. A Git-listing or tar failure aborts deployment; any
  partial archive from a failed run is invalid and must never be restored.
  Archive existence alone is not proof of success, and successful creation is
  not a checksum verification or restore drill. Inspect and verify recovery
  material before use; preserved bad permissions are not automatically repaired.
  This is not a general backup engine: it excludes the database and untracked
  collected static files, env/key material and virtualenv. Independently verified
  database/static backups and required dependency/env/key recovery material remain
  prerequisites for apply; this script does not enforce their verification.
- Use `CODE_UMASK=0022` for the current disjoint `ubuntu`/`www-data` identities.
  `0027` is allowed only with a preprovisioned shared read group and setgid
  directories. It is not a way to retrofit access: no chmod of existing paths.
- Archive the pinned candidate into a fresh directory under a separate stage
  parent (default `/tmp`), with new directory mode `2755` or `2750` respectively.
  Check ancestor traversal and code, templates and virtualenv readability and
  non-writability as the Web identity. A private recovery directory is not a
  suitable Web-readable stage parent.
- `DEPLOY_PREPARE_ONLY=1` fetches the branch, creates a temporary candidate archive
  and private journal/code backup, and checks candidate static assets plus both candidate and
  existing code/runtime paths. Static file links escaping the checked root fail.
  It does not switch the application checkout or run dependency installation,
  migrations, collectstatic or restart. It can remove a detected stale Git lock;
  it is not a zero-write Git inspection. The temporary stage is removed on exit.
  A successful staged check does not replace checks on the final live paths.

For an authorized apply, use the same pinned tools and settings with
`DEPLOY_PREPARE_ONLY=0`. The operator must first stop Web through the approved
maintenance procedure. Apply requires both `ActiveState=inactive` and
`MainPID=0`; the script never stops the service itself. `activating` with PID 0
is not stopped. Check final code permissions immediately after reset, before
migrations, and again after dependency work. Check collected static assets as
`STATIC_USER` before the existing restart step. Existing incorrect `0600`
application files must fail the checks, not be automatically repaired.

Do not use blanket recursive chown/chmod on `/srv/animeimages`. Investigate the
specific failed path, its ancestors, owner, group and consumer identity; any
repair requires a separately reviewed, narrowly scoped operator action. Static
files must remain readable by Nginx, and application assets must not be writable
by their serving identities.

The 2026-10-11 incident was caused by the task-created wrapper's global umask
`077` being inherited by Git reset. Direct observations included `0600` on
`xhs_download/quality_route_views.py`, `xhs_download/upstream_routes.py` and
`templates/base.html`; `staticfiles/js/provider_lab.js` was `0644`. Do not infer
other exact modes from those examples. That deployment wrapper did not call
pip; earlier preparation ran `pip check` and a zero-install pip dry-run. The
failure was an operational permissions error, not evidence of dependency changes.

Production configuration belongs in `.env.production` or systemd environment variables. Do not edit `djangoProject/settings.py` on the server. A CI guard blocks `djangoProject/settings.py` changes on feature branches so production-only settings do not leak into code review.

When a `.env.production` value contains spaces, quote it so shell-based deploy commands can load it safely:

```text
YTDLP_BINARY="/srv/animeimages/.venv/bin/python -m yt_dlp"
GALLERYDL_BINARY="/srv/animeimages/.venv/bin/python -m gallery_dl"
```

Important production posture:

```text
DJANGO_DEBUG=False
XHS_ALLOW_SERVER_SAVE=False
XHS_ALLOW_ABSOLUTE_SAVE_PATH=False
DOWNLOAD_ASYNC_TASKS_ENABLED=False
ULTRA_HD_PROVIDER_ENABLED=True
ULTRA_HD_PUBLIC_ACCESS_ENABLED=False
KUKUTOOL_PROVIDER_ENABLED=True
THIRD_PARTY_PROVIDER_ENABLED=True
YTDLP_PROVIDER_ENABLED=True
YTDLP_MERGE_PROVIDER_ENABLED=True
GALLERYDL_PROVIDER_ENABLED=True
YOUTUBE_MERGE_PROVIDER_ENABLED=True
```

The regular Provider capability gates can be enabled after dependencies are installed, but the initial `ProviderRuntimeSetting` database values stay conservative: all modes are `disabled` and `rollout_percent=0`. Move them through `staff`, `public_gray`, and `public` from the admin console only after smoke proves the exact runtime, timeout, output-size, final-host validation, and recovery behavior. See `docs/provider-runtime-gates.md`.

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
  --worker-class gthread \
  --workers 2 \
  --threads 4 \
  --timeout 60 \
  --access-logfile - \
  --error-logfile -
```

Keep the worker count conservative. Web request slots and Provider concurrency
are separate controls. TianTianJieXi currently uses a Redis-backed counter with
an effective production intent of two; observe CPU, memory, Redis latency,
fallback rate, and request latency before changing either limit.

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
UMask=0007
ExecStart=/srv/animeimages/.venv/bin/gunicorn djangoProject.wsgi:application --bind 127.0.0.1:8000 --worker-class gthread --workers 2 --threads 4 --timeout 60 --access-logfile - --error-logfile -
Restart=on-failure
RestartSec=5
PrivateTmp=true
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
```

The repository-controlled unit is `ops/systemd/animeimages.service`. Installing
or restarting it is a separate production operation; merging the template does
not alter the live service.

Gunicorn runs as `www-data`. Keep the application checkout owned by `ubuntu:ubuntu`; the `ubuntu` account remains responsible for Git fetch/reset and deployment operations. The Web service only needs read access to application files and approved local Git refs.

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
upstream animeimages_app {
    server 127.0.0.1:8000;
}

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
        proxy_pass http://animeimages_app;
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

## Future Recommended Hardening Target (Not Currently Deployed)

A future infrastructure PR may move Gunicorn from local TCP to a systemd-managed Unix socket. The following settings are reference material only and are not active on the current production server:

```ini
[Service]
RuntimeDirectory=animeimages
RuntimeDirectoryMode=0750
ExecStart=/srv/animeimages/.venv/bin/gunicorn djangoProject.wsgi:application --bind unix:/run/animeimages/gunicorn.sock --pid /run/animeimages/gunicorn.ctl --workers 2 --timeout 60
```

The matching future Nginx upstream would be:

```nginx
upstream animeimages_app {
    server unix:/run/animeimages/gunicorn.sock;
}
```

Adopting this target requires a separate systemd/Nginx rollout, socket permission checks, restart validation, and readiness smoke tests. This runbook does not imply that those changes have already been deployed.

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

Production first launch separates Provider capability from Provider exposure:

```text
THIRD_PARTY_PROVIDER_ENABLED=True
THIRD_PARTY_KUKUTOOL_PROVIDER_ENABLED=True
YTDLP_PROVIDER_ENABLED=True
YTDLP_MERGE_PROVIDER_ENABLED=True
GALLERYDL_PROVIDER_ENABLED=True
YOUTUBE_MERGE_PROVIDER_ENABLED=True
ULTRA_HD_PROVIDER_ENABLED=True
ULTRA_HD_PUBLIC_ACCESS_ENABLED=False
KUKUTOOL_PROVIDER_ENABLED=True
```

The database runtime defaults remain closed: `third_party_mode=disabled`, `youtube_merge_mode=disabled`, `douyin_ultra_mode=disabled`, and `rollout_percent=0`. After launch, provider work can continue in staging or staff-only smoke:

1. Install and verify one provider capability at a time.
2. Verify `inspect_third_party_provider --health --json`.
3. Verify parse, preview, signed manifest download, content type, final host, output size, and failure recovery.
4. Move the admin runtime mode from `disabled` to `staff`.
5. Promote through `public_gray` and `public` only after logs show stable latency and failure behavior.

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

- Previous and target application Git commits, and the separate tool commit.
- Python and deployment-tool versions.
- Installed package snapshot.
- Migration state.
- Static asset collection time.
- Environment file checksum without secret values.

If an apply fails after reset, stop the deployment sequence and keep Web in
maintenance; do not restart from the failure handler. Preserve the recorded old
SHA, failed step and private logs. If Web has already restarted, obtain an
operator-confirmed stop before changing files again.

The operator restores known-good code and the verified static backup, preserving
its exact modes and contents, then repeats final identity-based permission
checks before an authorized restart. Umask only affects newly created files;
reset alone does not repair every existing bad mode. Restore dependencies only
when they changed, using the recorded recovery material, not an unreviewed
upgrade. Preserve env and key permissions.

Do not automatically reverse migrations or overwrite the live database with a
backup. Confirm old-code compatibility with the upgraded schema and retain new
tables/data; a database rollback needs its own reviewed recovery procedure.
After restart, verify `/health/live/`, `/health/ready/`, home page and worker
stability. Real provider parsing, downloads, mail and Daily execution remain
separately authorized smoke tests, not permission checks.
