# ADR-009: Loyalty points (earning only)

- Booking points are pro-rata and floored: `floor(amount * points / unit)`, so PKR 1,999 earns 19 points (rule: 10 per PKR 1,000). Rules live in the database (`PointRule`, editable in the admin); the newest active rule inside its validity window wins.
- Credits happen on `booking_completed` only. Milestone bonuses (1st, 5th, 10th completed booking) are keyed to the customer and awarded when the completed count reaches the threshold, so each is awarded exactly once even if completions race or a rule is enabled late.
- The ledger is append-only and `(reason, reference_type, reference_id)` is unique, so replayed events never double-credit. Reversals are new negative rows linked to the original (`<reason>_reversal`), also idempotent. Booking points reverse on a `refunded` payment; milestone bonuses stay.
- `LoyaltyAccount.balance`/`lifetime_earned` are updated in the same transaction under a row lock; both equal the ledger sum until redemption exists (Phase 2). A nightly beat task compares summaries with the ledger, logs mismatches at ERROR and can repair on demand.
- Profile completion = name, photo, default city and a verified phone or email; checked on every profile change (once-only via the unique reference).
- Points carry `expires_at` (12 months) and `remaining` for later FIFO redemption; expiry processing is out of scope.
