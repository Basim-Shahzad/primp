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
}
# endpoints that are open by design (they authenticate by credentials, not by a token)
PUBLIC: set[tuple[str, str]] = {("post", "/admin/auth/login")}


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
        key="commission_rate_bps",
        template_id=FAKE,
        rule_id=FAKE,
    )
