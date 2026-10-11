# Linux deployment permission acceptance

Run from the repository root (use `docker --context colima` on the development Mac):

```sh
docker build -t animeimages-deploy-permission-tests -f ops/deploy/tests/Dockerfile .
docker run --rm --network none -v "$PWD:/source:ro" animeimages-deploy-permission-tests
```

Use `--counterexample-only` after the image name to reproduce the old behavior:
outer `umask 077`, Git reset/archive output `0600`, actual web UID denied.
This test passes only when that negative control is observed.

The image build installs dependencies; acceptance runs have no network. No host
Docker socket, production configuration, database, credential or port is mounted.
The source mount is read-only. All repositories, commits, service logs and files
are synthetic and container-local. Root initializes users, sudo policy and test
directories. Repository operations run as deploy; consumers run as web/staticweb
through real runuser/sudo identity changes. Only systemctl is simulated.

Deployment tests copy the actual five tooling files into a separate clean Git
checkout, create a local bare remote and invoke the actual deploy.sh. Python,
venv dependency imports, Django migrations and static collection are real, using
a synthetic Django project with an in-memory database and no provider code.
No application service starts. The service shim reports an inactive fixture.

A separate real-application smoke test initializes AnimeImages WSGI from the
read-only source as the web UID, loads base.html and reads a real static asset.
It uses an empty inherited environment, synthetic secrets, disabled third-party
gates, and SQLite memory settings. The URL is sqlite::memory: because this
application preserves the leading slash in sqlite:///:memory:. No real env is read.

Failure cases cover existing unreadable code/directories, candidate creation and
static identity denial, private journal creation, active services, dependency
installation, final code/static readability, and escaping static file symlinks.
Consumer-owned read-only files/directories and consumer-writable ancestors are
also rejected: real consumers demonstrate chmod or directory replacement in
those disposable negative fixtures. A positive case retains support for a
deploy-owned candidate under root-owned sticky `/tmp`.
Multi-hop static links and the original runtime alias are checked before their
final targets can hide a consumer-controlled intermediate path.
The suite asserts no switch before failed prechecks and no restart after failed
post-switch checks, plus private record/backup isolation and 0022/0027 success.

The actual deploy script creates a private tracked-code tar archive. Tests check
its contents, original file modes and 0600 privacy, and force real tar failure
with a deploy-unreadable tracked file. Only that fixture uses assume-unchanged
to get past Git's earlier clean-tree gate and exercise tar itself.
Database and collected-static backups remain separate operator prerequisites.
Synthetic backup tests use a scoped `(umask 077; synthetic_backup_command)`
followed by the actual deploy command through `&&` or `set -e`. They check
private partial output, unchanged outer umask, and no deployment on failure.
These tests do not perform or claim PostgreSQL backup/restore verification.
