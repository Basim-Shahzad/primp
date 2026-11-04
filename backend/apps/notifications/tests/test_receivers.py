from datetime import timedelta

import pytest
from django.core import mail

from apps.accounts.factories import UserFactory, make_customer
from apps.accounts.models import Role
from apps.bookings import services
from apps.bookings.factories import AppointmentFactory
from apps.bookings.tests.api_helpers import book, capacity_business, customer_session, place_hold
from apps.bookings.tests.helpers import NOW
from apps.businesses.factories import BusinessFactory
from apps.notifications.adapters import FakeSMS
from apps.notifications.models import Notification

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("frozen")]


def keys() -> list[tuple[str, str]]:
    return list(Notification.objects.order_by("id").values_list("template_key", "channel"))


def booked(run_commit, **business_kw):  # type: ignore[no-untyped-def]
    owner = UserFactory(role=Role.BUSINESS_OWNER, email="owner@example.com", phone=None)
    b, svc = capacity_business(owner=owner, **business_kw)
    user, headers = customer_session()
    token = place_hold(headers, b, [svc]).json()["token"]
    with run_commit():
        appointment_id = book(headers, token).json()["id"]
    return b, user, owner, headers, appointment_id


def test_instant_booking_notifies_customer_confirmation_and_provider(run_commit) -> None:  # type: ignore[no-untyped-def]
    b, user, _, _, _ = booked(run_commit)
    assert ("booking_confirmed", "sms") in keys() and ("booking_confirmed", "in_app") in keys()
    assert ("new_booking", "email") in keys()
    assert ("booking_received", "sms") not in keys()
    assert len(FakeSMS.outbox) == 1 and FakeSMS.outbox[0].to == user.phone
    assert b.name in FakeSMS.outbox[0].body and "12:00" in FakeSMS.outbox[0].body
    assert mail.outbox[0].to == ["owner@example.com"] and "booked" in mail.outbox[0].body
    assert {n.status for n in Notification.objects.all()} <= {"sent", "delivered"}


def test_manual_booking_sends_received_not_confirmed(run_commit) -> None:  # type: ignore[no-untyped-def]
    booked(run_commit, confirm_mode="manual")
    assert ("booking_received", "sms") in keys() and ("booking_confirmed", "sms") not in keys()
    assert "received your booking" in FakeSMS.outbox[0].body


def test_provider_confirmation_notifies_customer(run_commit) -> None:  # type: ignore[no-untyped-def]
    _, _, owner, _, public_id = booked(run_commit, confirm_mode="manual")
    from apps.bookings.models import Appointment

    pk = Appointment.objects.get(public_id=public_id).pk
    with run_commit():
        services.confirm(pk, services.Actor(owner.pk, "business_owner"))
    assert ("booking_confirmed", "sms") in keys()


def test_customer_cancellation_notifies_provider_not_customer(run_commit) -> None:  # type: ignore[no-untyped-def]
    _, user, owner, _, public_id = booked(run_commit)
    before = Notification.objects.count()
    with run_commit():
        services.cancel_by_customer(user.pk, public_id, "price", now=NOW)
    new = list(Notification.objects.order_by("id")[before:].values_list("template_key", "user_id"))
    assert {k for k, _ in new} == {"booking_cancelled_by_customer"} and {u for _, u in new} == {
        owner.pk
    }
    assert mail.outbox[-1].subject == "Booking cancelled"


def test_business_cancellation_notifies_customer(run_commit) -> None:  # type: ignore[no-untyped-def]
    _, _, owner, _, public_id = booked(run_commit)
    from apps.bookings.models import Appointment

    pk = Appointment.objects.get(public_id=public_id).pk
    FakeSMS.outbox.clear()
    with run_commit():
        services.cancel_by_business(pk, services.Actor(owner.pk, "business_owner"), "closed")
    assert FakeSMS.outbox and "cancelled your booking" in FakeSMS.outbox[0].body


def test_completion_sends_review_prompt(run_commit) -> None:  # type: ignore[no-untyped-def]
    customer = make_customer(phone="+923001112222")
    a = AppointmentFactory(
        customer=customer,
        business=BusinessFactory(name="Glow"),
        scheduled_start=NOW - timedelta(hours=2),
        scheduled_end=NOW - timedelta(hours=1),
    )
    with run_commit():
        services.complete(a.pk, services.Actor(1, "business_owner"), now=NOW)
    assert ("review_prompt", "sms") in keys() and "Glow" in FakeSMS.outbox[-1].body
    assert (
        Notification.objects.get(template_key="review_prompt", channel="sms").template_key
        == "review_prompt"
    )


def test_notification_problems_never_break_bookings(run_commit, settings) -> None:  # type: ignore[no-untyped-def]
    from apps.notifications.models import NotificationTemplate

    settings.NOTIFICATIONS_RAISE_ON_RENDER_ERROR = False
    NotificationTemplate.objects.filter(key="booking_confirmed").delete()
    public_id = booked(run_commit)[4]
    from apps.bookings.models import Appointment

    assert Appointment.objects.filter(public_id=public_id, status="confirmed").exists()
