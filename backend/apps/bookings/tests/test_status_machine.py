import itertools
import threading
from datetime import timedelta

import pytest
import time_machine
from django.db import connection

from apps.bookings import services
from apps.bookings.availability import find_slot
from apps.bookings.factories import AppointmentFactory
from apps.bookings.models import Appointment, AppointmentEvent, AppointmentStatus
from apps.bookings.tests.helpers import NOW, TUESDAY, local
from apps.businesses import selectors as biz
from apps.businesses.factories import BusinessFactory, ServiceFactory, open_all_week
from apps.core.errors import AppError

pytestmark = pytest.mark.django_db

S = AppointmentStatus
ALL = [s.value for s in AppointmentStatus]
ACTOR = services.Actor(user_id=42, role="business_owner")


def make(status: str, **kw):  # type: ignore[no-untyped-def]
    kw.setdefault("scheduled_start", NOW - timedelta(hours=2))
    kw.setdefault("scheduled_end", NOW - timedelta(hours=1))
    return AppointmentFactory(status=status, **kw)


PAIRS = list(itertools.product(ALL, ALL))
ALLOWED = [(a, b) for a, b in PAIRS if b in services.ALLOWED_TRANSITIONS[a]]
FORBIDDEN = [(a, b) for a, b in PAIRS if b not in services.ALLOWED_TRANSITIONS[a]]


def test_transition_table_shape() -> None:
    assert len(ALLOWED) == 3 + 4 and len(FORBIDDEN) == 36 - 7
    for terminal in (S.COMPLETED, S.CANCELLED_BY_CUSTOMER, S.CANCELLED_BY_BUSINESS, S.NO_SHOW):
        assert services.ALLOWED_TRANSITIONS[terminal] == set()


@pytest.mark.parametrize(("src", "dst"), ALLOWED)
def test_allowed_transition_succeeds_and_writes_event(src: str, dst: str) -> None:
    a = make(src)
    out = services.transition(a.pk, dst, ACTOR, note="because")
    a.refresh_from_db()
    assert out.status == a.status == dst
    ev = AppointmentEvent.objects.get(appointment=a)
    assert (ev.from_status, ev.to_status, ev.actor_id, ev.actor_role, ev.note) == (
        src,
        dst,
        42,
        "business_owner",
        "because",
    )
    assert (a.cancelled_at is not None) == dst.startswith("cancelled")


@pytest.mark.parametrize(("src", "dst"), FORBIDDEN)
def test_forbidden_transition_409_and_nothing_written(src: str, dst: str) -> None:
    a = make(src)
    with pytest.raises(AppError) as exc:
        services.transition(a.pk, dst, ACTOR)
    assert exc.value.status == 409 and exc.value.code == "invalid_transition"
    a.refresh_from_db()
    assert a.status == src and AppointmentEvent.objects.count() == 0


def test_unknown_appointment_404() -> None:
    with pytest.raises(AppError) as exc:
        services.confirm(999999, ACTOR)
    assert exc.value.status == 404


def test_confirm_pending_emits_event(run_commit, captured_events) -> None:  # type: ignore[no-untyped-def]
    a = make(S.PENDING)
    with run_commit():
        services.confirm(a.pk, ACTOR)
    assert [(n, i) for n, i, _ in captured_events] == [("booking_confirmed", a.pk)]


def test_confirm_twice_conflicts() -> None:
    a = make(S.PENDING)
    services.confirm(a.pk, ACTOR)
    with pytest.raises(AppError) as exc:
        services.confirm(a.pk, ACTOR)
    assert exc.value.code == "invalid_transition"


def test_complete_emits_booking_completed_on_commit_only(
    django_capture_on_commit_callbacks, captured_events
) -> None:  # type: ignore[no-untyped-def]
    a = make(S.CONFIRMED)
    with django_capture_on_commit_callbacks(execute=False) as cbs:
        services.complete(a.pk, ACTOR, now=NOW)
        assert captured_events == []
    for cb in cbs:
        cb()
    assert [(n, i) for n, i, _ in captured_events] == [("booking_completed", a.pk)]


def test_complete_and_no_show_not_allowed_before_start() -> None:
    a = make(
        S.CONFIRMED,
        scheduled_start=NOW + timedelta(hours=1),
        scheduled_end=NOW + timedelta(hours=2),
    )
    for fn in (services.complete, services.mark_no_show):
        with pytest.raises(AppError) as exc:
            fn(a.pk, ACTOR, now=NOW)
        assert exc.value.code == "appointment_not_started" and exc.value.status == 409
    a.refresh_from_db()
    assert a.status == S.CONFIRMED and AppointmentEvent.objects.count() == 0
    # exactly at the start time it is allowed
    services.complete(a.pk, ACTOR, now=a.scheduled_start)


def test_complete_pending_conflicts() -> None:
    a = make(S.PENDING)
    with pytest.raises(AppError) as exc:
        services.complete(a.pk, ACTOR, now=NOW)
    assert exc.value.code == "invalid_transition"


def test_mark_no_show_emits(run_commit, captured_events) -> None:  # type: ignore[no-untyped-def]
    a = make(S.CONFIRMED)
    with run_commit():
        services.mark_no_show(a.pk, ACTOR, now=NOW)
    assert [(n, i) for n, i, _ in captured_events] == [("booking_no_show", a.pk)]


@pytest.mark.parametrize("src", [S.PENDING, S.CONFIRMED])
def test_cancel_by_business_records_reason_and_emits(src: str, run_commit, captured_events) -> None:  # type: ignore[no-untyped-def]
    a = make(
        src, scheduled_start=NOW + timedelta(days=1), scheduled_end=NOW + timedelta(days=1, hours=1)
    )
    with run_commit():
        services.cancel_by_business(a.pk, ACTOR, "Stylist unwell")
    a.refresh_from_db()
    assert a.status == S.CANCELLED_BY_BUSINESS and a.cancellation_reason == "business"
    assert a.cancellation_note == "Stylist unwell" and a.cancelled_at is not None
    assert (
        captured_events[0][0] == "booking_cancelled"
        and captured_events[0][2]["cancelled_by"] == "business"
    )
    assert AppointmentEvent.objects.get().note == "Stylist unwell"


def test_cancel_by_business_completed_conflicts() -> None:
    a = make(S.COMPLETED)
    with pytest.raises(AppError):
        services.cancel_by_business(a.pk, ACTOR, "x")
    a.refresh_from_db()
    assert a.status == S.COMPLETED and a.cancellation_reason == ""


def test_cancelled_booking_frees_the_slot() -> None:
    b = BusinessFactory(lead_time_minutes=0)
    open_all_week(b)
    svc = ServiceFactory(business=b, duration_minutes=60)
    cfg = biz.get_booking_config_by_id(b.pk)
    assert cfg
    a = AppointmentFactory(
        business=b,
        status=S.CONFIRMED,
        scheduled_start=local(TUESDAY, 12),
        scheduled_end=local(TUESDAY, 13),
    )
    assert find_slot(cfg, local(TUESDAY, 12), [svc.pk], now=NOW) is None
    services.cancel_by_business(a.pk, ACTOR, "x")
    assert find_slot(cfg, local(TUESDAY, 12), [svc.pk], now=NOW) is not None


@pytest.mark.django_db(transaction=True)
def test_concurrent_confirm_only_one_wins() -> None:
    a = make(S.PENDING)
    results: list[str] = []
    barrier = threading.Barrier(4)

    def worker() -> None:
        try:
            barrier.wait()
            services.confirm(a.pk, ACTOR)
            results.append("ok")
        except AppError as exc:
            results.append(exc.code)
        finally:
            connection.close()

    threads = [threading.Thread(target=worker) for _ in range(4)]
    with time_machine.travel(NOW, tick=False):
        [t.start() for t in threads]
        [t.join() for t in threads]
    assert sorted(results) == ["invalid_transition"] * 3 + ["ok"]
    assert AppointmentEvent.objects.filter(appointment=a).count() == 1
    assert Appointment.objects.get(pk=a.pk).status == S.CONFIRMED


def test_set_payment_status_idempotent_and_unknown(run_commit, captured_events) -> None:  # type: ignore[no-untyped-def]
    a = make(S.CONFIRMED)
    with run_commit():
        assert services.set_payment_status(str(a.public_id), "paid") is True
        assert services.set_payment_status(str(a.public_id), "paid") is True
    a.refresh_from_db()
    assert a.payment_status == "paid"
    assert [n for n, _, _ in captured_events] == [
        "booking_payment_changed"
    ]  # second call changed nothing
    import uuid

    assert services.set_payment_status(str(uuid.uuid4()), "paid") is False
