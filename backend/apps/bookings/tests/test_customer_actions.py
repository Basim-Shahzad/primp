import json
from datetime import timedelta

import pytest
import time_machine
from django.test import Client

from apps.accounts.factories import UserFactory
from apps.accounts.models import Role
from apps.accounts.tests.helpers import bearer, login_client
from apps.bookings import services
from apps.bookings.models import (
    Appointment,
    AppointmentEvent,
    AppointmentStatus,
    RescheduleRequest,
    RescheduleStatus,
)
from apps.bookings.tests.api_helpers import (
    book,
    capacity_business,
    customer_session,
    hold_body,
    place_hold,
    post,
    staff_business,
)
from apps.bookings.tests.helpers import NOW, TUESDAY, local
from apps.businesses.factories import ServiceFactory
from apps.core.errors import AppError

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("frozen", "long_tokens")]
BASE = "/api/v1/appointments"


def patch(path: str, data: dict | None, headers: dict[str, str]):  # type: ignore[no-untyped-def,type-arg]
    return Client().patch(
        f"{BASE}/{path}", data=json.dumps(data or {}), content_type="application/json", **headers
    )


def make_booking(hour: int = 12, **business_kw):  # type: ignore[no-untyped-def]
    b, svc = capacity_business(**business_kw)
    user, headers = customer_session()
    token = place_hold(headers, b, [svc], hour).json()["token"]
    public_id = book(headers, token).json()["id"]
    return b, svc, user, headers, public_id


# ---------------- cancel ----------------


def test_cancel_success_records_everything_and_frees_slot(run_commit, captured_events) -> None:  # type: ignore[no-untyped-def]
    b, svc, user, headers, pid = make_booking()
    captured_events.clear()
    with run_commit():
        r = patch(
            f"{pid}/cancel", {"reason": "schedule_conflict", "note": "  meeting moved  "}, headers
        )
    assert r.status_code == 200
    body = r.json()
    assert (
        body["status"] == "cancelled_by_customer"
        and body["cancellation_reason"] == "schedule_conflict"
    )
    assert body["is_late_cancellation"] is False and body["cancelled_at"]
    a = Appointment.objects.get(public_id=pid)
    assert a.cancellation_note == "meeting moved" and a.cancelled_at is not None
    ev = AppointmentEvent.objects.filter(appointment=a).latest("id")
    assert (ev.from_status, ev.to_status, ev.actor_id, ev.note) == (
        "confirmed",
        "cancelled_by_customer",
        user.pk,
        "schedule_conflict",
    )
    assert [(n, k.get("cancelled_by")) for n, _, k in captured_events] == [
        ("booking_cancelled", "customer")
    ]
    # the slot is released immediately: someone else can now hold and book it
    _, other = customer_session()
    assert book(other, place_hold(other, b, [svc]).json()["token"]).status_code == 201


def test_cancel_pending_booking_allowed() -> None:
    _, _, _, headers, pid = make_booking(confirm_mode="manual")
    assert (
        patch(f"{pid}/cancel", {"reason": "other"}, headers).json()["status"]
        == "cancelled_by_customer"
    )


@pytest.mark.parametrize(
    ("seconds_before_start", "late"),
    [(24 * 3600 + 1, False), (24 * 3600, False), (24 * 3600 - 1, True), (3600, True)],
)
def test_late_cancellation_flag_at_notice_boundary(seconds_before_start: int, late: bool) -> None:
    _b, _svc, _user, headers, pid = make_booking(cancellation_notice_hours=24)
    start = local(TUESDAY, 12)
    with time_machine.travel(start - timedelta(seconds=seconds_before_start), tick=False):
        r = patch(f"{pid}/cancel", {"reason": "other"}, headers)
    assert r.status_code == 200 and r.json()["is_late_cancellation"] is late
    assert Appointment.objects.get(public_id=pid).is_late_cancellation is late
    ev = AppointmentEvent.objects.latest("id")
    assert ("(late)" in ev.note) is late


def test_zero_notice_never_late() -> None:
    _, _, _, headers, pid = make_booking(cancellation_notice_hours=0)
    with time_machine.travel(local(TUESDAY, 12) - timedelta(seconds=1), tick=False):
        assert (
            patch(f"{pid}/cancel", {"reason": "other"}, headers).json()["is_late_cancellation"]
            is False
        )


def test_cancel_twice_409() -> None:
    _, _, _, headers, pid = make_booking()
    assert patch(f"{pid}/cancel", {"reason": "other"}, headers).status_code == 200
    r = patch(f"{pid}/cancel", {"reason": "other"}, headers)
    assert r.status_code == 409 and r.json()["code"] == "invalid_transition"
    assert AppointmentEvent.objects.filter(to_status="cancelled_by_customer").count() == 1


@pytest.mark.parametrize("status", ["completed", "no_show", "cancelled_by_business"])
def test_cancel_in_terminal_status_409(status: str) -> None:
    _, _, _, headers, pid = make_booking()
    Appointment.objects.filter(public_id=pid).update(status=status)
    r = patch(f"{pid}/cancel", {"reason": "other"}, headers)
    assert r.status_code == 409 and r.json()["code"] == "invalid_transition"


def test_cancel_after_start_409() -> None:
    _, _, _, headers, pid = make_booking()
    with time_machine.travel(local(TUESDAY, 12), tick=False):
        r = patch(f"{pid}/cancel", {"reason": "other"}, headers)
    assert r.status_code == 409 and r.json()["code"] == "appointment_started"


def test_cancel_someone_elses_booking_404_and_unchanged() -> None:
    _, _, _, _, pid = make_booking()
    _, intruder = customer_session()
    r = patch(f"{pid}/cancel", {"reason": "other"}, intruder)
    assert r.status_code == 404 and r.json()["code"] == "not_found"
    assert Appointment.objects.get(public_id=pid).status == AppointmentStatus.CONFIRMED


def test_cancel_unknown_id_404() -> None:
    import uuid

    _, headers = customer_session()
    assert patch(f"{uuid.uuid4()}/cancel", {"reason": "other"}, headers).status_code == 404


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"reason": ""},
        {"reason": "because"},
        {"reason": 5},
        {"reason": "other", "note": "x" * 501},
    ],
)
def test_cancel_validation_422(body: dict) -> None:  # type: ignore[type-arg]
    _, _, _, headers, pid = make_booking()
    assert patch(f"{pid}/cancel", body, headers).status_code == 422


def test_cancel_note_boundary_500() -> None:
    _, _, _, headers, pid = make_booking()
    assert (
        patch(f"{pid}/cancel", {"reason": "other", "note": "x" * 500}, headers).status_code == 200
    )


def test_cancel_auth() -> None:
    _, _, _, _, pid = make_booking()
    assert patch(f"{pid}/cancel", {"reason": "other"}, {}).status_code == 401
    _, session = login_client(UserFactory(role=Role.ADMIN))
    assert patch(f"{pid}/cancel", {"reason": "other"}, bearer(session)).status_code == 403


def test_all_cancel_reason_codes_accepted() -> None:
    for reason in services.CANCEL_REASONS:
        _, _, _, headers, pid = make_booking()
        assert patch(f"{pid}/cancel", {"reason": reason}, headers).status_code == 200


# ---------------- policy ----------------


def get_policy(pid: str, headers: dict[str, str]):  # type: ignore[no-untyped-def]
    return Client().get(f"{BASE}/{pid}/policy", **headers)


def test_policy_terms_before_cancelling() -> None:
    _, _, _, headers, pid = make_booking(cancellation_notice_hours=24)
    r = get_policy(pid, headers)
    assert r.status_code == 200
    body = r.json()
    assert body["cancellation_notice_hours"] == 24 and body["can_cancel"] is True
    assert body["is_late_now"] is False
    assert body["free_cancellation_until"] == "2026-11-02T12:00:00+05:00"
    assert "Free cancellation until" in body["message"]


def test_policy_inside_notice_window_and_after_cancel() -> None:
    _, _, _, headers, pid = make_booking(cancellation_notice_hours=48)
    body = get_policy(pid, headers).json()
    assert body["is_late_now"] is True and "late" in body["message"]
    patch(f"{pid}/cancel", {"reason": "other"}, headers)
    after = get_policy(pid, headers).json()
    assert after["can_cancel"] is False and "no longer" in after["message"]


def test_policy_ownership_and_auth() -> None:
    _, _, _, _headers, pid = make_booking()
    _, intruder = customer_session()
    assert get_policy(pid, intruder).status_code == 404
    assert get_policy(pid, {}).status_code == 401


# ---------------- reschedule ----------------


def reschedule(pid: str, token: str, headers: dict[str, str]):  # type: ignore[no-untyped-def]
    return patch(f"{pid}/reschedule", {"hold_token": token}, headers)


def hold_for(headers, b, svc, hour, pid=None, minute=0, **extra):  # type: ignore[no-untyped-def]
    body = hold_body(b, [svc], hour, minute, **extra)
    if pid:
        body["appointment_id"] = pid
    r = post(f"{BASE}/hold", body, headers)
    assert r.status_code == 201, r.content
    return r.json()["token"]


def test_instant_reschedule_moves_booking(run_commit, captured_events) -> None:  # type: ignore[no-untyped-def]
    b, svc, user, headers, pid = make_booking(12)
    token = hold_for(headers, b, svc, 15)
    captured_events.clear()
    with run_commit():
        r = reschedule(pid, token, headers)
    assert r.status_code == 200
    body = r.json()
    assert body["applied"] is True and body["request"] is None
    assert body["appointment"]["start"] == "2026-11-03T15:00:00+05:00"
    a = Appointment.objects.get(public_id=pid)
    assert a.scheduled_start == local(TUESDAY, 15) and a.scheduled_end == local(TUESDAY, 16)
    assert a.status == AppointmentStatus.CONFIRMED and a.total_price == 2000
    ev = AppointmentEvent.objects.latest("id")
    assert (ev.from_status, ev.to_status, ev.note, ev.actor_id) == (
        "confirmed",
        "confirmed",
        "rescheduled",
        user.pk,
    )
    assert [n for n, _, _ in captured_events] == ["booking_rescheduled"]
    from apps.bookings import holds

    assert holds.get_hold(token) is None
    # old slot is free again, new slot is taken
    _, other = customer_session()
    assert place_hold(other, b, [svc], 12).status_code == 201
    assert place_hold(other, b, [svc], 15).status_code == 409


def test_reschedule_to_overlapping_time_ignores_own_booking() -> None:
    b, svc, _, headers, pid = make_booking(12)
    # 12:30 overlaps the booking being moved; without appointment_id the hold is refused
    r = post(f"{BASE}/hold", hold_body(b, [svc], 12, 30), headers)
    assert r.status_code == 409
    token = hold_for(headers, b, svc, 12, pid=pid, minute=30)
    r = reschedule(pid, token, headers)
    assert r.status_code == 200 and r.json()["appointment"]["start"] == "2026-11-03T12:30:00+05:00"


def test_hold_with_foreign_or_mismatched_appointment_id_422() -> None:
    b, svc, _, headers, pid = make_booking()
    _, intruder = customer_session()
    r = post(f"{BASE}/hold", {**hold_body(b, [svc], 15), "appointment_id": pid}, intruder)
    assert r.status_code == 404
    other_b, other_svc = capacity_business()
    r = post(
        f"{BASE}/hold", {**hold_body(other_b, [other_svc], 15), "appointment_id": pid}, headers
    )
    assert r.status_code == 422 and r.json()["code"] == "hold_mismatch"


def test_reschedule_manual_confirm_creates_pending_request(run_commit, captured_events) -> None:  # type: ignore[no-untyped-def]
    b, svc, _, headers, pid = make_booking(12, confirm_mode="manual")
    token = hold_for(headers, b, svc, 15)
    captured_events.clear()
    with run_commit():
        r = reschedule(pid, token, headers)
    assert r.status_code == 202
    body = r.json()
    assert body["applied"] is False and body["request"]["status"] == "pending"
    assert body["request"]["new_start"] == "2026-11-03T15:00:00+05:00"
    a = Appointment.objects.get(public_id=pid)
    assert a.scheduled_start == local(TUESDAY, 12)  # unchanged until the provider accepts
    assert RescheduleRequest.objects.get().status == RescheduleStatus.PENDING
    assert [n for n, _, _ in captured_events] == ["booking_reschedule_requested"]
    # a second request while one is pending is refused
    second = hold_for(headers, b, svc, 17)
    r = reschedule(pid, second, headers)
    assert r.status_code == 409 and r.json()["code"] == "reschedule_pending"


def test_accept_reschedule_applies_move(run_commit, captured_events) -> None:  # type: ignore[no-untyped-def]
    b, svc, _, headers, pid = make_booking(12, confirm_mode="manual")
    reschedule(pid, hold_for(headers, b, svc, 15), headers)
    req = RescheduleRequest.objects.get()
    captured_events.clear()
    actor = services.Actor(b.owner_id, "business_owner")
    with run_commit():
        result = services.accept_reschedule(req.pk, actor, now=NOW)
    assert result.applied
    a = Appointment.objects.get(public_id=pid)
    assert a.scheduled_start == local(TUESDAY, 15)
    req.refresh_from_db()
    assert req.status == RescheduleStatus.ACCEPTED and req.decided_at
    assert [n for n, _, _ in captured_events] == ["booking_rescheduled"]
    with pytest.raises(AppError) as exc:
        services.accept_reschedule(req.pk, actor, now=NOW)
    assert exc.value.code == "invalid_transition"


def test_accept_reschedule_when_slot_was_taken_meanwhile_409() -> None:
    b, svc, _, headers, pid = make_booking(12, confirm_mode="manual")
    reschedule(pid, hold_for(headers, b, svc, 15), headers)
    _, other = customer_session()
    book(
        other, place_hold(other, b, [svc], 15).json()["token"]
    )  # takes 15:00 (manual: pending, still blocks)
    req = RescheduleRequest.objects.get()
    with pytest.raises(AppError) as exc:
        services.accept_reschedule(req.pk, services.Actor(1, "business_owner"), now=NOW)
    assert exc.value.code == "slot_unavailable"
    req.refresh_from_db()
    assert req.status == RescheduleStatus.PENDING
    assert Appointment.objects.get(public_id=pid).scheduled_start == local(TUESDAY, 12)


def test_decline_reschedule(run_commit, captured_events) -> None:  # type: ignore[no-untyped-def]
    b, svc, _, headers, pid = make_booking(12, confirm_mode="manual")
    reschedule(pid, hold_for(headers, b, svc, 15), headers)
    req = RescheduleRequest.objects.get()
    captured_events.clear()
    with run_commit():
        services.decline_reschedule(req.pk, services.Actor(1, "business_owner"))
    req.refresh_from_db()
    assert req.status == RescheduleStatus.DECLINED
    assert Appointment.objects.get(public_id=pid).scheduled_start == local(TUESDAY, 12)
    assert [n for n, _, _ in captured_events] == ["booking_reschedule_declined"]
    with pytest.raises(AppError):
        services.decline_reschedule(req.pk, services.Actor(1, "business_owner"))
    with pytest.raises(AppError) as exc:
        services.decline_reschedule(999999, services.Actor(1, "business_owner"))
    assert exc.value.status == 404
    with pytest.raises(AppError) as exc:
        services.accept_reschedule(999999, services.Actor(1, "business_owner"))
    assert exc.value.status == 404
    # after a decline the customer may ask again
    assert reschedule(pid, hold_for(headers, b, svc, 17), headers).status_code == 202


def test_cancel_clears_pending_reschedule_request() -> None:
    b, svc, _, headers, pid = make_booking(12, confirm_mode="manual")
    reschedule(pid, hold_for(headers, b, svc, 15), headers)
    patch(f"{pid}/cancel", {"reason": "other"}, headers)
    assert RescheduleRequest.objects.get().status == RescheduleStatus.CANCELLED


def test_reschedule_into_slot_taken_after_holding_409() -> None:
    b, svc, _, headers, pid = make_booking(12)
    token = hold_for(headers, b, svc, 15)
    from apps.bookings.factories import AppointmentFactory

    AppointmentFactory(
        business=b, scheduled_start=local(TUESDAY, 15), scheduled_end=local(TUESDAY, 16)
    )
    r = reschedule(pid, token, headers)
    assert r.status_code == 409 and r.json()["code"] == "slot_unavailable"
    assert Appointment.objects.get(public_id=pid).scheduled_start == local(TUESDAY, 12)


def test_reschedule_hold_problems() -> None:
    b, svc, _, headers, pid = make_booking(12)
    _, intruder = customer_session()
    assert reschedule(pid, "unknown", headers).json()["code"] == "hold_expired"
    foreign_token = hold_for(intruder, b, svc, 15)
    assert reschedule(pid, foreign_token, headers).status_code == 404  # someone else's hold
    # hold for a different set of services
    extra = ServiceFactory(business=b, duration_minutes=30)
    token = post(f"{BASE}/hold", hold_body(b, [svc, extra], 17), headers).json()["token"]
    r = reschedule(pid, token, headers)
    assert r.status_code == 422 and r.json()["code"] == "hold_mismatch"
    # hold for another business
    other_b, other_svc = capacity_business()
    token = place_hold(headers, other_b, [other_svc], 15).json()["token"]
    assert reschedule(pid, token, headers).json()["code"] == "hold_mismatch"


def test_reschedule_to_the_same_time_422() -> None:
    b, svc, _, headers, pid = make_booking(12)
    # own booking blocks the plain hold, so use the reschedule-aware hold
    token = hold_for(headers, b, svc, 12, pid=pid)
    r = reschedule(pid, token, headers)
    assert r.status_code == 422 and r.json()["code"] == "same_time"


def test_reschedule_expired_hold_409() -> None:
    b, svc, _, headers, pid = make_booking(12)
    token = hold_for(headers, b, svc, 15)
    with time_machine.travel(NOW + timedelta(seconds=301), tick=False):
        assert reschedule(pid, token, headers).status_code == 409


@pytest.mark.parametrize("status", ["cancelled_by_customer", "completed", "no_show"])
def test_reschedule_only_active_bookings(status: str) -> None:
    b, svc, _, headers, pid = make_booking(12)
    token = hold_for(headers, b, svc, 15)
    Appointment.objects.filter(public_id=pid).update(status=status)
    r = reschedule(pid, token, headers)
    assert r.status_code == 409 and r.json()["code"] == "invalid_transition"


def test_reschedule_after_start_409() -> None:
    b, svc, _, headers, pid = make_booking(12)
    with time_machine.travel(local(TUESDAY, 12, 1), tick=False):
        token = hold_for(headers, b, svc, 17)  # a fresh hold, so only the start time is at fault
        r = reschedule(pid, token, headers)
    assert r.status_code == 409 and r.json()["code"] == "appointment_started"


def test_reschedule_other_users_booking_404_and_auth_validation() -> None:
    b, svc, _, headers, pid = make_booking(12)
    _, intruder = customer_session()
    token = hold_for(intruder, b, svc, 15)
    assert reschedule(pid, token, intruder).status_code == 404
    assert reschedule(pid, token, {}).status_code == 401
    assert patch(f"{pid}/reschedule", {}, headers).status_code == 422
    assert patch(f"{pid}/reschedule", {"hold_token": ""}, headers).status_code == 422


def test_reschedule_staff_mode_can_change_staff() -> None:
    b, svc, (alice, bob) = staff_business()
    _, headers = customer_session()
    token = hold_for(headers, b, svc, 12, staff_id=alice.pk)
    pid = book(headers, token).json()["id"]
    new_token = hold_for(headers, b, svc, 14, staff_id=bob.pk)
    r = reschedule(pid, new_token, headers)
    assert r.status_code == 200 and r.json()["appointment"]["staff"]["id"] == bob.pk
    a = Appointment.objects.get(public_id=pid)
    assert a.staff_id == bob.pk and a.scheduled_start == local(TUESDAY, 14)
