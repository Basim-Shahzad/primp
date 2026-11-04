"""Every admin endpoint with the permission it needs (Plan 6.3 permission-matrix test).

Steps add their endpoints here. `test_permission_matrix.py` checks the registry is complete
against the OpenAPI schema and walks every endpoint x every role.
"""

from typing import Any

ZERO = "00000000-0000-0000-0000-000000000000"
FAKE = 987654
REASON = {"reason": "Because of a policy breach"}

# (method, path template) -> (permission or None for "any admin", json body, query string)
ENDPOINTS: dict[tuple[str, str], tuple[str | None, Any, str]] = {
    ("get", "/admin/me"): (None, None, ""),
    ("post", "/admin/reauth"): (None, {"password": "x"}, ""),
    ("get", "/admin/users"): ("users.view", None, ""),
    ("get", "/admin/users/{user_id}"): ("users.view", None, ""),
    ("post", "/admin/users/{user_id}/suspend"): ("users.suspend", REASON, ""),
    ("post", "/admin/users/{user_id}/reactivate"): ("users.suspend", REASON, ""),
    ("post", "/admin/users/{user_id}/force-logout"): ("users.force_logout", {}, ""),
    ("post", "/admin/users/{user_id}/password-reset"): ("users.reset_password", {}, ""),
    ("get", "/admin/deletion-requests"): ("users.view", None, ""),
    ("post", "/admin/deletion-requests/{request_id}/process"): ("users.suspend", {}, ""),
    ("get", "/admin/admins"): ("admins.manage", None, ""),
    ("post", "/admin/admins"): (
        "admins.manage",
        {"email": "new.admin@example.com", "name": "New", "group": "support_agent"},
        "",
    ),
    ("patch", "/admin/admins/{user_id}/groups"): ("admins.manage", {"group": "ops_admin"}, ""),
    ("get", "/admin/review-queue"): ("businesses.review", None, ""),
    ("get", "/admin/review-queue/{item_id}"): ("businesses.review", None, ""),
    ("post", "/admin/review-queue/{item_id}/claim"): ("businesses.review", {"version": 1}, ""),
    ("post", "/admin/review-queue/{item_id}/approve"): (
        "businesses.review",
        {
            "version": 1,
            "checklist": {
                "identity_matches": True,
                "address_plausible": True,
                "photos_genuine": True,
                "category_correct": True,
                "no_duplicate": True,
            },
        },
        "",
    ),
    ("post", "/admin/review-queue/{item_id}/reject"): (
        "businesses.review",
        {"version": 1, "reason": "No"},
        "",
    ),
    ("post", "/admin/review-queue/{item_id}/request-info"): (
        "businesses.review",
        {"version": 1, "message": "More please"},
        "",
    ),
    ("post", "/admin/review-queue/{item_id}/approve-change"): (
        "businesses.review",
        {"version": 1},
        "",
    ),
    ("post", "/admin/review-queue/{item_id}/reject-change"): (
        "businesses.review",
        {"version": 1, "reason": "No"},
        "",
    ),
    ("post", "/admin/review-queue/{item_id}/notes"): ("businesses.review", {"body": "note"}, ""),
    ("post", "/admin/businesses/{business_id}/revoke-verification"): (
        "businesses.revoke",
        REASON,
        "",
    ),
    ("get", "/admin/categories"): ("taxonomy.manage", None, ""),
    ("post", "/admin/categories"): ("taxonomy.manage", {"name": "Cat"}, ""),
    ("post", "/admin/categories/reorder"): (
        "taxonomy.manage",
        {"parent_id": None, "ids": []},
        "",
    ),
    ("patch", "/admin/categories/{category_id}"): (
        "taxonomy.manage",
        {"version": 1, "name": "X"},
        "",
    ),
    ("post", "/admin/categories/{category_id}/deactivate"): (
        "taxonomy.manage",
        {"version": 1},
        "",
    ),
    ("post", "/admin/categories/{category_id}/activate"): (
        "taxonomy.manage",
        {"version": 1},
        "",
    ),
    ("get", "/admin/cities"): ("geo.manage", None, ""),
    ("post", "/admin/cities"): ("geo.manage", {"name": "City", "lat": 24.0, "lng": 67.0}, ""),
    ("patch", "/admin/cities/{city_id}"): ("geo.manage", {"version": 1, "name": "X"}, ""),
    ("get", "/admin/cities/{city_id}/areas"): ("geo.manage", None, ""),
    ("post", "/admin/cities/{city_id}/areas"): (
        "geo.manage",
        {"name": "Area", "lat": 24.0, "lng": 67.0},
        "",
    ),
    ("patch", "/admin/cities/{city_id}/areas/{area_id}"): (
        "geo.manage",
        {"version": 1, "name": "X"},
        "",
    ),
    ("get", "/admin/tickets"): ("tickets.view", None, ""),
    ("get", "/admin/tickets/stats"): ("tickets.view", None, ""),
    ("get", "/admin/tickets/{ticket_id}"): ("tickets.view", None, ""),
    ("post", "/admin/tickets/{ticket_id}/assign"): (
        "tickets.handle",
        {"version": 1, "assignee_id": None},
        "",
    ),
    ("post", "/admin/tickets/{ticket_id}/status"): (
        "tickets.handle",
        {"version": 1, "status": "in_progress"},
        "",
    ),
    ("post", "/admin/tickets/{ticket_id}/messages"): ("tickets.handle", {"body": "hi"}, ""),
    ("post", "/admin/reviews/{review_id}/hide"): ("reviews.moderate", REASON, ""),
    ("post", "/admin/reviews/{review_id}/restore"): ("reviews.moderate", REASON, ""),
    ("post", "/admin/reviews/{review_id}/remove"): ("reviews.moderate", REASON, ""),
    ("get", "/admin/banners"): ("banners.manage", None, ""),
    ("post", "/admin/banners"): (
        "banners.manage",
        {
            "title": "Eid sale",
            "link_url": "/offers",
            "starts_at": "2030-01-01T00:00:00+05:00",
            "ends_at": "2030-01-02T00:00:00+05:00",
        },
        "",
    ),
    ("post", "/admin/banners/reorder"): ("banners.manage", {"ids": []}, ""),
    ("patch", "/admin/banners/{banner_id}"): ("banners.manage", {"version": 1}, ""),
    ("delete", "/admin/banners/{banner_id}"): ("banners.manage", None, "version=1"),
    ("post", "/admin/points/adjust/preview"): (
        "points.adjust",
        {"customer_id": ZERO, "amount": 5},
        "",
    ),
    ("post", "/admin/points/adjust"): (
        "points.adjust",
        {"customer_id": ZERO, "amount": 5, "category": "goodwill", "note": "a long enough note"},
        "",
    ),
    ("get", "/admin/points/adjustments"): ("points.adjust", None, ""),
    ("get", "/admin/config"): ("config.manage", None, ""),
    ("patch", "/admin/config/{key}"): ("config.manage", {"value": 400, "version": 0}, ""),
    ("get", "/admin/config/{key}/history"): ("config.manage", None, ""),
    ("get", "/admin/notification-templates"): ("config.manage", None, ""),
    ("post", "/admin/notification-templates/{template_id}/preview"): (
        "config.manage",
        {"body": "hi"},
        "",
    ),
    ("patch", "/admin/notification-templates/{template_id}"): (
        "config.manage",
        {"version": 1, "body": "hi"},
        "",
    ),
    ("get", "/admin/point-rules"): ("config.manage", None, ""),
    ("patch", "/admin/point-rules/{rule_id}"): ("config.manage", {"version": 1}, ""),
}
# endpoints that are open by design (they authenticate by credentials, not by a token)
PUBLIC: set[tuple[str, str]] = {("post", "/admin/auth/login")}
# multipart uploads need a file to get past validation; the matrix test sends one
UPLOADS: dict[tuple[str, str], str] = {
    ("post", "/admin/cities/{city_id}/areas/import"): "geo.manage",
    ("post", "/admin/banners/{banner_id}/images/{kind}"): "banners.manage",
}


def fill(template: str) -> str:
    return template.format(
        user_id=ZERO,
        request_id=FAKE,
        ticket_id=ZERO,
        item_id=ZERO,
        business_id=ZERO,
        category_id=FAKE,
        city_id=FAKE,
        area_id=FAKE,
        banner_id=FAKE,
        review_id=ZERO,
        key="slot_hold_seconds",
        template_id=FAKE,
        rule_id=FAKE,
        kind="desktop",
    )
