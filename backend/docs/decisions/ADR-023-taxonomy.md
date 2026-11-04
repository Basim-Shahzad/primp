# ADR-023: Category taxonomy administration

- **Shape**: two levels (top-level -> child). A parent must be top-level; a category with children cannot be nested; a category cannot become its own parent or move under its own child (422 `parent_id`). The model's `clean()` still re-validates on save.
- **Optimistic concurrency**: `Category.version` (new column) is sent on `PATCH`, `activate` and `deactivate` and bumped on every change including reordering; a mismatch is 409 `stale_version`. Row locks serialise two admins editing the same category, so exactly one wins.
- **Slugs** are lowercase letters, digits and hyphens (<= 50), unique; omitted on create they are generated from the name with a numeric suffix. A clash is 409 `slug_taken`.
- **Reorder** takes `parent_id` (null = top level) plus the complete ordered list of that parent's children; missing, duplicate or foreign ids are 422. New categories go last among their siblings.
- **Deactivation** keeps the row and every service, business and appointment that references it. The public taxonomy (`GET /categories`), onboarding and service selection (`get_active_category`) hide a category once it or its parent is inactive; existing services still show on business pages. A child cannot be reactivated while its parent is inactive (409 `parent_inactive`). Responses include usage counts (services, businesses).
- **Public cache**: `GET /categories` caches the active tree for 5 minutes under one key; every admin change deletes it immediately and again after commit.
- Each action writes exactly one audit entry (`admin.category_created|updated|activated|deactivated`, `admin.categories_reordered`) with before/after snapshots, actor and IP. The `taxonomy.manage` permission is required to view the admin tree too.
