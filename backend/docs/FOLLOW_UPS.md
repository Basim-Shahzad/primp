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

## Handed over to Plan 6.3 (admin)

- **Verification endpoints** only need to call `businesses.verification.mark_verified`,
  `revoke_verification`, `request_more_info` and `reject` (they audit, notify, refresh caches and emit
  `business_verification_changed`). Consumers of `business_submitted` build the review queue.
- **Pending name/category changes** of verified businesses are stored in `Business.pending_review_changes`
  and announced by `business_change_review_requested`. There is no function yet to approve or reject them
  (apply to `name`/`category`, clear the field, refresh caches): 6.3 must add it.
- **Documents**: admins review `BusinessDocument` rows (status `accepted`/`rejected`, `reviewer_note`) and
  open files with `businesses.files.signed_url(document.pk)` (private storage, 5-minute links).
- **Failed notifications**: `notifications.feed.failed_for_admin()` lists rows with `admin_flagged`.
- **Templates** are data (`NotificationTemplate`); an admin editor is a 6.3 task.
- Admin endpoints should call `reviews.services.moderate_review` (hide/remove/restore) and turn
  `ReviewOutbox` rows (`review_flagged`) into support tickets.
- Account deletion requests are only recorded (`AccountDeletionRequest`); processing belongs to 6.3.
- `ReviewReply` exists but has no endpoint yet (not part of Plan 6.2).

## Provider side: known limits

- No staff logins, real-time dashboard push, CRM, promotions or multi-branch UI (Phase 2).
- Revoking a verification removes the badge only; delisting a business is a separate admin decision.
- Walk-in bookings without an account have no way to be notified (no contact beyond the phone number).
- Analytics are bucketed by appointment start date; there is no discount concept yet, so revenue is the
  sum of `price_at_booking` of completed bookings.
- Private verification documents were verified on the in-memory and filesystem backends only; the S3
  variant (`AWS_PRIVATE_BUCKET_NAME`) is configured but not exercised, and the new `private_media` Docker
  volume could not be run in this sandbox (no Docker daemon).

## Phase 2 / nice to have

- Loyalty redemption and point expiry processing (`expires_at` and `remaining` are stored already).
- Urdu translations (strings are wrapped with `gettext_lazy`; notification templates support a language).
- Hold limits per user beyond the per-minute rate limit.

## Admin side (Plan 6.3)

- **TOTP two-factor for admins** (`ADMIN_2FA_REQUIRED`) is not implemented. Design: a `TotpDevice` row per admin
  (encrypted secret, confirmed_at, recovery-code hashes); `POST /admin/auth/login` returns a short-lived
  `challenge` token when a device exists, and `POST /admin/auth/2fa` exchanges challenge + 6-digit code (30 s
  window, +-1 step, replay-protected in the cache) for the session; admins without a device are forced through
  enrolment when the flag is on. Re-authentication would then accept password + code.
