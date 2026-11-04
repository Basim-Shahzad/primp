import threading
from datetime import timedelta
from typing import Any

import pytest
import time_machine
from django.db import connection
from django.test import Client

from apps.accounts.factories import CustomerProfileFactory, UserFactory, make_customer
from apps.accounts.models import CustomerProfile
from apps.bookings import provider_escalation
from apps.bookings.factories import AppointmentFactory
from apps.bookings.models import (
    Appointment,
    AppointmentEvent,
    AppointmentService,
    RescheduleRequest,
)
from apps.bookings.tasks import escalate_pending_bookings
from apps.bookings.tests.helpers import NOW, TUESDAY, local
from apps.businesses.factories import (
    BusinessFactory,
    ClosureFactory,
    ServiceFactory,
    StaffFactory,
    StaffServiceFactory,
    open_all_week,
)
from apps.businesses.models import Business, Service, Staff
from apps.businesses.tests.provider_helpers import P, call, customer_headers, owner_headers
from apps.loyalty.models import PointTransaction
from apps.notifications.models import Notification

pytestmark = pytest.mark.django_db


def shop(
    staff_level: bool = False, **kw: Any
) -> tuple[Business, dict[str, str], Service, list[Staff]]:
    owner, h = owner_headers()
    kw.setdefault("lead_time_minutes", 0)
    b = BusinessFactory(owner=owner, staff_level_booking=staff_level, **kw)
    open_all_week(b)
    svc = ServiceFactory(business=b, duration_minutes=60, price=2000)
    staff: list[Staff] = []
    if staff_level:
        staff = [StaffFactory(business=b) for _ in range(2)]
        for s in staff:
            StaffServiceFactory(staff=s, service=svc)
    return b, h, svc, staff


def base(b: Business) -> str:
    return f"{P}/businesses/{b.public_id}"


def appt(
    b: Business,
    svc: Service,
    start,
    *,
    staff: Staff | None = None,
    status: str = "confirmed",
    customer: Any = "new",
    **kw: Any,
) -> Appointment:
    cust = make_customer(name="Sana Khan") if customer == "new" else customer
    a = AppointmentFactory(
        business=b,
        staff=staff,
        customer=cust,
        status=status,
        scheduled_start=start,
        scheduled_end=start + timedelta(hours=1),
        total_price=svc.price,
        **kw,
    )
    AppointmentService.objects.create(
        appointment=a,
        service=svc,
        name=svc.name,
        price_at_booking=svc.price,
        duration_at_booking=60,
    )
    return a


def act(
    h: dict[str, str], a: Appointment, action: str, body: Any = None, method: str = "post"
) -> Any:
    return call(
        method,
        f"{P}/appointments/{a.public_id}/{action}",
        h,
        body if body is not None else ({} if method != "get" else None),
    )


# ------------------------------------------------------------ access matrix


def test_dashboard_endpoints_auth_role_ownership(frozen) -> None:  # type: ignore[no-untyped-def]
    b, h, svc, _ = shop()
    a = appt(b, svc, local(TUESDAY, 12), status="pending")
    _, other = owner_headers()
    cust = customer_headers()
    biz = base(b)
    ap = f"{P}/appointments/{a.public_id}"
    calls: list[tuple[str, str, Any]] = [
        ("get", f"{biz}/appointments", None),
        ("get", f"{biz}/appointments/today", None),
        ("get", f"{biz}/appointments/needs-action", None),
        ("get", f"{biz}/appointments/changes?since=2026-01-01T00:00:00Z", None),
        ("get", ap, None),
        ("post", f"{ap}/accept", {}),
        ("post", f"{ap}/decline", {}),
        ("post", f"{ap}/cancel", {"reason": "x"}),
        ("post", f"{ap}/reschedule", {"start": local(TUESDAY, 14).isoformat()}),
        ("post", f"{ap}/complete", {}),
        ("post", f"{ap}/no-show", {}),
        ("put", f"{ap}/note", {"note": "x"}),
        ("post", f"{ap}/reschedule-request/accept", {}),
        ("post", f"{ap}/reschedule-request/decline", {}),
    ]
    for method, path, body in calls:
        assert call(method, path, {}, body).status_code == 401, path
        assert call(method, path, {"HTTP_AUTHORIZATION": "Bearer x"}, body).status_code == 401, path
        assert call(method, path, cust, body).status_code == 403, path
        assert call(method, path, other, body).status_code == 404, path
    a.refresh_from_db()
    assert a.status == "pending" and a.business_note == ""


# ------------------------------------------------------------ list


def test_list_sorted_filtered_and_paginated(frozen) -> None:  # type: ignore[no-untyped-def]
    b, h, svc, (s1, s2) = shop(staff_level=True)
    other_svc = ServiceFactory(business=b)
    rows = [
        appt(b, svc, local(TUESDAY, 10), staff=s1),
        appt(b, svc, local(TUESDAY, 12), staff=s2, status="pending"),
        appt(b, other_svc, local(TUESDAY, 14), staff=s1, status="completed"),
        appt(b, svc, local(TUESDAY + timedelta(days=1), 10), staff=s1),
        appt(
            b, svc, local(TUESDAY + timedelta(days=2), 10), staff=s2, status="cancelled_by_customer"
        ),
    ]
    ids = [str(a.public_id) for a in rows]
    appt(BusinessFactory(), svc, local(TUESDAY, 11))  # another business is never listed

    def ids_of(qs: str) -> list[str]:
        r = call("get", f"{base(b)}/appointments{qs}", h)
        assert r.status_code == 200, r.content
        return [x["id"] for x in r.json()["items"]]

    assert ids_of("") == ids
    assert ids_of("?order=desc") == ids[::-1]
    assert ids_of("?from=2026-11-03&to=2026-11-03") == ids[:3]  # inclusive local days
    assert ids_of("?from=2026-11-04") == ids[3:]
    assert ids_of(f"?staff_id={s2.pk}") == [ids[1], ids[4]]
    assert ids_of("?status=pending") == [ids[1]]
    assert ids_of("?status=pending,completed") == [ids[1], ids[2]]
    assert ids_of(f"?service_id={other_svc.pk}") == [ids[2]]
    assert ids_of("?q=Sana") == ids
    assert ids_of("?q=nobody") == []
    first = call("get", f"{base(b)}/appointments?limit=2", h).json()
    assert [x["id"] for x in first["items"]] == ids[:2] and first["next_cursor"]
    seen: list[str] = []
    cursor = None
    while True:
        qs = "?limit=2" + (f"&cursor={cursor}" if cursor else "")
        page = call("get", f"{base(b)}/appointments{qs}", h).json()
        seen += [x["id"] for x in page["items"]]
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert seen == ids


def test_list_search_by_phone_and_walk_in_name(frozen) -> None:  # type: ignore[no-untyped-def]
    b, h, svc, _ = shop()
    known = make_customer(name="Ayesha Rao", phone="+923331234567")
    a1 = appt(b, svc, local(TUESDAY, 10), customer=known)
    a2 = appt(
        b,
        svc,
        local(TUESDAY, 12),
        customer=None,
        walk_in_name="Bilal Walkin",
        walk_in_phone="+923451112222",
    )

    def ids_of(q: str) -> list[str]:
        r = call("get", f"{base(b)}/appointments?q={q}", h)
        assert r.status_code == 200, r.content
        return [x["id"] for x in r.json()["items"]]

    assert ids_of("Ayesha") == [str(a1.public_id)]
    assert ids_of("0333 1234567".replace(" ", "%20")) == [str(a1.public_id)]
    assert ids_of("%2B923331234567") == [str(a1.public_id)]
    assert ids_of("1234567") == [str(a1.public_id)]
    assert ids_of("walkin") == [str(a2.public_id)]
    assert ids_of("03451112222") == [str(a2.public_id)]


@pytest.mark.parametrize(
    "qs,field",
    [
        ("?status=bogus", "status"),
        ("?status=", "status"),
        ("?q=a", "q"),
        ("?q=" + "x" * 101, "q"),
        ("?from=2026-11-05&to=2026-11-01", "to"),
        ("?from=2025-01-01&to=2026-11-01", "to"),
    ],
)
def test_list_validation(qs: str, field: str, frozen) -> None:  # type: ignore[no-untyped-def]
    b, h, svc, _ = shop()
    r = call("get", f"{base(b)}/appointments{qs}", h)
    assert r.status_code in (422, 400), r.content
    assert field in r.json()["fields"] or r.json()["code"] == "validation_error"


def test_list_bad_params_and_cursor(frozen) -> None:  # type: ignore[no-untyped-def]
    b, h, svc, _ = shop()
    assert call("get", f"{base(b)}/appointments?limit=0", h).status_code == 422
    assert call("get", f"{base(b)}/appointments?limit=101", h).status_code == 422
    assert call("get", f"{base(b)}/appointments?order=sideways", h).status_code == 422
    r = call("get", f"{base(b)}/appointments?cursor=garbage", h)
    assert r.status_code == 400 and r.json()["code"] == "invalid_cursor"
    ok = call("get", f"{base(b)}/appointments?from=2025-11-03&to=2026-11-03", h)
    assert ok.status_code == 200  # exactly 366 days is allowed


def test_list_query_count_is_bounded(frozen, django_assert_max_num_queries) -> None:  # type: ignore[no-untyped-def]
    b, h, svc, _ = shop()
    for hour in range(10, 19):
        appt(b, svc, local(TUESDAY, hour))
    with django_assert_max_num_queries(10):
        r = call("get", f"{base(b)}/appointments?limit=50", h)
    assert r.status_code == 200 and len(r.json()["items"]) == 9


# ------------------------------------------------------------ today / needs-action


def test_today_is_business_day_ordered_without_cancelled(frozen) -> None:  # type: ignore[no-untyped-def]
    b, h, svc, _ = shop()
    monday = NOW.date()
    late = appt(b, svc, local(monday, 16))
    early = appt(b, svc, local(monday, 10), status="completed")
    appt(b, svc, local(monday, 12), status="cancelled_by_business")
    appt(b, svc, local(monday + timedelta(days=1), 0, 0))  # tomorrow 00:00 PKT
    appt(b, svc, local(monday, 0, 0) - timedelta(minutes=1))  # yesterday 23:59 PKT
    r = call("get", f"{base(b)}/appointments/today", h)
    assert [x["id"] for x in r.json()] == [str(early.public_id), str(late.public_id)]


def test_needs_action_pending_and_reschedule_requests_with_count(frozen) -> None:  # type: ignore[no-untyped-def]
    b, h, svc, _ = shop(confirm_mode="manual")
    p1 = appt(b, svc, local(TUESDAY, 12), status="pending")
    appt(b, svc, local(TUESDAY, 14), status="confirmed")
    past_pending = appt(b, svc, NOW - timedelta(hours=2), status="pending")
    c = appt(b, svc, local(TUESDAY, 16), status="confirmed")
    req = RescheduleRequest.objects.create(
        appointment=c, new_start=local(TUESDAY, 18), new_end=local(TUESDAY, 19)
    )
    done = appt(b, svc, local(TUESDAY, 11))
    RescheduleRequest.objects.create(
        appointment=done, new_start=local(TUESDAY, 9), new_end=local(TUESDAY, 10), status="declined"
    )
    r = call("get", f"{base(b)}/appointments/needs-action", h).json()
    assert r["unread_count"] == 2
    assert [x["id"] for x in r["pending"]] == [str(p1.public_id)]
    assert r["pending"][0]["allowed_actions"] == ["accept", "decline", "cancel", "reschedule"]
    assert [x["request"]["id"] for x in r["reschedule_requests"]] == [req.pk]
    assert r["reschedule_requests"][0]["appointment"]["id"] == str(c.public_id)
    assert str(past_pending.public_id) not in [x["id"] for x in r["pending"]]


# ------------------------------------------------------------ detail


def test_detail_has_timeline_customer_summary_and_phone_rules(frozen) -> None:  # type: ignore[no-untyped-def]
    b, h, svc, _ = shop()
    cust = make_customer(name="Sana Khan", phone="+923001234567")
    CustomerProfile.objects.filter(user=cust).update(no_show_count=2)
    appt(b, svc, local(TUESDAY - timedelta(days=9), 12), status="completed", customer=cust)
    appt(b, svc, local(TUESDAY - timedelta(days=8), 12), status="completed", customer=cust)
    appt(
        b,
        svc,
        local(TUESDAY - timedelta(days=7), 12),
        status="cancelled_by_customer",
        customer=cust,
    )
    appt(
        BusinessFactory(),
        svc,
        local(TUESDAY - timedelta(days=6), 12),
        status="completed",
        customer=cust,
    )
    upcoming = appt(b, svc, local(TUESDAY, 12), customer=cust)
    AppointmentEvent.objects.create(
        appointment=upcoming, from_status="", to_status="confirmed", actor_role="customer"
    )
    r = call("get", f"{P}/appointments/{upcoming.public_id}", h)
    assert r.status_code == 200, r.content
    data = r.json()
    assert data["customer_summary"] == {
        "user_id": str(cust.public_id),
        "name": "Sana Khan",
        "phone": "+923001234567",
        "previous_completed_visits": 2,
        "no_show_count": 2,
    }
    assert data["timeline"][0]["to_status"] == "confirmed" and data["reschedule_request"] is None
    assert data["customer"]["phone"] == "+923001234567"
    past = appt(b, svc, local(TUESDAY - timedelta(days=9), 15), status="completed", customer=cust)
    cancelled = appt(b, svc, local(TUESDAY, 16), status="cancelled_by_customer", customer=cust)
    stale = appt(b, svc, NOW - timedelta(hours=5), status="confirmed", customer=cust)
    for hidden in (past, cancelled, stale):
        d = call("get", f"{P}/appointments/{hidden.public_id}", h).json()
        assert d["customer"]["phone"] is None and d["customer_summary"]["phone"] is None


def test_detail_for_walk_in_counts_visits_by_phone(frozen) -> None:  # type: ignore[no-untyped-def]
    b, h, svc, _ = shop()
    common = {"customer": None, "walk_in_name": "Walk In", "walk_in_phone": "+923451112222"}
    appt(b, svc, local(TUESDAY - timedelta(days=3), 12), status="completed", **common)
    a = appt(b, svc, local(TUESDAY, 12), **common)
    s = call("get", f"{P}/appointments/{a.public_id}", h).json()["customer_summary"]
    assert s["previous_completed_visits"] == 1 and s["user_id"] is None and s["no_show_count"] == 0
    assert s["name"] == "Walk In" and s["phone"] == "+923451112222"


def test_detail_includes_pending_reschedule_request(frozen) -> None:  # type: ignore[no-untyped-def]
    b, h, svc, _ = shop()
    a = appt(b, svc, local(TUESDAY, 12))
    RescheduleRequest.objects.create(
        appointment=a, new_start=local(TUESDAY, 15), new_end=local(TUESDAY, 16)
    )
    d = call("get", f"{P}/appointments/{a.public_id}", h).json()
    assert d["reschedule_request"]["status"] == "pending"
    assert d["reschedule_request"]["new_start"].startswith("2026-11-03T10:00:00")


# ------------------------------------------------------------ transition matrix

ALL_STATUSES = [
    "pending",
    "confirmed",
    "completed",
    "cancelled_by_customer",
    "cancelled_by_business",
    "no_show",
]
FUTURE_ACTIONS = {
    "accept": ({"pending"}, "confirmed"),
    "decline": ({"pending"}, "cancelled_by_business"),
    "cancel": ({"pending", "confirmed"}, "cancelled_by_business"),
    "reschedule": ({"pending", "confirmed"}, None),
}
PAST_ACTIONS = {
    "complete": ({"confirmed"}, "completed"),
    "no-show": ({"confirmed"}, "no_show"),
}


def body_for(action: str) -> dict[str, Any]:
    if action == "cancel":
        return {"reason": "Equipment failure"}
    if action == "reschedule":
        return {"start": local(TUESDAY + timedelta(days=1), 14).isoformat()}
    return {}


@pytest.mark.parametrize("status", ALL_STATUSES)
@pytest.mark.parametrize("action", sorted(FUTURE_ACTIONS))
def test_transition_matrix_future_actions(status: str, action: str, frozen, run_commit) -> None:  # type: ignore[no-untyped-def]
    b, h, svc, _ = shop()
    a = appt(b, svc, local(TUESDAY, 12), status=status)
    allowed, target = FUTURE_ACTIONS[action]
    with run_commit():
        r = call("post", f"{P}/appointments/{a.public_id}/{action}", h, body_for(action))
    a.refresh_from_db()
    if status in allowed:
        assert r.status_code == 200, r.content
        assert a.status == (target or status)
        if action == "reschedule":
            assert a.scheduled_start == local(TUESDAY + timedelta(days=1), 14)
    else:
        assert r.status_code == 409 and r.json()["code"] == "invalid_transition", r.content
        assert a.status == status and a.scheduled_start == local(TUESDAY, 12)


@pytest.mark.parametrize("status", ALL_STATUSES)
@pytest.mark.parametrize("action", sorted(PAST_ACTIONS))
def test_transition_matrix_closing_actions(status: str, action: str, frozen, run_commit) -> None:  # type: ignore[no-untyped-def]
    b, h, svc, _ = shop()
    a = appt(b, svc, NOW - timedelta(hours=2), status=status)
    allowed, target = PAST_ACTIONS[action]
    with run_commit():
        r = call("post", f"{P}/appointments/{a.public_id}/{action}", h, {})
    a.refresh_from_db()
    if status in allowed:
        assert r.status_code == 200, r.content
        assert a.status == target
    else:
        assert r.status_code == 409 and r.json()["code"] == "invalid_transition"
        assert a.status == status


def test_matrix_started_appointments_cannot_be_accepted_or_rescheduled(frozen) -> None:  # type: ignore[no-untyped-def]
    b, h, svc, _ = shop()
    a = appt(b, svc, NOW - timedelta(minutes=10), status="pending")
    assert act(h, a, "accept").json()["code"] == "appointment_started"
    c = appt(b, svc, NOW - timedelta(minutes=10), status="confirmed")
    r = act(h, c, "reschedule", {"start": local(TUESDAY, 14).isoformat()})
    assert r.status_code == 409 and r.json()["code"] == "appointment_started"
    assert (
        act(h, a, "cancel", {"reason": "late"}).status_code == 200
    )  # but it can still be closed out


def test_allowed_actions_reflect_status_and_time(frozen) -> None:  # type: ignore[no-untyped-def]
    b, h, svc, _ = shop()
    now = NOW
    soon = appt(b, svc, now + timedelta(minutes=14))
    later = appt(b, svc, now + timedelta(minutes=16))
    ended_grace = appt(b, svc, now - timedelta(minutes=16))

    def actions(a: Appointment) -> list[str]:
        return call("get", f"{P}/appointments/{a.public_id}", h).json()["allowed_actions"]

    assert actions(later) == ["cancel", "reschedule"]
    assert actions(soon) == ["cancel", "reschedule", "complete"]
    assert actions(ended_grace) == ["cancel", "complete", "no_show"]


# ------------------------------------------------------------ complete / no-show timing


def test_complete_boundary_with_configurable_tolerance(frozen, settings) -> None:  # type: ignore[no-untyped-def]
    b, h, svc, _ = shop()
    tolerance = settings.PROVIDER_COMPLETE_TOLERANCE_MINUTES
    start = NOW + timedelta(minutes=tolerance)
    too_early = appt(b, svc, start + timedelta(seconds=1))
    r = act(h, too_early, "complete")
    assert r.status_code == 409 and r.json()["code"] == "appointment_not_started"
    on_time = appt(b, svc, start)
    assert act(h, on_time, "complete").status_code == 200
    settings.PROVIDER_COMPLETE_TOLERANCE_MINUTES = 0
    strict = appt(b, svc, NOW + timedelta(minutes=1))
    assert act(h, strict, "complete").status_code == 409


def test_no_show_only_after_grace(frozen, settings) -> None:  # type: ignore[no-untyped-def]
    b, h, svc, _ = shop()
    grace = settings.PROVIDER_NO_SHOW_GRACE_MINUTES
    early = appt(b, svc, NOW - timedelta(minutes=grace) + timedelta(seconds=1))
    r = act(h, early, "no-show")
    assert r.status_code == 409 and r.json()["code"] == "no_show_too_early"
    exact = appt(b, svc, NOW - timedelta(minutes=grace))
    assert act(h, exact, "no-show").status_code == 200
    future = appt(b, svc, NOW + timedelta(hours=1))
    assert act(h, future, "no-show").status_code == 409


def test_complete_credits_points_once_and_prompts_review(frozen, run_commit) -> None:  # type: ignore[no-untyped-def]
    b, h, svc, _ = shop()
    a = appt(b, svc, NOW - timedelta(hours=1))
    with run_commit():
        assert act(h, a, "complete").status_code == 200
    earned = PointTransaction.objects.filter(customer=a.customer, reason="booking")
    assert earned.count() == 1 and earned.get().delta > 0
    assert Notification.objects.filter(user=a.customer, template_key="review_prompt").exists()
    with run_commit():
        again = act(h, a, "complete")
    assert again.status_code == 409
    assert PointTransaction.objects.filter(customer=a.customer, reason="booking").count() == 1
    assert PointTransaction.objects.filter(customer=a.customer, reason="first_booking").count() == 1
    assert Notification.objects.filter(template_key="review_prompt", channel="in_app").count() == 1


def test_no_show_counts_against_customer_not_walk_ins(frozen, run_commit) -> None:  # type: ignore[no-untyped-def]
    b, h, svc, _ = shop()
    cust = make_customer(name="Late Lena")
    a = appt(b, svc, NOW - timedelta(hours=1), customer=cust)
    walk_in = appt(
        b,
        svc,
        NOW - timedelta(hours=3),
        customer=None,
        walk_in_name="W",
        walk_in_phone="+923451112222",
    )
    with run_commit():
        assert act(h, a, "no-show").status_code == 200
        assert act(h, walk_in, "no-show").status_code == 200
    assert CustomerProfile.objects.get(user=cust).no_show_count == 1
    again = appt(b, svc, NOW - timedelta(hours=5), customer=cust)
    with run_commit():
        act(h, again, "no-show")
    assert CustomerProfile.objects.get(user=cust).no_show_count == 2


# ------------------------------------------------------------ accept / decline / cancel


def test_accept_confirms_and_notifies_customer(frozen, run_commit) -> None:  # type: ignore[no-untyped-def]
    b, h, svc, _ = shop(confirm_mode="manual")
    a = appt(b, svc, local(TUESDAY, 12), status="pending")
    with run_commit():
        r = act(h, a, "accept")
    assert r.status_code == 200 and r.json()["status"] == "confirmed"
    ev = AppointmentEvent.objects.get(appointment=a, to_status="confirmed")
    assert ev.actor_role == "business_owner" and ev.from_status == "pending"
    assert Notification.objects.filter(user=a.customer, template_key="booking_confirmed").exists()


def test_decline_stores_reason_and_tells_customer(frozen, run_commit) -> None:  # type: ignore[no-untyped-def]
    b, h, svc, _ = shop(confirm_mode="manual")
    a = appt(b, svc, local(TUESDAY, 12), status="pending")
    with run_commit():
        r = act(h, a, "decline", {"reason": "Fully booked"})
    assert r.status_code == 200 and r.json()["status"] == "cancelled_by_business"
    a.refresh_from_db()
    assert a.cancellation_note == "Fully booked" and a.cancelled_at is not None
    assert Notification.objects.filter(
        user=a.customer, template_key="booking_cancelled_by_business"
    ).exists()
    assert (
        act(
            h, appt(b, svc, local(TUESDAY, 14), status="pending"), "decline", {"reason": "r" * 501}
        ).status_code
        == 422
    )
    blank = appt(b, svc, local(TUESDAY, 16), status="pending")
    assert act(h, blank, "decline", {}).status_code == 200  # reason optional


def test_cancel_requires_reason_and_cancels_pending_reschedule(frozen, run_commit) -> None:  # type: ignore[no-untyped-def]
    b, h, svc, _ = shop()
    a = appt(b, svc, local(TUESDAY, 12))
    req = RescheduleRequest.objects.create(
        appointment=a, new_start=local(TUESDAY, 15), new_end=local(TUESDAY, 16)
    )
    assert act(h, a, "cancel", {}).status_code == 422
    assert act(h, a, "cancel", {"reason": ""}).status_code == 422
    assert act(h, a, "cancel", {"reason": "r" * 501}).status_code == 422
    with run_commit():
        r = act(h, a, "cancel", {"reason": "r" * 500})
    assert r.status_code == 200 and r.json()["cancellation_reason"] == "business"
    req.refresh_from_db()
    assert req.status == "cancelled"
    assert Notification.objects.filter(
        user=a.customer, template_key="booking_cancelled_by_business"
    ).exists()


def test_cancel_frees_the_slot_publicly(frozen) -> None:  # type: ignore[no-untyped-def]
    b, h, svc, _ = shop()
    a = appt(b, svc, local(TUESDAY, 12))
    url = f"/api/v1/businesses/{b.public_id}/availability?date={TUESDAY}&service_ids={svc.pk}"
    assert not any("T12:00" in s["start"] for s in Client().get(url).json()["slots"])
    act(h, a, "cancel", {"reason": "x"})
    assert any("T12:00" in s["start"] for s in Client().get(url).json()["slots"])


# ------------------------------------------------------------ owner reschedule


def reschedule(h: dict[str, str], a: Appointment, start, **kw: Any) -> Any:  # type: ignore[no-untyped-def]
    return act(h, a, "reschedule", {"start": start.isoformat(), **kw})


def test_owner_reschedule_updates_notifies_customer_only(frozen, run_commit) -> None:  # type: ignore[no-untyped-def]
    b, h, svc, _ = shop()
    a = appt(b, svc, local(TUESDAY, 12))
    pending = RescheduleRequest.objects.create(
        appointment=a, new_start=local(TUESDAY, 15), new_end=local(TUESDAY, 16)
    )
    with run_commit():
        r = reschedule(h, a, local(TUESDAY, 15, 10))  # not on the 30-minute grid: allowed
    assert r.status_code == 200, r.content
    a.refresh_from_db()
    assert a.scheduled_start == local(TUESDAY, 15, 10) and a.scheduled_end == local(TUESDAY, 16, 10)
    assert a.status == "confirmed"
    pending.refresh_from_db()
    assert pending.status == "cancelled"
    assert AppointmentEvent.objects.filter(appointment=a, note="rescheduled by business").exists()
    assert Notification.objects.filter(
        user=a.customer, template_key="booking_rescheduled_by_business"
    ).exists()
    assert not Notification.objects.filter(template_key="provider_booking_rescheduled").exists()


def test_owner_reschedule_rejections(frozen) -> None:  # type: ignore[no-untyped-def]
    b, h, svc, _ = shop()
    a = appt(b, svc, local(TUESDAY, 12))
    appt(b, svc, local(TUESDAY, 15))
    ClosureFactory(business=b, starts_at=local(TUESDAY, 17), ends_at=local(TUESDAY, 18))
    same = reschedule(h, a, local(TUESDAY, 12))
    assert same.status_code == 422 and same.json()["code"] == "same_time"
    busy = reschedule(h, a, local(TUESDAY, 15, 30))
    assert busy.status_code == 409 and busy.json()["code"] == "slot_unavailable"
    blocked = reschedule(h, a, local(TUESDAY, 17, 15))
    assert blocked.status_code == 409 and blocked.json()["code"] == "time_blocked"
    closed = reschedule(h, a, local(TUESDAY, 21))
    assert closed.status_code == 422 and closed.json()["code"] == "outside_working_hours"
    past = reschedule(h, a, NOW - timedelta(hours=1))
    assert past.status_code == 422 and "start" in past.json()["fields"]
    assert act(h, a, "reschedule", {"start": "2026-11-04T12:00:00"}).status_code == 422
    assert act(h, a, "reschedule", {}).status_code == 422
    a.refresh_from_db()
    assert a.scheduled_start == local(TUESDAY, 12)
    # moving over its own old slot is fine (it ignores itself)
    assert reschedule(h, a, local(TUESDAY, 12, 30)).status_code == 200


def test_owner_reschedule_staff_mode(frozen) -> None:  # type: ignore[no-untyped-def]
    b, h, svc, (s1, s2) = shop(staff_level=True)
    a = appt(b, svc, local(TUESDAY, 12), staff=s1)
    appt(b, svc, local(TUESDAY, 15), staff=s2)
    stay = reschedule(h, a, local(TUESDAY, 15))  # keeps s1, who is free
    assert stay.status_code == 200 and stay.json()["staff"]["id"] == s1.pk
    clash = reschedule(h, a, local(TUESDAY, 15), staff_id=s2.pk)
    assert clash.status_code == 409
    moved = reschedule(h, a, local(TUESDAY, 17), staff_id=s2.pk)
    assert moved.status_code == 200 and moved.json()["staff"]["id"] == s2.pk
    StaffServiceFactory(staff=s1, service=ServiceFactory(business=b))
    foreign = reschedule(h, a, local(TUESDAY, 18), staff_id=StaffFactory().pk)
    assert foreign.status_code == 422 and foreign.json()["code"] == "staff_cannot_perform"


# ------------------------------------------------------------ customer reschedule requests


def test_accept_and_decline_customer_reschedule_requests(frozen, run_commit) -> None:  # type: ignore[no-untyped-def]
    b, h, svc, _ = shop(confirm_mode="manual")
    a = appt(b, svc, local(TUESDAY, 12))
    RescheduleRequest.objects.create(
        appointment=a, new_start=local(TUESDAY, 15), new_end=local(TUESDAY, 16)
    )
    with run_commit():
        r = act(h, a, "reschedule-request/accept")
    assert r.status_code == 200, r.content
    a.refresh_from_db()
    assert a.scheduled_start == local(TUESDAY, 15)
    assert RescheduleRequest.objects.get(appointment=a).status == "accepted"
    assert Notification.objects.filter(
        user=a.customer, template_key="reschedule_request_accepted"
    ).exists()
    assert not Notification.objects.filter(template_key="provider_booking_rescheduled").exists()
    assert act(h, a, "reschedule-request/accept").json()["code"] == "no_pending_request"

    c = appt(b, svc, local(TUESDAY, 17))
    req = RescheduleRequest.objects.create(
        appointment=c, new_start=local(TUESDAY, 18), new_end=local(TUESDAY, 19)
    )
    with run_commit():
        d = act(h, c, "reschedule-request/decline")
    assert d.status_code == 200
    req.refresh_from_db()
    c.refresh_from_db()
    assert req.status == "declined" and c.scheduled_start == local(TUESDAY, 17)
    assert Notification.objects.filter(
        user=c.customer, template_key="reschedule_request_declined"
    ).exists()


def test_accepting_request_for_taken_slot_is_409(frozen) -> None:  # type: ignore[no-untyped-def]
    b, h, svc, _ = shop(confirm_mode="manual")
    a = appt(b, svc, local(TUESDAY, 12))
    RescheduleRequest.objects.create(
        appointment=a, new_start=local(TUESDAY, 15), new_end=local(TUESDAY, 16)
    )
    appt(b, svc, local(TUESDAY, 15))
    r = act(h, a, "reschedule-request/accept")
    assert r.status_code == 409 and r.json()["code"] == "slot_unavailable"
    a.refresh_from_db()
    assert a.scheduled_start == local(TUESDAY, 12)


# ------------------------------------------------------------ notes


def test_note_is_private_to_the_business(frozen) -> None:  # type: ignore[no-untyped-def]
    from apps.accounts.tests.helpers import bearer, login_client

    b, h, svc, _ = shop()
    a = appt(b, svc, local(TUESDAY, 12))
    r = call("put", f"{P}/appointments/{a.public_id}/note", h, {"note": "  Allergic to ammonia  "})
    assert r.status_code == 200 and r.json()["business_note"] == "Allergic to ammonia"
    assert (
        call("get", f"{P}/appointments/{a.public_id}", h).json()["business_note"]
        == "Allergic to ammonia"
    )
    _, session = login_client(a.customer)
    mine = Client().get(f"/api/v1/appointments/{a.public_id}", **bearer(session))
    assert mine.status_code == 200 and "ammonia" not in mine.content.decode()
    assert "business_note" not in mine.json()
    assert (
        call("put", f"{P}/appointments/{a.public_id}/note", h, {"note": "n" * 1001}).status_code
        == 422
    )
    assert (
        call("put", f"{P}/appointments/{a.public_id}/note", h, {"note": "n" * 1000}).status_code
        == 200
    )
    assert (
        call("put", f"{P}/appointments/{a.public_id}/note", h, {"note": ""}).json()["business_note"]
        == ""
    )
    closed = appt(b, svc, local(TUESDAY, 15), status="completed")
    assert (
        call("put", f"{P}/appointments/{closed.public_id}/note", h, {"note": "ok"}).status_code
        == 200
    )


# ------------------------------------------------------------ polling feed


def test_changes_feed_since(frozen) -> None:  # type: ignore[no-untyped-def]
    b, h, svc, _ = shop()
    old = appt(b, svc, local(TUESDAY, 10))
    Appointment.objects.filter(pk=old.pk).update(updated_at=NOW - timedelta(hours=2))
    touched = appt(b, svc, local(TUESDAY, 12))
    Appointment.objects.filter(pk=touched.pk).update(updated_at=NOW - timedelta(minutes=30))
    via_request = appt(b, svc, local(TUESDAY, 14))
    Appointment.objects.filter(pk=via_request.pk).update(updated_at=NOW - timedelta(hours=3))
    RescheduleRequest.objects.create(
        appointment=via_request, new_start=local(TUESDAY, 16), new_end=local(TUESDAY, 17)
    )
    since = (NOW - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    r = call("get", f"{base(b)}/appointments/changes?since={since}", h)
    assert r.status_code == 200, r.content
    data = r.json()
    assert [x["id"] for x in data["appointments"]] == [
        str(touched.public_id),
        str(via_request.public_id),
    ] or {x["id"] for x in data["appointments"]} == {
        str(touched.public_id),
        str(via_request.public_id),
    }
    assert data["server_time"].startswith("2026-11-02T04:00:00")
    later = call("get", f"{base(b)}/appointments/changes?since=2026-11-02T04:00:01Z", h).json()
    assert later["appointments"] == []
    assert call("get", f"{base(b)}/appointments/changes", h).status_code == 422
    assert call("get", f"{base(b)}/appointments/changes?since=notadate", h).status_code == 422


# ------------------------------------------------------------ concurrency


@pytest.mark.django_db(transaction=True)
def test_concurrent_accept_and_customer_cancel_end_consistent(frozen) -> None:  # type: ignore[no-untyped-def]
    from apps.accounts.tests.helpers import bearer, login_client

    b, h, svc, _ = shop(confirm_mode="manual")
    a = appt(b, svc, local(TUESDAY, 12), status="pending")
    _, session = login_client(a.customer)
    barrier = threading.Barrier(2)
    out: dict[str, int] = {}

    def owner() -> None:
        try:
            barrier.wait()
            out["accept"] = act(h, a, "accept").status_code
        finally:
            connection.close()

    def customer() -> None:
        try:
            barrier.wait()
            r = Client().patch(
                f"/api/v1/appointments/{a.public_id}/cancel",
                data='{"reason": "other"}',
                content_type="application/json",
                **bearer(session),
            )
            out["cancel"] = r.status_code
        finally:
            connection.close()

    threads = [threading.Thread(target=owner), threading.Thread(target=customer)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    a.refresh_from_db()
    # Either the owner accepted first (then the customer's cancel of a confirmed booking works) or
    # the customer cancelled first (then the accept conflicts). Never a lost update.
    if out["accept"] == 200 and out["cancel"] == 200:
        assert a.status == "cancelled_by_customer"
        history = list(a.events.values_list("to_status", flat=True))
        assert history == ["confirmed", "cancelled_by_customer"]
    else:
        assert out["accept"] == 409 and out["cancel"] == 200
        assert a.status == "cancelled_by_customer"
    assert a.status in ("cancelled_by_customer", "confirmed")


@pytest.mark.django_db(transaction=True)
def test_concurrent_complete_credits_points_once(frozen) -> None:  # type: ignore[no-untyped-def]
    b, h, svc, _ = shop()
    a = appt(b, svc, NOW - timedelta(hours=1))
    barrier = threading.Barrier(4)
    codes: list[int] = []

    def worker() -> None:
        try:
            barrier.wait()
            codes.append(act(h, a, "complete").status_code)
        finally:
            connection.close()

    threads = [threading.Thread(target=worker) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(codes) == [200, 409, 409, 409]
    assert PointTransaction.objects.filter(customer=a.customer, reason="booking").count() == 1
    assert PointTransaction.objects.filter(customer=a.customer, reason="first_booking").count() == 1


# ------------------------------------------------------------ auto-escalation


def pending_at(b: Business, svc: Service, created, start=None, **kw: Any) -> Appointment:  # type: ignore[no-untyped-def]
    a = appt(b, svc, start or local(TUESDAY + timedelta(days=5), 12), status="pending", **kw)
    Appointment.objects.filter(pk=a.pk).update(created_at=created)
    return a


def test_escalation_alert_boundaries_and_idempotency(settings, run_commit) -> None:  # type: ignore[no-untyped-def]
    settings.PENDING_ESCALATION_MINUTES = 120
    settings.PENDING_AUTO_CANCEL_MINUTES = 720
    b, h, svc, _ = shop(confirm_mode="manual")
    created = NOW
    a = pending_at(b, svc, created)
    owner_alerts = lambda: Notification.objects.filter(  # noqa: E731
        user=b.owner, template_key="pending_escalation"
    )
    with time_machine.travel(created + timedelta(minutes=119, seconds=59), tick=False):
        assert provider_escalation.escalate_pending().escalated == 0
    assert not owner_alerts().exists()
    with time_machine.travel(created + timedelta(minutes=120), tick=False), run_commit():
        first = provider_escalation.escalate_pending()
        second = provider_escalation.escalate_pending()
    assert (first.escalated, first.cancelled) == (1, 0) and second.escalated == 0
    assert owner_alerts().filter(channel="in_app").count() == 1
    assert owner_alerts().filter(channel="sms").count() == 1
    with time_machine.travel(created + timedelta(minutes=300), tick=False):
        assert provider_escalation.escalate_pending().escalated == 0  # still only once
    a.refresh_from_db()
    assert a.status == "pending"


def test_auto_cancel_at_boundary_notifies_customer_once(settings, run_commit) -> None:  # type: ignore[no-untyped-def]
    settings.PENDING_AUTO_CANCEL_MINUTES = 720
    b, h, svc, _ = shop(confirm_mode="manual")
    a = pending_at(b, svc, NOW)
    with time_machine.travel(NOW + timedelta(minutes=719, seconds=59), tick=False):
        assert provider_escalation.escalate_pending().cancelled == 0
    with time_machine.travel(NOW + timedelta(minutes=720), tick=False), run_commit():
        r1 = provider_escalation.escalate_pending()
        r2 = provider_escalation.escalate_pending()
    assert (r1.cancelled, r2.cancelled) == (1, 0)
    a.refresh_from_db()
    assert (
        a.status == "cancelled_by_business"
        and a.cancellation_note == "No response from the business"
    )
    ev = AppointmentEvent.objects.get(appointment=a, to_status="cancelled_by_business")
    assert ev.actor_role == "system" and ev.actor_id is None
    n = Notification.objects.filter(user=a.customer, template_key="booking_cancelled_by_business")
    assert n.filter(channel="in_app").count() == 1


def test_cancel_wins_when_both_thresholds_passed_in_one_run(run_commit) -> None:  # type: ignore[no-untyped-def]
    b, h, svc, _ = shop(confirm_mode="manual")
    a = pending_at(b, svc, NOW - timedelta(minutes=800))
    with time_machine.travel(NOW, tick=False), run_commit():
        r = provider_escalation.escalate_pending()
    assert (r.escalated, r.cancelled) == (0, 1)
    a.refresh_from_db()
    assert a.status == "cancelled_by_business"
    assert not Notification.objects.filter(template_key="pending_escalation").exists()


def test_auto_cancel_happens_at_start_when_that_comes_first() -> None:
    b, h, svc, _ = shop(confirm_mode="manual")
    start = NOW + timedelta(hours=3)
    a = pending_at(b, svc, NOW, start=start)
    with time_machine.travel(start - timedelta(seconds=1), tick=False):
        assert provider_escalation.escalate_pending().cancelled == 0
    with time_machine.travel(start, tick=False):
        assert provider_escalation.escalate_pending().cancelled == 1
    a.refresh_from_db()
    assert a.status == "cancelled_by_business"


def test_escalation_skips_answered_and_started_bookings(settings) -> None:  # type: ignore[no-untyped-def]
    b, h, svc, _ = shop(confirm_mode="manual")
    confirmed = appt(b, svc, local(TUESDAY + timedelta(days=5), 12))
    Appointment.objects.filter(pk=confirmed.pk).update(created_at=NOW - timedelta(days=2))
    soon = pending_at(b, svc, NOW - timedelta(minutes=121), start=NOW + timedelta(minutes=30))
    with time_machine.travel(NOW, tick=False):
        r = provider_escalation.escalate_pending()
    assert r.escalated == 1 and r.cancelled == 0
    confirmed.refresh_from_db(), soon.refresh_from_db()
    assert confirmed.status == "confirmed" and soon.status == "pending"


def test_celery_task_runs_escalation_and_reports_counts(settings, run_commit) -> None:  # type: ignore[no-untyped-def]
    b, h, svc, _ = shop(confirm_mode="manual")
    pending_at(b, svc, NOW - timedelta(minutes=130))
    pending_at(b, svc, NOW - timedelta(minutes=800))
    with time_machine.travel(NOW, tick=False), run_commit():
        assert escalate_pending_bookings() == {"escalated": 1, "cancelled": 1}
        assert escalate_pending_bookings() == {"escalated": 0, "cancelled": 0}


def test_beat_schedule_contains_escalation(settings) -> None:  # type: ignore[no-untyped-def]
    entry = settings.CELERY_BEAT_SCHEDULE["pending-escalation"]
    assert entry["task"] == "apps.bookings.tasks.escalate_pending_bookings"


def test_unused_imports_guard() -> None:
    assert UserFactory and CustomerProfileFactory
