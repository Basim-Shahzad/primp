# Follow-ups (items blocked on external inputs or left for later plans)

## Needs credentials / infrastructure

- **Payment gateways:** JazzCash, Easypaisa and Stripe adapters need merchant credentials. Done: the
  `PaymentGateway` interface, `FakeGateway`, a signed idempotent webhook endpoint and the `pay_at_venue`
  flow. Not wired: online checkout (creating a payment during booking and confirming on payment).
- **SMS provider:** `SMS_ADAPTER=http` is a generic JSON-over-HTTP skeleton (5xx transient, 4xx permanent).
  The real provider's request/response format, credentials and sender ID are still needed.
- **Push notifications:** the `push` channel exists in the model but has no adapter.
- **Docker verification:** the sandbox this was built in has no Docker daemon, so `docker compose up` was
  not run here. The same flow (migrate, `seed_demo`, API, `/readyz`, `scripts/smoke_test.py`) was verified
  against locally installed PostgreSQL/PostGIS and Redis.
- **Object storage:** S3/R2/MinIO is configured through `AWS_*` variables but only the in-memory and
  filesystem backends were exercised.

## For Plans 6.2 and 6.3

- Provider endpoints should call the service functions that already exist: `bookings.services.confirm`,
  `complete`, `mark_no_show`, `cancel_by_business`, `accept_reschedule`, `decline_reschedule`.
- Admin endpoints should call `reviews.services.moderate_review` (hide/remove/restore) and turn
  `ReviewOutbox` rows (`review_flagged`) into support tickets.
- Account deletion requests are only recorded (`AccountDeletionRequest`); processing belongs to 6.3.
- `ReviewReply` exists but has no endpoint yet (6.2).

## Phase 2 / nice to have

- Loyalty redemption and point expiry processing (`expires_at` and `remaining` are stored already).
- Urdu translations (strings are wrapped with `gettext_lazy`; notification templates support a language).
- Hold limits per user beyond the per-minute rate limit.
