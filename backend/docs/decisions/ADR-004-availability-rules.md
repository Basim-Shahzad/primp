# ADR-004: Availability rules

Context: the plan leaves several availability edge cases open.
Decisions:
- Slot starts are aligned to the clock grid (minutes since midnight divisible by `slot_interval_minutes`), not to the window open time.
- Appointment duration is the sum of selected service durations. `buffer_minutes` is reserved after every booking and after the candidate slot; it does not need to fit inside opening hours. Closures never get a buffer.
- Staff shifts for a weekday override business shifts when the staff member has any shift row that weekday; otherwise business shifts apply. Time off is modelled as a `Closure` (kind `leave`).
- Past dates return 422 `date_in_past`; dates beyond `max_advance_days` return an empty list.
- In capacity mode (staff-level booking off) the `staff_id` filter and staff-specific closures are ignored.
- Holds always carry a concrete staff id in staff mode; "any staff" is resolved when the hold is placed.
- Hold expiry and OTP/cooldown deadlines are stored as timestamps inside Redis values so they are exact and testable with a frozen clock.
- Bookings only read business data through `apps.businesses.selectors` (plain dataclasses), keeping import boundaries.
Consequences: appointments models exist from Step 7 so the engine can be tested against real bookings; Step 10 adds the booking services and endpoints.
