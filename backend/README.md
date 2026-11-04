# Primp backend

Django 5.2 + Django Ninja API for **Primp**, a beauty and grooming booking marketplace for Pakistan.
This service implements the customer side (PRD 6.1) and the shared foundation that the provider (6.2)
and admin (6.3) plans build on. Run every command from `backend/`.

## Architecture

```text
config/            settings/{base,dev,test,prod}.py, api.py (Ninja v1 + routers), celery.py, urls.py
apps/
  core/            base models, error envelope, cursor pagination, audit log, adapters, health, logging
  accounts/        User, JWT + rotating refresh tokens, OTP, Google, profile, wishlist
  geo/             City, Area
  catalog/         two-level category taxonomy
  businesses/      Business, services, staff, hours, closures; search; public business page
  bookings/        availability engine, Redis holds, appointments, status machine, history
  reviews/         reviews, flags, rating aggregates, moderation service
  loyalty/         point rules, append-only ledger, account summary, reconciliation
  notifications/   templates, dispatcher, SMS/email adapters, reminders
  payments/        gateway interface, fake gateway, signed idempotent webhooks
docs/              progress/, decisions/ (ADRs), OUTSIDE_SCOPE.md, FOLLOW_UPS.md
tests/             cross-cutting tests (OpenAPI freshness, rate limits, PII-in-logs)
```

Rules the code follows (enforced by `import-linter`, `mypy --strict`, `ruff`):

- Thin endpoints in `api.py`; writes in `services.py`, reads in `selectors.py`. Apps never import another
  app's `models`; they use its `services`, `selectors`, `schemas` or domain events.
- Money is integer PKR. Timestamps are stored in UTC; availability and "open now" run in the business
  timezone (`Asia/Karachi`).
- Every error uses `{"code": "...", "message": "...", "fields": {...}}`.
- Domain events (`apps/*/events.py`) are sent after commit; notifications and loyalty subscribe to them.
- Postgres (with PostGIS, `btree_gist`, `pg_trgm`) enforces the hard rules: staff booking overlap is an
  exclusion constraint, opening-hours overlap is an exclusion constraint, ledger rows are unique per event.

Design decisions are recorded in `docs/decisions/ADR-*.md`.

## Setup with Docker

```bash
cp .env.example .env
docker compose up --build
```

This starts PostGIS, Redis, Mailpit, MinIO, the API (migrates and loads `seed_demo` data first), a Celery
worker and Celery beat. Check `http://localhost:8000/readyz` and the docs at `/api/v1/docs`.

## Setup without Docker

Requires Python 3.12+, [uv](https://docs.astral.sh/uv/), PostgreSQL 16 with PostGIS, Redis and GDAL/GEOS/PROJ.

```bash
sudo apt-get install postgresql-16-postgis-3 redis-server libgdal-dev libgeos-dev libproj-dev
make install
createuser --superuser primp && createdb -O primp primp   # password "primp" in the default DATABASE_URL
cp .env.example .env
make migrate
DJANGO_SETTINGS_MODULE=config.settings.dev uv run python manage.py seed_demo
make run          # API on :8000
make worker       # Celery worker (queues critical, default, bulk)
make beat         # reminder scan every 5 min, nightly loyalty reconciliation
```

The extensions are created by a migration (`btree_gist`, `pg_trgm`) and by Django's PostGIS backend, so the
database user must be allowed to create extensions (a superuser, or pre-create them).

## Commands

| Command | What it does |
|---|---|
| `make install` | install dependencies with uv |
| `make lint` | ruff check, ruff format check, import-linter contracts |
| `make typecheck` | mypy (strict) with django-stubs |
| `make test` | pytest on real PostgreSQL/PostGIS and Redis |
| `make cov` | tests with coverage gate (90 % lines and branches) |
| `make bench` | slow tests, including the 5,000-business search benchmark |
| `make check` | lint + typecheck + test |
| `make openapi` | regenerate `openapi.json` (a test fails if it is stale) |
| `make audit` | `pip-audit` dependency scan |
| `make smoke` | end-to-end smoke test against a running API (`BASE_URL`, default `http://localhost:8000`) |
| `make run` / `worker` / `beat` / `migrate` | run services |

Tests use `TEST_REDIS_URL` (default DB 15) and create their own PostgreSQL test database. Only external
boundaries are faked: SMS, email (locmem), Google token verification, payment gateway, object storage
(in-memory) and time (`time-machine`).

## Environment variables

Set in the environment or `.env`. Production (`config.settings.prod`) refuses to start without
`DJANGO_SECRET_KEY`, `DJANGO_ALLOWED_HOSTS`, `DATABASE_URL`, `REDIS_URL`, `CELERY_BROKER_URL`,
`JWT_SIGNING_KEY` and `CORS_ALLOWED_ORIGINS`.

| Variable | Default | Purpose |
|---|---|---|
| `DJANGO_SETTINGS_MODULE` | `config.settings.dev` (manage.py), `prod` (asgi/wsgi) | settings module |
| `DJANGO_DEBUG` | `False` (`True` in dev) | debug mode |
| `DJANGO_SECRET_KEY` | insecure dev key | Django secret (required in prod) |
| `DJANGO_ALLOWED_HOSTS` | empty | comma separated hosts (required in prod) |
| `DATABASE_URL` | `postgis://primp:primp@localhost:5432/primp` | PostGIS database |
| `DB_CONN_MAX_AGE` | `60` | connection reuse seconds |
| `REDIS_URL` | `redis://localhost:6379/0` | cache, holds, rate limits, OTP |
| `CELERY_BROKER_URL` | `redis://localhost:6379/1` | Celery broker |
| `CORS_ALLOWED_ORIGINS` | empty (`localhost:3000` in dev) | CORS allow-list |
| `JWT_SIGNING_KEY` | insecure dev key | HS256 signing key (required in prod) |
| `ACCESS_TOKEN_MINUTES` | `15` | access token lifetime |
| `ADMIN_ACCESS_TOKEN_MINUTES` | `5` | access token lifetime for admin roles |
| `REFRESH_TOKEN_DAYS` | `30` | refresh token lifetime |
| `ADMIN_REFRESH_TOKEN_HOURS` | `8` | refresh token lifetime for admin roles |
| `REFRESH_COOKIE_NAME` | `primp_refresh` | refresh cookie name |
| `REFRESH_COOKIE_SECURE` | `True` | `Secure` flag on refresh/CSRF cookies |
| `REFRESH_CSRF_COOKIE_NAME` | `primp_csrf` | double-submit CSRF cookie |
| `REFRESH_TOKEN_IN_BODY` | `False` | also return/accept refresh tokens in JSON (non-browser clients) |
| `GOOGLE_CLIENT_IDS` | empty | allowed Google OAuth client IDs (audience) |
| `FRONTEND_URL` | `http://localhost:3000` | base URL for emailed links |
| `FRONTEND_REVALIDATE_URL` | empty | frontend revalidation webhook (no-op when empty) |
| `FRONTEND_REVALIDATE_SECRET` | empty | HMAC secret for that webhook |
| `SMS_ADAPTER` | `console` | `console`, `fake` or `http` |
| `SMS_API_URL` / `SMS_API_KEY` / `SMS_SENDER_ID` | empty / empty / `Primp` | HTTP SMS provider |
| `EMAIL_BACKEND` / `EMAIL_HOST` / `EMAIL_PORT` | console / `localhost` / `1025` | email delivery |
| `DEFAULT_FROM_EMAIL` | `Primp <no-reply@primp.pk>` | sender address |
| `AWS_STORAGE_BUCKET_NAME` | empty | enables S3/R2/MinIO storage when set |
| `AWS_S3_ENDPOINT_URL` / `AWS_S3_CUSTOM_DOMAIN` | empty | S3 endpoint and public domain |
| `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` | empty | storage credentials |
| `BOOKING_HOLD_SECONDS` | `300` | slot hold lifetime |
| `REMINDER_24H_SECONDS` / `REMINDER_2H_SECONDS` | `86400` / `7200` | reminder offsets |
| `REMINDER_GRACE_SECONDS` | `1800` | how long after its window opens a reminder may still be sent |
| `PAYMENT_GATEWAY` | `fake` | payment gateway adapter |
| `PAYMENT_WEBHOOK_SECRET` | dev secret | webhook signing secret |
| `THROTTLE_ANON` / `THROTTLE_USER` | `120/min` / `600/min` | global API throttles |
| `TRUSTED_PROXY_COUNT` | `0` | proxies in front of the app (for `X-Forwarded-For`) |
| `API_DOCS_ENABLED` | `True` (`False` in prod) | serve `/api/v1/docs` |
| `LOG_LEVEL` | `INFO` | log level |
| `LOG_REDACT_PII` | `True` (`False` in dev) | redact emails, phones, tokens from logs |
| `SENTRY_DSN` | empty | enables Sentry (no PII sent) |
| `NOTIFICATIONS_RAISE_ON_RENDER_ERROR` | `False` | raise on template errors (tests) |
| `LOYALTY_RAISE_ON_ERROR` | `False` | raise on loyalty handler errors (tests) |
| `PROVIDER_TERMS_VERSION` | `2026-01` | version of the provider agreement/commission terms owners must accept |
| `CITY_MAX_RADIUS_KM` | `60` | max distance of a business pin from its city centre |
| `PRIVATE_MEDIA_ROOT` | `private_media/` | folder for verification documents (never served publicly) |
| `AWS_PRIVATE_BUCKET_NAME` | empty | private bucket for verification documents (enables S3 for them) |
| `PRIVATE_URL_TTL_SECONDS` | `300` | validity of signed document download links |
| `DOCUMENT_MAX_BYTES` | `10485760` | max verification document size (10 MB) |
| `ANALYTICS_RAISE_ON_ERROR` | `False` | raise on daily-stats refresh errors (tests) |
| `PENDING_ESCALATION_MINUTES` | `120` | pending manual-accept booking: alert the owner after this long |
| `PENDING_AUTO_CANCEL_MINUTES` | `720` | pending booking auto-cancelled after this long (or at its start time, whichever is first) |
| `PROVIDER_COMPLETE_TOLERANCE_MINUTES` | `15` | an owner may mark a booking complete this long before its start |
| `PROVIDER_NO_SHOW_GRACE_MINUTES` | `15` | an owner may mark a no-show only this long after the start |

## API overview

All routes live under `/api/v1/` (see `openapi.json` or `/api/v1/docs`):

- **auth** `register, login, refresh, logout, otp/request, otp/verify, google, password/reset/*, email/verify`
- **users/me** profile, photo, phone/email change, deletion request, points, appointments, wishlist
- **search/businesses**, **businesses/{slug}**, `{id}/availability`, `{id}/reviews`
- **appointments** `hold`, create, `{id}` detail/policy/cancel/reschedule/rebook-draft/review
- **reviews** edit and flag; **payments/webhooks/{provider}**; `/healthz`, `/readyz`, `/metrics`

`/metrics` (Prometheus) should be reachable only from the internal network; see `docs/OUTSIDE_SCOPE.md`.

## Quality gates

`make check` must pass before every commit: ruff, import contracts, strict mypy and the full test suite.
`make cov` enforces at least 90 % line and branch coverage. Security notes are in `docs/SECURITY.md`.
