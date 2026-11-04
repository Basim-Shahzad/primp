import threading
from datetime import timedelta
from typing import Any

import pytest
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import Client

from apps.accounts.factories import make_customer
from apps.bookings import availability
from apps.bookings.factories import AppointmentFactory
from apps.bookings.models import Appointment, AppointmentEvent, AppointmentService
from apps.bookings.tests.helpers import NOW, TUESDAY, local
from apps.businesses import selectors
from apps.businesses.factories import (
    BusinessFactory,
    ServiceFactory,
    StaffFactory,
    StaffServiceFactory,
    open_all_week,
)
from apps.businesses.models import Business, OpeningHours, Service, Staff, StaffService
from apps.businesses.tests.provider_helpers import (
    P,
    call,
    customer_headers,
    owner_headers,
    png_bytes,
)
from apps.notifications.models import Notification

pytestmark = pytest.mark.django_db


def salon(staff_count: int = 2, **kw: Any) -> tuple[Business, dict[str, str], Service, list[Staff]]:
    owner, h = owner_headers()
    kw.setdefault("lead_time_minutes", 0)
    b = BusinessFactory(owner=owner, staff_level_booking=True, **kw)
    open_all_week(b)
    svc = ServiceFactory(business=b, duration_minutes=60, price=2000)
    staff = [StaffFactory(business=b) for _ in range(staff_count)]
    for s in staff:
        StaffServiceFactory(staff=s, service=svc)
    return b, h, svc, staff


def appt(
    b: Business,
    staff: Staff | None,
    svc: Service,
    hour: int = 12,
    status: str = "confirmed",
    day=TUESDAY,
) -> Appointment:
    start = local(day, hour)
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


def res(a: Appointment, action: str = "cancel", new_staff: Staff | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"appointment_id": str(a.public_id), "action": action}
    if new_staff is not None:
        out["new_staff_id"] = new_staff.pk
    return out


# ------------------------------------------------------------ CRUD & access


def test_staff_endpoints_auth_role_and_ownership_matrix(frozen) -> None:  # type: ignore[no-untyped-def]
    b, h, svc, (s1, _s2) = salon()
    _, other = owner_headers()
    cust = customer_headers()
    calls: list[tuple[str, str, Any]] = [
        ("get", f"{P}/businesses/{b.public_id}/staff", None),
        ("post", f"{P}/businesses/{b.public_id}/staff", {"name": "New"}),
        ("patch", f"{P}/staff/{s1.pk}", {"name": "X"}),
        ("get", f"{P}/staff/{s1.pk}/hours", None),
        ("put", f"{P}/staff/{s1.pk}/hours", {"days": []}),
        ("put", f"{P}/staff/{s1.pk}/services", {"service_ids": [svc.pk]}),
        ("get", f"{P}/staff/{s1.pk}/impact?action=deactivate", None),
        ("post", f"{P}/staff/{s1.pk}/deactivate", {"resolutions": []}),
    ]
    for method, path, body in calls:
        assert call(method, path, {}, body).status_code == 401, path
        assert call(method, path, {"HTTP_AUTHORIZATION": "Bearer x"}, body).status_code == 401, path
        assert call(method, path, cust, body).status_code == 403, path
        assert call(method, path, other, body).status_code == 404, path
    r = Client().post(
        f"{P}/staff/{s1.pk}/photo",
        {"file": SimpleUploadedFile("a.png", png_bytes())},
        **other,
    )
    assert r.status_code == 404
    s1.refresh_from_db()
    assert s1.is_active and s1.name != "X"


def test_create_list_patch_staff(frozen) -> None:  # type: ignore[no-untyped-def]
    b, h, svc, _ = salon(staff_count=0)
    r = call(
        "post",
        f"{P}/businesses/{b.public_id}/staff",
        h,
        {"name": "Ayesha", "bio": "Colourist", "service_ids": [svc.pk]},
    )
    assert r.status_code == 201, r.content
    sid = r.json()["id"]
    assert r.json()["service_ids"] == [svc.pk] and r.json()["has_hours_override"] is False
    assert StaffService.objects.filter(staff_id=sid, service=svc).exists()
    assert [s["name"] for s in call("get", f"{P}/businesses/{b.public_id}/staff", h).json()] == [
        "Ayesha"
    ]
    p = call("patch", f"{P}/staff/{sid}", h, {"name": "Ayesha K", "bio": ""})
    assert p.status_code == 200 and p.json()["name"] == "Ayesha K" and p.json()["bio"] == ""


@pytest.mark.parametrize(
    "bad,field",
    [
        ({"name": ""}, "name"),
        ({"name": "n" * 151}, "name"),
        ({"bio": "b" * 1001}, "bio"),
        ({"service_ids": [0]}, "service_ids"),
    ],
)
def test_staff_validation(bad: dict[str, Any], field: str) -> None:
    b, h, svc, _ = salon(staff_count=0)
    r = call("post", f"{P}/businesses/{b.public_id}/staff", h, {"name": "OK", **bad})
    assert r.status_code == 422 and field in r.json()["fields"]
    assert not b.staff.exists()


def test_staff_services_must_belong_to_same_business() -> None:
    b, h, svc, _ = salon(staff_count=0)
    foreign = ServiceFactory()
    r = call(
        "post", f"{P}/businesses/{b.public_id}/staff", h, {"name": "A", "service_ids": [foreign.pk]}
    )
    assert r.status_code == 422 and "service_ids" in r.json()["fields"]
    assert not StaffService.objects.filter(service=foreign).exists()


def test_patch_cannot_deactivate_but_can_reactivate() -> None:
    b, h, svc, (s1, _) = salon()
    r = call("patch", f"{P}/staff/{s1.pk}", h, {"is_active": False})
    assert r.status_code == 409 and r.json()["code"] == "use_deactivate"
    Staff.objects.filter(pk=s1.pk).update(is_active=False)
    assert call("patch", f"{P}/staff/{s1.pk}", h, {"is_active": True}).json()["is_active"] is True
    assert call("patch", f"{P}/staff/{s1.pk}", h, {"name": None}).status_code == 422


def test_staff_photo_resized_and_old_deleted(run_commit) -> None:  # type: ignore[no-untyped-def]
    b, h, svc, (s1, _) = salon()
    for colour in (1, 2):
        with run_commit():
            r = Client().post(
                f"{P}/staff/{s1.pk}/photo",
                {"file": SimpleUploadedFile("a.png", png_bytes((900, 600)))},
                **h,
            )
        assert r.status_code == 200 and r.json()["photo_url"]
        if colour == 1:
            s1.refresh_from_db()
            first = s1.photo
    s1.refresh_from_db()
    assert (
        s1.photo != first and not default_storage.exists(first) and default_storage.exists(s1.photo)
    )
    bad = Client().post(
        f"{P}/staff/{s1.pk}/photo", {"file": SimpleUploadedFile("a.png", b"junk")}, **h
    )
    assert bad.status_code == 422


def test_staff_hours_override_and_clear(frozen) -> None:  # type: ignore[no-untyped-def]
    b, h, svc, (s1, _) = salon()
    week = {"days": [{"weekday": 1, "shifts": [{"opens_at": "13:00", "closes_at": "17:00"}]}]}
    r = call("put", f"{P}/staff/{s1.pk}/hours", h, week)
    assert r.status_code == 200 and r.json()["days"][1]["shifts"][0]["opens_at"] == "13:00:00"
    assert call("get", f"{P}/staff/{s1.pk}/hours", h).json() == r.json()
    assert (
        call("get", f"{P}/businesses/{b.public_id}/staff", h).json()[0]["has_hours_override"]
        is True
    )
    cfg = selectors.get_booking_config_by_id(b.pk)
    assert cfg is not None
    slots = availability.get_available_slots(cfg, TUESDAY, [svc.pk], s1.pk, now=NOW)
    assert min(s.start for s in slots) == local(TUESDAY, 13)  # override replaces business hours
    assert call("put", f"{P}/staff/{s1.pk}/hours", h, {"days": []}).status_code == 200
    assert not OpeningHours.objects.filter(staff=s1).exists()
    slots = availability.get_available_slots(cfg, TUESDAY, [svc.pk], s1.pk, now=NOW)
    assert min(s.start for s in slots) == local(TUESDAY, 10)  # back to business hours


def test_staff_hours_validation_keeps_override() -> None:
    b, h, svc, (s1, _) = salon()
    OpeningHours.objects.create(
        business=b, staff=s1, weekday=0, opens_at="09:00", closes_at="12:00"
    )
    bad = {"days": [{"weekday": 0, "shifts": [{"opens_at": "12:00", "closes_at": "09:00"}]}]}
    assert call("put", f"{P}/staff/{s1.pk}/hours", h, bad).status_code == 422
    assert OpeningHours.objects.filter(staff=s1).count() == 1


# ------------------------------------------------------------ impact


def test_impact_lists_only_future_active_for_that_staff(frozen) -> None:  # type: ignore[no-untyped-def]
    b, h, svc, (s1, s2) = salon()
    future = appt(b, s1, svc, 12)
    pending = appt(b, s1, svc, 14, status="pending")
    appt(b, s1, svc, 16, status="completed")
    appt(b, s1, svc, 17, status="cancelled_by_customer")
    appt(b, s2, svc, 12)  # other staff
    past = appt(b, s1, svc, 12, day=TUESDAY - timedelta(days=2))
    assert past.scheduled_start < NOW
    r = call("get", f"{P}/staff/{s1.pk}/impact?action=deactivate", h)
    assert r.status_code == 200, r.content
    ids = [a["appointment_id"] for a in r.json()["appointments"]]
    assert ids == [str(future.public_id), str(pending.public_id)] and r.json()["count"] == 2
    assert r.json()["appointments"][0]["customer_name"] == "Test Customer"


def test_impact_remove_services_filters_by_service(frozen) -> None:  # type: ignore[no-untyped-def]
    b, h, svc, (s1, _) = salon()
    other = ServiceFactory(business=b)
    StaffServiceFactory(staff=s1, service=other)
    a1, a2 = appt(b, s1, svc, 12), appt(b, s1, other, 14)
    r = call("get", f"{P}/staff/{s1.pk}/impact?action=remove_services&service_ids={other.pk}", h)
    assert [a["appointment_id"] for a in r.json()["appointments"]] == [str(a2.public_id)]
    assert call("get", f"{P}/staff/{s1.pk}/impact?action=remove_services", h).status_code == 422
    assert (
        call("get", f"{P}/staff/{s1.pk}/impact?action=remove_services&service_ids=x", h).status_code
        == 422
    )
    assert call("get", f"{P}/staff/{s1.pk}/impact?action=other", h).status_code == 422
    assert a1.pk


# ------------------------------------------------------------ deactivate


def deactivate(h: dict[str, str], s: Staff, resolutions: list[dict[str, Any]]) -> Any:
    return call("post", f"{P}/staff/{s.pk}/deactivate", h, {"resolutions": resolutions})


def test_deactivate_without_affected_bookings_just_deactivates(frozen, run_commit) -> None:  # type: ignore[no-untyped-def]
    b, h, svc, (s1, s2) = salon()
    with run_commit():
        r = deactivate(h, s1, [])
    assert r.status_code == 200 and r.json() == {"reassigned": 0, "cancelled": 0}
    s1.refresh_from_db()
    assert s1.is_active is False
    again = deactivate(h, s1, [])
    assert again.status_code == 409 and again.json()["code"] == "already_inactive"


def test_deactivate_requires_every_affected_booking_resolved(frozen) -> None:  # type: ignore[no-untyped-def]
    b, h, svc, (s1, s2) = salon()
    a1, a2, a3 = appt(b, s1, svc, 11), appt(b, s1, svc, 13), appt(b, s1, svc, 15)
    r = deactivate(h, s1, [res(a1)])
    assert r.status_code == 422 and r.json()["code"] == "unresolved_appointments"
    assert sorted(r.json()["fields"]["resolutions"]) == sorted(
        [str(a2.public_id), str(a3.public_id)]
    )
    s1.refresh_from_db()
    a1.refresh_from_db()
    assert s1.is_active and a1.status == "confirmed"


def test_deactivate_rejects_unknown_duplicate_and_foreign_resolutions(frozen) -> None:  # type: ignore[no-untyped-def]
    b, h, svc, (s1, s2) = salon()
    a1 = appt(b, s1, svc, 12)
    foreign = appt(*salon(1)[0:1], None, ServiceFactory(), 12)  # someone else's appointment
    for bad in ([res(a1), res(a1)], [res(a1), res(foreign)]):
        r = deactivate(h, s1, bad)
        assert r.status_code == 422 and "resolutions" in r.json()["fields"]
    r = deactivate(h, s1, [{"appointment_id": str(a1.public_id), "action": "reassign"}])
    assert r.status_code == 422
    assert Appointment.objects.get(pk=foreign.pk).status == "confirmed"
    assert (
        call(
            "post",
            f"{P}/staff/{s1.pk}/deactivate",
            h,
            {"resolutions": [{"appointment_id": "x", "action": "cancel"}]},
        ).status_code
        == 422
    )


def test_reassign_to_staff_lacking_service_422(frozen) -> None:  # type: ignore[no-untyped-def]
    b, h, svc, (s1, s2) = salon()
    StaffService.objects.filter(staff=s2).delete()
    a = appt(b, s1, svc, 12)
    r = deactivate(h, s1, [res(a, "reassign", s2)])
    assert r.status_code == 422 and r.json()["code"] == "staff_cannot_perform"
    a.refresh_from_db()
    assert a.staff_id == s1.pk


def test_reassign_to_self_inactive_or_foreign_staff_422(frozen) -> None:  # type: ignore[no-untyped-def]
    b, h, svc, (s1, s2) = salon()
    a = appt(b, s1, svc, 12)
    assert deactivate(h, s1, [res(a, "reassign", s1)]).status_code == 422
    Staff.objects.filter(pk=s2.pk).update(is_active=False)
    assert deactivate(h, s1, [res(a, "reassign", s2)]).status_code == 422
    foreign = StaffFactory()
    assert deactivate(h, s1, [res(a, "reassign", foreign)]).status_code == 422
    a.refresh_from_db()
    assert a.staff_id == s1.pk


def test_reassign_into_conflicting_slot_is_409(frozen) -> None:  # type: ignore[no-untyped-def]
    b, h, svc, (s1, s2) = salon()
    a = appt(b, s1, svc, 12)
    appt(b, s2, svc, 12)  # s2 already busy then
    r = deactivate(h, s1, [res(a, "reassign", s2)])
    assert r.status_code == 409 and r.json()["code"] == "slot_unavailable"
    a.refresh_from_db()
    assert a.staff_id == s1.pk


def test_reassign_outside_staff_hours_or_into_block_422(frozen) -> None:  # type: ignore[no-untyped-def]
    b, h, svc, (s1, s2) = salon()
    OpeningHours.objects.create(
        business=b, staff=s2, weekday=1, opens_at="14:00", closes_at="18:00"
    )
    a = appt(b, s1, svc, 12)
    r = deactivate(h, s1, [res(a, "reassign", s2)])
    assert r.status_code == 422 and r.json()["code"] == "staff_unavailable"
    OpeningHours.objects.filter(staff=s2).delete()
    from apps.businesses.models import Closure

    Closure.objects.create(
        business=b, staff=s2, starts_at=local(TUESDAY, 11), ends_at=local(TUESDAY, 13)
    )
    assert deactivate(h, s1, [res(a, "reassign", s2)]).status_code == 422


def test_deactivate_mixed_resolutions_in_one_transaction(frozen, run_commit) -> None:  # type: ignore[no-untyped-def]
    b, h, svc, (s1, s2) = salon()
    a1, a2 = appt(b, s1, svc, 11), appt(b, s1, svc, 14, status="pending")
    with run_commit():
        r = deactivate(h, s1, [res(a1, "reassign", s2), res(a2, "cancel")])
    assert r.status_code == 200, r.content
    assert r.json() == {"reassigned": 1, "cancelled": 1}
    a1.refresh_from_db(), a2.refresh_from_db(), s1.refresh_from_db()
    assert a1.staff_id == s2.pk and a1.status == "confirmed"
    assert a2.status == "cancelled_by_business" and a2.cancelled_at is not None
    assert s1.is_active is False
    timeline = list(AppointmentEvent.objects.filter(appointment=a1).values_list("note", flat=True))
    assert "reassigned" in timeline
    # customers are told: reassigned template to one, cancellation to the other
    keys = {n.user_id: n.template_key for n in Notification.objects.exclude(channel="in_app")}
    assert (
        keys[a1.customer_id] == "booking_reassigned" and keys[a2.customer_id] == "booking_cancelled"
    )
    # the staff member vanishes from availability and the public listing at once
    cfg = selectors.get_booking_config_by_id(b.pk)
    assert cfg is not None
    slots = availability.get_available_slots(cfg, TUESDAY, [svc.pk], now=NOW)
    assert all(s1.pk not in s.staff_ids for s in slots)
    page = Client().get(f"/api/v1/businesses/{b.public_id}").json()
    assert [m["id"] for m in page["staff"]] == [s2.pk]


def test_failed_resolution_rolls_everything_back(frozen) -> None:  # type: ignore[no-untyped-def]
    b, h, svc, (s1, s2) = salon()
    a1, a2 = appt(b, s1, svc, 11), appt(b, s1, svc, 14)
    appt(b, s2, svc, 14)
    r = deactivate(h, s1, [res(a1, "cancel"), res(a2, "reassign", s2)])
    assert r.status_code == 409
    a1.refresh_from_db(), a2.refresh_from_db(), s1.refresh_from_db()
    assert a1.status == "confirmed" and a2.staff_id == s1.pk and s1.is_active


def test_no_notifications_when_deactivation_rolls_back(frozen, run_commit) -> None:  # type: ignore[no-untyped-def]
    b, h, svc, (s1, s2) = salon()
    a1, a2 = appt(b, s1, svc, 11), appt(b, s1, svc, 14)
    appt(b, s2, svc, 14)
    with run_commit():
        deactivate(h, s1, [res(a1, "cancel"), res(a2, "reassign", s2)])
    assert not Notification.objects.exists()


# ------------------------------------------------------------ remove services


def test_remove_service_requires_resolutions_then_applies(frozen, run_commit) -> None:  # type: ignore[no-untyped-def]
    b, h, svc, (s1, s2) = salon()
    extra = ServiceFactory(business=b, duration_minutes=60)
    StaffServiceFactory(staff=s1, service=extra)
    StaffServiceFactory(staff=s2, service=extra)
    a = appt(b, s1, extra, 12)
    body = {"service_ids": [svc.pk]}  # drops `extra`
    r = call("put", f"{P}/staff/{s1.pk}/services", h, body)
    assert r.status_code == 422 and r.json()["fields"]["resolutions"] == [str(a.public_id)]
    assert StaffService.objects.filter(staff=s1, service=extra).exists()
    with run_commit():
        ok = call(
            "put",
            f"{P}/staff/{s1.pk}/services",
            h,
            {**body, "resolutions": [res(a, "reassign", s2)]},
        )
    assert ok.status_code == 200 and ok.json() == {"reassigned": 1, "cancelled": 0}
    a.refresh_from_db()
    assert a.staff_id == s2.pk
    assert set(StaffService.objects.filter(staff=s1).values_list("service_id", flat=True)) == {
        svc.pk
    }
    s1.refresh_from_db()
    assert s1.is_active


def test_service_update_without_impact_and_validation(frozen) -> None:  # type: ignore[no-untyped-def]
    b, h, svc, (s1, s2) = salon()
    extra = ServiceFactory(business=b)
    r = call("put", f"{P}/staff/{s1.pk}/services", h, {"service_ids": [svc.pk, extra.pk]})
    assert r.status_code == 200 and StaffService.objects.filter(staff=s1).count() == 2
    assert (
        call("put", f"{P}/staff/{s1.pk}/services", h, {"service_ids": [svc.pk]}).status_code == 200
    )
    foreign = ServiceFactory()
    assert (
        call("put", f"{P}/staff/{s1.pk}/services", h, {"service_ids": [foreign.pk]}).status_code
        == 422
    )
    a = appt(b, s1, svc, 12)
    nothing = call(
        "put", f"{P}/staff/{s1.pk}/services", h, {"service_ids": [svc.pk], "resolutions": [res(a)]}
    )
    assert nothing.status_code == 422
    assert (
        call("put", f"{P}/staff/{s1.pk}/services", h, {"service_ids": [svc.pk, svc.pk]}).status_code
        == 200
    )
    assert Appointment.objects.get(pk=a.pk).status == "confirmed"


# ------------------------------------------------------------ staff-level toggle


def test_turning_staff_level_off_keeps_staff_bookings_and_uses_capacity(frozen) -> None:  # type: ignore[no-untyped-def]
    b, h, svc, (s1, s2) = salon()
    a = appt(b, s1, svc, 12)
    r = call(
        "patch",
        f"{P}/businesses/{b.public_id}/booking-settings",
        h,
        {"staff_level_booking": False, "parallel_capacity": 1},
    )
    assert r.status_code == 200
    a.refresh_from_db()
    assert a.staff_id == s1.pk and a.status == "confirmed"
    cfg = selectors.get_booking_config_by_id(b.pk)
    assert cfg is not None
    starts = [s.start for s in availability.get_available_slots(cfg, TUESDAY, [svc.pk], now=NOW)]
    assert local(TUESDAY, 12) not in starts  # the staff-specific booking still occupies capacity
    assert local(TUESDAY, 13) in starts


# ------------------------------------------------------------ concurrency


@pytest.mark.django_db(transaction=True)
def test_two_owner_reassignments_into_the_same_slot_one_wins(frozen) -> None:  # type: ignore[no-untyped-def]
    b, h, svc, (s1, s2, s3) = salon(3)
    a1, a3 = appt(b, s1, svc, 12), appt(b, s3, svc, 12)
    barrier = threading.Barrier(2)
    results: list[int] = []

    def worker(source: Staff, appointment: Appointment) -> None:
        try:
            barrier.wait()
            r = deactivate(h, source, [res(appointment, "reassign", s2)])
            results.append(r.status_code)
        finally:
            connection.close()

    threads = [
        threading.Thread(target=worker, args=(s1, a1)),
        threading.Thread(target=worker, args=(s3, a3)),
    ]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(results) == [200, 409]
    assert Appointment.objects.filter(staff=s2, status="confirmed").count() == 1


@pytest.mark.django_db(transaction=True)
def test_reassignment_into_a_slot_held_by_a_customer_conflicts(frozen) -> None:  # type: ignore[no-untyped-def]
    from apps.bookings.tests.api_helpers import book, customer_session, place_hold

    b, h, svc, (s1, s2) = salon()
    a = appt(b, s1, svc, 12)
    _, ch = customer_session()
    held = place_hold(ch, b, [svc], 12, staff_id=s2.pk)
    assert held.status_code in (200, 201), held.content
    r = deactivate(h, s1, [res(a, "reassign", s2)])
    assert r.status_code == 409 and r.json()["code"] == "slot_unavailable"
    booked = book(ch, held.json()["token"])
    assert booked.status_code == 201
    assert Appointment.objects.filter(staff=s2, status__in=["pending", "confirmed"]).count() == 1
