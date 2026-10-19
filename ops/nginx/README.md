# AnimeImages Nginx Baseline

This directory is the canonical desired configuration for the AnimeImages
production Nginx boundary. It records desired behavior, not proof that the latest
revision is installed in production; it does
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
- Five existing Django APIs are also exact-match exceptions:
  `/media/upstream-candidate/`, `/media/xhs-livephoto-motion-descriptor/`,
  `/media/xhs-livephoto-pair-descriptor/`, `/media/ytdlp-selected-format/`, and
  `/media/service-selected-format/`. Their subpaths remain denied. Slashless
  stems receive Nginx's canonical-slash 301 without reaching the upstream.
  Django still enforces methods, CSRF, staff admission,
  resource credentials and current source authorization; Nginx adds no grants.
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

`tests/run_nginx_acceptance.py` requires an Nginx executable. It loads the actual
managed site template and snippet, substituting only the server-IP placeholder,
test listeners, TLS directives, static directory and snippet include path. It
does not reconstruct locations or change proxy headers/upstream semantics.
The synthetic upstream records method and full request path (including query),
so application receipt is not inferred from status or request-ID headers.
Unknown media paths, uploaded files and subpaths of the five exact APIs must
return 404 without an upstream request. Existing download routes, static files,
homepage, readiness, telemetry and the Service Worker remain covered.

Each run also deletes each of the five required locations in a temporary snippet
and proves the same reachability assertion fails for exactly that path. Drift
tests independently reject deletion, prefix widening and proxy/header drift.
`xhs_download.tests_nginx_route_boundaries` explicitly classifies Django media
routes (including the intentionally denied DEBUG upload route) and exercises the
real views' method, CSRF and authorization rejection. New URLs fail classification
until reviewed; neither test generates public configuration. Existing CI test
discovery and the existing Nginx acceptance job pick up these regressions.

The harness uses fixed ports 8000, 18080 and 18081. Run it in an isolated
container, never by stopping the daily host listener. For example, from the
repository root with the available Colima context:

```bash
name=animeimages-f2-nginx-acceptance
docker --context colima run -d --name "$name" \
  --mount "type=bind,src=$PWD/ops/nginx,dst=/source/ops/nginx,readonly" \
  --workdir /source python:3.11-slim-bookworm sleep infinity
trap 'docker --context colima rm -f "$name"' EXIT
docker --context colima exec "$name" sh -c \
  'apt-get update -qq && apt-get install -y --no-install-recommends nginx'
docker --context colima network disconnect bridge "$name"
docker --context colima exec "$name" nginx -v
docker --context colima exec "$name" python -B ops/nginx/tests/run_nginx_acceptance.py
```

No ports are published; installation is confined to the disposable container,
and test traffic uses only its loopback after disconnecting the external network.
The harness stops its Nginx/upstream and removes temporary files; the trap removes
the container. The F2 countertest was run before changing the snippet: all five
paths returned 404 and the upstream received none. This is isolated validation,
not a claim that production 404s have been fixed.

## Reviewed Manual Deployment

There is intentionally no automatic apply script in v1.

Before a future release, review existing drift rather than blindly overwriting
or reloading the installed site. The main task's limited read-only inspection
reported an additional non-media exact site-verification location in the
installed site, absent from this template. Preserve that intentional unmanaged
location in any proposed site replacement after review; this F2 change does not
add it to the managed baseline or suppress exact-comparison drift. The inspection
could read the installed site/snippet but not the full effective `nginx -T`, so
these file observations do not establish the loaded configuration or edge chain.
If the reviewed installed site already includes the snippet and retains the media
deny, this route fix may only require installing the updated snippet, not replacing
the site file. Verify both managed files and all relevant existing differences
before deciding what to install.

1. Verify the target hostname, application SHA, current service state, and
   installed file ownership.
2. Render the site template into a root-inaccessible staging location using the
   target server IP. Do not edit the live file in place.
3. Stage the managed candidates listed above, including the new exact-route snippet.
   Compare them with the current production files and run the read-only checker.
   Review every difference, preserve intentional site-specific verification routes,
   and run the isolated acceptance harness before install. A drift result is not
   permission to discard installed additions.
4. Create a timestamped root-only backup outside the repository and web root.
   Use directory mode `0700` and file mode `0600`.
5. Install only the reviewed changed files as `root:root 0644` using a temporary file and
   atomic rename or equivalent safe installation procedure.
6. Run `nginx -t`. Do not reload if validation fails.
7. Reload, never restart, Nginx after validation succeeds.
8. Verify readiness, homepage, telemetry GET 405, no-CSRF POST rejection,
   schema rejection, one bounded synthetic event when required, and the broad
   `/media/` 404 boundary. Check the five exact APIs with non-business probes
   (GET 405 and no-CSRF POST rejection), corroborating application receipt in
   bounded request records; unknown/upload/subpaths must not reach Django.
9. Confirm the application was not restarted and ordinary media still has no
   server data-plane requests.
10. Run the live drift comparison again.

Rollback keeps the maintenance session open, restores the root-only backup,
runs `nginx -t`, reloads Nginx, and repeats the health and media-boundary checks.
Application code, database state, and runtime settings are not part of an Nginx
rollback.

Production rollback backups remain host-local, root-only artifacts. Do not add,
move, upload, or delete them from repository workflows.
