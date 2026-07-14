# Release Executor Production Templates

These files define the optional Release Management v1 execution boundary. They are versioned templates only. This change does not install them, change production configuration, start a deployment, or enable `RELEASE_MANAGEMENT_EXECUTOR_COMMAND`.

## Security Boundary

Gunicorn and Django continue to run as `www-data`. The checkout remains owned by `ubuntu:ubuntu`, and deployment work runs as `ubuntu`. The Web process receives no deploy key and no direct permission to run a shell, Git, `deploy.sh`, or a general `systemctl` command.

The only privileged submission path is:

```text
/usr/bin/sudo -n /usr/local/sbin/animeimages-release-submit <numeric-job-id>
```

The helper accepts exactly one positive integer and then uses `exec` with the fixed absolute command `/bin/systemctl start --no-block animeimages-deploy@<numeric-job-id>.service`. The caller cannot select another unit, action, executable, or shell fragment.

The installed helper must be `root:root` mode `0755`. Both `ubuntu` and `www-data` must be unable to modify it. Never grant sudo access to a helper or deploy script under `/srv/animeimages`, because that checkout is intentionally writable by the deployment user.

## Static Validation

Run from the repository root before installing anything:

```bash
bash -n ops/release-executor/animeimages-release-submit
bash -n scripts/validate_release_executor.sh
./scripts/validate_release_executor.sh
```

The validator does not use sudo, write under `/etc`, call systemd, or create a `ReleaseDeployJob`. Missing `systemd-analyze`, `visudo`, or `shellcheck` is reported as `SKIPPED`.

## Installation

Perform these steps manually during a separately approved production change. Review the rendered files before installation.

```bash
cd /srv/animeimages/ops/release-executor
sudo install -o root -g root -m 0755 animeimages-release-submit /usr/local/sbin/animeimages-release-submit
sudo install -o root -g root -m 0644 animeimages-deploy@.service /etc/systemd/system/animeimages-deploy@.service
sudo install -o root -g root -m 0440 animeimages-release.sudoers /etc/sudoers.d/animeimages-release
sudo visudo -cf /etc/sudoers.d/animeimages-release
sudo systemd-analyze verify /etc/systemd/system/animeimages-deploy@.service
sudo systemctl daemon-reload
```

Verify ownership and permissions:

```bash
sudo stat -c '%U:%G %a %n' \
  /usr/local/sbin/animeimages-release-submit \
  /etc/systemd/system/animeimages-deploy@.service \
  /etc/sudoers.d/animeimages-release
```

Expected values are `root:root 755`, `root:root 644`, and `root:root 440`, respectively.

The one-shot runs the existing deployment workflow as `ubuntu`. That workflow calls `scripts/restart.sh`, which currently uses sudo for `systemctl daemon-reload`, `restart animeimages`, and `status animeimages`. This executor sudoers template intentionally does not broaden `ubuntu` privileges. Before enabling the executor, verify the production deployment user's existing non-interactive deployment boundary can run those exact operations. If it cannot, stop and review a separate least-privilege rule; do not grant general systemctl or shell access as a shortcut.

## Controlled Test Job

Keep `RELEASE_MANAGEMENT_EXECUTOR_COMMAND` empty during installation and static verification. For a separately approved controlled test:

1. Confirm `/srv/animeimages/.venv/bin/python`, `.env.production`, the database, the current `ubuntu` deployment path, and its exact non-interactive restart permissions are healthy.
2. Create one main-only pending job through the existing admin workflow and record its numeric ID. Do not use a fabricated ID.
3. Submit exactly `sudo -u www-data /usr/bin/sudo -n /usr/local/sbin/animeimages-release-submit <job-id>`.
4. Inspect `sudo journalctl -u animeimages-deploy@<job-id>.service --no-pager` and the admin job status.
5. Confirm the job is claimed once, the deployed SHA equals `job.target_sha`, and `/health/ready/` returns HTTP 200.

Only after that controlled test passes may an operator set this value in the real `/srv/animeimages/.env.production` and restart Gunicorn so it reloads the environment:

```dotenv
RELEASE_MANAGEMENT_EXECUTOR_COMMAND="/usr/bin/sudo -n /usr/local/sbin/animeimages-release-submit {job_id}"
```

This repository template intentionally leaves the value empty.

## Immediate Disable And Rollback

To stop new automatic submissions, clear `RELEASE_MANAGEMENT_EXECUTOR_COMMAND` in the production environment and restart Gunicorn. Existing systemd jobs are not cancelled automatically; inspect them with `systemctl status` and `journalctl` before taking a separately approved operational action.

If the boundary itself must be removed, first disable the environment setting, then remove the sudoers file, helper, and unit in a reviewed maintenance window and run `systemctl daemon-reload`. The existing manual `scripts/deploy.sh` process remains the fallback. Database rollback is never automatic.

The current production topology remains Gunicorn as `www-data` on `127.0.0.1:8000` behind Nginx. These templates do not modify `animeimages.service`, Nginx, or migrate Gunicorn to a Unix socket.
