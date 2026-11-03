# ADR-008: Reviews

- Review text is plain text. HTML tags are stripped and control characters removed on the way in (policy), so stored reviews never carry markup; clients must still render as text.
- Create/edit are customer-only; flagging is open to any authenticated user (providers report too) except the review's author. Hidden/removed reviews return 404 to everyone but moderation.
- Edit window is 48 h inclusive (`now - created_at <= 48 h`). Editing is limited to visible reviews.
- Photos (max 3) are validated, stripped of metadata and resized to 1280 px before anything is stored; if creation fails the stored files are deleted.
- The business aggregate (`rating_avg`, `rating_count`) is recomputed from visible reviews inside the same transaction on create, edit and moderation, and the public page cache is invalidated.
- `review_flagged` is stored as a `ReviewOutbox` row (for Plan 6.3 to turn into a ticket) and also emitted as a signal after commit. `review_created` is emitted for loyalty points.
- `moderate_review` (hide/remove/restore) is a service for Plan 6.3 to expose; it writes an audit entry.
