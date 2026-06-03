# Deployment Guide

## Recommended Target

This is a Django backend project. Deploy it to a Python WSGI/ASGI host such as Render, Railway, Fly.io, or a VPS with Gunicorn and Nginx.

Netlify can host a future static frontend, but it is not the right target for the full Django backend.

## Required Environment Variables

- `DJANGO_SECRET_KEY`: long random production secret
- `DJANGO_DEBUG=False`
- `DJANGO_ALLOWED_HOSTS`: comma-separated hostnames
- `DJANGO_SECURE_SSL_REDIRECT=True` when HTTPS is available
- `DJANGO_SESSION_COOKIE_SECURE=True` when HTTPS is available
- `DJANGO_CSRF_COOKIE_SECURE=True` when HTTPS is available
- `DJANGO_SECURE_HSTS_SECONDS=31536000` only after confirming HTTPS for the full site
- `DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS=True` only if all subdomains are HTTPS-only
- `DJANGO_SECURE_HSTS_PRELOAD=True` only if the domain is ready for browser preload

## Build Commands

```bash
python -m pip install -r requirements.txt
python manage.py migrate
python manage.py collectstatic --noinput
python manage.py check --deploy
```

## Runtime

Use a production server instead of `runserver`.

Example:

```bash
gunicorn djangoProject.wsgi:application
```

Add `gunicorn` to dependencies when a deployment target is chosen.

## Database

SQLite is acceptable for local development. Production should use PostgreSQL or another managed relational database.

Before switching databases:

1. Add database configuration via environment variables.
2. Run migrations on a staging database.
3. Verify upload and media paths.

## Media Files

User uploads and downloaded files live under `media/` locally. For production, use persistent storage or object storage. Do not rely on ephemeral container disk for important uploads.

## Rollback

Keep each deployed version tied to a Git commit. If deployment fails:

1. Revert to the previous known-good commit.
2. Re-run migrations only if rollback migrations are safe.
3. Confirm `check`, smoke URLs, and logs.
