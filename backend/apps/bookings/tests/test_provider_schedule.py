import threading
from datetime import datetime, time, timedelta
from typing import Any

import pytest
from django.db import connection
from django.test import Client
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from apps.accounts.factories import UserFactory, make_customer
from apps.bookings import availability
from apps.bookings.factories import AppointmentFactory
from apps.bookings.models import Appointment, AppointmentEvent
from apps.bookings.tests.helpers import MONDAY, NOW, TUESDAY, local
from apps.businesses import hours, selectors
from apps.businesses.factories import (
    BusinessFactory,
    ClosureFactory,
    ServiceFactory,
    StaffFactory,
    StaffServiceFactory,
    open_all_week,
)
from apps.businesses.models import Business, Closure, OpeningHours, Service, Staff
from apps.businesses.tests.provider_helpers import P, call, customer_headers, owner_headers
from apps.notifications.models import Notification

pytestmark = pytest.mark.django_db
PUBLIC = "/api/v1/businesses"


def shop(
    staff_level: bool = False, **kw: Any
) -> tuple[Business, dict[str, str], Service, list[Staff]]:
    owner, h = owner_headers()
    kw.setdefault("lead_time_minutes", 0)
    b = BusinessFactory(owner=owner, staff_level_booking=staff_level, **kw)
    open_all_week(b)  # 10:00-20:00 every day
    svc = ServiceFactory(business=b, duration_minutes=60, price=2000)
    staff: list[Staff] = []
    if staff_level:
        staff = [StaffFactory(business=b) for _ in range(2)]
        for s in staff:
            StaffServiceFactory(staff=s, service=svc)
    return b, h, svc, staff


def base(b: Business) -> str:
    return f"{P}/businesses/{b.public_id}"


def manual(h: dict[str, str], b: Business, svc: Service, at: datetime, **kw: Any) -> Any:
    body = {
        "customer_name": "Walk In",
        "customer_phone": "0300 1234567",
        "service_ids": [svc.pk],
        "start": at.isoformat(),
        **kw,
    }
    return call("post", f"{base(b)}/manual-bookings", h, body)


def public_starts(b: Business, svc: Service, day=TUESDAY, **params: Any) -> list[str]:  # type: ignore[no-untyped-def]
    qs = f"date={day.isoformat()}&service_ids={svc.pk}" + "".join(
        f"&{k}={v}" for k, v in params.items()
    )
    r = Client().get(f"{PUBLIC}/{b.public_id}/availability?{qs}")
    assert r.status_code == 200, r.content
    return [s["start"] for s in r.json()["slots"]]


def appt(
    b: Business,
    svc: Service,
    start: datetime,
    staff: Staff | None = None,
    status: str = "confirmed",
) -> Appointment:
    from apps.bookings.models import AppointmentService

    a = AppointmentFactory(
        business=b,
        staff=staff,
        customer=make_customer(),
        status=status,
        scheduled_start=start,
        scheduled_end=start + timedelta(hours=1),
    )
    AppointmentService.objects.create(
        appointment=a,
        service=svc,
        name=svc.name,
        price_at_booking=svc.price,
        duration_at_booking=60,
    )
    return a


# ------------------------------------------------------------ access matrix


def test_schedule_endpoints_auth_role_and_ownership(frozen) -> None:  # type: ignore[no-untyped-def]
    b, h, svc, _ = shop()
    block = ClosureFactory(business=b)
    _, other = owner_headers()
    cust = customer_headers()
    calls: list[tuple[str, str, Any]] = [
        ("get", f"{base(b)}/blocks", None),
        (
            "post",
            f"{base(b)}/blocks",
            {"starts_at": "2026-12-01T10:00:00+05:00", "ends_at": "2026-12-01T12:00:00+05:00"},
        ),
        ("delete", f"{P}/blocks/{block.pk}", None),
        (
            "post",
            f"{base(b)}/manual-bookings",
            {
                "customer_name": "A",
                "customer_phone": "03001234567",
                "service_ids": [svc.pk],
                "start": local(TUESDAY, 12).isoformat(),
            },
        ),
        ("get", f"{base(b)}/conflicts", None),
        ("get", f"{base(b)}/schedule?from=2026-11-02&to=2026-11-03", None),
    ]
    for method, path, body in calls:
        assert call(method, path, {}, body).status_code == 401, path
        assert call(method, path, {"HTTP_AUTHORIZATION": "Bearer x"}, body).status_code == 401, path
        assert call(method, path, cust, body).status_code == 403, path
        assert call(method, path, other, body).status_code == 404, path
    assert Closure.objects.filter(pk=block.pk).exists() and not Appointment.objects.exists()


# ------------------------------------------------------------ blocks


def block(h: dict[str, str], b: Business, start: datetime, end: datetime, **kw: Any) -> Any:
    return call(
        "post",
        f"{base(b)}/blocks",
        h,
        {"starts_at": start.isoformat(), "ends_at": end.isoformat(), **kw},
    )


def test_part_day_block_removes_slots_from_public_availability_immediately(frozen) -> None:  # type: ignore[no-untyped-def]
    b, h, svc, _ = shop()
    before = public_starts(b, svc)
    assert any("T12:00" in s for s in before)
    r = block(h, b, local(TUESDAY, 12), local(TUESDAY, 14), kind="leave", reason="Dentist")
    assert r.status_code == 201, r.content
    assert (
        r.json()["kind"] == "leave"
        and r.json()["reason"] == "Dentist"
        and r.json()["staff_id"] is None
    )
    after = public_starts(b, svc)
    hours_of_day = {datetime.fromisoformat(s).hour for s in after}
    assert (
        12 not in hours_of_day
        and 13 not in hours_of_day
        and 11 in hours_of_day
        and 14 in hours_of_day
    )
    assert call("delete", f"{P}/blocks/{r.json()['id']}", h).status_code == 204
    assert public_starts(b, svc) == before  # removal is reflected at once too


def test_staff_block_only_affects_that_staff(frozen) -> None:  # type: ignore[no-untyped-def]
    b, h, svc, (s1, s2) = shop(staff_level=True)
    r = block(h, b, local(TUESDAY, 10), local(TUESDAY, 20), staff_id=s1.pk)
    assert r.status_code == 201 and r.json()["staff_id"] == s1.pk
    assert public_starts(b, svc, staff_id=s1.pk) == []
    assert public_starts(b, svc, staff_id=s2.pk) != []
    assert public_starts(b, svc) != []


@pytest.mark.parametrize(
    "body,field",
    [
        ({"ends_at": "2026-12-01T10:00:00+05:00"}, "ends_at"),
        ({"ends_at": "2026-12-01T09:00:00+05:00"}, "ends_at"),
        ({"ends_at": "2027-12-03T10:00:00+05:00"}, "ends_at"),
        ({"kind": "party"}, "kind"),
        ({"starts_at": "2026-12-01T10:00:00"}, "starts_at"),
        ({"staff_id": 0}, "staff_id"),
        ({"reason": "r" * 256}, "reason"),
    ],
)
def test_block_validation(body: dict[str, Any], field: str, frozen) -> None:  # type: ignore[no-untyped-def]
    b, h, svc, _ = shop()
    payload = {
        "starts_at": "2026-12-01T10:00:00+05:00",
        "ends_at": "2026-12-01T12:00:00+05:00",
        **body,
    }
    r = call("post", f"{base(b)}/blocks", h, payload)
    assert r.status_code == 422 and field in r.json()["fields"], r.content
    assert not b.closures.exists()


def test_block_for_foreign_staff_rejected_and_boundary_366_days(frozen) -> None:  # type: ignore[no-untyped-def]
    b, h, svc, _ = shop()
    foreign = StaffFactory()
    r = block(h, b, local(TUESDAY, 10), local(TUESDAY, 12), staff_id=foreign.pk)
    assert r.status_code == 422 and "staff_id" in r.json()["fields"]
    start = local(TUESDAY, 10)
    assert block(h, b, start, start + timedelta(days=366)).status_code == 201
    assert block(h, b, start, start + timedelta(days=366, seconds=1)).status_code == 422


def test_list_blocks_filters(frozen) -> None:  # type: ignore[no-untyped-def]
    b, h, svc, (s1, _s2) = shop(staff_level=True)
    block(h, b, local(TUESDAY, 10), local(TUESDAY, 11), staff_id=s1.pk)
    block(h, b, local(TUESDAY + timedelta(days=5), 10), local(TUESDAY + timedelta(days=5), 11))
    assert len(call("get", f"{base(b)}/blocks", h).json()) == 2
    assert len(call("get", f"{base(b)}/blocks?staff_id={s1.pk}", h).json()) == 1
    after = local(TUESDAY + timedelta(days=1), 0).isoformat().replace("+", "%2B")
    assert len(call("get", f"{base(b)}/blocks?after={after}", h).json()) == 1


# ------------------------------------------------------------ conflicts


def test_block_over_existing_booking_keeps_it_and_surfaces_conflict(frozen) -> None:  # type: ignore[no-untyped-def]
    b, h, svc, _ = shop()
    a = appt(b, svc, local(TUESDAY, 12))
    assert block(h, b, local(TUESDAY, 11), local(TUESDAY, 13)).status_code == 201
    a.refresh_from_db()
    assert a.status == "confirmed"  # never cancelled automatically
    r = call("get", f"{base(b)}/conflicts", h)
    assert r.status_code == 200
    items = r.json()
    assert [i["appointment"]["id"] for i in items] == [str(a.public_id)]
    assert items[0]["reasons"] == ["blocked"] and items[0]["actions"] == ["reschedule", "cancel"]
    assert items[0]["appointment"]["customer"]["phone"]


def test_changing_hours_never_cancels_and_lists_outside_hours(frozen) -> None:  # type: ignore[no-untyped-def]
    b, h, svc, _ = shop()
    early, late = appt(b, svc, local(TUESDAY, 10)), appt(b, svc, local(TUESDAY, 18))
    week = {
        "days": [
            {"weekday": d, "shifts": [{"opens_at": "12:00", "closes_at": "17:00"}]}
            for d in range(7)
        ]
    }
    assert call("put", f"{base(b)}/hours", h, week).status_code == 200
    for a in (early, late):
        a.refresh_from_db()
        assert a.status == "confirmed"
    reasons = {
        i["appointment"]["id"]: i["reasons"] for i in call("get", f"{base(b)}/conflicts", h).json()
    }
    assert reasons == {
        str(early.public_id): ["outside_hours"],
        str(late.public_id): ["outside_hours"],
    }


def test_conflicts_ignore_past_inactive_and_straddling_shift_end(frozen) -> None:  # type: ignore[no-untyped-def]
    b, h, svc, _ = shop()
    appt(b, svc, local(MONDAY, 9) - timedelta(days=7))  # past
    appt(b, svc, local(TUESDAY, 12), status="cancelled_by_customer")
    appt(b, svc, local(TUESDAY, 11))  # fine
    straddle = appt(b, svc, local(TUESDAY, 19, 30))  # ends 20:30 > closes 20:00
    items = call("get", f"{base(b)}/conflicts", h).json()
    assert [i["appointment"]["id"] for i in items] == [str(straddle.public_id)]


def test_conflicts_use_staff_hours_and_staff_blocks(frozen) -> None:  # type: ignore[no-untyped-def]
    b, h, svc, (s1, s2) = shop(staff_level=True)
    OpeningHours.objects.create(
        business=b, staff=s1, weekday=1, opens_at=time(14), closes_at=time(18)
    )
    a1, a2 = appt(b, svc, local(TUESDAY, 11), s1), appt(b, svc, local(TUESDAY, 11), s2)
    ClosureFactory(business=b, staff=s2, starts_at=local(TUESDAY, 10), ends_at=local(TUESDAY, 12))
    got = {
        i["appointment"]["id"]: i["reasons"] for i in call("get", f"{base(b)}/conflicts", h).json()
    }
    assert got == {str(a1.public_id): ["outside_hours"], str(a2.public_id): ["blocked"]}


def test_conflicts_query_count_is_constant(frozen, django_assert_max_num_queries) -> None:  # type: ignore[no-untyped-def]
    b, h, svc, _ = shop()
    for hour in range(10, 19):
        appt(b, svc, local(TUESDAY, hour))
    ClosureFactory(business=b, starts_at=local(TUESDAY, 10), ends_at=local(TUESDAY, 20))
    with django_assert_max_num_queries(14):
        r = call("get", f"{base(b)}/conflicts", h)
    assert r.status_code == 200 and len(r.json()) == 9


# ------------------------------------------------------------ manual bookings


def test_manual_booking_success_body_db_events(frozen, run_commit, captured_events) -> None:  # type: ignore[no-untyped-def]
    b, h, svc, _ = shop()
    with run_commit():
        r = manual(h, b, svc, local(TUESDAY, 12), customer_phone="+92 300 1234567")
    assert r.status_code == 201, r.content
    data = r.json()
    assert data["status"] == "confirmed" and data["source"] == "manual"
    assert data["total_price"] == 2000 and data["payment_method"] == "pay_at_venue"
    assert data["customer"] == {"user_id": None, "name": "Walk In", "phone": "+923001234567"}
    assert data["start"].startswith("2026-11-03T12:00:00+05:00") and data["end"].startswith(
        "2026-11-03T13:00:00+05:00"
    )
    a = Appointment.objects.get(public_id=data["id"])
    assert (
        a.customer_id is None and a.walk_in_name == "Walk In" and a.walk_in_phone == "+923001234567"
    )
    assert a.scheduled_start == local(TUESDAY, 12) and a.items.get().price_at_booking == 2000
    assert AppointmentEvent.objects.get(appointment=a).note == "manual booking"
    assert [e[0] for e in captured_events] == ["booking_created", "booking_confirmed"]
    assert captured_events[0][2]["source"] == "manual"
    assert not Notification.objects.exists()  # no account to tell, owner needs no alert
    assert not any("T12:00" in s for s in public_starts(b, svc))  # the slot is gone publicly


def test_manual_booking_links_account_by_verified_phone_and_notifies(frozen, run_commit) -> None:  # type: ignore[no-untyped-def]
    b, h, svc, _ = shop()
    user = UserFactory(phone="+923001234567", is_phone_verified=True)
    from apps.accounts.factories import CustomerProfileFactory

    CustomerProfileFactory(user=user, name="Real Customer")
    with run_commit():
        r = manual(h, b, svc, local(TUESDAY, 12))
    assert r.status_code == 201
    a = Appointment.objects.get()
    assert a.customer_id == user.pk
    assert r.json()["customer"]["user_id"] == str(user.public_id)
    assert r.json()["customer"]["name"] == "Real Customer"
    assert Notification.objects.filter(user=user, template_key="booking_confirmed").exists()
    assert not Notification.objects.filter(template_key="provider_booking_created").exists()


def test_manual_booking_does_not_link_unverified_phone(frozen) -> None:  # type: ignore[no-untyped-def]
    b, h, svc, _ = shop()
    UserFactory(phone="+923001234567", is_phone_verified=False)
    assert manual(h, b, svc, local(TUESDAY, 12)).status_code == 201
    assert Appointment.objects.get().customer_id is None


def test_manual_booking_overlap_is_409_and_nothing_created(frozen) -> None:  # type: ignore[no-untyped-def]
    b, h, svc, _ = shop()
    assert manual(h, b, svc, local(TUESDAY, 12)).status_code == 201
    r = manual(h, b, svc, local(TUESDAY, 12, 30))
    assert r.status_code == 409 and r.json()["code"] == "slot_unavailable"
    assert Appointment.objects.count() == 1
    assert manual(h, b, svc, local(TUESDAY, 13)).status_code == 201  # adjacent is fine


def test_manual_booking_respects_capacity_mode(frozen) -> None:  # type: ignore[no-untyped-def]
    b, h, svc, _ = shop(parallel_capacity=2)
    appt(b, svc, local(TUESDAY, 12))  # an online booking uses one seat
    assert manual(h, b, svc, local(TUESDAY, 12)).status_code == 201
    r = manual(h, b, svc, local(TUESDAY, 12))
    assert r.status_code == 409 and r.json()["code"] == "slot_unavailable"
    assert Appointment.objects.filter(source="manual").count() == 1


def test_manual_booking_staff_mode_requires_capable_staff(frozen) -> None:  # type: ignore[no-untyped-def]
    b, h, svc, (s1, s2) = shop(staff_level=True)
    missing = manual(h, b, svc, local(TUESDAY, 12))
    assert missing.status_code == 422 and "staff_id" in missing.json()["fields"]
    other = ServiceFactory(business=b)
    r = manual(h, b, other, local(TUESDAY, 12), staff_id=s1.pk)
    assert r.status_code == 422 and r.json()["code"] == "staff_cannot_perform"
    ok = manual(h, b, svc, local(TUESDAY, 12), staff_id=s1.pk)
    assert ok.status_code == 201 and ok.json()["staff"]["id"] == s1.pk
    same = manual(h, b, svc, local(TUESDAY, 12), staff_id=s1.pk)
    assert same.status_code == 409
    assert manual(h, b, svc, local(TUESDAY, 12), staff_id=s2.pk).status_code == 201
    foreign = StaffFactory()
    assert manual(h, b, svc, local(TUESDAY, 14), staff_id=foreign.pk).status_code == 422


def test_manual_booking_hours_blocks_and_off_grid(frozen) -> None:  # type: ignore[no-untyped-def]
    b, h, svc, _ = shop()
    early = manual(h, b, svc, local(TUESDAY, 9))
    assert early.status_code == 422 and early.json()["code"] == "outside_working_hours"
    over = manual(h, b, svc, local(TUESDAY, 19, 30))  # ends 20:30 > close
    assert over.status_code == 422
    ClosureFactory(business=b, starts_at=local(TUESDAY, 15), ends_at=local(TUESDAY, 16))
    blocked = manual(h, b, svc, local(TUESDAY, 15, 30))
    assert blocked.status_code == 409 and blocked.json()["code"] == "time_blocked"
    off_grid = manual(h, b, svc, local(TUESDAY, 10, 7))  # grid is 30 min, manual allows any minute
    assert off_grid.status_code == 201


def test_manual_booking_accepts_utc_instants_in_business_timezone(frozen) -> None:  # type: ignore[no-untyped-def]
    b, h, svc, _ = shop()
    r = call(
        "post",
        f"{base(b)}/manual-bookings",
        h,
        {
            "customer_name": "A",
            "customer_phone": "03001234567",
            "service_ids": [svc.pk],
            "start": "2026-11-03T07:00:00Z",
        },
    )
    assert r.status_code == 201 and r.json()["start"].startswith("2026-11-03T12:00:00+05:00")


def test_manual_booking_time_window_limits(frozen) -> None:  # type: ignore[no-untyped-def]
    b, h, svc, _ = shop(max_advance_days=3)
    assert manual(h, b, svc, NOW - timedelta(hours=13)).status_code == 422
    assert manual(h, b, svc, local(TUESDAY + timedelta(days=10), 12)).status_code == 422
    assert manual(h, b, svc, local(TUESDAY, 12)).status_code == 201


@pytest.mark.parametrize(
    "bad,field",
    [
        ({"customer_name": ""}, "customer_name"),
        ({"customer_name": "n" * 101}, "customer_name"),
        ({"customer_phone": "abc"}, "customer_phone"),
        ({"customer_phone": ""}, "customer_phone"),
        ({"service_ids": []}, "service_ids"),
        ({"service_ids": list(range(1, 12))}, "service_ids"),
        ({"start": "2026-11-03T12:00:00"}, "start"),
        ({"start": "nope"}, "start"),
    ],
)
def test_manual_booking_validation(bad: dict[str, Any], field: str, frozen) -> None:  # type: ignore[no-untyped-def]
    b, h, svc, _ = shop()
    r = manual(h, b, svc, local(TUESDAY, 12), **bad)
    assert r.status_code == 422 and field in r.json()["fields"], r.content
    assert not Appointment.objects.exists()


def test_manual_booking_rejects_inactive_or_foreign_service(frozen) -> None:  # type: ignore[no-untyped-def]
    b, h, svc, _ = shop()
    inactive = ServiceFactory(business=b, is_active=False)
    foreign = ServiceFactory()
    for bad in (inactive, foreign):
        r = manual(h, b, bad, local(TUESDAY, 12))
        assert r.status_code == 422 and r.json()["code"] == "invalid_services"


@pytest.mark.django_db(transaction=True)
def test_parallel_manual_bookings_same_slot_one_wins(frozen) -> None:  # type: ignore[no-untyped-def]
    b, h, svc, (s1, _s2) = shop(staff_level=True)
    barrier = threading.Barrier(4)
    codes: list[int] = []

    def worker() -> None:
        try:
            barrier.wait()
            codes.append(manual(h, b, svc, local(TUESDAY, 12), staff_id=s1.pk).status_code)
        finally:
            connection.close()

    threads = [threading.Thread(target=worker) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(codes) == [201, 409, 409, 409]
    assert Appointment.objects.filter(staff=s1).count() == 1


# ------------------------------------------------------------ schedule


def sched(h: dict[str, str], b: Business, frm: str, to: str, extra: str = "") -> Any:
    return call("get", f"{base(b)}/schedule?from={frm}&to={to}{extra}", h)


def test_schedule_returns_hours_blocks_appointments_and_holds(frozen) -> None:  # type: ignore[no-untyped-def]
    from apps.bookings.tests.api_helpers import customer_session, place_hold

    b, h, svc, _ = shop()
    a = appt(b, svc, local(TUESDAY, 12))
    appt(b, svc, local(TUESDAY + timedelta(days=30), 12))  # outside the range
    appt(b, svc, local(TUESDAY, 15), status="cancelled_by_customer")
    ClosureFactory(
        business=b, starts_at=local(TUESDAY, 17), ends_at=local(TUESDAY, 18), kind="block"
    )
    _, ch = customer_session()
    assert place_hold(ch, b, [svc], 14).status_code in (200, 201)
    r = sched(h, b, "2026-11-02", "2026-11-04")
    assert r.status_code == 200, r.content
    data = r.json()
    assert data["timezone"] == "Asia/Karachi" and data["from_date"] == "2026-11-02"
    assert len(data["hours"]) == 7 and data["hours"][0]["shifts"] == [
        {"opens_at": "10:00", "closes_at": "20:00"}
    ]
    assert [x["id"] for x in data["appointments"]] == [
        str(a.public_id),
        data["appointments"][1]["id"],
    ]
    assert {x["status"] for x in data["appointments"]} == {"confirmed", "cancelled_by_customer"}
    assert len(data["blocks"]) == 1 and data["blocks"][0]["kind"] == "block"
    assert data["holds_count"] == 1 and data["staff_hours"] == {}


def test_schedule_range_limits(frozen) -> None:  # type: ignore[no-untyped-def]
    b, h, svc, _ = shop()
    assert sched(h, b, "2026-11-01", "2026-12-31").status_code == 200  # exactly 61 days
    assert sched(h, b, "2026-11-01", "2027-01-01").status_code == 200  # exactly 62 days
    r = sched(h, b, "2026-11-01", "2027-01-02")  # 63 days
    assert r.status_code == 422 and "to" in r.json()["fields"]
    assert sched(h, b, "2026-11-05", "2026-11-04").status_code == 422
    assert sched(h, b, "nope", "2026-11-04").status_code == 422
    assert call("get", f"{base(b)}/schedule", h).status_code == 422


def test_schedule_staff_filter_and_staff_hours(frozen) -> None:  # type: ignore[no-untyped-def]
    b, h, svc, (s1, s2) = shop(staff_level=True)
    OpeningHours.objects.create(
        business=b, staff=s1, weekday=1, opens_at=time(13), closes_at=time(17)
    )
    a1, a2 = appt(b, svc, local(TUESDAY, 14), s1), appt(b, svc, local(TUESDAY, 14), s2)
    ClosureFactory(business=b, staff=s2, starts_at=local(TUESDAY, 10), ends_at=local(TUESDAY, 11))
    allr = sched(h, b, "2026-11-03", "2026-11-03").json()
    assert {x["id"] for x in allr["appointments"]} == {str(a1.public_id), str(a2.public_id)}
    assert set(allr["staff_hours"]) == {str(s1.pk)}
    one = sched(h, b, "2026-11-03", "2026-11-03", f"&staff_id={s1.pk}").json()
    assert [x["id"] for x in one["appointments"]] == [str(a1.public_id)] and one["blocks"] == []
    two = sched(h, b, "2026-11-03", "2026-11-03", f"&staff_id={s2.pk}").json()
    assert len(two["blocks"]) == 1
    assert (
        sched(h, b, "2026-11-03", "2026-11-03", f"&staff_id={StaffFactory().pk}").status_code == 404
    )


def test_schedule_midnight_boundaries_in_business_timezone(frozen) -> None:  # type: ignore[no-untyped-def]
    b, h, svc, _ = shop()
    late = appt(b, svc, local(TUESDAY, 23, 30))  # 23:30 PKT = 18:30 UTC, still Tuesday locally
    just_after = appt(b, svc, local(TUESDAY + timedelta(days=1), 0, 0))
    tue = {x["id"] for x in sched(h, b, "2026-11-03", "2026-11-03").json()["appointments"]}
    wed = {x["id"] for x in sched(h, b, "2026-11-04", "2026-11-04").json()["appointments"]}
    # a booking that crosses midnight is shown on both days it overlaps
    assert tue == {str(late.public_id)}
    assert wed == {str(late.public_id), str(just_after.public_id)}


def test_schedule_masks_phone_of_inactive_bookings(frozen) -> None:  # type: ignore[no-untyped-def]
    b, h, svc, _ = shop()
    appt(b, svc, local(TUESDAY, 12))
    appt(b, svc, local(TUESDAY, 14), status="completed")
    rows = {x["status"]: x for x in sched(h, b, "2026-11-03", "2026-11-03").json()["appointments"]}
    assert rows["confirmed"]["customer"]["phone"] and rows["completed"]["customer"]["phone"] is None


def test_schedule_query_count_bounded(frozen, django_assert_max_num_queries) -> None:  # type: ignore[no-untyped-def]
    b, h, svc, _ = shop()
    for hour in range(10, 19):
        appt(b, svc, local(TUESDAY, hour))
    with django_assert_max_num_queries(16):
        assert sched(h, b, "2026-11-03", "2026-11-03").status_code == 200


# ------------------------------------------------------------ hypothesis: edits never leak slots

edits = st.lists(
    st.one_of(
        st.tuples(
            st.just("hours"),
            st.integers(0, 6),
            st.lists(st.tuples(st.integers(0, 23), st.integers(1, 6)), max_size=2),
        ),
        st.tuples(st.just("block"), st.integers(0, 23 * 60), st.integers(15, 600)),
    ),
    max_size=6,
)


@settings(
    max_examples=30, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(edits=edits)
def test_availability_never_returns_slot_in_block_or_outside_hours(edits: list[Any]) -> None:
    b = BusinessFactory(lead_time_minutes=0, slot_interval_minutes=30)
    open_all_week(b)
    svc = ServiceFactory(business=b, duration_minutes=30)
    for edit in edits:
        if edit[0] == "hours":
            _, weekday, spans = edit
            shifts: list[tuple[time, time]] = []
            cursor = 0
            for start_h, length in spans:
                s = max(start_h, cursor)
                e = min(s + length, 24)
                if e > s and e <= 23:
                    shifts.append((time(s), time(e)))
                    cursor = e + 1
            week = [
                hours.DayShifts(d, shifts if d == weekday else [(time(10), time(20))])
                for d in range(7)
            ]
            hours.replace_week(b.pk, None, week)
        else:
            _, start_min, length = edit
            start = local(TUESDAY, 0) + timedelta(minutes=start_min)
            Closure.objects.create(
                business=b, starts_at=start, ends_at=start + timedelta(minutes=length)
            )
    cfg = selectors.get_booking_config_by_id(b.pk)
    assert cfg is not None
    slots = availability.get_available_slots(cfg, TUESDAY, [svc.pk], now=NOW)
    shifts_tue = [
        (o, c) for o, c in selectors.opening_windows(b.pk, TUESDAY.weekday()).get(None, [])
    ]
    blocks = [(c.starts_at, c.ends_at) for c in Closure.objects.filter(business=b)]
    for s in slots:
        assert any(
            local(TUESDAY, o.hour, o.minute) <= s.start
            and s.end <= local(TUESDAY, c.hour, c.minute)
            for o, c in shifts_tue
        )
        assert not any(s.start < be and bs < s.end for bs, be in blocks)
