# ADR-029: Django admin hardening and state-changing admin actions

Status: accepted

## Context
The Django admin lets staff edit rows directly, which bypasses the audited, versioned services
that the admin API uses (suspensions, points, configuration, listings).

## Decision
- Every model whose changes must be audited is registered with `ReadOnlyAdmin`: view and
  search only; add, change, delete and bulk actions are refused (403). This covers AuditLog,
  PointTransaction, LoyaltyAccount, PointRule, Appointment, PaymentWebhookEvent,
  RefreshToken (no token hash), BusinessDocument (no file path) and PlatformSettingHistory.
- State-changing admin actions exist only as API endpoints under `/api/v1/admin/`, where they
  check permissions, re-authentication for high-risk actions and optimistic versions, and write
  exactly one audit row.
- The Django admin sits on a configurable path (`ADMIN_URL_PATH`) and can be limited to an IP
  allow-list (`ADMIN_ALLOWED_IPS`); other addresses get a bare 404. The client address uses the
  trusted-proxy logic, so a spoofed `X-Forwarded-For` does not help.
- The audit log is readable at `GET /admin/audit-log` (filters, pagination, allow-listed
  sorting) and exportable as CSV with spreadsheet-formula neutralisation; exports are audited.

## Consequences
Direct database-style edits through the Django admin are impossible for sensitive records;
everything goes through the audited path.
