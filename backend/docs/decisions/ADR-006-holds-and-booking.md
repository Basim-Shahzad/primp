# ADR-006: Holds, soft holds and booking transactions

- A hold is a Redis key (`SET NX PX`, default 300 s) plus a token index; it stores the services, staff and times so `POST /appointments` only needs the token. Capacity mode adds a slot suffix so `parallel_capacity` holds can coexist on one start time.
- Check-then-hold is serialised with a Redis lock per bookable resource; "any staff" tries candidates least-busy first.
- If Redis is down when a hold is requested, the API issues a signed *soft hold* (stateless token, 5 minute max age) and logs a warning. Soft holds reserve nothing; the booking transaction (business or staff row lock + availability re-check + PostgreSQL exclusion constraint on staff time ranges) is what prevents double booking. A real (non-soft) token cannot be verified while Redis is down: 503 `holds_unavailable`.
- Booking: lock business row (capacity mode) or staff row (staff mode), re-run the availability engine ignoring the caller's own hold, snapshot prices/durations, write the timeline event, emit domain events after commit.
- A hold owned by someone else looks like 404; an expired/unknown hold is 409 `hold_expired`; a lost race is 409 `slot_unavailable`.
- `Idempotency-Key` is optional; a replay returns the original booking with 200. Without a key a replay fails with `hold_expired` because the hold is consumed.
- `complete` and `mark_no_show` are only allowed once the appointment has started, so providers cannot complete future bookings to farm loyalty points.
- Only `pay_at_venue` is bookable; online payment awaits real gateway adapters (FOLLOW_UPS).
