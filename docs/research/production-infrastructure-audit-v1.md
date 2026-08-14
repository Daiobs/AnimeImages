# Production Infrastructure Audit v1

Audit date: 2026-08-14

This record captures a read-only audit of the accepted production
infrastructure. It does not represent a production configuration change.

## Application Baseline

- Production application SHA: `fc1f1368431fb2ff063231dfaa4f35a9c8154430`
- Canonical main at audit start: `fc1f1368431fb2ff063231dfaa4f35a9c8154430`
- Production checkpoint: `checkpoint/production-main-converged-v2`
- Production checkout: clean, `main...origin/main`, ahead/behind `0/0`

## Nginx Inventory

- Version: `nginx/1.24.0 (Ubuntu)`
- Main include root: `/etc/nginx/nginx.conf`
- Enabled site: `/etc/nginx/sites-enabled/animeimages`
- Site source: `/etc/nginx/sites-available/animeimages`
- Site SHA-256: `9fa9631b908a7d60ebda3fc764462ca608af11e5513d137c87b34a2d4f1eab24`
- Project media snippet: `/etc/nginx/snippets/animeimages-v1-media.conf`
- Media snippet SHA-256: `c934a642cba73088c11dea3c515bf17b8b8355a45c89cb8e87e98ba73b50973c`
- Installed managed-file ownership: `root:root 0644`
- Upstream: local Gunicorn at `127.0.0.1:8000`
- Static root: `/srv/animeimages/staticfiles/`, readable and owned by
  `ubuntu:ubuntu`
- TLS: Certbot-managed paths; certificate and key content were not read.

The distro `nginx.conf` includes modules, `mime.types`, `conf.d/*.conf`, and
`sites-enabled/*`. No project-specific `conf.d` file participated in the
AnimeImages chain. The AnimeImages site directly includes the single project
media snippet and Certbot's TLS options.

Non-root `nginx -T` could not read the Certbot certificate path. The include
graph was therefore audited file-by-file, and the prior root validation plus
the active service were checked without changing or reloading Nginx.

## Ownership Classification

| Classification | Paths |
| --- | --- |
| `ANIMEIMAGES_OWNED` | Site vhost, media snippet, enabled-site symlink |
| `SHARED_SERVER_CONFIG` | Global include directories and logging defaults |
| `DISTRO_MANAGED` | `nginx.conf`, `mime.types`, enabled modules |
| `CERTBOT_MANAGED` | Certificate paths, TLS options, DH parameters, redirect/TLS lines |
| `THIRD_PARTY_MANAGED` | None in the AnimeImages request chain |
| `UNKNOWN` | None found |

## Effective Contracts

- Public telemetry GET: 405
- Public telemetry no-CSRF POST: 403
- Valid-CSRF invalid schema: 400
- Valid synthetic telemetry: 204
- Generic `/media/` test: 404
- Local/public readiness, homepage, and anonymous Batch page: 200
- Nginx and AnimeImages services: active/running
- AnimeImages `NRestarts`: 0

The telemetry location is an exact control-plane exception. Other media routes
are explicit Django APIs; the broad `/media/` catch-all remains protected.
There is no Provider/CDN `proxy_pass` in the AnimeImages configuration.

## Systemd Comparison

The production unit at `/etc/systemd/system/animeimages.service` is
byte-identical to `ops/systemd/animeimages.service` with SHA-256
`12d32dbc2f768fc01543d6fc1528c077c398202239e4b1c1e1e38b25714e1f4b`.
Effective runtime remains `www-data:www-data`, working directory
`/srv/animeimages`, Gunicorn `gthread`, two workers, four threads, timeout 60,
and bind `127.0.0.1:8000`.

Result: `SYSTEMD_BASELINE_MATCH`.

## Repository Reconciliation

The canonical media snippet is byte-identical to production. Rendering the
site template with the audited production server IP is also byte-identical to
the installed site file. The IP is not recorded in this document or manifest;
it remains an explicit host render value.

The root-only rollback backup created during the telemetry route alignment was
confirmed through the production privilege audit trail and remains host-local.
Its content, permissions, and path are not copied into Git.
