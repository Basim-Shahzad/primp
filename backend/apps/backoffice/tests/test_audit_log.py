import csv
import io
from datetime import timedelta

import pytest
from django.utils import timezone

from apps.backoffice.tests.helpers import A, admin_headers, call
from apps.core import audit
from apps.core.models import AuditLog

pytestmark = pytest.mark.django_db
URL = f"{A}/audit-log"


def seed(admin_id: int) -> None:
    audit.record("admin.user_suspended", actor_id=admin_id, target_type="user", target_id="u1")
    audit.record("admin.config_changed", actor_id=admin_id, target_type="setting", target_id="k")
    audit.record("system.thing", actor_id=None, target_type="user", target_id="u2")


def test_listing_filters_and_actor_rendering() -> None:
    admin, h = admin_headers("super_admin")
    seed(admin.pk)
    body = call("get", URL, h).json()
    assert body["total"] >= 3 and body["limit"] == 20
    by_action = {r["action"]: r for r in body["items"]}
    assert by_action["admin.user_suspended"]["actor"] == str(admin.public_id)
    assert by_action["system.thing"]["actor"] is None
    q = call("get", f"{URL}?actor={admin.public_id}&action=admin.config_changed", h).json()
    assert [r["action"] for r in q["items"]] == ["admin.config_changed"]
    prefix = call("get", f"{URL}?action=admin.", h).json()
    assert {r["action"] for r in prefix["items"]} >= {
        "admin.user_suspended",
        "admin.config_changed",
    }
    assert call("get", f"{URL}?target_type=user&target_id=u2", h).json()["total"] == 1
    unknown = call("get", f"{URL}?actor=00000000-0000-0000-0000-000000000000", h).json()
    assert unknown["total"] == 0


def test_date_range_sorting_and_pagination() -> None:
    admin, h = admin_headers("super_admin")
    seed(admin.pk)
    now = timezone.now()
    old = AuditLog.objects.create(action="old.thing", created_at=now - timedelta(days=30))
    window = f"created_from={(now - timedelta(days=1)).isoformat()}".replace("+", "%2B")
    recent = call("get", f"{URL}?{window}", h).json()
    assert old.pk not in [r["id"] for r in recent["items"]]
    asc = call("get", f"{URL}?sort=created_at&limit=1", h).json()
    assert asc["items"][0]["id"] == old.pk and len(asc["items"]) == 1
    assert call("get", f"{URL}?sort=action", h).status_code == 422
    assert call("get", f"{URL}?limit=1000", h).status_code == 422
    assert (
        call("get", f"{URL}?limit=1&offset=1", h).json()["items"][0]["id"] != asc["items"][0]["id"]
    )


def test_support_agent_cannot_view() -> None:
    _, h = admin_headers("support_agent")
    assert call("get", URL, h).status_code == 403


def test_export_is_csv_injection_safe_and_audited() -> None:
    admin, h = admin_headers("super_admin")
    audit.record(
        "admin.thing",
        actor_id=admin.pk,
        target_type="=HYPERLINK(1)",
        target_id="+1",
        after={"x": "@y"},
    )
    r = call("get", f"{URL}/export.csv", h)
    assert r.status_code == 200 and r["Content-Type"].startswith("text/csv")
    assert r["Cache-Control"] == "no-store"
    rows = list(csv.reader(io.StringIO(b"".join(r.streaming_content).decode())))
    assert rows[0][0] == "id"
    line = next(x for x in rows if x[3] == "admin.thing")
    assert line[4] == "'=HYPERLINK(1)" and line[5] == "'+1" and line[2] == str(admin.public_id)
    assert AuditLog.objects.filter(action="admin.audit_exported", actor_id=admin.pk).count() == 1


def test_export_respects_filters() -> None:
    admin, h = admin_headers("super_admin")
    seed(admin.pk)
    r = call("get", f"{URL}/export.csv?action=system.thing", h)
    rows = list(csv.reader(io.StringIO(b"".join(r.streaming_content).decode())))
    assert [x[3] for x in rows[1:]] == ["system.thing"]
