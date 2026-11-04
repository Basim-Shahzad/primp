# ADR-013: Profile, menu and settings edits

- **Review-gated fields**: only `name` and `category_id` of a **verified** business are held back. The new values go into `Business.pending_review_changes` (merged across edits), the live values stay, an audit entry records the request and `business_change_review_requested` is emitted after commit for Plan 6.3. Re-sending the current name is not a change. For draft/pending/needs_info/rejected/revoked businesses the edit applies at once.
- **PATCH semantics**: only fields present in the body are changed; an explicit `null` is a 422 except for `area_id` (clears the area) and a service's `category_id`.
- **Services**: duration 5-600 in steps of 5, price 0..10,000,000 PKR, `gender` column added (default unisex). Deleting a service that any appointment references only deactivates it (`{"deactivated": true}`); appointments keep their `price_at_booking`/name snapshots. Order endpoints require the complete id list (a permutation).
- **Closures** from `POST .../closures` are whole days (`starts_on`..`ends_on` inclusive, <= 366 days) converted to `[00:00, next-day 00:00)` in the business timezone, kind `holiday`, business-wide. Part-day / staff blocks arrive in Step 4 on the same model.
- **Photos** keep the original column pointing at the 1600 px copy; the 480 px and 160 px copies are stored next to it with `_480` / `_160` suffixes. Re-encoding strips all metadata.
- Every mutation calls `invalidation.touch(business_id)`: drops the cached public page and next-slot value and queues the signed frontend revalidation webhook (which retries with backoff).
- `GET .../preview` renders through the same function as the public page endpoint, so it matches exactly even for unlisted businesses.
