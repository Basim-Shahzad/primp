import json
import uuid
from datetime import timedelta

import pytest
import time_machine
from django.test import Client

from apps.accounts.factories import UserFactory, make_customer
from apps.accounts.models import Role
from apps.accounts.tests.helpers import bearer, login_client
from apps.bookings.factories import AppointmentFactory
from apps.bookings.models import (
    Appointment,
    AppointmentEvent,
    AppointmentService,
    RescheduleRequest,
)
from apps.bookings.tests.helpers import NOW
from apps.businesses.factories import (
    BusinessFactory,
    ServiceFactory,
    StaffFactory,
    StaffServiceFactory,
)
from apps.reviews.factories import ReviewFactory

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("frozen")]
H = timedelta(hours=1)


def session():  # type: ignore[no-untyped-def]
    user = make_customer()
    _, s = login_client(user)
    return user, bearer(s)


def appt(user, start_in: timedelta, status: str = "confirmed", **kw):  # type: ignore[no-untyped-def]
    kw.setdefault("business", BusinessFactory())
    return AppointmentFactory(
        customer=user,
        status=status,
        scheduled_start=NOW + start_in,
        scheduled_end=NOW + start_in + H,
        **kw,
    )


def listing(headers, scope=None, **params):  # type: ignore[no-untyped-def]
    if scope is not None:
        params["scope"] = scope
    return Client().get("/api/v1/users/me/appointments", params, **headers)


def ids(r):  # type: ignore[no-untyped-def]
    assert r.status_code == 200, r.content
    return [i["id"] for i in r.json()["items"]]


# ---------------- scopes ----------------


def test_scope_partition_at_now() -> None:
    user, h = session()
    future = appt(user, 2 * H)
    starting_now = appt(user, timedelta(0))
    started_unfinished = appt(user, -30 * timedelta(minutes=1))  # started 30 min ago, not completed
    done = appt(user, -3 * H, "completed")
    no_show = appt(user, -4 * H, "no_show")
    cancelled_future = appt(user, 5 * H, "cancelled_by_customer")
    cancelled_past = appt(user, -5 * H, "cancelled_by_business")

    def pk(a: Appointment) -> str:
        return str(a.public_id)

    assert set(ids(listing(h, "upcoming"))) == {pk(future), pk(starting_now)}
    assert set(ids(listing(h, "past"))) == {pk(started_unfinished), pk(done), pk(no_show)}
    assert set(ids(listing(h, "cancelled"))) == {pk(cancelled_future), pk(cancelled_past)}
    # the three tabs partition everything exactly once
    everything = (
        ids(listing(h, "upcoming")) + ids(listing(h, "past")) + ids(listing(h, "cancelled"))
    )
    assert len(everything) == len(set(everything)) == 7


def test_one_second_either_side_of_now() -> None:
    user, h = session()
    just_before = appt(user, -timedelta(seconds=1))
    just_after = appt(user, timedelta(seconds=1))
    assert ids(listing(h, "upcoming")) == [str(just_after.public_id)]
    assert ids(listing(h, "past")) == [str(just_before.public_id)]


def test_default_scope_is_upcoming_and_invalid_scope_422() -> None:
    user, h = session()
    future = appt(user, H)
    appt(user, -H, "completed")
    assert ids(listing(h)) == [str(future.public_id)]
    assert listing(h, "all").status_code == 422
    assert listing(h, "").status_code == 422


def test_ordering_upcoming_ascending_past_and_cancelled_descending() -> None:
    user, h = session()
    up = [appt(user, d * H) for d in (5, 1, 3)]
    past = [appt(user, -d * H, "completed") for d in (5, 1, 3)]
    cancelled = [appt(user, d * H, "cancelled_by_customer") for d in (-2, 4, 1)]

    def by_start(xs: list[Appointment], rev: bool) -> list[str]:
        return [str(a.public_id) for a in sorted(xs, key=lambda a: a.scheduled_start, reverse=rev)]

    assert ids(listing(h, "upcoming")) == by_start(up, False)
    assert ids(listing(h, "past")) == by_start(past, True)
    assert ids(listing(h, "cancelled")) == by_start(cancelled, True)


def test_only_own_appointments_listed() -> None:
    user, h = session()
    mine = appt(user, H)
    appt(make_customer(), H)
    assert ids(listing(h, "upcoming")) == [str(mine.public_id)]


def test_empty_lists() -> None:
    _, h = session()
    for scope in ("upcoming", "past", "cancelled"):
        r = listing(h, scope)
        assert r.json() == {"items": [], "next_cursor": None}


def test_item_shape_and_local_times() -> None:
    user, h = session()
    business = BusinessFactory(name="Glow")
    staff = StaffFactory(business=business, name="Ayesha")
    a = appt(user, 2 * H, business=business, staff=staff, total_price=2500)
    svc = ServiceFactory(business=business, name="Cut", price=2500, duration_minutes=60)
    AppointmentService.objects.create(
        appointment=a, service=svc, name="Cut", price_at_booking=2500, duration_at_booking=60
    )
    item = listing(h, "upcoming").json()["items"][0]
    assert (
        item["id"] == str(a.public_id)
        and item["status"] == "confirmed"
        and item["total_price"] == 2500
    )
    assert item["business"] == {
        "id": str(business.public_id),
        "name": "Glow",
        "slug": business.slug,
    }
    assert item["staff"] == {"id": staff.pk, "name": "Ayesha"}
    assert item["services"] == [
        {"id": svc.pk, "name": "Cut", "price": 2500, "duration_minutes": 60}
    ]
    assert item["start"].endswith("+05:00")


def test_cursor_pagination_all_scopes() -> None:
    user, h = session()
    for i in range(7):
        appt(user, (i + 1) * H)
        appt(user, -(i + 1) * H, "completed")
        appt(user, (i + 1) * H, "cancelled_by_customer")
    for scope in ("upcoming", "past", "cancelled"):
        seen, cursor = [], None
        for _ in range(10):
            params = {"limit": 3, **({"cursor": cursor} if cursor else {})}
            data = listing(h, scope, **params).json()
            seen += [i["id"] for i in data["items"]]
            cursor = data["next_cursor"]
            if not cursor:
                break
        assert len(seen) == 7 == len(set(seen)), scope
    assert listing(h, "upcoming", cursor="garbage").status_code == 400
    assert listing(h, "upcoming", limit=0).status_code == 422
    assert listing(h, "upcoming", limit=101).status_code == 422


def test_cursor_stable_when_new_booking_appears() -> None:
    user, h = session()
    for i in range(5):
        appt(user, (i + 2) * H)
    first = listing(h, "upcoming", limit=2).json()
    appt(user, H)  # sorts before everything already seen
    second = listing(h, "upcoming", limit=2, cursor=first["next_cursor"]).json()
    assert {i["id"] for i in first["items"]}.isdisjoint({i["id"] for i in second["items"]})


def test_list_query_count_bounded(django_assert_max_num_queries) -> None:  # type: ignore[no-untyped-def]
    user, h = session()
    for i in range(15):
        a = appt(user, (i + 1) * H)
        svc = ServiceFactory(business=a.business)
        AppointmentService.objects.create(
            appointment=a, service=svc, name="S", price_at_booking=1, duration_at_booking=30
        )
    with django_assert_max_num_queries(6):
        r = listing(h, "upcoming", limit=15)
    assert len(r.json()["items"]) == 15


def test_history_auth() -> None:
    assert listing({}).status_code == 401
    _, s = login_client(UserFactory(role=Role.ADMIN))
    assert listing(bearer(s)).status_code == 403


# ---------------- next ----------------


def test_next_appointment() -> None:
    user, h = session()
    assert Client().get("/api/v1/users/me/appointments/next", **h).json() is None
    appt(user, 10 * H)
    soonest = appt(user, 2 * H)
    appt(user, 1 * H, "cancelled_by_customer")
    appt(user, -1 * H)
    r = Client().get("/api/v1/users/me/appointments/next", **h)
    assert r.status_code == 200 and r.json()["id"] == str(soonest.public_id)


def test_next_appointment_auth_and_isolation() -> None:
    other = make_customer()
    appt(other, H)
    _, h = session()
    assert Client().get("/api/v1/users/me/appointments/next", **h).json() is None
    assert Client().get("/api/v1/users/me/appointments/next").status_code == 401


# ---------------- detail & allowed actions ----------------


def detail(headers, a):  # type: ignore[no-untyped-def]
    return Client().get(f"/api/v1/appointments/{a.public_id}", **headers)


def actions(r):  # type: ignore[no-untyped-def]
    assert r.status_code == 200, r.content
    return r.json()["actions"]


ALL_FALSE = {"can_cancel": False, "can_reschedule": False, "can_review": False}


@pytest.mark.parametrize(
    ("status", "offset", "expected"),
    [
        ("pending", 5 * H, {"can_cancel": True, "can_reschedule": True, "can_review": False}),
        ("confirmed", 5 * H, {"can_cancel": True, "can_reschedule": True, "can_review": False}),
        (
            "confirmed",
            timedelta(seconds=1),
            {"can_cancel": True, "can_reschedule": True, "can_review": False},
        ),
        ("confirmed", timedelta(0), ALL_FALSE),  # starting right now: too late
        ("confirmed", -1 * H, ALL_FALSE),
        ("pending", -1 * H, ALL_FALSE),
        ("completed", -2 * H, {"can_cancel": False, "can_reschedule": False, "can_review": True}),
        ("no_show", -2 * H, ALL_FALSE),
        ("cancelled_by_customer", 5 * H, ALL_FALSE),
        ("cancelled_by_business", 5 * H, ALL_FALSE),
    ],
)
def test_allowed_actions_matrix(status: str, offset: timedelta, expected: dict[str, bool]) -> None:
    user, h = session()
    assert actions(detail(h, appt(user, offset, status))) == expected


def test_review_action_disappears_after_reviewing() -> None:
    user, h = session()
    a = appt(user, -2 * H, "completed")
    assert actions(detail(h, a))["can_review"] is True
    ReviewFactory(appointment=a)
    r = detail(h, a)
    assert actions(r)["can_review"] is False and r.json()["has_review"] is True


def test_reschedule_action_off_while_request_pending() -> None:
    user, h = session()
    a = appt(user, 5 * H)
    RescheduleRequest.objects.create(appointment=a, new_start=NOW + 8 * H, new_end=NOW + 9 * H)
    r = detail(h, a)
    assert actions(r) == {"can_cancel": True, "can_reschedule": False, "can_review": False}
    assert r.json()["pending_reschedule"]["status"] == "pending"
    assert r.json()["pending_reschedule"]["new_start"].endswith("+05:00")


def test_detail_timeline_and_contact() -> None:
    user, h = session()
    business = BusinessFactory(phone="+923001234567", whatsapp="+923007654321", address="Plot 9")
    a = appt(user, 5 * H, business=business)
    AppointmentEvent.objects.create(
        appointment=a, from_status="", to_status="pending", actor_id=user.pk, actor_role="customer"
    )
    AppointmentEvent.objects.create(
        appointment=a,
        from_status="pending",
        to_status="confirmed",
        actor_id=1,
        actor_role="business_owner",
        note="ok",
    )
    body = detail(h, a).json()
    assert [
        (e["from_status"], e["status"], e["actor_role"], e["note"]) for e in body["timeline"]
    ] == [
        ("", "pending", "customer", ""),
        ("pending", "confirmed", "business_owner", "ok"),
    ]
    assert "actor_id" not in body["timeline"][0]
    assert body["contact"] == {
        "phone": "+923001234567",
        "whatsapp": "+923007654321",
        "address": "Plot 9",
        "lat": pytest.approx(24.8607, abs=1e-4),
        "lng": pytest.approx(67.0011, abs=1e-4),
    }
    assert body["id"] == str(a.public_id) and body["business"]["slug"] == business.slug


def test_detail_other_users_appointment_404_and_unknown_404() -> None:
    other = make_customer()
    a = appt(other, H)
    _, h = session()
    r = detail(h, a)
    assert r.status_code == 404 and r.json()["code"] == "not_found"
    ghost = type("A", (), {"public_id": uuid.uuid4()})()
    assert detail(h, ghost).status_code == 404
    assert detail({}, a).status_code == 401


def test_detail_query_count_bounded(django_assert_max_num_queries) -> None:  # type: ignore[no-untyped-def]
    user, h = session()
    a = appt(user, 5 * H)
    for _ in range(5):
        AppointmentEvent.objects.create(appointment=a, to_status="confirmed")
        svc = ServiceFactory(business=a.business)
        AppointmentService.objects.create(
            appointment=a, service=svc, name="S", price_at_booking=1, duration_at_booking=1
        )
    with django_assert_max_num_queries(10):
        assert detail(h, a).status_code == 200


# ---------------- rebook draft ----------------


def draft(headers, a):  # type: ignore[no-untyped-def]
    return Client().post(f"/api/v1/appointments/{a.public_id}/rebook-draft", **headers)


def full_appointment(user, **business_kw):  # type: ignore[no-untyped-def]
    business = BusinessFactory(staff_level_booking=True, **business_kw)
    svc1, svc2 = (
        ServiceFactory(business=business, name="Cut"),
        ServiceFactory(business=business, name="Colour"),
    )
    staff = StaffFactory(business=business, name="Ayesha")
    for s in (svc1, svc2):
        StaffServiceFactory(staff=staff, service=s)
    a = appt(user, -2 * H, "completed", business=business, staff=staff)
    for s in (svc1, svc2):
        AppointmentService.objects.create(
            appointment=a,
            service=s,
            name=s.name,
            price_at_booking=s.price,
            duration_at_booking=s.duration_minutes,
        )
    return a, business, [svc1, svc2], staff


def test_rebook_draft_prefills_without_creating_a_booking() -> None:
    user, h = session()
    a, business, services, staff = full_appointment(user)
    before = Appointment.objects.count()
    r = draft(h, a)
    assert r.status_code == 200
    body = r.json()
    assert body["business_id"] == str(business.public_id) and body["business_slug"] == business.slug
    assert body["service_ids"] == [s.pk for s in services] and body["staff_id"] == staff.pk
    assert (
        body["staff_available"] is True
        and body["unavailable_service_ids"] == []
        and body["can_book"] is True
    )
    assert [s["name"] for s in body["services"]] == ["Cut", "Colour"]
    assert Appointment.objects.count() == before


def test_rebook_draft_flags_inactive_services_staff_and_unlisted_business() -> None:
    user, h = session()
    a, business, services, staff = full_appointment(user)
    services[1].is_active = False
    services[1].save()
    body = draft(h, a).json()
    assert body["unavailable_service_ids"] == [services[1].pk] and body["can_book"] is False
    services[1].is_active = True
    services[1].save()
    staff.is_active = False
    staff.save()
    body = draft(h, a).json()
    assert (
        body["staff_available"] is False and body["staff_id"] is None and body["can_book"] is True
    )
    business.is_listed = False
    business.save()
    assert draft(h, a).json()["can_book"] is False


def test_rebook_draft_works_for_cancelled_appointment_and_without_staff() -> None:
    user, h = session()
    a = appt(user, 5 * H, "cancelled_by_customer")
    svc = ServiceFactory(business=a.business)
    AppointmentService.objects.create(
        appointment=a, service=svc, name="S", price_at_booking=1, duration_at_booking=1
    )
    body = draft(h, a).json()
    assert (
        body["staff_id"] is None
        and body["staff_available"] is True
        and body["service_ids"] == [svc.pk]
    )


def test_rebook_draft_ownership_and_auth() -> None:
    other = make_customer()
    a = appt(other, H)
    _, h = session()
    assert draft(h, a).status_code == 404
    assert draft({}, a).status_code == 401
    assert (
        Client().post(f"/api/v1/appointments/{uuid.uuid4()}/rebook-draft", **h).status_code == 404
    )


def test_rebook_draft_roundtrip_into_hold(frozen) -> None:  # type: ignore[no-untyped-def]
    """The draft contains everything the frontend needs to start a new hold."""
    from apps.bookings.tests.api_helpers import HOLD, post
    from apps.bookings.tests.helpers import TUESDAY, local
    from apps.businesses.factories import open_all_week

    user, h = session()
    a, business, _services, staff = full_appointment(user, lead_time_minutes=0)
    open_all_week(business)
    d = draft(h, a).json()
    body = {
        "business_id": d["business_id"],
        "service_ids": d["service_ids"],
        "staff_id": d["staff_id"],
        "start": local(TUESDAY, 12).isoformat(),
    }
    r = post(HOLD, body, h)
    assert r.status_code == 201, r.content
    assert json.loads(r.content)["staff_id"] == staff.pk
    assert time_machine and Appointment.objects.filter(customer=user).count() == 1
