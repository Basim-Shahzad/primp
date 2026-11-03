# Security notes

Checked in Step 16 (OWASP Top 10 mindset).

| Area | What is in place |
|---|---|
| Authentication | Argon2 password hashing, 15 min JWT access tokens (`HS256`, algorithm pinned, `exp/iat/sub/jti/sid` required), server-stored rotating refresh tokens (hash only) with reuse detection that revokes the whole family, shorter lifetimes for admin roles, suspended/deleted users and revoked sessions rejected on every request. |
| Cookies / CSRF | Refresh token in `httpOnly`, `Secure`, `SameSite=Lax` cookie scoped to `/api/v1/auth`; cookie-based refresh/logout need the double-submit `X-CSRF-Token` header. |
| Account abuse | 5 failed logins lock an account for 15 min with a generic error; OTP codes are hashed, single use, 5 attempts, 60 s cooldown, 5/hour per phone and a per-IP cap; password reset never reveals whether an address exists and tokens are single use and expire. |
| Rate limiting | Every `/auth/*` route has a limit (`apps/accounts/ratelimits.py`, proven by `tests/test_hardening.py`); global anonymous and per-user throttles on the whole API; hold creation is limited per user. |
| Authorisation | Object-level checks live in services: another customer's appointment, review, hold or deletion request answers 404 (no existence leak); role checks answer 403. |
| Input handling | Pydantic validation on every body/query; images are validated by content, stripped of EXIF, resized and re-encoded; review text is plain text (tags stripped); search uses parameterised ORM queries only. |
| Data integrity | Double booking is prevented by row locks plus a PostgreSQL exclusion constraint; ledger and audit tables reject updates/deletes; unique constraints back every idempotency guarantee. |
| Secrets | All configuration from the environment; production refuses to start without secrets; `.env.example` holds placeholders only. |
| Logging | JSON logs with request IDs; a log-record factory redacts emails, phone numbers, JWTs, bearer tokens, hold tokens, OTP codes and location/token query values; `tests/test_hardening.py` captures logs across the main flows and asserts nothing sensitive appears. Sentry runs with `send_default_pii=False`. |
| Transport / headers | `check --deploy` passes with production settings (HTTPS redirect, HSTS with preload, secure cookies, `nosniff`, `X-Frame-Options: DENY`, same-origin referrer policy); CORS is an explicit allow-list. |
| Dependencies | `make audit` (`pip-audit`) reported no known vulnerabilities on the locked versions when this was written. Re-run it regularly. |

Known limits (see also `FOLLOW_UPS.md`): the Prometheus `/metrics` route must be blocked from the public
internet at the proxy; the GDAL system check `gis.W001` is silenced because the API never opens
user-supplied spatial references.
