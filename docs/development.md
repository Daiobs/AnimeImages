# Development Guide

## Baseline

- Python: 3.14.5
- Django: 5.2.x
- Project root: the directory containing `manage.py`
- PyCharm interpreter: `.venv/bin/python`

## Daily Workflow

1. Pull or sync the latest project files.
2. Activate `.venv` or select it in PyCharm.
3. Create a local `.env` from `.env.example.local` when environment values need to differ from defaults. Runtime reads only the project-root `.env`; the example files are templates.
4. Run checks before meaningful commits:

```bash
.venv/bin/python manage.py check
.venv/bin/python manage.py makemigrations --check --dry-run
.venv/bin/python manage.py test
```

5. Run the app locally:

```bash
.venv/bin/python manage.py runserver
```

If `.venv` is already activated, the shorter `python manage.py ...` form is fine.

## Git Workflow

Use Git for every delivery-quality change. Keep commits small and named by intent.
Small routine changes can be pushed on a feature branch after local checks. Larger modules or risky infrastructure changes should open a PR, fill in `.github/pull_request_template.md`, and wait for CI before merging to `main`.

Suggested branch names:

- `feature/xhs-parser-service`
- `fix/submission-validation`
- `docs/deployment-guide`

Suggested commit message style:

```text
type: concise change summary
```

Examples:

- `docs: add deployment checklist`
- `feat: add image library initialization command`
- `fix: validate download save paths`

## Local Artifacts

Do not commit local/runtime artifacts:

- `.venv/`
- `.env`
- `__pycache__/`
- `staticfiles/`
- `static/images/` because it is a large local asset library
- local SQLite databases unless intentionally using a sample database
- generated or downloaded files under `media/`

## Before Handoff

Run:

```bash
.venv/bin/python manage.py check
.venv/bin/python manage.py test
.venv/bin/python manage.py collectstatic --noinput
```

## Before Draft PR

For larger modules, run the normal checks plus the same production-style checks used by CI:

```bash
git diff --check
.venv/bin/python manage.py check
.venv/bin/python manage.py makemigrations --check --dry-run
.venv/bin/python manage.py test --noinput
node --check xhs_download/services/platforms/kukutool_provider.js
DJANGO_DEBUG=False \
DJANGO_SECRET_KEY=ci-local-deploy-check-8c2f9b1e7a4d6c0f5e3b9a2d1c8f7e6a4b3d2c1f9e8a7b6c5d4e3f2a1b0c9d8 \
DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1,testserver \
DJANGO_CACHE_URL=redis://localhost:6379/0 \
ULTRA_HD_PROVIDER_ENABLED=False \
KUKUTOOL_PROVIDER_ENABLED=False \
THIRD_PARTY_PROVIDER_ENABLED=False \
THIRD_PARTY_PUBLIC_ACCESS_ENABLED=False \
DJANGO_SECURE_SSL_REDIRECT=True \
DJANGO_SESSION_COOKIE_SECURE=True \
DJANGO_CSRF_COOKIE_SECURE=True \
DJANGO_SECURE_HSTS_SECONDS=31536000 \
DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS=True \
DJANGO_SECURE_HSTS_PRELOAD=True \
.venv/bin/python manage.py check --deploy
.venv/bin/python manage.py inspect_third_party_provider --health --json
```

CI starts a Redis service for the full test job. Locally, run Redis on `localhost:6379` only when you need to reproduce Redis-backed behavior exactly; otherwise use the normal local checks above. The provider health command stays structured when Redis is unavailable and reports the cache issue in its JSON output.

The Node syntax check expects Node.js 18 or newer. CI installs Node 20; locally, either put `node` on `PATH` or run the same check with the executable configured as `KUKUTOOL_NODE_BINARY`.

Generic third-party parse diagnostics redact provider CDN URLs by default. Use `inspect_third_party_provider --show-urls` only for local smoke tests where you need to manually copy the provider URL.

Before committing, verify the file list:

```bash
git status --short --branch
(git diff --name-only; git ls-files --others --exclude-standard) | sort
```

Do not commit `.env`, SQLite databases, generated media, `staticfiles/`, or the local `static/images/` asset library.

For production-like validation:

```bash
DJANGO_DEBUG=False \
DJANGO_SECRET_KEY=local-production-check-7f4e2d9c8b1a6e5d3c0f9a8b7e6d5c4b3a2f1e0d9c8b7a6f5e4d3c2b1a0f9e8 \
DJANGO_ALLOWED_HOSTS=example.com \
DJANGO_CACHE_URL=redis://localhost:6379/0 \
ULTRA_HD_PROVIDER_ENABLED=False \
KUKUTOOL_PROVIDER_ENABLED=False \
THIRD_PARTY_PROVIDER_ENABLED=False \
THIRD_PARTY_PUBLIC_ACCESS_ENABLED=False \
DJANGO_SECURE_SSL_REDIRECT=True \
DJANGO_SESSION_COOKIE_SECURE=True \
DJANGO_CSRF_COOKIE_SECURE=True \
DJANGO_SECURE_HSTS_SECONDS=31536000 \
DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS=True \
DJANGO_SECURE_HSTS_PRELOAD=True \
.venv/bin/python manage.py check --deploy
```
