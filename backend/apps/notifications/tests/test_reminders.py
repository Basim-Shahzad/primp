import threading
from datetime import timedelta

import pytest
import time_machine
from django.db import connection

from apps.accounts.factories import make_customer
from apps.bookings.factories import AppointmentFactory
from apps.bookings.models import Appointment
from apps.bookings.tests.helpers import NOW
from apps.businesses.factories import BusinessFactory
from apps.notifications import reminders, services
from apps.notifications.adapters import FakeSMS
from apps.notifications.models import Notification, Reminder
from apps.notifications.tasks import scan_reminders

pytestmark = pytest.mark.django_db


_phones = iter(range(3_001_000_000, 3_001_999_999))


def appt(start_in: timedelta, *, created_before_start: timedelta = timedelta(days=3), **kw):  # type: ignore[no-untyped-def]
    if "customer" not in kw:
        kw["customer"] = make_customer(phone=f"+92{next(_phones)}", name="Sana")
    if "business" not in kw:
        kw["business"] = BusinessFactory(name="Glow Studio")
    a = AppointmentFactory(
        scheduled_start=NOW + start_in, scheduled_end=NOW + start_in + timedelta(hours=1), **kw
    )
    Appointment.objects.filter(pk=a.pk).update(created_at=NOW + start_in - created_before_start)
    return a


def sms_bodies() -> list[str]:
    return [s.body for s in FakeSMS.outbox]


H, M = timedelta(hours=1), timedelta(minutes=1)


def test_24h_reminder_sent_once_across_repeated_scans(run_commit) -> None:  # type: ignore[no-untyped-def]
    a = appt(23 * H + 45 * M)
    with run_commit():
        assert reminders.scan(NOW) == 1
        assert reminders.scan(NOW + 5 * M) == 0
        assert reminders.scan(NOW + 10 * M) == 0
    assert len(FakeSMS.outbox) == 1
    assert "tomorrow" in sms_bodies()[0] and "Glow Studio" in sms_bodies()[0]
    reminder = Reminder.objects.get()
    assert reminder.appointment_id == a.pk and reminder.kind == "reminder_24h"


def test_window_boundaries() -> None:
    appt(25 * H)
    assert reminders.scan(NOW) == 0  # window not open yet
    appt(24 * H, customer=make_customer(phone="+923009998877"))
    assert reminders.scan(NOW) == 1  # opens exactly at start - 24h
    appt(23 * H + 30 * M, customer=make_customer(phone="+923009998866"))
    assert reminders.scan(NOW) == 0  # the 30 min grace is over: stale reminders are not sent
    appt(23 * H + 31 * M, customer=make_customer(phone="+923009998855"))
    assert reminders.scan(NOW) == 1  # 29 min after the window opened


def test_2h_reminder_only_inside_its_window(run_commit) -> None:  # type: ignore[no-untyped-def]
    appt(1 * H + 45 * M)
    with run_commit():
        assert reminders.scan(NOW) == 1
    assert Reminder.objects.get().kind == "reminder_2h"
    assert "today at" in sms_bodies()[0]


def test_both_kinds_for_one_booking_over_time(run_commit) -> None:  # type: ignore[no-untyped-def]
    a = appt(26 * H)
    with run_commit():
        assert reminders.scan(NOW) == 0
        assert reminders.scan(NOW + 2 * H + 15 * M) == 1  # 23h45m before
        assert reminders.scan(NOW + 24 * H + 15 * M) == 1  # 1h45m before
        assert reminders.scan(NOW + 24 * H + 20 * M) == 0
    kinds = set(Reminder.objects.filter(appointment_id=a.pk).values_list("kind", flat=True))
    assert kinds == {"reminder_24h", "reminder_2h"} and len(FakeSMS.outbox) == 2


def test_not_sent_for_cancelled_pending_completed_or_past_bookings() -> None:
    for status in (
        "cancelled_by_customer",
        "cancelled_by_business",
        "pending",
        "completed",
        "no_show",
    ):
        appt(23 * H + 45 * M, status=status)
        appt(1 * H + 45 * M, status=status)
    appt(-1 * H)  # already started
    assert reminders.scan(NOW) == 0
    assert FakeSMS.outbox == [] and Reminder.objects.count() == 0


def test_booking_cancelled_after_scan_query_before_claim_is_skipped() -> None:
    a = appt(1 * H + 45 * M)
    from unittest import mock

    from apps.bookings import selectors

    original = selectors.due_for_reminder

    def due_then_cancel(*args, **kwargs):  # type: ignore[no-untyped-def]
        ids = original(*args, **kwargs)
        Appointment.objects.filter(pk=a.pk).update(status="cancelled_by_customer")
        return ids

    with mock.patch(
        "apps.notifications.reminders.bookings.due_for_reminder", side_effect=due_then_cancel
    ):
        assert reminders.scan(NOW) == 0
    assert FakeSMS.outbox == []


def test_booking_made_after_the_window_opened_gets_no_stale_reminder() -> None:
    # 24h reminder window opened at start-24h; booking created 10h before start -> skipped
    appt(23 * H + 45 * M, created_before_start=10 * H)
    assert reminders.scan(NOW) == 0
    # created 3h before start, 2h window: created before the window opened -> sent
    b = appt(
        1 * H + 45 * M, created_before_start=3 * H, customer=make_customer(phone="+923007776655")
    )
    assert reminders.scan(NOW) == 1
    assert [r.kind for r in Reminder.objects.filter(appointment_id=b.pk)] == ["reminder_2h"]
    # created 1h before start: never reminded
    appt(
        1 * H + 45 * M,
        created_before_start=1 * H + 50 * M,
        customer=make_customer(phone="+923007776644"),
    )
    assert reminders.scan(NOW) == 0


def test_each_appointment_gets_its_own_reminder() -> None:
    appt(1 * H + 45 * M)
    appt(1 * H + 45 * M, customer=make_customer(phone="+923009990000"))
    assert reminders.scan(NOW) == 2
    assert Notification.objects.filter(template_key="reminder_2h", channel="sms").count() == 2


@pytest.mark.django_db(transaction=True)
def test_claim_reminder_concurrent_scanners_only_one_claims() -> None:
    results: list[bool] = []
    barrier = threading.Barrier(6)

    def worker() -> None:
        try:
            barrier.wait()
            results.append(services.claim_reminder(12345, "reminder_24h"))
        finally:
            connection.close()

    threads = [threading.Thread(target=worker) for _ in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert results.count(True) == 1 and Reminder.objects.count() == 1


@time_machine.travel(NOW, tick=False)
def test_scan_task_and_beat_schedule(run_commit) -> None:  # type: ignore[no-untyped-def]
    from django.conf import settings

    appt(1 * H + 45 * M)
    with run_commit():
        assert scan_reminders() == 1
    entry = settings.CELERY_BEAT_SCHEDULE["reminder-scan"]
    assert entry["task"] == "apps.notifications.tasks.scan_reminders" and entry["schedule"] == 300.0
    assert len(FakeSMS.outbox) == 1


def test_offsets_are_configurable(settings) -> None:  # type: ignore[no-untyped-def]
    settings.REMINDER_OFFSETS = {"reminder_24h": 48 * 3600}
    appt(47 * H + 45 * M, created_before_start=timedelta(days=5))
    assert reminders.scan(NOW) == 1
