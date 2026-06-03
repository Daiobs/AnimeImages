# Development Guide

## Baseline

- Python: 3.14.5
- Django: 5.2.x
- Project root: the directory containing `manage.py`
- PyCharm interpreter: `.venv/bin/python`

## Daily Workflow

1. Pull or sync the latest project files.
2. Activate `.venv` or select it in PyCharm.
3. Run checks before meaningful commits:

```bash
python manage.py check
python manage.py makemigrations --check --dry-run
python manage.py test
```

4. Run the app locally:

```bash
python manage.py runserver
```

## Git Workflow

Use Git for every delivery-quality change. Keep commits small and named by intent.

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
python manage.py check
python manage.py test
python manage.py collectstatic --noinput
```

For production-like validation:

```bash
DJANGO_DEBUG=False \
DJANGO_SECRET_KEY=replace-with-a-long-random-secret \
DJANGO_ALLOWED_HOSTS=example.com \
DJANGO_SECURE_SSL_REDIRECT=True \
DJANGO_SESSION_COOKIE_SECURE=True \
DJANGO_CSRF_COOKIE_SECURE=True \
DJANGO_SECURE_HSTS_SECONDS=31536000 \
python manage.py check --deploy
```
