# Production Infrastructure Baseline v1

## Request Architecture

The production request path is:

```text
Internet -> Nginx TLS termination -> Gunicorn 127.0.0.1:8000 -> Django
```

Gunicorn runs as `www-data:www-data` from `/srv/animeimages`; the checkout is
owned by `ubuntu:ubuntu`. Nginx serves collected static files directly from
`/srv/animeimages/staticfiles/` and forwards dynamic routes to Django.

Ordinary direct-capable media is a separate browser data plane:

```text
Provider/CDN -> Browser Service Worker -> Browser Download Manager -> Disk
```

AnimeImages resolves, authorizes, and signs the browser handoff. It does not
relay ordinary media body bytes. Explicit Django endpoints for previews,
continuation, ZIP/task output, merge, and other server-required artifacts are
different resource-processing contracts and must not become an ordinary-video
fallback.

## Nginx Ownership

AnimeImages owns these canonical files:

- `ops/nginx/sites/download.cosflow.icu.conf.template`
- `ops/nginx/snippets/animeimages-v1-media.conf`
- `ops/nginx/manifest.json`

The site template maps to `/etc/nginx/sites-available/animeimages`; its single
server-IP placeholder is rendered per host. The media snippet maps byte-for-byte
to `/etc/nginx/snippets/animeimages-v1-media.conf`.

External dependencies are:

- Ubuntu/Nginx: `/etc/nginx/nginx.conf`, `mime.types`, modules, and global
  include directories;
- Certbot: certificate issuance, private keys, TLS options, DH parameters, and
  managed redirect/TLS lines;
- systemd: `/etc/systemd/system/animeimages.service` and effective process
  state;
- application runtime: `/srv/animeimages/.env.production` and persisted runtime
  database settings.

Certificate and private-key contents are never repository artifacts.

## Path Contracts

- `/`: Django application proxy, including homepage and readiness routes.
- `/ordinary-media-download-sw.js`: root-scoped Service Worker response from
  Django; it is outside `/media/` and receives `Service-Worker-Allowed: /`.
- `/static/`: Nginx alias to collected static files.
- `/media/browser-download-telemetry/`: exact Django control-plane route.
- `/media/`: protected catch-all returning 404.

Telemetry GET returning 405 is expected: Nginx successfully reached Django and
Django enforced POST-only behavior. A no-CSRF POST is rejected by Django. Nginx
must not reproduce CSRF, schema, rate-limit, payload-size, or failure-enum logic.

## Sources Of Truth

| Concern | Source of truth |
| --- | --- |
| Desired AnimeImages Nginx files | Repository `ops/nginx/` |
| Effective Nginx runtime | Authorized `nginx -T` plus active installed files |
| Application deployment rollback | Immutable `checkpoint/production-*` ref |
| Web process topology | systemd unit and `systemctl show` |
| Runtime application values | Production environment and runtime database |

`NGINX_CONFIG_DRIFT` means a project-owned installed file does not match the
canonical exact or rendered repository file, or a route invariant fails.
Certbot-managed material, distro-global configuration, unrelated vhosts, and
documented template values are outside that comparison.

Follow `ops/nginx/README.md` for read-only validation, reviewed manual
installation, health checks, and rollback. Repository merge does not modify or
reload production.

## Recovery Dependencies

A replacement host needs Ubuntu Nginx, a valid Certbot-managed certificate for
the public domain, the AnimeImages systemd unit, the application checkout and
virtual environment, production environment configuration, Redis, PostgreSQL,
and collected static files. Install and validate these dependencies separately;
do not copy credentials or certificate material from Git.

Future inventory may cover Redis, PostgreSQL, firewall, Cloud-init, DNS,
Certbot automation, and host recovery. Those systems are not managed by this
Nginx baseline.
