# AnimeImages Nginx Baseline

This directory is the canonical desired configuration for the AnimeImages
production Nginx boundary. It records the accepted production behavior; it does
not install files, call `sudo`, run `nginx -s reload`, or modify a host.

## Managed Files

| Repository source | Production destination | Comparison |
| --- | --- | --- |
| `sites/download.cosflow.icu.conf.template` | `/etc/nginx/sites-available/animeimages` | Exact after replacing `{{ANIMEIMAGES_SERVER_IP}}` |
| `snippets/animeimages-v1-media.conf` | `/etc/nginx/snippets/animeimages-v1-media.conf` | Byte-exact |

The site is enabled through
`/etc/nginx/sites-enabled/animeimages -> /etc/nginx/sites-available/animeimages`.
Both installed files are expected to be `root:root 0644`. The server IP is the
only template value because it changes when a host is replaced. The public
domain, local Gunicorn target, application paths, and standard Certbot paths are
stable parts of this production baseline.

`manifest.json` is the machine-readable ownership map. System-wide
`nginx.conf`, `mime.types`, `modules-enabled`, and empty/shared `conf.d` are
Ubuntu/Nginx dependencies, not copied project files. Certbot owns certificate
material and the TLS lifecycle. The repository records certificate paths only;
it never stores certificate or private-key bytes.

## Request Boundary

- Nginx terminates TLS and proxies application traffic to Gunicorn at
  `127.0.0.1:8000`.
- `/static/` is served from `/srv/animeimages/staticfiles/`.
- `/ordinary-media-download-sw.js`, `/health/ready/`, and the homepage reach
  Django through the root application proxy.
- `/media/browser-download-telemetry/` is an exact control-plane exception.
  Django remains responsible for POST-only behavior, CSRF, schema validation,
  the 8 KiB body limit, rate limiting, and sanitized logging.
- The broad `/media/` location returns 404. Never replace it with a generic
  proxy or expose the development media directory.
- Other explicit `/media/` routes are application APIs for approved previews,
  continuation, ZIP/task output, merge, or other server-required artifacts.
  They are not permission to relay ordinary Provider video through Nginx.

Ordinary direct-capable video remains:

```text
Provider/CDN -> Browser Service Worker -> Browser Download Manager -> Disk
```

Nginx must never proxy Provider/CDN hosts. AnimeImages ordinary-media body
bytes remain zero.

## Read-Only Validation

Repository self-check:

```bash
python3 ops/nginx/tools/check_nginx_drift.py
```

Compare installed project-owned files without writing them:

```bash
python3 ops/nginx/tools/check_nginx_drift.py \
  --live-site /etc/nginx/sites-available/animeimages \
  --server-ip <SERVER_IP> \
  --live-snippet /etc/nginx/snippets/animeimages-v1-media.conf
```

An authorized operator may capture `nginx -T` outside the repository and pass
that temporary output to `--effective-config`. The checker reads only the two
AnimeImages sections and never prints the dump.

Exit codes are stable:

- `0`: canonical files and semantic invariants pass.
- `1`: managed configuration drift is present.
- `2`: input is missing, malformed, unreadable, or cannot be verified.

Run semantic tests with:

```bash
python3 -m unittest discover -s ops/nginx/tests -p 'test_*.py' -v
```

`tests/run_nginx_acceptance.py` requires an Nginx executable. It creates a
temporary unprivileged prefix and a synthetic local application, validates
syntax and route behavior, then removes all temporary state. CI runs this
harness without contacting production or a real Provider.

## Reviewed Manual Deployment

There is intentionally no automatic apply script in v1.

1. Verify the target hostname, application SHA, current service state, and
   installed file ownership.
2. Render the site template into a root-inaccessible staging location using the
   target server IP. Do not edit the live file in place.
3. Compare the staged files with the current production files and run the
   read-only checker. Review every difference.
4. Create a timestamped root-only backup outside the repository and web root.
   Use directory mode `0700` and file mode `0600`.
5. Install the reviewed candidate as `root:root 0644` using a temporary file and
   atomic rename or equivalent safe installation procedure.
6. Run `nginx -t`. Do not reload if validation fails.
7. Reload, never restart, Nginx after validation succeeds.
8. Verify readiness, homepage, telemetry GET 405, no-CSRF POST rejection,
   schema rejection, one bounded synthetic event when required, and the broad
   `/media/` 404 boundary.
9. Confirm the application was not restarted and ordinary media still has no
   server data-plane requests.
10. Run the live drift comparison again.

Rollback keeps the maintenance session open, restores the root-only backup,
runs `nginx -t`, reloads Nginx, and repeats the health and media-boundary checks.
Application code, database state, and runtime settings are not part of an Nginx
rollback.

Production rollback backups remain host-local, root-only artifacts. Do not add,
move, upload, or delete them from repository workflows.
