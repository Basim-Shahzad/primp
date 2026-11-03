# ADR-010: Booking history tabs

- Tabs partition a customer's bookings at "now": **cancelled** (any cancelled status), **upcoming** (pending/confirmed starting now or later), **past** (everything else: completed, no-show, and bookings whose time passed without being closed). A booking starting exactly now is upcoming.
- Upcoming is ordered by start ascending, past and cancelled by start descending, all with an id tie-breaker and signed cursors.
- Allowed actions: `can_cancel` = pending/confirmed and not started; `can_reschedule` = same and no pending request; `can_review` = completed and not yet reviewed.
- `rebook-draft` only reads: it returns the original services/staff, flags inactive services or an unavailable staff member and says whether the business can still be booked; it never creates a booking or hold.
