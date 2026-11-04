from typing import Any

import pytest

from apps.backoffice.tests.helpers import A, admin_headers, call, reauth
from apps.core.models import AuditLog
from apps.loyalty.models import PointRule
from apps.notifications.models import NotificationTemplate

pytestmark = pytest.mark.django_db


def boss() -> dict[str, str]:
    _, h = admin_headers("super_admin")
    assert reauth(h).status_code == 200
    return h


def tpl(key: str = "booking_confirmed", channel: str = "sms") -> NotificationTemplate:
    return NotificationTemplate.objects.get(key=key, channel=channel, language="en")


def patch_tpl(h: dict[str, str], t: NotificationTemplate, **data: Any) -> Any:
    return call("patch", f"{A}/notification-templates/{t.pk}", h, {"version": t.version, **data})


def test_templates_list_with_allowed_placeholders_and_filter() -> None:
    _, h = admin_headers("super_admin")
    rows = call("get", f"{A}/notification-templates?key=booking_confirmed", h).json()
    assert {r["channel"] for r in rows} == {"sms", "email", "in_app"}
    assert "business_name" in rows[0]["placeholders"] and rows[0]["version"] == 1


def test_template_edit_is_validated_versioned_and_audited() -> None:
    h = boss()
    t = tpl()
    r = patch_tpl(h, t, body="Hello, {business_name} at {time}!")
    assert r.status_code == 200, r.content
    t.refresh_from_db()
    assert t.body.startswith("Hello") and t.version == 2
    row = AuditLog.objects.get(action="admin.template_changed")
    assert row.target_id == str(t.pk) and row.before["version"] == 1 and row.after["version"] == 2


def test_template_rejects_unknown_placeholders_broken_braces_and_empty_body() -> None:
    h = boss()
    t = tpl()
    for body in ("Hi {nope}", "Hi {unclosed", "   "):
        assert patch_tpl(h, t, body=body).status_code == 422
    t.refresh_from_db()
    assert t.version == 1 and not AuditLog.objects.filter(action="admin.template_changed")


def test_template_stale_version_and_missing_template() -> None:
    h = boss()
    t = tpl()
    assert patch_tpl(h, t, is_active=False).status_code == 200
    stale = patch_tpl(h, t, is_active=True)
    assert stale.status_code == 409 and stale.json()["code"] == "stale_version"
    assert call("patch", f"{A}/notification-templates/999999", h, {"version": 1}).status_code == 404


def test_template_edit_needs_recent_reauthentication() -> None:
    _, h = admin_headers("super_admin")
    t = tpl()
    r = patch_tpl(h, t, body="Hi {business_name}")
    assert r.status_code == 403 and tpl().version == 1


def test_template_preview_renders_sample_values_without_saving() -> None:
    _, h = admin_headers("super_admin")
    t = tpl()
    r = call(
        "post",
        f"{A}/notification-templates/{t.pk}/preview",
        h,
        {"body": "{business_name} / {time}"},
    )
    assert r.status_code == 200 and r.json()["body"] == "<business_name> / <time>"
    bad = call("post", f"{A}/notification-templates/{t.pk}/preview", h, {"body": "{x}"})
    assert bad.status_code == 422
    assert (
        call("post", f"{A}/notification-templates/999999/preview", h, {"body": "a"}).status_code
        == 404
    )
    assert tpl().version == 1


def test_edited_template_is_used_without_restart() -> None:
    from apps.notifications.templating import render

    h = boss()
    t = tpl()
    patch_tpl(h, t, body="Edited for {business_name}")
    assert render("booking_confirmed", "sms", {"business_name": "Glow"})[1] == "Edited for Glow"


def rule() -> PointRule:
    return PointRule.objects.order_by("id").first()  # type: ignore[return-value]


def test_point_rules_list_and_edit_with_audit() -> None:
    h = boss()
    r = rule()
    assert len(call("get", f"{A}/point-rules", h).json()) == PointRule.objects.count()
    out = call("patch", f"{A}/point-rules/{r.pk}", h, {"version": 1, "points": 77})
    assert out.status_code == 200 and out.json()["points"] == 77 and out.json()["version"] == 2
    r.refresh_from_db()
    assert r.points == 77
    row = AuditLog.objects.get(action="admin.point_rule_changed")
    assert row.before["points"] != 77 and row.after["points"] == 77


def test_point_rule_validation_stale_and_missing() -> None:
    h = boss()
    r = rule()
    url = f"{A}/point-rules/{r.pk}"
    assert call("patch", url, h, {"version": 1, "points": -1}).status_code == 422
    assert (
        call(
            "patch",
            url,
            h,
            {
                "version": 1,
                "valid_from": "2030-01-02T00:00:00+05:00",
                "valid_to": "2030-01-01T00:00:00+05:00",
            },
        ).status_code
        == 422
    )
    assert call("patch", url, h, {"version": 1, "description": "x"}).status_code == 200
    assert call("patch", url, h, {"version": 1, "description": "y"}).status_code == 409
    assert call("patch", f"{A}/point-rules/999999", h, {"version": 1}).status_code == 404


def test_per_amount_rule_cannot_lose_its_unit() -> None:
    h = boss()
    r = PointRule.objects.filter(kind="per_amount").first()
    assert r is not None
    out = call("patch", f"{A}/point-rules/{r.pk}", h, {"version": r.version, "unit_amount": None})
    assert out.status_code == 422
    r.refresh_from_db()
    assert r.unit_amount and r.version == 1


def test_rule_window_can_be_cleared_with_explicit_null() -> None:
    h = boss()
    r = rule()
    url = f"{A}/point-rules/{r.pk}"
    d = {"version": 1, "valid_to": "2030-01-01T00:00:00+05:00"}
    assert call("patch", url, h, d).status_code == 200
    out = call("patch", url, h, {"version": 2, "valid_to": None})
    assert out.status_code == 200 and out.json()["valid_to"] is None
