# ADR-003: Public listing policy and business lookup

Context: the plan says "listed + verified-or-pending policy per ADR: only `is_listed=True` businesses appear".
Decision: a business is publicly visible iff `is_listed=True` and its city is active with visibility `public`.
`verification_status` does not gate visibility; it only drives the verified badge and `verified_only` filter.
Admins (Plan 6.3) control `is_listed`. Slugs are unique per city, so `GET /businesses/{ref}` accepts a public UUID
or a slug with an optional `city` query parameter; an ambiguous slug resolves to the oldest business.
Consequences: hidden/preview cities never leak businesses to customers.
