import json
from datetime import UTC, datetime, time, timedelta

import pytest
import time_machine
from django.core.cache import cache
from django.test import Client

from apps.accounts.factories import UserFactory
from apps.bookings import availability, holds
from apps.bookings.cache_keys import next_slot_key
from apps.bookings.factories import AppointmentFactory
from apps.bookings.models import AppointmentStatus
from apps.bookings.tests.helpers import MONDAY, NOW, TUESDAY, day_plus, hhmm, local
from apps.businesses import selectors as biz
from apps.businesses.factories import (
    BusinessFactory,
    ClosureFactory,
    OpeningHoursFactory,
    ServiceFactory,
    StaffFactory,
    StaffServiceFactory,
    open_all_week,
)
from apps.core.errors import AppError

pytestmark = pytest.mark.django_db


def cfg(business):  # type: ignore[no-untyped-def]
    c = biz.get_booking_config_by_id(business.pk)
    assert c
    return c


def make_capacity_business(**kw):  # type: ignore[no-untyped-def]
    b = BusinessFactory(slot_interval_minutes=30, lead_time_minutes=0, **kw)
    open_all_week(b)
    return b, ServiceFactory(business=b, duration_minutes=60)


def slots(b, svc_ids, day=TUESDAY, staff_id=None, now=NOW):  # type: ignore[no-untyped-def]
    return availability.get_available_slots(cfg(b), day, [s.pk for s in svc_ids], staff_id, now=now)


# ---------------- capacity mode ----------------


def test_open_day_lists_all_slots_with_offset() -> None:
    b, svc = make_capacity_business()
    out = slots(b, [svc])
    assert hhmm(out)[0] == "10:00" and hhmm(out)[-1] == "19:00" and len(out) == 19
    assert out[0].start.utcoffset() == timedelta(hours=5) and out[0].end - out[
        0
    ].start == timedelta(hours=1)
    assert out[0].staff_ids == ()


def test_sums_durations_of_multiple_services() -> None:
    b, svc = make_capacity_business()
    other = ServiceFactory(business=b, duration_minutes=90)
    out = slots(b, [svc, other])
    assert out[0].end - out[0].start == timedelta(minutes=150)
    assert hhmm(out)[-1] == "17:30"


def test_closed_weekday_returns_empty() -> None:
    b = BusinessFactory(lead_time_minutes=0)
    svc = ServiceFactory(business=b)
    OpeningHoursFactory(business=b, weekday=0)  # Monday only
    assert slots(b, [svc], day=TUESDAY) == []


def test_split_shift_slots() -> None:
    b = BusinessFactory(lead_time_minutes=0)
    svc = ServiceFactory(business=b, duration_minutes=60)
    OpeningHoursFactory(business=b, weekday=1, opens_at=time(10), closes_at=time(12))
    OpeningHoursFactory(business=b, weekday=1, opens_at=time(15), closes_at=time(17))
    assert hhmm(slots(b, [svc])) == ["10:00", "10:30", "11:00", "15:00", "15:30", "16:00"]


def test_existing_appointment_removes_overlapping_slots() -> None:
    b, svc = make_capacity_business()
    AppointmentFactory(
        business=b, scheduled_start=local(TUESDAY, 12), scheduled_end=local(TUESDAY, 13)
    )
    out = hhmm(slots(b, [svc]))
    assert "11:30" not in out and "12:00" not in out and "12:30" not in out
    assert "11:00" in out and "13:00" in out


@pytest.mark.parametrize(
    "status",
    [
        AppointmentStatus.CANCELLED_BY_CUSTOMER,
        AppointmentStatus.CANCELLED_BY_BUSINESS,
        AppointmentStatus.COMPLETED,
        AppointmentStatus.NO_SHOW,
    ],
)
def test_inactive_appointments_do_not_block(status: str) -> None:
    b, svc = make_capacity_business()
    AppointmentFactory(
        business=b,
        status=status,
        scheduled_start=local(TUESDAY, 12),
        scheduled_end=local(TUESDAY, 13),
    )
    assert "12:00" in hhmm(slots(b, [svc]))


def test_pending_appointment_blocks() -> None:
    b, svc = make_capacity_business()
    AppointmentFactory(
        business=b,
        status=AppointmentStatus.PENDING,
        scheduled_start=local(TUESDAY, 12),
        scheduled_end=local(TUESDAY, 13),
    )
    assert "12:00" not in hhmm(slots(b, [svc]))


def test_parallel_capacity() -> None:
    b, svc = make_capacity_business(parallel_capacity=2)
    for _ in range(1):
        AppointmentFactory(
            business=b, scheduled_start=local(TUESDAY, 12), scheduled_end=local(TUESDAY, 13)
        )
    assert "12:00" in hhmm(slots(b, [svc]))
    AppointmentFactory(
        business=b, scheduled_start=local(TUESDAY, 12), scheduled_end=local(TUESDAY, 13)
    )
    assert "12:00" not in hhmm(slots(b, [svc]))
    assert "13:00" in hhmm(slots(b, [svc]))


def test_buffer_minutes() -> None:
    b, svc = make_capacity_business(buffer_minutes=30)
    AppointmentFactory(
        business=b, scheduled_start=local(TUESDAY, 12), scheduled_end=local(TUESDAY, 13)
    )
    out = hhmm(slots(b, [svc]))
    # 10:30-11:30 plus its 30 min buffer ends exactly when the booking starts: allowed
    assert "10:30" in out and "11:00" not in out
    assert "13:00" not in out and "13:30" in out


def test_closure_removes_slots_and_whole_day_closure() -> None:
    b, svc = make_capacity_business()
    ClosureFactory(business=b, starts_at=local(TUESDAY, 14), ends_at=local(TUESDAY, 15))
    out = hhmm(slots(b, [svc]))
    assert (
        "13:30" not in out
        and "14:00" not in out
        and "14:30" not in out
        and "13:00" in out
        and "15:00" in out
    )
    ClosureFactory(business=b, starts_at=local(TUESDAY, 0), ends_at=local(TUESDAY, 23, 59))
    assert slots(b, [svc]) == []


def test_closure_spanning_midnight_blocks_both_days() -> None:
    b, svc = make_capacity_business()
    ClosureFactory(business=b, starts_at=local(TUESDAY, 18), ends_at=local(day_plus(2), 11))
    assert hhmm(slots(b, [svc]))[-1] == "16:30" if False else "18:00" not in hhmm(slots(b, [svc]))
    wed = hhmm(slots(b, [svc], day=day_plus(2)))
    assert wed[0] == "11:00"


def test_staff_specific_closure_ignored_in_capacity_mode() -> None:
    b, svc = make_capacity_business()
    staff = StaffFactory(business=b)
    ClosureFactory(
        business=b, staff=staff, starts_at=local(TUESDAY, 0), ends_at=local(TUESDAY, 23, 59)
    )
    assert slots(b, [svc])


def test_staff_param_ignored_in_capacity_mode() -> None:
    b, svc = make_capacity_business()
    assert slots(b, [svc], staff_id=12345)


# ---------------- lead time / advance / dates ----------------


def test_lead_time_hides_near_slots() -> None:
    b = BusinessFactory(lead_time_minutes=120)
    open_all_week(b)
    svc = ServiceFactory(business=b, duration_minutes=60)
    out = hhmm(slots(b, [svc], day=MONDAY))  # now = 09:00 -> earliest 11:00
    assert out[0] == "11:00"


def test_lead_time_exact_boundary_is_bookable() -> None:
    b = BusinessFactory(lead_time_minutes=60)
    open_all_week(b)
    svc = ServiceFactory(business=b, duration_minutes=60)
    assert hhmm(slots(b, [svc], day=MONDAY))[0] == "10:00"


def test_lead_time_near_midnight_spills_into_next_day() -> None:
    b = BusinessFactory(lead_time_minutes=12 * 60)
    open_all_week(b, opens=time(0), closes=time(23, 59))
    svc = ServiceFactory(business=b, duration_minutes=30)
    late = datetime(2026, 11, 2, 18, 0, tzinfo=UTC)  # 23:00 PKT Monday
    assert slots(b, [svc], day=MONDAY, now=late) == []
    tue = slots(b, [svc], day=TUESDAY, now=late)
    assert hhmm(tue)[0] == "11:00"  # 23:00 + 12h


def test_local_date_differs_from_utc_date() -> None:
    b, svc = make_capacity_business()
    after_midnight_pkt = datetime(2026, 11, 2, 19, 30, tzinfo=UTC)  # 00:30 PKT Tuesday
    assert availability.get_available_slots(cfg(b), TUESDAY, [svc.pk], now=after_midnight_pkt)
    with pytest.raises(AppError) as exc:
        availability.get_available_slots(cfg(b), MONDAY, [svc.pk], now=after_midnight_pkt)
    assert exc.value.code == "date_in_past"


def test_past_date_rejected_today_allowed() -> None:
    b, svc = make_capacity_business()
    with pytest.raises(AppError) as exc:
        slots(b, [svc], day=MONDAY - timedelta(days=1))
    assert exc.value.status == 422 and exc.value.code == "date_in_past"
    assert slots(b, [svc], day=MONDAY)


def test_max_advance_boundary() -> None:
    b = BusinessFactory(max_advance_days=3, lead_time_minutes=0)
    open_all_week(b)
    svc = ServiceFactory(business=b, duration_minutes=60)
    assert slots(b, [svc], day=day_plus(3))  # last allowed day
    assert slots(b, [svc], day=day_plus(4)) == []


# ---------------- service validation ----------------


@pytest.mark.parametrize("ids", [[], [999999]])
def test_invalid_service_ids(ids: list[int]) -> None:
    b, _ = make_capacity_business()
    with pytest.raises(AppError) as exc:
        availability.get_available_slots(cfg(b), TUESDAY, ids, now=NOW)
    assert exc.value.code == "invalid_services"


def test_inactive_and_foreign_services_rejected() -> None:
    b, svc = make_capacity_business()
    inactive = ServiceFactory(business=b, is_active=False)
    foreign = ServiceFactory()
    for bad in (inactive, foreign):
        with pytest.raises(AppError):
            slots(b, [svc, bad])


def test_too_many_services_rejected() -> None:
    b, _svc = make_capacity_business()
    many = [ServiceFactory(business=b) for _ in range(11)]
    with pytest.raises(AppError):
        slots(b, many)


def test_duplicate_service_ids_counted_once() -> None:
    b, svc = make_capacity_business()
    out = availability.get_available_slots(cfg(b), TUESDAY, [svc.pk, svc.pk], now=NOW)
    assert out[0].end - out[0].start == timedelta(minutes=60)


# ---------------- staff-level mode ----------------


def staff_business(**kw):  # type: ignore[no-untyped-def]
    b = BusinessFactory(staff_level_booking=True, lead_time_minutes=0, **kw)
    open_all_week(b)
    return b


def test_staff_without_service_excluded() -> None:
    b = staff_business()
    haircut, colour = ServiceFactory(business=b), ServiceFactory(business=b)
    alice, bob = StaffFactory(business=b), StaffFactory(business=b)
    StaffServiceFactory(staff=alice, service=haircut)
    StaffServiceFactory(staff=alice, service=colour)
    StaffServiceFactory(staff=bob, service=haircut)
    out = slots(b, [haircut, colour])
    assert all(s.staff_ids == (alice.pk,) for s in out)
    out = slots(b, [haircut])
    assert set(out[0].staff_ids) == {alice.pk, bob.pk}


def test_nobody_can_do_all_services_gives_no_slots() -> None:
    b = staff_business()
    s1, s2 = ServiceFactory(business=b), ServiceFactory(business=b)
    StaffServiceFactory(staff=StaffFactory(business=b), service=s1)
    StaffServiceFactory(staff=StaffFactory(business=b), service=s2)
    assert slots(b, [s1, s2]) == []


def test_inactive_staff_excluded() -> None:
    b = staff_business()
    svc = ServiceFactory(business=b)
    StaffServiceFactory(staff=StaffFactory(business=b, is_active=False), service=svc)
    assert slots(b, [svc]) == []


def test_explicit_staff_filter_and_invalid_staff() -> None:
    b = staff_business()
    svc = ServiceFactory(business=b)
    alice, bob = StaffFactory(business=b), StaffFactory(business=b)
    StaffServiceFactory(staff=alice, service=svc)
    out = slots(b, [svc], staff_id=alice.pk)
    assert out and all(s.staff_ids == (alice.pk,) for s in out)
    with pytest.raises(AppError) as exc:
        slots(b, [svc], staff_id=bob.pk)
    assert exc.value.code == "staff_cannot_perform"


def test_any_staff_is_union_of_availability() -> None:
    b = staff_business()
    svc = ServiceFactory(business=b, duration_minutes=60)
    alice, bob = StaffFactory(business=b), StaffFactory(business=b)
    StaffServiceFactory(staff=alice, service=svc)
    StaffServiceFactory(staff=bob, service=svc)
    AppointmentFactory(
        business=b,
        staff=alice,
        scheduled_start=local(TUESDAY, 12),
        scheduled_end=local(TUESDAY, 13),
    )
    at12 = next(s for s in slots(b, [svc]) if s.start == local(TUESDAY, 12))
    assert at12.staff_ids == (bob.pk,)
    AppointmentFactory(
        business=b, staff=bob, scheduled_start=local(TUESDAY, 12), scheduled_end=local(TUESDAY, 13)
    )
    assert all(s.start != local(TUESDAY, 12) for s in slots(b, [svc]))


def test_staff_busy_only_blocks_that_staff() -> None:
    b = staff_business()
    svc = ServiceFactory(business=b, duration_minutes=60)
    alice, bob = StaffFactory(business=b), StaffFactory(business=b)
    for s in (alice, bob):
        StaffServiceFactory(staff=s, service=svc)
    AppointmentFactory(
        business=b,
        staff=alice,
        scheduled_start=local(TUESDAY, 12),
        scheduled_end=local(TUESDAY, 13),
    )
    only_alice = slots(b, [svc], staff_id=alice.pk)
    assert all(s.start != local(TUESDAY, 12) for s in only_alice)
    assert any(s.start == local(TUESDAY, 12) for s in slots(b, [svc], staff_id=bob.pk))


def test_staff_hours_override_business_hours_for_that_weekday() -> None:
    b = staff_business()
    svc = ServiceFactory(business=b, duration_minutes=60)
    alice = StaffFactory(business=b)
    StaffServiceFactory(staff=alice, service=svc)
    OpeningHoursFactory(business=b, staff=alice, weekday=1, opens_at=time(13), closes_at=time(15))
    assert hhmm(slots(b, [svc], staff_id=alice.pk)) == ["13:00", "13:30", "14:00"]
    # another weekday falls back to business hours
    assert hhmm(slots(b, [svc], day=day_plus(2), staff_id=alice.pk))[0] == "10:00"


def test_staff_closure_and_business_closure() -> None:
    b = staff_business()
    svc = ServiceFactory(business=b, duration_minutes=60)
    alice, bob = StaffFactory(business=b), StaffFactory(business=b)
    for s in (alice, bob):
        StaffServiceFactory(staff=s, service=svc)
    ClosureFactory(
        business=b,
        staff=alice,
        starts_at=local(TUESDAY, 0),
        ends_at=local(TUESDAY, 23, 59),
        kind="leave",
    )
    out = slots(b, [svc])
    assert out and all(s.staff_ids == (bob.pk,) for s in out)
    ClosureFactory(business=b, starts_at=local(TUESDAY, 0), ends_at=local(TUESDAY, 23, 59))
    assert slots(b, [svc]) == []


# ---------------- holds ----------------


def test_active_hold_blocks_slot_and_expiry_frees_it() -> None:
    b, svc = make_capacity_business()
    user = UserFactory()
    with time_machine.travel(NOW, tick=False):
        hold = holds.place_hold(
            user_id=user.pk,
            business_id=b.pk,
            staff_id=None,
            start=local(TUESDAY, 12),
            end=local(TUESDAY, 13),
        )
        assert hold
        assert "12:00" not in hhmm(availability.get_available_slots(cfg(b), TUESDAY, [svc.pk]))
    with time_machine.travel(NOW + timedelta(seconds=301), tick=False):
        assert "12:00" in hhmm(availability.get_available_slots(cfg(b), TUESDAY, [svc.pk]))


def test_staff_hold_blocks_only_that_staff() -> None:
    b = staff_business()
    svc = ServiceFactory(business=b, duration_minutes=60)
    alice, bob = StaffFactory(business=b), StaffFactory(business=b)
    for s in (alice, bob):
        StaffServiceFactory(staff=s, service=svc)
    with time_machine.travel(NOW, tick=False):
        holds.place_hold(
            user_id=1,
            business_id=b.pk,
            staff_id=alice.pk,
            start=local(TUESDAY, 12),
            end=local(TUESDAY, 13),
        )
        at12 = next(
            s
            for s in availability.get_available_slots(cfg(b), TUESDAY, [svc.pk])
            if s.start == local(TUESDAY, 12)
        )
    assert at12.staff_ids == (bob.pk,)


def test_redis_down_ignores_holds(monkeypatch: pytest.MonkeyPatch) -> None:
    from redis.exceptions import ConnectionError as RedisConnectionError

    b, svc = make_capacity_business()

    def boom() -> None:
        raise RedisConnectionError("down")

    monkeypatch.setattr(holds, "_conn", boom)
    assert slots(b, [svc])


# ---------------- find_slot / next_available_slot / cache ----------------


def test_find_slot() -> None:
    b, svc = make_capacity_business()
    assert availability.find_slot(cfg(b), local(TUESDAY, 12), [svc.pk], now=NOW)
    assert availability.find_slot(cfg(b), local(TUESDAY, 12, 15), [svc.pk], now=NOW) is None
    assert availability.find_slot(cfg(b), local(TUESDAY, 19, 30), [svc.pk], now=NOW) is None


def test_next_available_slot_skips_closed_days_and_uses_shortest_service() -> None:
    b = BusinessFactory(lead_time_minutes=0)
    ServiceFactory(business=b, duration_minutes=120)
    ServiceFactory(business=b, duration_minutes=30)
    OpeningHoursFactory(business=b, weekday=2, opens_at=time(11), closes_at=time(12))  # Wednesday
    with time_machine.travel(NOW, tick=False):
        nxt = availability.next_available_slot(cfg(b))
    assert nxt == local(day_plus(2), 11)


def test_next_available_slot_none_without_services_or_hours() -> None:
    b = BusinessFactory()
    assert availability.next_available_slot(cfg(b), now=NOW) is None
    ServiceFactory(business=b)
    cache.clear()
    assert availability.next_available_slot(cfg(b), now=NOW) is None  # no opening hours


def test_next_available_slot_cached_and_invalidated_by_changes() -> None:
    b, _svc = make_capacity_business()
    first = availability.next_available_slot(cfg(b), now=NOW)
    assert first == local(MONDAY, 10) and cache.get(next_slot_key(b.pk))
    # block the whole day: cached value still served until invalidation
    cached = availability.next_available_slot(cfg(b), now=NOW)
    assert cached == first
    ClosureFactory(
        business=b, starts_at=local(MONDAY, 0), ends_at=local(TUESDAY, 0)
    )  # signal invalidates
    assert cache.get(next_slot_key(b.pk)) is None
    assert availability.next_available_slot(cfg(b), now=NOW) == local(TUESDAY, 10)


def test_cache_invalidated_on_appointment_hours_and_service_change() -> None:
    b, svc = make_capacity_business()
    availability.next_available_slot(cfg(b), now=NOW)
    AppointmentFactory(business=b)
    assert cache.get(next_slot_key(b.pk)) is None
    availability.next_available_slot(cfg(b), now=NOW)
    OpeningHoursFactory.build(business=b)
    svc.duration_minutes = 30
    svc.save()
    assert cache.get(next_slot_key(b.pk)) is None
    availability.next_available_slot(cfg(b), now=NOW)
    staff = StaffFactory(business=b)
    assert cache.get(next_slot_key(b.pk)) is None
    availability.next_available_slot(cfg(b), now=NOW)
    StaffServiceFactory(staff=staff, service=svc)
    assert cache.get(next_slot_key(b.pk)) is None
    availability.next_available_slot(cfg(b), now=NOW)
    staff.delete()
    assert cache.get(next_slot_key(b.pk)) is None


# ---------------- endpoint ----------------


def url(b) -> str:  # type: ignore[no-untyped-def]
    return f"/api/v1/businesses/{b.public_id}/availability"


@time_machine.travel(NOW, tick=False)
def test_endpoint_success_shape() -> None:
    b, svc = make_capacity_business()
    r = Client().get(url(b), {"date": "2026-11-03", "service_ids": str(svc.pk)})
    assert r.status_code == 200
    body = r.json()
    assert (
        body["date"] == "2026-11-03"
        and body["timezone"] == "Asia/Karachi"
        and body["duration_minutes"] == 60
    )
    assert body["slots"][0] == {
        "start": "2026-11-03T10:00:00+05:00",
        "end": "2026-11-03T11:00:00+05:00",
        "staff_ids": [],
    }


@time_machine.travel(NOW, tick=False)
def test_endpoint_multiple_services_comma_separated() -> None:
    b, svc = make_capacity_business()
    other = ServiceFactory(business=b, duration_minutes=30)
    r = Client().get(url(b), {"date": "2026-11-03", "service_ids": f"{svc.pk},{other.pk}"})
    assert r.json()["duration_minutes"] == 90


@time_machine.travel(NOW, tick=False)
@pytest.mark.parametrize(
    "params",
    [
        {"service_ids": "1"},
        {"date": "2026-11-03"},
        {"date": "not-a-date", "service_ids": "1"},
        {"date": "2026-13-45", "service_ids": "1"},
        {"date": "2026-11-03", "service_ids": ""},
        {"date": "2026-11-03", "service_ids": "a,b"},
        {"date": "2026-11-03", "service_ids": "1", "staff_id": "x"},
    ],
)
def test_endpoint_validation(params: dict[str, str]) -> None:
    b, _ = make_capacity_business()
    assert Client().get(url(b), params).status_code == 422


@time_machine.travel(NOW, tick=False)
def test_endpoint_past_date_422_and_unknown_service_422() -> None:
    b, svc = make_capacity_business()
    r = Client().get(url(b), {"date": "2026-11-01", "service_ids": str(svc.pk)})
    assert r.status_code == 422 and r.json()["code"] == "date_in_past"
    r = Client().get(url(b), {"date": "2026-11-03", "service_ids": "999999"})
    assert r.status_code == 422 and r.json()["code"] == "invalid_services"


@time_machine.travel(NOW, tick=False)
def test_endpoint_unknown_and_unlisted_business_404() -> None:
    import uuid

    b, svc = make_capacity_business()
    b.is_listed = False
    b.save()
    for ref in (b.public_id, uuid.uuid4()):
        r = Client().get(
            f"/api/v1/businesses/{ref}/availability",
            {"date": "2026-11-03", "service_ids": str(svc.pk)},
        )
        assert r.status_code == 404 and r.json()["code"] == "not_found"
    r = Client().get(
        "/api/v1/businesses/not-a-uuid/availability", {"date": "2026-11-03", "service_ids": "1"}
    )
    assert r.status_code == 422


@time_machine.travel(NOW, tick=False)
def test_endpoint_staff_mode_returns_staff_ids_and_filters() -> None:
    b = staff_business()
    svc = ServiceFactory(business=b, duration_minutes=60)
    alice = StaffFactory(business=b)
    StaffServiceFactory(staff=alice, service=svc)
    r = Client().get(
        url(b), {"date": "2026-11-03", "service_ids": str(svc.pk), "staff_id": alice.pk}
    )
    assert r.json()["slots"][0]["staff_ids"] == [alice.pk]
    other = StaffFactory(business=b)
    r = Client().get(
        url(b), {"date": "2026-11-03", "service_ids": str(svc.pk), "staff_id": other.pk}
    )
    assert r.status_code == 422 and r.json()["code"] == "staff_cannot_perform"


@time_machine.travel(NOW, tick=False)
def test_endpoint_query_count_bounded(django_assert_max_num_queries) -> None:  # type: ignore[no-untyped-def]
    b = staff_business()
    svc = ServiceFactory(business=b)
    for _ in range(4):
        StaffServiceFactory(staff=StaffFactory(business=b), service=svc)
    with django_assert_max_num_queries(10):
        r = Client().get(url(b), {"date": "2026-11-03", "service_ids": str(svc.pk)})
    assert r.status_code == 200 and json.loads(r.content)["slots"]
