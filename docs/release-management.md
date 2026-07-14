# Release Management v1

Status: merged and deployed through PR #17 and PR #18 as of 2026-07-14.

Release Management v1 provides an audited Django admin view of the production release state. Its production defaults are deliberately conservative: it supports `main` only, reads local Git refs only, and records jobs without executing them until a separate systemd boundary has been installed and verified.

Admin entrypoint:

```text
/admin/release-management/
```

## Architecture And Responsibilities

| Component | Responsibility |
| --- | --- |
| Release Management admin view | Displays local Git state, readiness, recent tags, and release jobs; creates or cancels audited jobs. |
| `ReleaseDeployJob` | Stores actor, request ID, pinned target SHA, status, command plan, output tails, readiness result, and errors. |
| Release Management service | Enforces read-only Web Git commands, resolves local `origin/main`, builds the command plan, and performs atomic job transitions. |
| External systemd one-shot | Starts outside the request thread and invokes the management command as the deployment user. |
| `run_release_deploy_job` | Atomically claims one pending job and runs the pinned deployment workflow. |
| `scripts/deploy.sh` | Fetches as the deployment user, verifies reachability, deploys the exact target SHA, migrates, collects static assets, restarts, and checks readiness. |

The Web request never runs `deploy.sh`, migrations, service restarts, or readiness polling. Its optional executor action only asks systemd to start a one-shot unit with `--no-block`.

## Production Permission Boundary

Production separates the Web identity from the deployment identity:

```text
Gunicorn / Django Web process: www-data
Application checkout owner:   ubuntu:ubuntu
Application checkout:         /srv/animeimages
Deployment user:              ubuntu
Runtime directory:            /run/animeimages (managed by systemd)
```

`www-data` may:

- read application files required by Gunicorn;
- read `HEAD`, local `origin/main`, tags, commit metadata, and ahead/behind counts;
- create and update `ReleaseDeployJob` rows through Django;
- submit the exact approved systemd one-shot command only after executor enablement.

`www-data` may not:

- run `git fetch`, `reset`, `checkout`, or any command that modifies refs;
- receive the GitHub deploy key or production SSH configuration;
- own `/srv/animeimages`;
- execute `deploy.sh` in a Web request;
- submit arbitrary shell commands or arbitrary systemd units.

Keep `/srv/animeimages` owned by `ubuntu:ubuntu`. Do not work around Git ownership checks by changing the repository owner to `www-data`.

## Record-only Default

The production example leaves the executor disabled:

```text
RELEASE_MANAGEMENT_EXECUTOR_COMMAND=
```

In this state, creating a main release job stores a pinned `target_sha` and leaves the job `pending`. It does not deploy code, restart Gunicorn, or run migrations. This record-only state is the expected default after installation, merge, and first deployment.

A pending record occupies the single active execution slot. Cancel an obsolete pending job from the admin page before creating another one.

## Local origin/main Behavior

Ordinary GET and POST requests never contact GitHub. The page reads the locally cached refs already present in `/srv/animeimages/.git`.

- `HEAD` represents the code currently checked out on the server.
- local `origin/main` represents the last remote state fetched by `ubuntu` or an approved external operations task.
- the update indicator compares `HEAD` with that local `origin/main`.
- creating a main job pins the exact local `origin/main` SHA at that moment.

The page can therefore display stale remote information if no external fetch has occurred recently. Refresh refs outside Gunicorn before requesting a release job:

```bash
cd /srv/animeimages
git fetch origin --prune --tags
```

Run that command as `ubuntu`, using the dedicated production SSH alias and deploy key. Never add a fetch button back to the Web page and never give the deploy key to `www-data`.

## Git safe.directory

Git rejects a repository owned by another account unless the path is explicitly trusted. Because Gunicorn runs as `www-data` and the checkout belongs to `ubuntu:ubuntu`, configure the exact production path at the system level:

```bash
sudo git config --system --add safe.directory /srv/animeimages
```

This setting only marks the exact repository as safe for Git ownership checks. It does not grant filesystem write access or GitHub credentials.

Do not use:

```text
safe.directory=*
```

Do not add a broad parent directory and do not change `/srv/animeimages` ownership. Verify read-only access with:

```bash
sudo -u www-data git -C /srv/animeimages rev-parse HEAD
sudo -u www-data git -C /srv/animeimages rev-parse origin/main
```

## Executor Wiring

Only enable the executor after the unit, sudoers rule, runtime user, paths, and permissions have been tested independently. The configured command must remain non-blocking:

```text
RELEASE_MANAGEMENT_EXECUTOR_COMMAND="/usr/bin/sudo /bin/systemctl start --no-block animeimages-deploy@{job_id}.service"
```

The Django service rejects direct `deploy.sh` executor commands, blocking `systemctl start` commands, and unsupported placeholders. Invalid command plans roll back job creation so they cannot leave a permanent pending slot.

An example one-shot unit shape is:

```ini
[Unit]
Description=AnimeImages release deployment job %i
After=network-online.target

[Service]
Type=oneshot
User=ubuntu
Group=ubuntu
WorkingDirectory=/srv/animeimages
EnvironmentFile=/srv/animeimages/.env.production
ExecStart=/srv/animeimages/.venv/bin/python manage.py run_release_deploy_job --job-id %i
```

The systemd service runs as `ubuntu` because it performs Git and deployment operations. Gunicorn remains `www-data`.

The sudoers rule must allow `www-data` to submit only the expected `systemctl start --no-block animeimages-deploy@<numeric-job-id>.service` command. Do not grant general `systemctl`, shell, Git, or deployment-script sudo access. Use a server-compatible exact or anchored rule and validate it with `visudo`; do not copy a broad wildcard rule without reviewing how the installed sudo version matches arguments.

Before setting the environment variable, verify:

```bash
sudo systemd-analyze verify /etc/systemd/system/animeimages-deploy@.service
sudo systemctl daemon-reload
sudo -u www-data sudo -n /bin/systemctl start --no-block animeimages-deploy@<test-job-id>.service
sudo systemctl status animeimages-deploy@<test-job-id>.service
sudo journalctl -u animeimages-deploy@<test-job-id>.service
```

Use a controlled test job ID that exists in the database. Confirm the actual `systemctl` and Python paths on the server before installing the unit or sudoers rule.

## Task State Machine

```text
pending -> running -> succeeded
                   -> failed
pending -> failed       (executor submission failure)
pending -> cancelled    (admin cancellation)
```

- Job creation and command-plan construction occur in one database transaction.
- A conditional unique constraint permits only one `pending` or `running` job.
- The external management command atomically claims `pending -> running`; duplicate execution cannot claim the same job twice.
- Admin cancellation atomically performs `pending -> cancelled` and cannot cancel a running job.
- Successful deployment requires the final `HEAD` to equal `job.target_sha`.
- Terminal jobs record `finished_at`; failures retain a bounded error and output tail for audit.

## Deployment Flow

With the executor enabled and validated:

1. An authorized admin creates a `main` job.
2. Django reads local `origin/main` and stores its exact SHA in `job.target_sha`.
3. Django commits the audit row and submits `systemctl start --no-block`.
4. The request returns without waiting for deployment.
5. systemd starts the one-shot as `ubuntu`.
6. `run_release_deploy_job` atomically claims the job.
7. The executor passes `DEPLOY_TARGET_SHA=job.target_sha` to `scripts/deploy.sh`.
8. The deploy script fetches `main`, verifies that the pinned SHA is reachable from `origin/main`, resets to that exact SHA, installs dependencies, runs migrations, collects static assets, restarts the application, and checks readiness.
9. The job verifies final `HEAD == job.target_sha` and records success or failure.

If the executor is empty, the flow stops after step 2 with a `pending` audit record.

## Troubleshooting

### Dubious ownership

Symptom: the page reports `detected dubious ownership` or cannot read Git state.

Fix only the exact safe directory:

```bash
sudo git config --system --add safe.directory /srv/animeimages
```

Then run the two `sudo -u www-data git ... rev-parse` checks above. Do not change ownership and do not use `safe.directory=*`.

### origin/main unavailable or stale

Run `git fetch origin --prune --tags` as `ubuntu`, then reload the page. A Web request cannot and should not repair stale refs.

### Job remains pending

This is expected in record-only mode. If an executor was expected, confirm the environment variable is present in the Gunicorn environment and that the command plan records an executor. Cancel an obsolete pending job before retrying.

### Executor submission fails

Check the job error, `sudo -n` behavior for `www-data`, the exact binary paths, the unit name, and `journalctl`. Keep `--no-block`; increasing the Web timeout does not fix a long deployment because the request should wait only for systemd submission.

### Job remains running

Inspect the one-shot unit and journal. Do not create another active job or change the database row casually; determine whether deployment, migration, restart, or health check is still running before recovery.

### Deployment fails or HEAD mismatches

Compare `job.target_sha`, `git rev-parse HEAD`, and local `origin/main`. Review the captured output tails and deployment journal. A target that is no longer reachable from fetched `origin/main` must fail rather than deploy a different commit.

### Readiness fails

Run:

```bash
curl -i http://127.0.0.1/health/ready/
sudo journalctl -u animeimages
```

Resolve database, cache, migration, socket, or application errors before marking the release successful.

## Rollback

To disable automated execution immediately, clear the executor and restart Gunicorn so it reloads the environment:

```text
RELEASE_MANAGEMENT_EXECUTOR_COMMAND=
```

Continue with the existing manual `scripts/deploy.sh` workflow as `ubuntu`. A code release can be reverted and deployed through the normal reviewed `main` process. Migration rollback is never automatic; review migration reversibility and production data impact before applying an older code version.

Migration `0023_releasedeployjob` may remain in place when the feature is disabled or application code is reverted. Keeping the audit table is safe and preserves deployment history.

## v1 Non-goals

Release Management v1 does not provide:

- Tag deployment;
- arbitrary commit deployment;
- automatic Tag or commit rollback;
- migration rollback;
- arbitrary branch deployment;
- GitHub fetch from the Web process;
- a general shell or systemd console;
- automatic executor or sudoers installation.
