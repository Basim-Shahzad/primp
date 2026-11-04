import threading
from datetime import timedelta
from typing import Any

import pytest
import time_machine
from django.contrib.gis.geos import Point
from django.db import connection
from django.test import Client
from django.utils import timezone

from apps.accounts.factories import UserFactory
from apps.accounts.roles import Role
from apps.backoffice import permissions as perms
from apps.backoffice.factories import make_admin
from apps.backoffice.models import QueueStatus, ReviewDecision, ReviewQueueItem
from apps.backoffice.receivers import enqueue
from apps.backoffice.services import listings as svc
from apps.backoffice.tests.helpers import A, admin_headers, call, reauth
from apps.businesses.factories import BusinessFactory
from apps.businesses.models import Business, BusinessDocument
from apps.businesses.tests.provider_helpers import P, owner_headers
from apps.catalog.factories import CategoryFactory
from apps.core.errors import AppError
from apps.core.models import AuditLog
from apps.geo.factories import CityFactory
from apps.notifications.models import Notification

pytestmark = pytest.mark.django_db
Q = f"{A}/review-queue"
PUBLIC = "/api/v1/businesses"
GOOD = {
    "identity_matches": True,
    "address_plausible": True,
    "photos_genuine": True,
    "category_correct": True,
    "no_duplicate": True,
}


def ops() -> tuple[Any, dict[str, str]]:
    return admin_headers("ops_admin")


def pending_business(**kw: Any) -> Business:
    kw.setdefault("verification_status", "pending")
    kw.setdefault("is_listed", False)
    kw.setdefault("owner", UserFactory(role=Role.BUSINESS_OWNER))
    return BusinessFactory(**kw)


def queued(
    business: Business | None = None, kind: str = "new_listing"
) -> tuple[Business, ReviewQueueItem]:
    business = business or pending_business()
    item = enqueue(business.pk, kind)
    assert item is not None
    return business, item


def act(h: dict[str, str], item: ReviewQueueItem, action: str, body: dict[str, Any]) -> Any:
    return call("post", f"{Q}/{item.public_id}/{action}", h, body)


def audit_for(business: Business, prefix: str = "business.") -> list[AuditLog]:
    return list(
        AuditLog.objects.filter(target_id=str(business.public_id), action__startswith=prefix)
    )


# ------------------------------------------------------------ consumers


def test_submission_creates_one_queue_item_even_if_delivered_twice(run_commit) -> None:  # type: ignore[no-untyped-def]
    from apps.businesses import events
    from apps.businesses.tests.test_onboarding import TERMS, complete_business

    owner, h = owner_headers()
    b = complete_business(owner)
    with run_commit():
        r = call("post", f"{P}/businesses/{b.public_id}/submit", h, TERMS)
    assert r.status_code == 200, r.content
    item = ReviewQueueItem.objects.get(business=b)
    assert item.kind == "new_listing" and item.status == "open" and item.version == 1
    events.business_submitted.send(
        sender=None, business_id=b.pk, submission=1
    )  # a duplicate delivery
    assert ReviewQueueItem.objects.filter(business=b).count() == 1


def test_resubmission_after_info_request_creates_a_new_item() -> None:
    b, item = queued()
    _, h = ops()
    assert (
        act(h, item, "request-info", {"version": 1, "message": "Upload a clearer CNIC"}).status_code
        == 200
    )
    Business.objects.filter(pk=b.pk).update(verification_status="pending")
    again = enqueue(b.pk, "new_listing")
    assert again is not None and again.pk != item.pk
    assert ReviewQueueItem.objects.filter(business=b).count() == 2
    assert enqueue(b.pk, "new_listing") is None  # the new one is open: no third


def test_change_request_event_queues_one_open_item_per_business(run_commit) -> None:  # type: ignore[no-untyped-def]
    owner, h = owner_headers()
    b = BusinessFactory(owner=owner, name="Old Name")
    with run_commit():
        call("patch", f"{P}/businesses/{b.public_id}", h, {"name": "New Name"})
        call("patch", f"{P}/businesses/{b.public_id}", h, {"category_id": CategoryFactory().pk})
    items = ReviewQueueItem.objects.filter(business=b)
    assert items.count() == 1 and items.get().kind == "change_request"


# ------------------------------------------------------------ queue list


def test_queue_is_oldest_first_filterable_and_excludes_closed_by_default() -> None:
    _, h = ops()
    city_a, city_b = CityFactory(), CityFactory()
    cat_a, cat_b = CategoryFactory(), CategoryFactory()
    start = timezone.now()
    with time_machine.travel(start - timedelta(hours=3), tick=False):
        b1, i1 = queued(pending_business(area__city=city_a, category=cat_a))
    with time_machine.travel(start - timedelta(hours=2), tick=False):
        b2, i2 = queued(pending_business(area__city=city_b, category=cat_b))
    with time_machine.travel(start - timedelta(hours=1), tick=False):
        b3, i3 = queued(
            BusinessFactory(verification_status="verified", pending_review_changes={"name": "x"}),
            "change_request",
        )
        b4, i4 = queued(pending_business())
    ReviewQueueItem.objects.filter(pk=i4.pk).update(status="closed", outcome="approved")

    def ids(qs: str = "") -> list[str]:
        r = call("get", f"{Q}{qs}", h)
        assert r.status_code == 200, r.content
        return [x["id"] for x in r.json()["items"]]

    assert ids() == [str(i1.public_id), str(i2.public_id), str(i3.public_id)]
    assert ids("?sort=-submitted_at") == [str(i3.public_id), str(i2.public_id), str(i1.public_id)]
    assert ids(f"?city_id={city_b.pk}") == [str(i2.public_id)]
    assert ids(f"?category_id={cat_a.pk}") == [str(i1.public_id)]
    assert ids("?kind=change_request") == [str(i3.public_id)]
    assert ids("?status=closed") == [str(i4.public_id)]
    data = call("get", f"{Q}?limit=2", h).json()
    assert data["total"] == 3 and len(data["items"]) == 2
    first = data["items"][0]
    assert (
        first["business"]["id"] == str(b1.public_id)
        and 3 * 3600 - 5 <= first["waiting_seconds"] <= 3 * 3600 + 60
    )
    assert first["version"] == 1 and first["assigned_to"] is None and b2 and b3 and b4


@pytest.mark.parametrize(
    "qs",
    [
        "?sort=business_id",
        "?sort=;",
        "?kind=x",
        "?status=x",
        "?limit=0",
        "?limit=101",
        "?city_id=abc",
    ],
)
def test_queue_validation(qs: str) -> None:
    _, h = ops()
    assert call("get", f"{Q}{qs}", h).status_code == 422


def test_queue_query_count_is_bounded(django_assert_max_num_queries) -> None:  # type: ignore[no-untyped-def]
    _, h = ops()
    for _i in range(15):
        queued()
    with django_assert_max_num_queries(10):
        assert call("get", f"{Q}?limit=50", h).status_code == 200


# ------------------------------------------------------------ detail


def test_detail_contains_preview_private_fields_documents_and_history() -> None:
    admin, h = ops()
    owner = UserFactory(role=Role.BUSINESS_OWNER, phone="+923001110000", email="owner@example.com")
    b = pending_business(
        owner=owner, name="Glow Studio", phone="+923001234567", address="12 Clifton"
    )
    b.submissions.create(
        number=1, terms_version="2026-01", accepted_at=timezone.now(), ip_address="203.0.113.7"
    )
    from apps.businesses import files
    from apps.businesses.tests.provider_helpers import png_bytes

    stored = files.save_private("documents/secret", png_bytes(), "png")
    doc = BusinessDocument.objects.create(
        business=b, kind="cnic_front", file=stored, content_type="image/png", size=10
    )
    _, item = queued(b)
    act(h, item, "claim", {"version": 1})
    call("post", f"{Q}/{item.public_id}/notes", h, {"body": "Looks fine so far"})
    r = call("get", f"{Q}/{item.public_id}", h)
    assert r.status_code == 200, r.content
    d = r.json()
    assert d["preview"]["name"] == "Glow Studio" and d["preview"]["id"] == str(b.public_id)
    assert (
        d["private"]["owner_email"] == "owner@example.com"
        and d["private"]["phone"] == "+923001234567"
    )
    assert d["private"]["location"] == {"lat": b.location.y, "lng": b.location.x}
    assert d["private"]["submissions"][0]["ip_address"] == "203.0.113.7"
    assert d["status"] == "in_review" and d["assigned_to"] == str(admin.public_id)
    assert [n["body"] for n in d["notes"]] == ["Looks fine so far"]
    (document,) = d["documents"]
    assert document["id"] == doc.pk and document["url"].startswith("/api/v1/files/documents/")
    assert 0 < document["expires_in_seconds"] <= 300
    assert stored not in r.content.decode() and "documents/secret" not in r.content.decode()
    assert Client().get(document["url"]).status_code == 200
    viewed = AuditLog.objects.filter(action="admin.documents_viewed")
    assert viewed.count() == 1 and viewed.get().after["document_ids"] == [doc.pk]
    assert viewed.get().actor_id == admin.pk and viewed.get().ip_address


def test_signed_document_links_expire_within_five_minutes(settings) -> None:  # type: ignore[no-untyped-def]
    settings.PRIVATE_URL_TTL_SECONDS = (
        3600  # misconfigured: the admin view still caps the advertised life
    )
    _, h = ops()
    b = pending_business()
    BusinessDocument.objects.create(
        business=b, kind="licence", file="documents/x.png", content_type="image/png", size=1
    )
    _, item = queued(b)
    doc = call("get", f"{Q}/{item.public_id}", h).json()["documents"][0]
    assert doc["expires_in_seconds"] == 300


def test_documents_are_withheld_without_documents_view_and_not_audited() -> None:
    admin = make_admin("ops_admin")
    b = pending_business()
    BusinessDocument.objects.create(
        business=b, kind="licence", file="documents/x.png", content_type="image/png", size=1
    )
    _, item = queued(b)
    actor = perms.AdminActor(admin, frozenset({"businesses.review"}), "s", None)
    data = svc.detail(actor, str(item.public_id))
    assert data["documents"] is None
    assert not AuditLog.objects.filter(action="admin.documents_viewed").exists()
    with pytest.raises(AppError) as exc:
        svc.detail(perms.AdminActor(admin, frozenset(), "s", None), str(item.public_id))
    assert exc.value.status == 403


def test_no_document_event_when_the_business_has_none() -> None:
    _, h = ops()
    _, item = queued()
    assert call("get", f"{Q}/{item.public_id}", h).json()["documents"] == []
    assert not AuditLog.objects.filter(action="admin.documents_viewed").exists()


def test_duplicate_detection_true_and_false_positives() -> None:
    _, h = ops()
    here = Point(67.0011, 24.8607, srid=4326)
    target = pending_business(name="Glow Beauty Salon", phone="+923001234567", location=here)
    same_phone = BusinessFactory(
        name="Totally Different", phone="+923001234567", location=Point(67.2, 24.9, srid=4326)
    )
    near_similar = BusinessFactory(
        name="Glow Beauty Saloon",
        phone="+923009999999",
        location=Point(67.00112, 24.86072, srid=4326),
    )
    near_other_name = BusinessFactory(
        name="Quick Auto Repair",
        phone="+923008888888",
        location=Point(67.00112, 24.86072, srid=4326),
    )
    far_same_name = BusinessFactory(
        name="Glow Beauty Salon", phone="+923007777777", location=Point(67.3, 24.95, srid=4326)
    )
    _, item = queued(target)
    dups = call("get", f"{Q}/{item.public_id}", h).json()["duplicates"]
    by_id = {d["business_id"]: d["reasons"] for d in dups}
    assert by_id == {
        str(same_phone.public_id): ["same_phone"],
        str(near_similar.public_id): ["nearby_similar_name"],
    }
    assert str(near_other_name.public_id) not in by_id and str(far_same_name.public_id) not in by_id
    assert str(target.public_id) not in by_id


def test_duplicate_thresholds_are_configurable(settings) -> None:  # type: ignore[no-untyped-def]
    _, h = ops()
    here = Point(67.0011, 24.8607, srid=4326)
    target = pending_business(name="Glow Salon", location=here, phone="")
    BusinessFactory(
        name="Glow Salon", location=Point(67.0016, 24.8607, srid=4326), phone=""
    )  # ~50 m
    _, item = queued(target)
    assert call("get", f"{Q}/{item.public_id}", h).json()["duplicates"] == []
    settings.DUPLICATE_DISTANCE_METERS = 100
    assert len(call("get", f"{Q}/{item.public_id}", h).json()["duplicates"]) == 1


def test_detail_404_and_422() -> None:
    _, h = ops()
    assert call("get", f"{Q}/00000000-0000-0000-0000-000000000000", h).status_code == 404
    assert call("get", f"{Q}/nope", h).status_code == 422


# ------------------------------------------------------------ approve


def test_approve_verifies_lists_notifies_audits_once_and_updates_search(run_commit) -> None:  # type: ignore[no-untyped-def]
    admin, h = ops()
    b = pending_business(name="Fresh Salon")
    _, item = queued(b)
    assert Client().get(f"{PUBLIC}/{b.public_id}").status_code == 404
    with run_commit():
        r = act(h, item, "approve", {"version": 1, "checklist": GOOD})
    assert r.status_code == 200, r.content
    assert (
        r.json()["status"] == "closed"
        and r.json()["outcome"] == "approved"
        and r.json()["version"] == 2
    )
    b.refresh_from_db()
    assert b.verification_status == "verified" and b.is_listed and b.verified_by_id == admin.pk
    page = Client().get(f"{PUBLIC}/{b.public_id}")
    assert page.status_code == 200 and page.json()["is_verified"] is True
    search = Client().get("/api/v1/search/businesses?verified_only=true").json()["items"]
    assert [i["name"] for i in search] == ["Fresh Salon"]
    rows = audit_for(b)
    assert len(rows) == 1 and rows[0].action == "business.verified"
    assert rows[0].actor_id == admin.pk and rows[0].ip_address
    assert rows[0].before["status"] == "pending" and rows[0].after["status"] == "verified"
    assert rows[0].after["checklist"] == GOOD and rows[0].after["queue_item"] == str(item.public_id)
    decision = ReviewDecision.objects.get(item=item)
    assert (
        decision.action == "approved"
        and decision.checklist == GOOD
        and decision.admin_id == admin.pk
    )
    assert (
        Notification.objects.filter(
            user=b.owner, template_key="onboarding_status_changed", channel="in_app"
        ).count()
        == 1
    )
    item.refresh_from_db()
    assert item.status == QueueStatus.CLOSED and item.closed_at and item.assigned_to_id == admin.pk


@pytest.mark.parametrize(
    "missing",
    ["identity_matches", "address_plausible", "photos_genuine", "category_correct", "no_duplicate"],
)
def test_approve_needs_every_checklist_item_confirmed(missing: str) -> None:
    _, h = ops()
    b, item = queued()
    r = act(h, item, "approve", {"version": 1, "checklist": {**GOOD, missing: False}})
    assert r.status_code == 422 and r.json()["code"] == "checklist_incomplete"
    assert r.json()["fields"]["checklist"] == [missing]
    b.refresh_from_db()
    assert b.verification_status == "pending" and not audit_for(b)
    body = {k: v for k, v in GOOD.items() if k != missing}
    assert act(h, item, "approve", {"version": 1, "checklist": body}).status_code == 422
    assert act(h, item, "approve", {"version": 1}).status_code == 422


def test_item_state_conflicts_and_validation() -> None:
    _, h = ops()
    b, item = queued()
    assert act(h, item, "approve", {"version": 0, "checklist": GOOD}).status_code == 422
    stale = act(h, item, "approve", {"version": 5, "checklist": GOOD})
    assert stale.status_code == 409 and stale.json()["code"] == "stale_version"
    assert act(h, item, "approve", {"version": 1, "checklist": GOOD}).status_code == 200
    closed = act(h, item, "reject", {"version": 2, "reason": "too late"})
    assert closed.status_code == 409 and closed.json()["code"] == "item_closed"
    assert act(h, item, "claim", {"version": 2}).status_code == 409
    b.refresh_from_db()
    assert b.verification_status == "verified" and len(audit_for(b)) == 1
    missing = Client()
    assert (
        call(
            "post",
            f"{Q}/00000000-0000-0000-0000-000000000000/approve",
            h,
            {"version": 1, "checklist": GOOD},
        ).status_code
        == 404
    )
    assert missing


def test_wrong_kind_actions_conflict() -> None:
    _, h = ops()
    new_b, new_item = queued()
    change_b, change_item = queued(
        BusinessFactory(pending_review_changes={"name": "N"}), "change_request"
    )
    assert act(h, new_item, "approve-change", {"version": 1}).json()["code"] == "wrong_kind"
    assert (
        act(h, change_item, "approve", {"version": 1, "checklist": GOOD}).json()["code"]
        == "wrong_kind"
    )
    assert act(h, change_item, "reject", {"version": 1, "reason": "no"}).status_code == 409
    assert act(h, change_item, "request-info", {"version": 1, "message": "no"}).status_code == 409
    assert act(h, new_item, "reject-change", {"version": 1, "reason": "no"}).status_code == 409
    new_b.refresh_from_db(), change_b.refresh_from_db()
    assert new_b.verification_status == "pending" and change_b.pending_review_changes == {
        "name": "N"
    }


def test_the_underlying_business_state_must_still_allow_the_decision() -> None:
    _, h = ops()
    b, item = queued()
    Business.objects.filter(pk=b.pk).update(
        verification_status="verified"
    )  # approved elsewhere meanwhile
    r = act(h, item, "approve", {"version": 1, "checklist": GOOD})
    assert r.status_code == 409 and r.json()["code"] == "invalid_transition"
    item.refresh_from_db()
    assert item.status == "open" and not ReviewDecision.objects.exists() and not audit_for(b)


# ------------------------------------------------------------ reject / request info


def test_reject_and_request_info_store_message_notify_and_audit_once(run_commit) -> None:  # type: ignore[no-untyped-def]
    admin, h = ops()
    b1, i1 = queued()
    with run_commit():
        r = act(
            h,
            i1,
            "reject",
            {
                "version": 1,
                "reason": "Not a salon",
                "checklist": {**GOOD, "identity_matches": False},
            },
        )
    assert r.status_code == 200 and r.json()["outcome"] == "rejected"
    b1.refresh_from_db()
    assert (
        b1.verification_status == "rejected"
        and b1.status_message == "Not a salon"
        and not b1.is_listed
    )
    rows = audit_for(b1)
    assert len(rows) == 1 and rows[0].action == "business.rejected" and rows[0].actor_id == admin.pk
    assert rows[0].after["checklist"]["identity_matches"] is False and rows[0].ip_address
    assert Notification.objects.filter(user=b1.owner, channel="in_app").count() == 1
    b2, i2 = queued()
    with run_commit():
        r = act(h, i2, "request-info", {"version": 1, "message": "Add the storefront photo"})
    assert r.status_code == 200 and r.json()["outcome"] == "info_requested"
    b2.refresh_from_db()
    assert (
        b2.verification_status == "needs_info" and b2.status_message == "Add the storefront photo"
    )
    rows = audit_for(b2)
    assert len(rows) == 1 and rows[0].action == "business.more_info_requested"
    decision = ReviewDecision.objects.get(item=i2)
    assert decision.message == "Add the storefront photo" and decision.checklist is None


@pytest.mark.parametrize("action,field", [("reject", "reason"), ("request-info", "message")])
@pytest.mark.parametrize("value", ["", "   ", "x" * 501])
def test_message_is_mandatory_and_bounded(action: str, field: str, value: str) -> None:
    _, h = ops()
    b, item = queued()
    assert act(h, item, action, {"version": 1, field: value}).status_code == 422
    b.refresh_from_db()
    assert b.verification_status == "pending" and not audit_for(b)
    assert act(h, item, action, {"version": 1, field: "x" * 500}).status_code == 200


def test_owner_sees_the_admin_message_and_can_resubmit_after_info_request() -> None:
    from apps.businesses.tests.test_onboarding import TERMS, complete_business

    owner, oh = owner_headers()
    b = complete_business(owner)
    Business.objects.filter(pk=b.pk).update(verification_status="pending")
    _, item = queued(b)
    _, h = ops()
    act(h, item, "request-info", {"version": 1, "message": "Clearer CNIC please"})
    mine = call("get", f"{P}/businesses/{b.public_id}", oh).json()
    assert mine["status"] == "needs_info" and mine["status_message"] == "Clearer CNIC please"
    assert call("post", f"{P}/businesses/{b.public_id}/submit", oh, TERMS).status_code == 200


# ------------------------------------------------------------ change requests


def test_approve_change_applies_name_and_category_and_refreshes_the_listing(run_commit) -> None:  # type: ignore[no-untyped-def]
    admin, h = ops()
    new_cat = CategoryFactory()
    b = BusinessFactory(
        name="Old Name", pending_review_changes={"name": "Brand New", "category_id": new_cat.pk}
    )
    assert Client().get(f"{PUBLIC}/{b.public_id}").json()["name"] == "Old Name"  # warm the cache
    _, item = queued(b, "change_request")
    with run_commit():
        r = act(h, item, "approve-change", {"version": 1})
    assert r.status_code == 200 and r.json()["outcome"] == "change_approved"
    b.refresh_from_db()
    assert (b.name, b.slug, b.category_id, b.pending_review_changes) == (
        "Brand New",
        "brand-new",
        new_cat.pk,
        None,
    )
    page = Client().get(f"{PUBLIC}/{b.public_id}").json()
    assert page["name"] == "Brand New" and page["category"]["id"] == new_cat.pk
    rows = audit_for(b)
    assert (
        len(rows) == 1
        and rows[0].action == "business.change_approved"
        and rows[0].actor_id == admin.pk
    )
    assert (
        rows[0].before["name"] == "Old Name"
        and rows[0].after["name"] == "Brand New"
        and rows[0].ip_address
    )
    assert Notification.objects.filter(user=b.owner, channel="in_app").count() == 1


def test_reject_change_discards_with_reason(run_commit) -> None:  # type: ignore[no-untyped-def]
    admin, h = ops()
    b = BusinessFactory(name="Old Name", pending_review_changes={"name": "Brand New"})
    _, item = queued(b, "change_request")
    with run_commit():
        r = act(h, item, "reject-change", {"version": 1, "reason": "Misleading name"})
    assert r.status_code == 200 and r.json()["outcome"] == "change_rejected"
    b.refresh_from_db()
    assert b.name == "Old Name" and b.pending_review_changes is None
    rows = audit_for(b)
    assert len(rows) == 1 and rows[0].action == "business.change_rejected"
    assert (
        rows[0].before == {"pending": {"name": "Brand New"}}
        and rows[0].after["reason"] == "Misleading name"
    )
    assert ReviewDecision.objects.get(item=item).message == "Misleading name"
    n = Notification.objects.get(user=b.owner, channel="in_app")
    assert n.payload["message"] == "Misleading name"


def test_change_decisions_without_pending_changes_conflict() -> None:
    _, h = ops()
    b = BusinessFactory(pending_review_changes=None)
    _, item = queued(b, "change_request")
    assert act(h, item, "approve-change", {"version": 1}).json()["code"] == "no_pending_changes"
    assert (
        act(h, item, "reject-change", {"version": 1, "reason": "x"}).json()["code"]
        == "no_pending_changes"
    )
    assert act(h, item, "reject-change", {"version": 1, "reason": ""}).status_code == 422
    item.refresh_from_db()
    assert item.status == "open"


def test_approving_a_change_to_a_deactivated_category_conflicts() -> None:
    _, h = ops()
    dead = CategoryFactory(is_active=False)
    b = BusinessFactory(pending_review_changes={"category_id": dead.pk})
    _, item = queued(b, "change_request")
    assert act(h, item, "approve-change", {"version": 1}).json()["code"] == "category_unavailable"
    b.refresh_from_db()
    assert b.pending_review_changes == {"category_id": dead.pk}


# ------------------------------------------------------------ claim / notes


def test_claiming_prevents_a_second_admin_from_reviewing() -> None:
    a1, h1 = ops()
    a2, h2 = ops()
    b, item = queued()
    r = act(h1, item, "claim", {"version": 1})
    assert r.status_code == 200 and r.json()["status"] == "in_review" and r.json()["version"] == 2
    assert r.json()["assigned_to"] == str(a1.public_id)
    other = act(h2, item, "claim", {"version": 2})
    assert other.status_code == 409 and other.json()["code"] == "already_claimed"
    blocked = act(h2, item, "approve", {"version": 2, "checklist": GOOD})
    assert blocked.status_code == 409 and blocked.json()["code"] == "already_claimed"
    assert act(h1, item, "claim", {"version": 1}).json()["code"] == "stale_version"
    assert act(h1, item, "claim", {"version": 2}).status_code == 200  # the holder may re-claim
    done = act(h1, item, "approve", {"version": 3, "checklist": GOOD})
    assert done.status_code == 200 and a2
    b.refresh_from_db()
    assert b.verification_status == "verified"


def test_notes_are_internal_and_validated() -> None:
    admin, h = ops()
    _, item = queued()
    r = call("post", f"{Q}/{item.public_id}/notes", h, {"body": "  Called the owner  "})
    assert (
        r.status_code == 201
        and r.json()["body"] == "Called the owner"
        and r.json()["author_id"] == str(admin.public_id)
    )
    for bad in ({}, {"body": ""}, {"body": "   "}, {"body": "n" * 2001}):
        assert call("post", f"{Q}/{item.public_id}/notes", h, bad).status_code == 422
    detail = call("get", f"{Q}/{item.public_id}", h).json()
    assert [n["body"] for n in detail["notes"]] == ["Called the owner"]
    owner_view = call(
        "get",
        f"{P}/businesses/{item.business.public_id}/onboarding",
        owner_headers(item.business.owner)[1],
    )
    assert "Called the owner" not in owner_view.content.decode()


# ------------------------------------------------------------ revoke


def test_revoke_removes_badge_and_verified_only_results() -> None:
    admin, h = ops()
    b = BusinessFactory(verification_status="verified", name="Glow")
    assert Client().get(f"{PUBLIC}/{b.public_id}").json()["is_verified"] is True
    stale = call(
        "post", f"{A}/businesses/{b.public_id}/revoke-verification", h, {"reason": "Fake documents"}
    )
    assert stale.status_code == 403 and stale.json()["code"] == "reauth_required"
    reauth(h)
    for bad in ({}, {"reason": ""}, {"reason": "r" * 501}):
        assert (
            call("post", f"{A}/businesses/{b.public_id}/revoke-verification", h, bad).status_code
            == 422
        )
    r = call(
        "post", f"{A}/businesses/{b.public_id}/revoke-verification", h, {"reason": "Fake documents"}
    )
    assert r.status_code == 200 and r.json() == {"status": "revoked"}
    assert Client().get(f"{PUBLIC}/{b.public_id}").json()["is_verified"] is False
    assert Client().get("/api/v1/search/businesses?verified_only=true").json()["items"] == []
    rows = audit_for(b)
    assert len(rows) == 1 and rows[0].action == "business.verification_revoked"
    assert (
        rows[0].actor_id == admin.pk
        and rows[0].ip_address
        and rows[0].after["revoked_reason"] == "Fake documents"
    )
    again = call(
        "post", f"{A}/businesses/{b.public_id}/revoke-verification", h, {"reason": "again"}
    )
    assert again.status_code == 409
    assert (
        call(
            "post",
            f"{A}/businesses/00000000-0000-0000-0000-000000000000/revoke-verification",
            h,
            {"reason": "x"},
        ).status_code
        == 404
    )
    assert len(audit_for(b)) == 1


def test_support_agents_cannot_revoke_or_review() -> None:
    _, h = admin_headers("support_agent")
    b = BusinessFactory(verification_status="verified")
    reauth(h)
    assert (
        call(
            "post", f"{A}/businesses/{b.public_id}/revoke-verification", h, {"reason": "x"}
        ).status_code
        == 403
    )
    assert call("get", Q, h).status_code == 403
    b.refresh_from_db()
    assert b.verification_status == "verified"


# ------------------------------------------------------------ concurrency


@pytest.mark.django_db(transaction=True)
def test_parallel_approve_and_reject_exactly_one_wins(frozen, long_tokens) -> None:  # type: ignore[no-untyped-def]
    _, h1 = ops()
    _, h2 = ops()
    b, item = queued()
    barrier = threading.Barrier(2)
    out: dict[str, int] = {}

    def approve() -> None:
        try:
            barrier.wait()
            out["approve"] = act(h1, item, "approve", {"version": 1, "checklist": GOOD}).status_code
        finally:
            connection.close()

    def reject() -> None:
        try:
            barrier.wait()
            out["reject"] = act(h2, item, "reject", {"version": 1, "reason": "Nope"}).status_code
        finally:
            connection.close()

    threads = [threading.Thread(target=approve), threading.Thread(target=reject)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(out.values()) == [200, 409], out
    b.refresh_from_db()
    winner = "verified" if out["approve"] == 200 else "rejected"
    assert b.verification_status == winner
    assert ReviewDecision.objects.filter(item=item).count() == 1
    rows = audit_for(b)
    assert len(rows) == 1 and rows[0].after["status"] == winner
    item.refresh_from_db()
    assert item.status == "closed" and item.version == 2
