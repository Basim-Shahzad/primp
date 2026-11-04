# Admin panel backend

All endpoints live under `/api/v1/admin/` and are tagged `admin` in the OpenAPI schema.

## Roles and permissions
Accounts with role `admin` or `super_admin`. `super_admin` has every permission; `admin`
accounts get the permissions of their group (`support_agent`, `ops_admin`). Create the first
super admin with `uv run python manage.py create_super_admin --email you@example.com`.
The permission table is `backoffice/permissions.py`; high-risk permissions also need a
re-authentication in the last 15 minutes (`POST /admin/reauth`).

## Conventions
- Lists use `limit`/`offset` and return `total`; `sort` accepts only allow-listed values.
- Mutable records carry a `version`; send the one you read, a stale one returns 409
  `stale_version`.
- Every sensitive action writes one audit row (actor, action, target, before, after, IP).

## Areas
Users and admins, listing review and verification, categories, cities and areas, tickets and
review moderation, banners, manual points adjustment, platform configuration
(`/config`, `/notification-templates`, `/point-rules`) and the audit log
(`/audit-log`, `/audit-log/export.csv`).

## Django admin
Read-only for sensitive records (see ADR-029). Environment variables:

| Variable | Default | Meaning |
| --- | --- | --- |
| `ADMIN_URL_PATH` | `admin/` | URL prefix of the Django admin; change it in production |
| `ADMIN_ALLOWED_IPS` | empty | comma-separated addresses allowed to reach it (empty = all) |

## Tests
`apps/backoffice/tests/test_permission_matrix.py` walks every admin endpoint with every role;
register new endpoints in `tests/matrix.py`.
