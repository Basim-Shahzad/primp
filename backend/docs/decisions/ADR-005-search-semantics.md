# ADR-005: Search semantics

- Without `lat`/`lng` results are ordered by rating; with them, by distance. `radius_km` defaults to 5 when coordinates are given and must be one of 1, 3, 5, 10.
- `gender_type=women|men` also returns `unisex` businesses (they serve both); `unisex` returns only unisex.
- `price_min/price_max` match a business with at least one *active* service in the range; `starting_price` is the minimum active price (null without services, sorted last).
- The `available` filter cannot be a SQL predicate (it depends on live bookings). A SQL prefilter on opening hours narrows candidates, then candidates are checked in sort order with the availability engine until the page is full. `sort=soonest` ranks up to 300 candidates by their cached next slot.
- `is_wishlisted` is `null` for anonymous callers.
- Suggestions on an empty first page: wider radius (with counts), up to three nearby areas, up to three related top-level categories.
- Next-slot values are cached for 60 s; a cold page costs a bounded number of queries per business, a warm page a constant number.
