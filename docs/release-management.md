# Release Management v1

Status: the admin and read-only Git boundary from PR #17 and PR #18 are deployed. The repository now includes the Release Executor Wiring v1 templates, but production has not installed or enabled them. Production remains record-only.

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
| Root-owned submit helper | Accepts one numeric job ID and submits one fixed systemd unit without exposing general root commands. |
| External systemd one-shot | Starts outside the request thread and invokes the management command as the deployment user. |
| `run_release_deploy_job` | Atomically claims one pending job and runs the pinned deployment workflow. |
| `scripts/deploy.sh` | Fetches as the deployment user, verifies reachability, deploys the exact target SHA, migrates, collects static assets, restarts, and checks readiness. |

The Web request never runs `deploy.sh`, migrations, service restarts, or readiness polling. Its optional executor action can only run `/usr/bin/sudo -n /usr/local/sbin/animeimages-release-submit <job-id>`. The root-owned helper submits the fixed systemd one-shot with `--no-block`.

## Production Permission Boundary

Production separates the Web identity from the deployment identity:

```text
Gunicorn / Django Web process: www-data
Gunicorn bind:                 127.0.0.1:8000
Nginx upstream:                http://127.0.0.1:8000
Application checkout owner:   ubuntu:ubuntu
Application checkout:         /srv/animeimages
Deployment user:              ubuntu
```

Production does not currently use `/run/animeimages/gunicorn.sock`. A systemd-managed Unix socket is a future recommended hardening target only; adopting it requires a separate reviewed systemd and Nginx rollout.

`www-data` may:

- read application files required by Gunicorn;
- read `HEAD`, local `origin/main`, tags, commit metadata, and ahead/behind counts;
- create and update `ReleaseDeployJob` rows through Django;
- submit the exact approved root-owned helper command only after executor enablement.

`www-data` may not:

- run `git fetch`, `reset`, `checkout`, or any command that modifies refs;
- receive the GitHub deploy key or production SSH configuration;
- own `/srv/animeimages`;
- execute `deploy.sh` in a Web request;
- submit arbitrary shell commands or arbitrary systemd units.
- modify `/usr/local/sbin/animeimages-release-submit` or the installed systemd and sudoers files.

Keep `/srv/animeimages` owned by `ubuntu:ubuntu`. Do not work around Git ownership checks by changing the repository owner to `www-data`.

## Record-only Default

The production example leaves the executor disabled:

```text
RELEASE_MANAGEMENT_EXECUTOR_COMMAND=
```

In this state, creating a main release job stores a pinned `target_sha` and leaves the job `pending`. It does not deploy code, restart Gunicorn, or run migrations. This record-only state remains the production default after the executor templates are merged; installing and enabling the executor requires a separate approved server change.

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

Versioned templates live in `ops/release-executor/`:

```text
animeimages-release-submit
animeimages-deploy@.service
animeimages-release.sudoers
README.md
```

They are not installed automatically. The only accepted Django executor configuration is:

```text
RELEASE_MANAGEMENT_EXECUTOR_COMMAND="/usr/bin/sudo -n /usr/local/sbin/animeimages-release-submit {job_id}"
```

The Django service compares parsed argv to that exact four-part shape and binds the final argument to the current `job.pk`. It rejects direct `systemctl`, direct `deploy.sh`, `bash -c` / `sh -c`, alternate helper paths, missing `sudo -n`, wrong IDs, extra arguments, and all other placeholders. The same check runs again before `subprocess.run`, which continues to receive an argv list without `shell=True`. Invalid plans roll back job creation so they cannot occupy the active slot.

The production helper must be installed at `/usr/local/sbin/animeimages-release-submit` as `root:root` mode `0755`. Neither `ubuntu` nor `www-data` may modify it. It accepts exactly one positive integer and uses `exec` for this fixed command:

```text
/bin/systemctl start --no-block animeimages-deploy@<numeric-job-id>.service
```

The unit is installed at `/etc/systemd/system/animeimages-deploy@.service` as `root:root` mode `0644`. It is `Type=oneshot`, runs as `ubuntu:ubuntu`, uses `/srv/animeimages` and `.env.production`, and executes the project virtualenv management command with `%i` as the numeric job ID.

The sudoers file is installed at `/etc/sudoers.d/animeimages-release` as `root:root` mode `0440`. It grants `www-data` passwordless access only to the root-owned helper. It must not grant direct `systemctl`, shell, Git, `deploy.sh`, or a helper below `/srv/animeimages`. Validate it with `visudo -cf` before installation or enablement.

Run repository-only validation first; it does not use sudo, write `/etc`, start systemd, or create a release job:

```bash
bash -n ops/release-executor/animeimages-release-submit
bash -n scripts/validate_release_executor.sh
./scripts/validate_release_executor.sh
```

During a separately approved production installation, follow `ops/release-executor/README.md`: install the three files with root ownership, run `visudo -cf`, run `systemd-analyze verify`, and then run `systemctl daemon-reload`. Keep the executor environment value empty throughout installation and static validation.

Use a controlled pending job that was created through the main-only admin workflow for the end-to-end check:

```bash
sudo -u www-data /usr/bin/sudo -n /usr/local/sbin/animeimages-release-submit <job-id>
sudo systemctl status animeimages-deploy@<job-id>.service
sudo journalctl -u animeimages-deploy@<job-id>.service --no-pager
```

Confirm the job is claimed exactly once, final `HEAD == job.target_sha`, and `/health/ready/` returns HTTP 200. Only then set the exact executor command in the real `.env.production` and restart Gunicorn to load it. This repository change does not perform those production steps.

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
3. Django commits the audit row and invokes the exact `sudo -n` root-owned submit helper.
4. The request returns without waiting for deployment.
5. The helper validates the numeric ID and submits the fixed systemd unit with `--no-block`.
6. systemd starts the one-shot as `ubuntu`.
7. `run_release_deploy_job` atomically claims the job.
8. The executor passes `DEPLOY_TARGET_SHA=job.target_sha` to `scripts/deploy.sh`.
9. The deploy script fetches `main`, verifies that the pinned SHA is reachable from `origin/main`, resets to that exact SHA, installs dependencies, runs migrations, collects static assets, restarts the application, and checks readiness.
10. The job verifies final `HEAD == job.target_sha` and records success or failure.

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

Check the sanitized job error, `sudo -n` behavior for `www-data`, helper ownership and mode, sudoers validation, the fixed unit, and `journalctl`. Increasing the Web timeout does not fix a long deployment because the request waits only for helper/systemd submission.

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

Continue with the existing manual `scripts/deploy.sh` workflow as `ubuntu`. A durable code rollback must be a reviewed revert on `main`; Release Management v1 does not deploy an older Tag or arbitrary commit. Migration rollback is never automatic; review migration reversibility and production data impact before applying older code.

If the installed executor boundary must also be removed, disable the environment value first, then remove the sudoers file, helper, and unit only in a separately approved maintenance window and run `systemctl daemon-reload`. Inspect already-running one-shot jobs before taking any cancellation action.

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
