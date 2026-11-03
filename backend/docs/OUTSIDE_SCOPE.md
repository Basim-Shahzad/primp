# Items outside `backend/` (for the repository owner)

Plan 6.1 only allows changes inside `backend/`. Add these yourself where they belong.

## CI workflow: `.github/workflows/backend.yml`

```yaml
name: backend
on:
  push:
    paths: ["backend/**"]
  pull_request:
    paths: ["backend/**"]
jobs:
  test:
    runs-on: ubuntu-latest
    defaults:
      run:
        working-directory: backend
    services:
      db:
        image: postgis/postgis:16-3.4
        env: {POSTGRES_USER: primp, POSTGRES_PASSWORD: primp, POSTGRES_DB: primp}
        ports: ["5432:5432"]
        options: >-
          --health-cmd "pg_isready -U primp" --health-interval 5s --health-retries 10
      redis:
        image: redis:7
        ports: ["6379:6379"]
    env:
      DATABASE_URL: postgis://primp:primp@localhost:5432/primp
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
      - run: sudo apt-get update && sudo apt-get install -y libgdal-dev libgeos-dev libproj-dev
      - run: make install
      - run: make lint typecheck
      - run: make cov
      - run: make audit
```

## Root `.gitignore`

Add `backend/media/`, `backend/media_test/`, `backend/staticfiles/`, `backend/.coverage` and `backend/htmlcov/`.

## Reverse proxy

- Block `/metrics` from the public internet (Prometheus scrapes it internally).
- Set `TRUSTED_PROXY_COUNT` to the number of proxies so client IPs (rate limits, audit log) are correct.
- Terminate TLS and send `X-Forwarded-Proto: https` (prod settings redirect to HTTPS and trust that header).

## Root README

Mention that the API lives in `backend/` and link to `backend/README.md`.
