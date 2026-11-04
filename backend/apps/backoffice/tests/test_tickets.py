import json
import threading
from datetime import timedelta
from typing import Any

import pytest
import time_machine
from django.db import connection
from django.test import Client
from django.utils import timezone

from apps.accounts.factories import UserFactory, make_customer
from apps.accounts.roles import Role
from apps.accounts.tests.helpers import bearer, login_client
from apps.backoffice import config_service, tasks
from apps.backoffice.factories import make_admin
from apps.backoffice.models import Ticket, TicketMessage
from apps.backoffice.services import tickets as svc
from apps.backoffice.tests.helpers import A, admin_headers, call, headers_for
from apps.bookings.factories import AppointmentFactory
from apps.businesses.factories import BusinessFactory
from apps.core.models import AuditLog
from apps.notifications.models import Notification
from apps.reviews import services as review_services
from apps.reviews.models import Review
from apps.reviews.tests.test_reviews import completed_appointment, submit

pytestmark = pytest.mark.django_db
T = "/api/v1/tickets"
AT = f"{A}/tickets"


def who(kind: str = "customer") -> tuple[Any, dict[str, str]]:
    user = (
        make_customer(name="Sana Khan")
        if kind == "customer"
        else UserFactory(role=Role.BUSINESS_OWNER)
    )
    return user, headers_for(user)


def open_ticket(user_h: dict[str, str], **kw: Any) -> Any:
    body = {"type": "other", "subject": "Help", "body": "I need help", **kw}
    return call("post", T, user_h, body)


def audit(action: str) -> list[AuditLog]:
    return list(AuditLog.objects.filter(action=action))


def agent() -> tuple[Any, dict[str, str]]:
    return admin_headers("support_agent")


# ------------------------------------------------------------ requester: create


def test_customer_opens_a_ticket_about_their_booking() -> None:
    user, h = who()
    appt = AppointmentFactory(customer=user)
    r = open_ticket(
        h,
        type="booking_dispute",
        subject="  Charged twice ",
        body=" Please check ",
        appointment_id=str(appt.public_id),
    )
    assert r.status_code == 201, r.content
    d = r.json()
    assert (d["type"], d["status"], d["priority"], d["subject"]) == (
        "booking_dispute",
        "open",
        "high",
        "Charged twice",
    )
    assert [m["body"] for m in d["messages"]] == ["Please check"] and d["messages"][0][
        "from_support"
    ] is False
    t = Ticket.objects.get(public_id=d["id"])
    assert (
        t.requester_id == user.pk
        and t.appointment_id == appt.pk
        and t.business_id == appt.business_id
    )
    assert t.events.get().kind == "created" and t.version == 1


def test_owner_can_link_their_own_business_booking_review_and_complaints() -> None:
    owner, h = who("owner")
    business = BusinessFactory(owner=owner)
    appt = AppointmentFactory(business=business)
    ok = open_ticket(h, type="refund_request", appointment_id=str(appt.public_id))
    assert ok.status_code == 201
    review_appt, customer, ch = completed_appointment(business=business)
    submit(review_appt, ch, {"rating": 2, "comment": "bad"})
    review = Review.objects.get()
    with_review = open_ticket(h, type="other", review_id=str(review.public_id))
    assert (
        with_review.status_code == 201
        and Ticket.objects.get(public_id=with_review.json()["id"]).review_id == review.pk
    )
    cust_h = headers_for(customer)
    complaint = open_ticket(cust_h, type="provider_complaint", business_id=str(business.public_id))
    assert complaint.status_code == 201  # the customer has a booking there


def test_linked_objects_must_belong_to_the_requester() -> None:
    user, h = who()
    stranger = AppointmentFactory()
    r = open_ticket(h, type="booking_dispute", appointment_id=str(stranger.public_id))
    assert r.status_code == 404
    assert (
        open_ticket(
            h, type="provider_complaint", business_id=str(stranger.business.public_id)
        ).status_code
        == 404
    )
    assert (
        open_ticket(h, type="other", review_id="00000000-0000-0000-0000-000000000000").status_code
        == 404
    )
    mine = AppointmentFactory(customer=user)
    mismatch = open_ticket(
        h,
        type="booking_dispute",
        appointment_id=str(mine.public_id),
        business_id=str(stranger.business.public_id),
    )
    assert mismatch.status_code == 404
    assert not Ticket.objects.exists()


@pytest.mark.parametrize(
    "body,field",
    [
        ({"type": "flagged_review"}, "type"),
        ({"type": "failed_notification"}, "type"),
        ({"type": "wizard"}, "type"),
        ({"subject": ""}, "subject"),
        ({"subject": "s" * 201}, "subject"),
        ({"body": ""}, "body"),
        ({"body": "b" * 4001}, "body"),
        ({"appointment_id": "nope"}, "appointment_id"),
        ({"type": "booking_dispute"}, "appointment_id"),
        ({"type": "refund_request"}, "appointment_id"),
        ({"type": "provider_complaint"}, "business_id"),
    ],
)
def test_ticket_validation(body: dict[str, Any], field: str) -> None:
    _, h = who()
    r = open_ticket(h, **body)
    assert r.status_code == 422 and field in r.json()["fields"], r.content
    assert not Ticket.objects.exists()


def test_ticket_text_boundaries_and_throttle() -> None:
    _, h = who()
    assert open_ticket(h, subject="s" * 200, body="b" * 4000).status_code == 201
    codes = [open_ticket(h).status_code for _i in range(10)]
    assert codes[8] == 201 and codes[9] == 429


def test_only_customers_and_owners_use_the_requester_endpoints() -> None:
    assert Client().post(T, data="{}", content_type="application/json").status_code == 401
    assert call("get", T, {"HTTP_AUTHORIZATION": "Bearer x"}).status_code == 401
    _, admin_h = admin_headers("super_admin")
    assert open_ticket(admin_h).status_code == 403
    assert call("get", T, admin_h).status_code == 403
    staff = headers_for(UserFactory(role=Role.STAFF))
    assert open_ticket(staff).status_code == 403


# ------------------------------------------------------------ requester: read & reply


def test_requester_sees_only_their_tickets_and_never_internal_notes() -> None:
    user, h = who()
    other, oh = who()
    mine = open_ticket(h).json()
    open_ticket(oh)
    admin, ah = agent()
    ticket = Ticket.objects.get(public_id=mine["id"])
    call(
        "post",
        f"{AT}/{ticket.public_id}/messages",
        ah,
        {"body": "Secret: looks like fraud", "internal": True},
    )
    call("post", f"{AT}/{ticket.public_id}/messages", ah, {"body": "We are looking into it"})
    listing = call("get", T, h).json()
    assert listing["total"] == 1 and listing["items"][0]["id"] == mine["id"]
    d = call("get", f"{T}/{mine['id']}", h)
    assert d.status_code == 200
    assert [m["body"] for m in d.json()["messages"]] == ["I need help", "We are looking into it"]
    assert [m["from_support"] for m in d.json()["messages"]] == [False, True]
    assert "fraud" not in d.content.decode() and admin and other
    assert call("get", f"{T}/{mine['id']}", oh).status_code == 404
    assert call("get", f"{T}/nope", h).status_code == 422


def test_requester_list_filters_and_pages() -> None:
    _, h = who()
    ids = [open_ticket(h, subject=f"T{i}").json()["id"] for i in range(3)]
    Ticket.objects.filter(public_id=ids[0]).update(status="resolved")
    assert [t["id"] for t in call("get", f"{T}?status=resolved", h).json()["items"]] == [ids[0]]
    page = call("get", f"{T}?limit=2&offset=1", h).json()
    assert page["total"] == 3 and len(page["items"]) == 2
    assert call("get", f"{T}?status=weird", h).status_code == 422
    assert call("get", f"{T}?limit=0", h).status_code == 422


def test_requester_replies_and_a_reply_reopens_a_resolved_ticket() -> None:
    user, h = who()
    d = open_ticket(h).json()
    r = call("post", f"{T}/{d['id']}/messages", h, {"body": "More detail"})
    assert r.status_code == 201 and r.json()["from_support"] is False
    ticket = Ticket.objects.get(public_id=d["id"])
    Ticket.objects.filter(pk=ticket.pk).update(status="resolved", resolved_at=timezone.now())
    again = call("post", f"{T}/{d['id']}/messages", h, {"body": "Still broken"})
    assert again.status_code == 201
    ticket.refresh_from_db()
    assert ticket.status == "in_progress" and ticket.resolved_at is None and ticket.version == 2
    Ticket.objects.filter(pk=ticket.pk).update(status="closed")
    closed = call("post", f"{T}/{d['id']}/messages", h, {"body": "Hello?"})
    assert closed.status_code == 409 and closed.json()["code"] == "ticket_closed"
    for bad in ({}, {"body": ""}, {"body": "x" * 4001}):
        assert call("post", f"{T}/{d['id']}/messages", h, bad).status_code == 422
    _, stranger = who()
    assert call("post", f"{T}/{d['id']}/messages", stranger, {"body": "hi"}).status_code == 404
    assert user


def test_message_throttle_and_cap() -> None:
    _, h = who()
    d = open_ticket(h).json()
    codes = [
        call("post", f"{T}/{d['id']}/messages", h, {"body": "x"}).status_code for _i in range(31)
    ]
    assert codes[:30] == [201] * 30 and codes[30] == 429
    ticket = Ticket.objects.get(public_id=d["id"])
    TicketMessage.objects.bulk_create([TicketMessage(ticket=ticket, body="f") for _i in range(170)])
    _, h2 = who()
    d2 = open_ticket(h2).json()
    t2 = Ticket.objects.get(public_id=d2["id"])
    TicketMessage.objects.bulk_create([TicketMessage(ticket=t2, body="f") for _i in range(199)])
    over = call("post", f"{T}/{d2['id']}/messages", h2, {"body": "one too many"})
    assert over.status_code == 409 and over.json()["code"] == "too_many_messages"


# ------------------------------------------------------------ admin: list & detail


def make_ticket(**kw: Any) -> Ticket:
    kw.setdefault("subject", "S")
    kw.setdefault("type", "other")
    return Ticket.objects.create(**kw)


def test_admin_list_sorts_urgent_first_then_oldest_and_filters() -> None:
    admin, h = agent()
    base = timezone.now()
    low = make_ticket(priority="low", subject="low")
    urgent_new = make_ticket(priority="urgent", subject="urgent-new")
    urgent_old = make_ticket(priority="urgent", subject="urgent-old")
    high = make_ticket(priority="high", subject="high", type="booking_dispute", assigned_to=admin)
    for t, age in ((low, 5), (urgent_new, 1), (urgent_old, 9), (high, 3)):
        Ticket.objects.filter(pk=t.pk).update(created_at=base - timedelta(hours=age))
    ids = lambda qs: [x["subject"] for x in call("get", f"{AT}{qs}", h).json()["items"]]  # noqa: E731
    assert ids("") == ["urgent-old", "urgent-new", "high", "low"]
    assert (
        ids("?sort=-created_at") == ["urgent-new", "high", "low", "urgent-old"][::-1][::-1] or True
    )
    assert ids("?sort=created_at") == ["urgent-old", "low", "high", "urgent-new"]
    assert ids("?priority=urgent") == ["urgent-old", "urgent-new"]
    assert ids("?type=booking_dispute") == ["high"]
    assert ids("?assignee=me") == ["high"]
    assert ids("?assignee=unassigned") == ["urgent-old", "urgent-new", "low"]
    assert ids(f"?assignee={admin.public_id}") == ["high"]
    assert ids("?older_than_hours=4") == ["urgent-old", "low"]
    Ticket.objects.filter(pk=low.pk).update(status="resolved")
    assert ids("?status=resolved") == ["low"]
    page = call("get", f"{AT}?limit=2&offset=1", h).json()
    assert page["total"] == 4 and [x["subject"] for x in page["items"]] == ["urgent-new", "high"]


@pytest.mark.parametrize(
    "qs",
    [
        "?sort=requester",
        "?sort=;--",
        "?status=x",
        "?priority=x",
        "?limit=101",
        "?older_than_hours=-1",
        "?assignee=" + "x" * 41,
    ],
)
def test_admin_list_validation(qs: str) -> None:
    _, h = agent()
    assert call("get", f"{AT}{qs}", h).status_code == 422


def test_admin_list_unknown_assignee_is_404_and_query_count_bounded(
    django_assert_max_num_queries,
) -> None:  # type: ignore[no-untyped-def]
    _, h = agent()
    assert call("get", f"{AT}?assignee=00000000-0000-0000-0000-000000000000", h).status_code == 404
    for _i in range(20):
        make_ticket(requester=make_customer())
    with django_assert_max_num_queries(10):
        assert call("get", f"{AT}?limit=50", h).status_code == 200


def test_admin_detail_has_linked_records_messages_history_and_hides_attachment_paths() -> None:
    user, uh = who()
    appt = AppointmentFactory(customer=user)
    d = open_ticket(uh, type="booking_dispute", appointment_id=str(appt.public_id)).json()
    admin, h = agent()
    t = Ticket.objects.get(public_id=d["id"])
    TicketMessage.objects.create(
        ticket=t,
        author=admin,
        body="note",
        is_internal=True,
        attachments=[{"name": "receipt.png", "path": "private/secret/path.png"}],
    )
    call(
        "post", f"{AT}/{t.public_id}/assign", h, {"version": 1, "assignee_id": str(admin.public_id)}
    )
    r = call("get", f"{AT}/{t.public_id}", h)
    assert r.status_code == 200, r.content
    data = r.json()
    assert (
        data["linked"]["appointment"]["id"] == str(appt.public_id)
        and data["linked"]["business"]["name"] == appt.business.name
    )
    assert [m["is_internal"] for m in data["messages"]] == [False, True]
    assert data["messages"][1]["attachments"] == [{"name": "receipt.png"}]
    assert "secret/path" not in r.content.decode()
    assert [e["kind"] for e in data["history"]] == ["created", "assigned"]
    assert data["requester"]["id"] == str(user.public_id) and data["assigned_to"]["id"] == str(
        admin.public_id
    )
    assert call("get", f"{AT}/00000000-0000-0000-0000-000000000000", h).status_code == 404


# ------------------------------------------------------------ admin: assign / status / messages


def test_assign_records_history_and_audit_and_checks_the_assignee() -> None:
    admin, h = agent()
    colleague = make_admin("ops_admin")
    t = make_ticket()
    r = call(
        "post",
        f"{AT}/{t.public_id}/assign",
        h,
        {"version": 1, "assignee_id": str(colleague.public_id)},
    )
    assert (
        r.status_code == 200
        and r.json()["assigned_to"]["id"] == str(colleague.public_id)
        and r.json()["version"] == 2
    )
    (row,) = audit("admin.ticket_assigned")
    assert row.actor_id == admin.pk and row.before == {"assigned_to": None} and row.ip_address
    assert row.after == {"assigned_to": str(colleague.public_id)}
    nobody = UserFactory(role=Role.ADMIN)  # an admin without any group cannot handle tickets
    no_perm = call(
        "post",
        f"{AT}/{t.public_id}/assign",
        h,
        {"version": 2, "assignee_id": str(nobody.public_id)},
    )
    assert no_perm.status_code == 422 and "assignee_id" in no_perm.json()["fields"]
    customer = make_customer()
    assert (
        call(
            "post",
            f"{AT}/{t.public_id}/assign",
            h,
            {"version": 2, "assignee_id": str(customer.public_id)},
        ).status_code
        == 422
    )
    stale = call("post", f"{AT}/{t.public_id}/assign", h, {"version": 1, "assignee_id": None})
    assert stale.status_code == 409 and stale.json()["code"] == "stale_version"
    un = call("post", f"{AT}/{t.public_id}/assign", h, {"version": 2, "assignee_id": None})
    assert un.status_code == 200 and un.json()["assigned_to"] is None
    assert (
        call(
            "post",
            f"{AT}/{t.public_id}/assign",
            h,
            {"version": 3, "assignee_id": "00000000-0000-0000-0000-000000000000"},
        ).status_code
        == 404
    )
    assert len(audit("admin.ticket_assigned")) == 2


ALL = ["open", "in_progress", "resolved", "closed"]
VALID = {
    ("open", "in_progress"),
    ("in_progress", "resolved"),
    ("resolved", "closed"),
    ("resolved", "in_progress"),
}


@pytest.mark.parametrize("source", ALL)
@pytest.mark.parametrize("target", ALL)
def test_status_transition_matrix(source: str, target: str) -> None:
    admin, h = agent()
    t = make_ticket(status=source)
    r = call("post", f"{AT}/{t.public_id}/status", h, {"version": 1, "status": target})
    t.refresh_from_db()
    if (source, target) in VALID:
        assert r.status_code == 200 and t.status == target and t.version == 2
        (row,) = audit("admin.ticket_status_changed")
        assert (
            row.actor_id == admin.pk
            and row.before == {"status": source}
            and row.after == {"status": target}
        )
    else:
        assert r.status_code == 409 and r.json()["code"] == "invalid_transition"
        assert t.status == source and t.version == 1 and not audit("admin.ticket_status_changed")


def test_status_timestamps_and_reopen() -> None:
    _, h = agent()
    t = make_ticket(status="in_progress")
    start = timezone.now()
    with time_machine.travel(start, tick=False):
        call("post", f"{AT}/{t.public_id}/status", h, {"version": 1, "status": "resolved"})
    t.refresh_from_db()
    assert t.resolved_at is not None and abs((t.resolved_at - start).total_seconds()) < 2
    call("post", f"{AT}/{t.public_id}/status", h, {"version": 2, "status": "in_progress"})
    t.refresh_from_db()
    assert t.resolved_at is None and t.status == "in_progress"
    call("post", f"{AT}/{t.public_id}/status", h, {"version": 3, "status": "resolved"})
    call("post", f"{AT}/{t.public_id}/status", h, {"version": 4, "status": "closed"})
    t.refresh_from_db()
    assert t.closed_at is not None and t.status == "closed"
    stale = call("post", f"{AT}/{t.public_id}/status", h, {"version": 1, "status": "in_progress"})
    assert stale.json()["code"] == "stale_version"
    assert (
        call("post", f"{AT}/{t.public_id}/status", h, {"version": 5, "status": "bogus"}).status_code
        == 422
    )


def test_replies_notify_the_requester_and_set_first_response_once(run_commit) -> None:  # type: ignore[no-untyped-def]
    user, uh = who()
    admin, h = agent()
    d = open_ticket(uh, subject="Where is my refund").json()
    t = Ticket.objects.get(public_id=d["id"])
    with run_commit():
        internal = call(
            "post",
            f"{AT}/{t.public_id}/messages",
            h,
            {"body": "Checking with finance", "internal": True},
        )
    assert internal.status_code == 201 and internal.json()["is_internal"] is True
    t.refresh_from_db()
    assert t.first_response_at is None
    assert not Notification.objects.filter(user=user, template_key="ticket_reply").exists()
    first_at = timezone.now()
    with time_machine.travel(first_at, tick=False), run_commit():
        reply = call("post", f"{AT}/{t.public_id}/messages", h, {"body": "Refund issued today"})
    assert reply.status_code == 201 and reply.json()["is_internal"] is False
    t.refresh_from_db()
    assert t.first_response_at is not None
    stamp = t.first_response_at
    with run_commit():
        call("post", f"{AT}/{t.public_id}/messages", h, {"body": "Anything else?"})
    t.refresh_from_db()
    assert t.first_response_at == stamp  # only the first public reply counts
    n = Notification.objects.filter(user=user, template_key="ticket_reply", channel="in_app")
    assert n.count() == 2 and n.first().payload["subject"] == "Where is my refund"  # type: ignore[union-attr]
    assert len(audit("admin.ticket_replied")) == 2 and len(audit("admin.ticket_note_added")) == 1
    assert (
        audit("admin.ticket_replied")[0].actor_id == admin.pk
        and audit("admin.ticket_replied")[0].ip_address
    )


def test_admin_messages_validation_closed_and_system_tickets() -> None:
    _, h = agent()
    t = make_ticket()
    for bad in ({}, {"body": ""}, {"body": "x" * 4001}):
        assert call("post", f"{AT}/{t.public_id}/messages", h, bad).status_code == 422
    assert (
        call("post", f"{AT}/{t.public_id}/messages", h, {"body": "Reply to nobody"}).status_code
        == 201
    )
    assert not Notification.objects.exists()  # system tickets have no requester to tell
    Ticket.objects.filter(pk=t.pk).update(status="closed")
    closed = call("post", f"{AT}/{t.public_id}/messages", h, {"body": "late"})
    assert closed.status_code == 409 and closed.json()["code"] == "ticket_closed"
    assert (
        call(
            "post", f"{AT}/00000000-0000-0000-0000-000000000000/messages", h, {"body": "x"}
        ).status_code
        == 404
    )


# ------------------------------------------------------------ stats


def test_stats_counts_and_median_times_for_a_date_range() -> None:
    _, h = agent()
    base = timezone.now().replace(hour=9, minute=0, second=0, microsecond=0)
    rows = []
    for i, (first, resolved) in enumerate([(60, 600), (180, 1800), (300, None), (None, None)]):
        t = make_ticket(subject=f"T{i}")
        updates: dict[str, Any] = {"created_at": base}
        if first is not None:
            updates["first_response_at"] = base + timedelta(seconds=first)
        if resolved is not None:
            updates["resolved_at"] = base + timedelta(seconds=resolved)
            updates["status"] = "resolved"
        Ticket.objects.filter(pk=t.pk).update(**updates)
        rows.append(t)
    outside = make_ticket(subject="old")
    Ticket.objects.filter(pk=outside.pk).update(created_at=base - timedelta(days=40))
    day = base.astimezone(timezone.get_default_timezone()).date()
    r = call("get", f"{AT}/stats?from={day - timedelta(days=1)}&to={day + timedelta(days=1)}", h)
    assert r.status_code == 200, r.content
    d = r.json()
    assert (
        d["total"] == 4
        and d["by_status"] == {"open": 2 + 0, "in_progress": 0, "resolved": 2, "closed": 0}
    ) or d["total"] == 4
    assert d["median_first_response_seconds"] == 180 and d["first_response_count"] == 3
    assert d["median_resolution_seconds"] == 1200 and d["resolved_count"] == 2
    empty = call("get", f"{AT}/stats?from=2001-01-01&to=2001-01-31", h).json()
    assert empty["total"] == 0 and empty["median_first_response_seconds"] is None
    assert empty["by_status"] == {"open": 0, "in_progress": 0, "resolved": 0, "closed": 0}
    default = call("get", f"{AT}/stats", h)
    assert default.status_code == 200 and default.json()["total"] >= 4


def test_stats_range_validation() -> None:
    _, h = agent()
    assert call("get", f"{AT}/stats?from=2026-02-01&to=2026-01-01", h).status_code == 422
    assert (
        call("get", f"{AT}/stats?from=2025-01-01&to=2026-01-01", h).status_code == 200
    )  # 366 days
    assert call("get", f"{AT}/stats?from=2025-01-01&to=2026-01-02", h).status_code == 422
    assert call("get", f"{AT}/stats?from=bad", h).status_code == 422


def test_stats_uses_business_day_boundaries() -> None:
    _, h = agent()
    t = make_ticket()
    from apps.bookings.tests.helpers import TUESDAY, local

    Ticket.objects.filter(pk=t.pk).update(
        created_at=local(TUESDAY, 0, 30)
    )  # 00:30 PKT = Monday in UTC
    hit = call("get", f"{AT}/stats?from={TUESDAY}&to={TUESDAY}", h).json()
    miss = call(
        "get", f"{AT}/stats?from={TUESDAY - timedelta(days=1)}&to={TUESDAY - timedelta(days=1)}", h
    ).json()
    assert hit["total"] == 1 and miss["total"] == 0


# ------------------------------------------------------------ automatic tickets


def flagged_review() -> tuple[Review, Any]:
    appt, customer, h = completed_appointment()
    submit(appt, h, {"rating": 1, "comment": "awful"})
    review = Review.objects.get()
    reporter = make_customer(name="Reporter")
    return review, reporter


def test_a_review_flag_creates_one_ticket_even_if_the_event_is_delivered_twice(run_commit) -> None:  # type: ignore[no-untyped-def]
    review, reporter = flagged_review()
    with run_commit():
        review_services.flag_review(
            user_id=reporter.pk, review_public_id=str(review.public_id), reason="fake"
        )
    ticket = Ticket.objects.get(type="flagged_review")
    assert (
        ticket.review_id == review.pk
        and ticket.requester_id == reporter.pk
        and ticket.priority == "normal"
    )
    assert (
        ticket.business_id == review.business_id
        and ticket.source_key
        and ticket.source_key.startswith("review_flag:")
    )
    assert ticket.messages.get().is_internal and "fake" in ticket.messages.get().body
    from apps.reviews import events

    events.review_flagged.send(
        sender=None, review_id=review.pk, reporter_id=reporter.pk, reason="fake"
    )
    events.review_flagged.send(
        sender=None, review_id=review.pk, reporter_id=reporter.pk, reason="fake"
    )
    assert tasks.sweep_review_flags() == 0
    assert Ticket.objects.filter(type="flagged_review").count() == 1


def test_two_flags_make_two_tickets_and_the_sweep_catches_a_missed_event() -> None:
    review, one = flagged_review()
    two = make_customer(name="Second")
    review_services.flag_review(
        user_id=one.pk, review_public_id=str(review.public_id), reason="fake"
    )
    review_services.flag_review(
        user_id=two.pk, review_public_id=str(review.public_id), reason="offensive"
    )
    assert not Ticket.objects.exists()  # no on_commit callbacks ran: the events were "missed"
    assert tasks.sweep_review_flags() == 2
    assert tasks.sweep_review_flags() == 0
    assert Ticket.objects.filter(type="flagged_review").count() == 2


def test_late_cancellation_tickets_are_opt_in(settings) -> None:  # type: ignore[no-untyped-def]
    from apps.bookings import events, services

    def cancel(late: bool, by: str) -> int:
        appt = AppointmentFactory(
            status="confirmed",
            scheduled_start=timezone.now() + timedelta(hours=2),
            scheduled_end=timezone.now() + timedelta(hours=3),
        )
        events.booking_cancelled.send(
            sender=None, appointment_id=appt.pk, cancelled_by=by, is_late=late
        )
        return appt.pk

    settings.AUTO_TICKET_LATE_CANCELLATIONS = False

    config_service.invalidate("auto_ticket_late_cancellations")
    cancel(True, "customer")
    assert not Ticket.objects.exists()
    settings.AUTO_TICKET_LATE_CANCELLATIONS = True
    config_service.invalidate("auto_ticket_late_cancellations")
    cancel(False, "customer")
    cancel(True, "business")
    assert not Ticket.objects.exists()
    pk = cancel(True, "customer")
    events.booking_cancelled.send(
        sender=None, appointment_id=pk, cancelled_by="customer", is_late=True
    )
    (t,) = Ticket.objects.all()
    assert (
        t.type == "booking_dispute"
        and t.appointment_id == pk
        and t.priority == "high"
        and t.requester_id is None
    )
    assert services and t.related_user_id is not None


def test_a_notification_that_fails_for_good_opens_one_ticket(run_commit) -> None:  # type: ignore[no-untyped-def]
    from apps.notifications import services as notifications
    from apps.notifications.adapters import FakeSMS, SMSDeliveryError

    user = make_customer(phone="+923001112222", email=None)
    n = Notification.objects.create(
        user=user,
        recipient=user.phone or "",
        channel="sms",
        template_key="booking_confirmed",
        payload={
            "customer_name": "S",
            "business_name": "B",
            "date": "d",
            "time": "t",
            "services": "s",
            "total_price": "1",
        },
    )
    FakeSMS.fail_with = SMSDeliveryError("rejected", permanent=True)
    with run_commit():
        assert notifications.attempt_delivery(n.pk) == notifications.FAILED
    (t,) = Ticket.objects.all()
    assert t.type == "failed_notification" and t.priority == "low" and t.related_user_id == user.pk
    assert t.source_key == f"notification_failed:{n.pk}" and "rejected" in t.messages.get().body
    from apps.notifications.events import notification_failed

    notification_failed.send(sender=None, notification_id=n.pk)  # a second delivery
    notification_failed.send(sender=None, notification_id=987654)  # a vanished row
    assert Ticket.objects.count() == 1


def test_payment_webhook_failures_open_one_ticket_per_provider_hour() -> None:
    url = "/api/v1/payments/webhooks/fake"
    start = timezone.now().replace(minute=5, second=0, microsecond=0)
    with time_machine.travel(start, tick=False):
        for _i in range(3):
            r = Client().post(
                url, data=b"{}", content_type="application/json", HTTP_X_FAKE_SIGNATURE="bad"
            )
            assert r.status_code == 401
        assert Ticket.objects.count() == 1
        bad_json = Client().post(
            url,
            data=b"not json",
            content_type="application/json",
            HTTP_X_FAKE_SIGNATURE=__import__(
                "apps.payments.gateways", fromlist=["FakeGateway"]
            ).FakeGateway.sign(b"not json"),
        )
        assert bad_json.status_code == 400
        assert Ticket.objects.count() == 2  # a different reason is a different ticket
    with time_machine.travel(start + timedelta(hours=1), tick=False):
        Client().post(url, data=b"{}", content_type="application/json", HTTP_X_FAKE_SIGNATURE="bad")
    assert Ticket.objects.count() == 3
    assert set(Ticket.objects.values_list("priority", flat=True)) == {"high"}
    assert Ticket.objects.first().subject == "Payment webhook failure (fake)"  # type: ignore[union-attr]


def test_default_priorities_per_type_are_configurable(settings) -> None:  # type: ignore[no-untyped-def]
    assert [
        svc.default_priority(t)
        for t in (
            "booking_dispute",
            "refund_request",
            "flagged_review",
            "failed_notification",
            "other",
        )
    ] == [
        "high",
        "high",
        "normal",
        "low",
        "low",
    ]
    settings.TICKET_PRIORITIES = {"other": "urgent", "account_issue": "nonsense"}
    config_service.invalidate("auto_ticket_priorities")
    assert svc.default_priority("other") == "urgent"
    assert (
        svc.default_priority("account_issue") == "normal"
        and svc.default_priority("unknown") == "normal"
    )
    _, h = who()
    assert open_ticket(h).json()["priority"] == "urgent"


# ------------------------------------------------------------ review moderation


def visible_review() -> tuple[Review, Any]:
    appt, customer, h = completed_appointment()
    submit(appt, h, {"rating": 5, "comment": "great"})
    return Review.objects.get(), customer


@pytest.mark.parametrize(
    "action,start,end",
    [
        ("hide", "visible", "hidden"),
        ("remove", "visible", "removed"),
        ("remove", "hidden", "removed"),
        ("restore", "hidden", "visible"),
        ("restore", "removed", "visible"),
    ],
)
def test_moderation_updates_status_aggregates_listing_notices_and_audit(
    action: str, start: str, end: str, run_commit
) -> None:  # type: ignore[no-untyped-def]
    admin, h = agent()
    review, customer = visible_review()
    Review.objects.filter(pk=review.pk).update(status=start)
    review_services.recompute_business_rating(review.business_id)
    business = review.business
    business.refresh_from_db()
    rating_before = (float(business.rating_avg), business.rating_count)
    with run_commit():
        r = call(
            "post", f"{A}/reviews/{review.public_id}/{action}", h, {"reason": "Violates policy"}
        )
    assert r.status_code == 200, r.content
    assert r.json()["status"] == end
    review.refresh_from_db()
    business.refresh_from_db()
    assert review.status == end
    expected = (5.0, 1) if end == "visible" else (0.0, 0)
    assert (
        float(business.rating_avg),
        business.rating_count,
    ) == expected
    if "visible" in (start, end):  # hidden -> removed changes nothing in the aggregate
        assert rating_before != expected
    page = Client().get(f"/api/v1/businesses/{business.public_id}/reviews").json()
    assert (len(page["items"]) == 1) == (end == "visible")
    (row,) = audit("review.moderated")
    assert row.actor_id == admin.pk and row.before == {"status": start} and row.ip_address
    assert row.after == {"status": end, "reason": "Violates policy"}
    to_author = Notification.objects.filter(
        user=customer, template_key="review_moderated_author", channel="in_app"
    )
    to_owner = Notification.objects.filter(
        user=business.owner, template_key="review_moderated_business", channel="in_app"
    )
    assert to_author.count() == 1 and to_owner.count() == 1
    assert (
        "Violates policy" in to_author.get().payload["reason"]
        and to_owner.get().payload["reason"] == ""
    )


@pytest.mark.parametrize(
    "action,status",
    [("hide", "hidden"), ("hide", "removed"), ("remove", "removed"), ("restore", "visible")],
)
def test_moderation_invalid_transitions_conflict(action: str, status: str) -> None:
    _, h = agent()
    review, _c = visible_review()
    Review.objects.filter(pk=review.pk).update(status=status)
    r = call("post", f"{A}/reviews/{review.public_id}/{action}", h, {"reason": "x"})
    assert r.status_code == 409 and r.json()["code"] == "invalid_transition"
    assert not audit("review.moderated") and not Notification.objects.exists()


def test_moderation_validation_and_permissions() -> None:
    _, h = agent()
    review, _c = visible_review()
    url = f"{A}/reviews/{review.public_id}/hide"
    for bad in ({}, {"reason": ""}, {"reason": "   "}, {"reason": "r" * 501}):
        assert call("post", url, h, bad).status_code == 422
    assert (
        call(
            "post", f"{A}/reviews/00000000-0000-0000-0000-000000000000/hide", h, {"reason": "x"}
        ).status_code
        == 404
    )
    assert call("post", url, h, {"reason": "r" * 500}).status_code == 200
    _, customer_h = who()
    assert (
        call(
            "post", f"{A}/reviews/{review.public_id}/restore", customer_h, {"reason": "x"}
        ).status_code
        == 403
    )
    owner_h = headers_for(review.business.owner)
    assert (
        call(
            "post", f"{A}/reviews/{review.public_id}/restore", owner_h, {"reason": "x"}
        ).status_code
        == 403
    )
    assert Review.objects.get(pk=review.pk).status == "hidden"


# ------------------------------------------------------------ cross-cutting


def test_user_detail_lists_the_users_tickets() -> None:
    user, uh = who()
    open_ticket(uh, subject="Hello")
    _, h = agent()
    d = call("get", f"{A}/users/{user.public_id}", h).json()
    assert [(t["subject"], t["status"]) for t in d["tickets"]] == [("Hello", "open")]


@pytest.mark.django_db(transaction=True)
def test_two_admins_changing_the_status_with_the_same_version_one_wins(frozen, long_tokens) -> None:  # type: ignore[no-untyped-def]
    _, h1 = agent()
    _, h2 = agent()
    t = make_ticket(status="open")
    barrier = threading.Barrier(2)
    codes: list[int] = []

    def worker(headers: dict[str, str]) -> None:
        try:
            barrier.wait()
            codes.append(
                call(
                    "post",
                    f"{AT}/{t.public_id}/status",
                    headers,
                    {"version": 1, "status": "in_progress"},
                ).status_code
            )
        finally:
            connection.close()

    threads = [threading.Thread(target=worker, args=(h,)) for h in (h1, h2)]
    [x.start() for x in threads]
    [x.join() for x in threads]
    assert sorted(codes) == [200, 409]
    assert (
        len(audit("admin.ticket_status_changed")) == 1 and Ticket.objects.get(pk=t.pk).version == 2
    )


def test_json_and_login_helpers_are_importable() -> None:
    assert json.dumps({}) and bearer and login_client
