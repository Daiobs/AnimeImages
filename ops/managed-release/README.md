# SSH Managed Release Channel v1

This is a manually approved SSH release channel for the `ubuntu` deploy user,
not the Web Release Management executor. Merging main does not deploy anything.
The Web executor environment variable remains empty and its helper/unit/sudoers
remain uninstalled. This channel adds no timer, webhook, systemd unit or SSH key.

## Permission Contract

Keep `/etc/sudoers.d/animeimages-deploy` with its existing exact root commands:

```text
/usr/bin/systemctl daemon-reload
/usr/bin/systemctl restart animeimages
/usr/bin/systemctl status animeimages --no-pager -l
```

The additional fragment permits only:

- `ubuntu -> root`: `/usr/bin/systemctl stop animeimages`.
- `ubuntu -> www-data:www-data`: the fixed root-owned consumer launcher.

The launcher is never run as root. Its closed argument parser accepts code or
static probes of the live checkout or an exactly named candidate directory. It
clears the environment and uses the fixed application virtualenv and installed
gate. The code probe **executes application/dependency imports as www-data**;
it is not a sandbox or a promise of read-only application behavior. The deploy
user already controls code/venv and can restart that application. Protect that
SSH identity accordingly. A PASS is a readability/import result, not a Git SHA
attestation; reviewed tooling constructs the tracked-path manifest and checks
the pinned target separately.

No root shell, root Git/Python/manage.py/deploy.sh, wildcard systemctl, other
service, arbitrary run-as user, or new permission for www-data is granted.
Normal password administrator rules and `animeadmin` recovery access remain
unchanged. Do not restore `NOPASSWD: ALL` or change cloud-init in this task.
This is a sudo-command boundary, not a whole-host privilege audit. Membership
in a group that controls a rootful Docker daemon is independently root-equivalent;
review such access separately rather than claiming sudo rules remove it.

## One-Time Bootstrap After Review And Merge

Do not install from an unreviewed Draft PR. Keep independent ubuntu and
animeadmin recovery SSH sessions available. Verify both identities, recovery
password sudo, actual `/usr/bin/systemctl` and `/usr/sbin/visudo` paths,
`visudo -c`, existing sudo rules, clean main, health, inactive Web executor and
no pending/running release jobs. Do not expose environment or key contents.

From a separate clean checkout pinned to the approved tool commit, package:

```bash
python3 scripts/package_managed_release.py --output /tmp/animeimages-managed-release.tar
```

Record the printed full tool SHA and bundle SHA256 from this trusted local
checkout. Transfer the tar to ubuntu's private staging directory. The following
is an administrator-run bootstrap structure, **not permission for the deploy
user to run an installer as root without a password**. Replace the placeholders
with the reviewed artifact path and digest before execution:

```bash
(
set -euo pipefail
sudo -v
bootstrap="$(sudo mktemp -d /root/animeimages-channel.XXXXXXXX)"
sudo chmod 0700 "$bootstrap"
sudo install -o root -g root -m 0600 /home/ubuntu/<private-staging>/animeimages-managed-release.tar "$bootstrap/bundle.tar"
printf '%s  %s\n' '<reviewed-bundle-sha256>' "$bootstrap/bundle.tar" | sudo sha256sum -c -
# set -e/pipefail prevents extraction if frozen archive verification fails.
sudo tar --extract --file "$bootstrap/bundle.tar" --directory "$bootstrap" \
  --no-same-owner --no-same-permissions \
  install.py animeimages-consumer-probe animeimages-managed-release.sudoers check_deploy_readability.py TOOL_SHA
sudo /usr/bin/python3 -I "$bootstrap/install.py"
)
```

Never execute the installer directly from an ubuntu-writable checkout. Verify
the copied archive before extraction/execution, not the mutable transfer source.
The installer rejects symlinked/untrusted parents, conflicting existing files
and invalid sudoers; it backs up its transaction record privately, opens the
sudo permission last, and removes only newly installed files on failure. An
exact reinstall is idempotent; an artifact upgrade requires a separate reviewed
administrator procedure, not silent replacement. It does not stop, restart,
deploy, reload systemd, edit env, alter users or install the Web executor.

Installed artifacts:

```text
/usr/local/sbin/animeimages-consumer-probe                              root:root 0755
/usr/local/libexec/animeimages-release/check_deploy_readability.py      root:root 0644
/etc/sudoers.d/animeimages-managed-release                             root:root 0440
```

Verify `visudo -c`, `sudo -l -U ubuntu`, exact application status without a
password, and refusal of generic `sudo -n /usr/bin/true`, root `id`, other
service status, shell and arbitrary consumer commands. Do not test restart or
stop while installing. Confirm animeadmin can still log in and use password
sudo, service PID is unchanged, both readiness URLs are 200 and executor remains
disabled. Installation alone does not release the application.

## Approved Release

Each release still requires explicit human approval of target SHA, tooling SHA,
maintenance window, backup/restore evidence and migration safety. Do not infer
approval from a merge or an expired window. Maintain verified private database,
code and collected-static backups before apply; this wrapper's code journal
does not replace database/static backups. Existing database migrations are not
automatically reversed.

Use an independent clean tooling checkout with the reviewed full tool SHA.
Prepare the fixed staging parent as ubuntu, without changing existing app,
runtime, secrets or static permissions. The wrapper uses a private state
directory and a nonblocking lock. Do not run other deployment entrypoints in
parallel; the lock coordinates this channel only.

```bash
# Run through the existing ubuntu SSH identity; no administrator password.
bash <pinned-tool-checkout>/scripts/managed_release.sh prepare <approved-target-sha>
# Only within the newly approved maintenance window:
bash <pinned-tool-checkout>/scripts/managed_release.sh apply <approved-target-sha> --maintenance-approved
bash <pinned-tool-checkout>/scripts/managed_release.sh verify <approved-target-sha>
```

`prepare` runs the real candidate and live consumer gates, but never stops or
updates live code. `apply` prepares again, checks exact service permissions,
stops only animeimages, checks inactive/PID=0, then calls the existing pinned
`deploy.sh`. That script owns Git target validation, dependency installation,
migrations, collectstatic, consumer checks, restart and health check. The wrapper
then checks exact live SHA, clean status, www-data service identity, running
state and local/public readiness. Capture the SSH exit status and output in a
private operator release record; any nonzero result is a failed release.
All wrapper modes share the same lock. HUP/INT/TERM terminates and waits for the
deployment subprocess group, then reports the observed service state. SIGKILL
or host failure cannot be handled; inspect the journal rather than assuming a
completed or rolled-back deployment. Readiness requires exact HTTP 200 without
curl rc files, redirects or proxies. Managed Git reads ignore replace refs.

`DEPLOY_CONSUMER_PROBE_MODE=managed` fails closed if fixed production settings,
root-controlled artifacts or their exact reviewed bytes differ. It never falls
back to direct sudo. The default `direct` mode is unchanged for other deployment
environments. Production env may not override managed channel control variables;
such an assignment stops deployment instead of silently changing the profile.

## Failure And Rollback

After a failure, inspect the private deploy journal, exact HEAD, actual service
state and non-sensitive logs. Do not assume the application stayed stopped after
a restart attempt. Stop it under renewed approval before restoring verified
code/static backups. Retain database data/migrations; review forward/revert
migration compatibility separately. Do not automatically reset to an older SHA
or restore a database in response to a health failure.

To remove this channel, use the independent administrator session: back up and
remove only `/etc/sudoers.d/animeimages-managed-release`, validate `visudo -c`,
then remove the two installed probe artifacts after confirming no release is
running. Keep the original three-command deployment fragment, animeadmin,
application, env, database, and Web executor state unchanged. Check generic sudo
is still refused and the original exact application status still works. No
application/database rollback or service restart is needed to remove the channel.

## Verification

The acceptance suite runs in a disposable Linux container with real ubuntu and
www-data identities, narrow sudoers and no network. Service control is stubbed
only inside that container. It tests permitted probes and exact stop, rejected
root/consumer commands, malformed paths/args, secret-environment isolation and
failure cases. The existing direct-mode deployment permission matrix remains
separate. Never run the root Linux fixture directly on a production host.
