# Development Guide

## Baseline

- Python: 3.14.5
- Django: 5.2.x
- Project root: the directory containing `manage.py`
- Local interpreter: `.venv/bin/python`
- Canonical development branch: latest fetched `origin/main`

Do not use a production checkout as a development baseline. Development main and the recorded production runtime are separate facts; see [Current Version Status](current-version-status.md).

## First Setup

```bash
git clone <repository>
cd AnimeImages
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
cp .env.example.local .env
python manage.py check
python manage.py migrate
python manage.py runserver
```

Runtime reads the project-root `.env`; example files are templates. Never commit `.env`, credentials, Cookie values or Provider tokens.

## Daily Workflow

1. Confirm the assigned worktree and current branch.
2. Fetch the remote before choosing a base.
3. Start new work from the latest `origin/main`, unless the task names an existing branch/PR.
4. Use one task, one branch and one PR.
5. Run checks before meaningful commits.

```bash
pwd
git status --short --branch
git fetch --prune origin
git rev-parse origin/main
python manage.py check
python manage.py makemigrations --check --dry-run
python manage.py test xhs_download --noinput
node --test tests_js/*.test.js
node --check static/js/*.js
```

Run locally with:

```bash
python manage.py runserver
```

If `.venv` is not activated, prefix Python commands with `.venv/bin/python`.

## Git Workflow

Branch from the current remote baseline:

```bash
git fetch --prune origin
git switch --detach origin/main
git switch -c <task-branch>
```

Follow an existing task's requested branch name exactly. Otherwise use a concise scoped name such as:

- `codex/fix-download-state`
- `docs/readme-current-state-refresh-v1`
- `research/provider-capability-audit`

Keep commits named by intent. Do not force-push, reset or delete another task's changes. A dirty or drifting worktree must be understood before editing.

## Documentation-Only PRs

For a documentation-only task:

- edit only the explicitly allowed Markdown files;
- do not change code, tests, workflows, environment examples or dependencies;
- derive current claims from code/tests and merged PR evidence, not from old README text;
- distinguish development main from recorded production runtime;
- keep research history under `docs/research/` and stable instructions under top-level `docs/`;
- validate relative links, code fences, heading order, stale claims and personal paths;
- run Django check and migration dry-run even though runtime code is unchanged.

Before commit:

```bash
git diff --check
git diff --name-only
python manage.py check
python manage.py makemigrations --check --dry-run
```

## Provider Lab Development

Provider Lab is separate from formal platform routes. Its whole-repository Provider runtimes must use isolated venvs outside the application `.venv`.

Bootstrap entrypoint:

```bash
python tools/provider_lab/runtime/bootstrap_provider.py \
  --provider parsehub \
  --target /tmp/animeimages-provider-lab/parsehub
```

Use `--provider videodl` and a separate target for videodl. Use `--check-only` to validate an existing exact-pin runtime without installing or updating it.

Provider Lab changes must preserve:

- Staff-only access and default-off flags;
- fixed Provider Registry;
- one POST per Provider and browser concurrency max three;
- parse-only Workers with Provider downloader disabled;
- bounded subprocess/output/process-tree cleanup;
- `LabProviderResultV1` normalization and raw sanitization;
- persistent sanitized execution summaries remain separate from short-lived
  resource tokens; no full-media Relay or historical download authority;
- no automatic formal route change.

videodl remains subject to PolyForm Noncommercial 1.0.0 and the existing
approved-use boundary. Current code also exposes reviewed execution through
shared parsing and scoped saved routes; that is not new production, public or
commercial authorization. See [Provider Lab](provider-lab.md) for exact pins,
flags, reviewed Common scope and cleanup.

## External Provider Tests

Automated tests must not access real Providers or public media. Use fixtures, fake Workers and deterministic local HTTP servers.

Real sample checks require a separately scoped research or browser acceptance task. Reports must omit source URLs, signed media URLs, query values, Cookie, credentials and user input.

Generic diagnostics redact Provider CDN URLs by default. Use URL-revealing debug flags only in local manual checks and never paste those results into a PR.

## Local Artifacts

Do not commit:

- `.venv/`
- `.env`
- `__pycache__/`
- `staticfiles/`
- local SQLite databases unless explicitly requested as fixtures
- generated/downloaded files under `media/`
- local assets under `static/images/`
- temporary Provider Lab venvs or checkouts
- browser profiles, screenshots or download files

Before handoff:

```bash
git status --short --branch
(git diff --name-only; git ls-files --others --exclude-standard) | sort
```

## Before Draft PR

Use risk-proportional focused tests, then the repository-wide gates required by the task. A normal application PR should include at least:

```bash
git diff --check
python manage.py check
python manage.py makemigrations --check --dry-run
python manage.py test --noinput
node --test tests_js/*.test.js
node --check static/js/*.js
```

Production-style `check --deploy` requires explicit local placeholder settings and Redis when reproducing shared-cache behavior. Do not place secret-like example values in documentation; use local shell variables or the repository's CI configuration as the exact reference.

The Node syntax gate expects Node.js 18 or newer; CI uses its configured Node version.

## Handoff

Record:

- base and final exact SHA;
- branch and PR state;
- files changed and explicit non-goals;
- focused/full test totals;
- browser acceptance scope;
- migrations/dependencies/settings status;
- production access/deployment status;
- worktree and ahead/behind state.

Deployment, service restart, migration, Nginx/systemd changes and checkpoint movement require a separate explicitly authorized task.
