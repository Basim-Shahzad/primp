from datetime import timedelta
from typing import Any

import pytest
from django.db import transaction
from django.test import Client

from apps.accounts.factories import UserFactory, make_customer
from apps.accounts.models import User
from apps.accounts.roles import Role
from apps.accounts.tests.helpers import bearer, login_client
from apps.bookings import events
from apps.bookings import services as booking_services
from apps.bookings.factories import AppointmentFactory
from apps.bookings.models import AppointmentService
from apps.bookings.tests.helpers import NOW, TUESDAY, local
from apps.businesses.factories import BusinessFactory, ServiceFactory, open_all_week
from apps.businesses.tests.provider_helpers import P, call, customer_headers, owner_headers
from apps.notifications import feed, preferences, services
from apps.notifications.adapters import FakeSMS, SMSDeliveryError
from apps.notifications.models import Channel, Notification, NotificationPreference
from apps.reviews.tests.test_reviews import completed_appointment, submit

pytestmark = pytest.mark.django_db
PREFS = f"{P}/notification-preferences"
FEED = f"{P}/notifications"
PAYLOAD = {
    "customer_name": "Sana",
    "business_name": "Glow",
    "staff_name": "Ayesha",
    "services": "Haircut",
    "date": "Tue 03 Nov",
    "time": "12:00",
    "total_price": "PKR 2,000",
    "appointment_id": "abc",
    "late_note": "",
    "reason": "price",
    "status": "pending",
    "message": "",
    "note_suffix": "",
    "rating": "5",
}


def owner_with_contacts() -> tuple[User, dict[str, str]]:
    user = UserFactory(role=Role.BUSINESS_OWNER, phone="+923001112222", email="owner@example.com")
    return owner_headers(user)


def in_app(user: User) -> list[str]:
    rows = Notification.objects.filter(user=user, channel=Channel.IN_APP).order_by("id")
    return [n.template_key for n in rows]


# ------------------------------------------------------------ preferences endpoints


def test_preferences_auth_and_role() -> None:
    for method, body in (
        ("get", None),
        ("patch", {"preferences": [{"event": "new_review", "sms": True}]}),
    ):
        assert call(method, PREFS, {}, body).status_code == 401
        assert call(method, PREFS, {"HTTP_AUTHORIZATION": "Bearer x"}, body).status_code == 401
        assert call(method, PREFS, customer_headers(), body).status_code == 403


def test_defaults_list_every_owner_event_with_dashboard_always_on() -> None:
    _, h = owner_with_contacts()
    rows = call("get", PREFS, h).json()
    assert [r["event"] for r in rows] == list(preferences.OWNER_EVENTS)
    assert all(r["dashboard"] is True for r in rows)
    by = {r["event"]: r for r in rows}
    assert (by["new_booking"]["sms"], by["new_booking"]["email"]) == (False, True)
    assert (by["pending_escalation"]["sms"], by["pending_escalation"]["email"]) == (True, False)
    assert by["new_booking"]["label"] == "New booking"


def test_patch_updates_only_sent_fields_and_persists_per_owner() -> None:
    user, h = owner_with_contacts()
    _, other = owner_with_contacts_other()
    r = call(
        "patch",
        PREFS,
        h,
        {
            "preferences": [
                {"event": "new_booking", "sms": True},
                {"event": "new_review", "email": False},
            ]
        },
    )
    assert r.status_code == 200, r.content
    by = {x["event"]: x for x in r.json()}
    assert (by["new_booking"]["sms"], by["new_booking"]["email"]) == (True, True)  # email untouched
    assert (by["new_review"]["sms"], by["new_review"]["email"]) == (False, False)
    again = {x["event"]: x for x in call("get", PREFS, h).json()}
    assert again["new_booking"]["sms"] is True
    untouched = {x["event"]: x for x in call("get", PREFS, other).json()}
    assert untouched["new_booking"]["sms"] is False
    assert NotificationPreference.objects.filter(user=user).count() == 2


def owner_with_contacts_other() -> tuple[User, dict[str, str]]:
    return owner_headers(
        UserFactory(role=Role.BUSINESS_OWNER, phone="+923009998888", email="o2@example.com")
    )


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"preferences": []},
        {"preferences": [{"event": "nope", "sms": True}]},
        {"preferences": [{"event": "", "sms": True}]},
        {"preferences": [{"event": "new_review", "sms": "maybe"}]},
        {"preferences": [{"event": "new_review", "sms": True}] * 21},
    ],
)
def test_patch_validation(body: dict[str, Any]) -> None:
    user, h = owner_with_contacts()
    r = call("patch", PREFS, h, body)
    assert r.status_code == 422, r.content
    assert not NotificationPreference.objects.filter(user=user).exists()


def test_dashboard_cannot_be_turned_off() -> None:
    _, h = owner_with_contacts()
    r = call("patch", PREFS, h, {"preferences": [{"event": "new_review", "dashboard": False}]})
    assert r.status_code == 200
    assert {x["event"]: x for x in r.json()}["new_review"]["dashboard"] is True


# ------------------------------------------------------------ preferences drive delivery


def test_owner_channels_follow_preferences_and_in_app_is_always_sent(run_commit) -> None:  # type: ignore[no-untyped-def]
    user, h = owner_with_contacts()
    call("patch", PREFS, h, {"preferences": [{"event": "new_booking", "sms": True, "email": True}]})
    with run_commit():
        services.notify(user.pk, "new_booking", PAYLOAD)
    channels = sorted(Notification.objects.filter(user=user).values_list("channel", flat=True))
    assert channels == ["email", "in_app", "sms"]
    assert [m.to for m in FakeSMS.outbox] == ["+923001112222"]
    Notification.objects.all().delete()
    FakeSMS.reset()
    call(
        "patch", PREFS, h, {"preferences": [{"event": "new_booking", "sms": False, "email": False}]}
    )
    with run_commit():
        services.notify(user.pk, "new_booking", PAYLOAD)
    assert list(Notification.objects.filter(user=user).values_list("channel", flat=True)) == [
        "in_app"
    ]
    assert FakeSMS.outbox == []


def test_missing_contact_skips_that_channel() -> None:
    user = UserFactory(role=Role.BUSINESS_OWNER, phone=None, email="only@example.com")
    NotificationPreference.objects.create(user=user, event_key="new_booking", sms=True, email=True)
    created = services.notify(user.pk, "new_booking", PAYLOAD)
    assert sorted(n.channel for n in created) == ["email", "in_app"]


def test_transactional_customer_messages_ignore_owner_preferences(run_commit) -> None:  # type: ignore[no-untyped-def]
    user, h = owner_with_contacts()
    for event in preferences.OWNER_EVENTS:  # switch every owner channel off
        NotificationPreference.objects.create(user=user, event_key=event, sms=False, email=False)
    with run_commit():
        services.notify(user.pk, "booking_confirmed", PAYLOAD)  # the owner booking as a customer
    assert "sms" in Notification.objects.filter(user=user).values_list("channel", flat=True)
    assert len(FakeSMS.outbox) == 1


# ------------------------------------------------------------ in-app feed


def feed_items(h: dict[str, str], qs: str = "") -> dict[str, Any]:
    r = call("get", f"{FEED}{qs}", h)
    assert r.status_code == 200, r.content
    return dict(r.json())


def test_feed_auth_and_role() -> None:
    for method, url, body in (("get", FEED, None), ("post", f"{FEED}/read", {"all": True})):
        assert call(method, url, {}, body).status_code == 401
        assert call(method, url, {"HTTP_AUTHORIZATION": "Bearer x"}, body).status_code == 401
        assert call(method, url, customer_headers(), body).status_code == 403


def test_feed_lists_own_owner_events_newest_first_with_rendered_text() -> None:
    user, h = owner_with_contacts()
    services.notify(user.pk, "new_booking", PAYLOAD)
    services.notify(user.pk, "new_review", {**PAYLOAD, "customer_name": "Ayesha K."})
    services.notify(user.pk, "booking_confirmed", PAYLOAD)  # customer-facing: not in the owner feed
    other, _ = owner_with_contacts_other()
    services.notify(other.pk, "new_booking", PAYLOAD)
    data = feed_items(h)
    assert [i["event"] for i in data["items"]] == ["new_review", "new_booking"]
    assert data["unread_count"] == 2 and data["next_cursor"] is None
    first = data["items"][0]
    assert (
        first["title"] == "New review" and "Ayesha K." in first["body"] and first["read_at"] is None
    )


def test_feed_pagination_unread_filter_and_mark_read() -> None:
    user, h = owner_with_contacts()
    for _ in range(5):
        services.notify(user.pk, "new_booking", PAYLOAD)
    first = feed_items(h, "?limit=2")
    assert len(first["items"]) == 2 and first["next_cursor"]
    seen = [i["id"] for i in first["items"]]
    cursor = first["next_cursor"]
    while cursor:
        page = feed_items(h, f"?limit=2&cursor={cursor}")
        seen += [i["id"] for i in page["items"]]
        cursor = page["next_cursor"]
    assert len(seen) == 5 and seen == sorted(seen, reverse=True) and len(set(seen)) == 5
    r = call("post", f"{FEED}/read", h, {"ids": seen[:2]})
    assert r.json() == {"updated": 2, "unread_count": 3}
    assert len(feed_items(h, "?unread_only=true")["items"]) == 3
    assert call("post", f"{FEED}/read", h, {"ids": seen[:2]}).json()["updated"] == 0  # idempotent
    r = call("post", f"{FEED}/read", h, {"all": True})
    assert r.json() == {"updated": 3, "unread_count": 0}
    assert feed_items(h, "?unread_only=true")["items"] == []
    assert Notification.objects.filter(user=user, read_at__isnull=False).count() == 5


def test_mark_read_ignores_other_owners_items() -> None:
    user, h = owner_with_contacts()
    other, other_h = owner_with_contacts_other()
    mine = services.notify(user.pk, "new_booking", PAYLOAD)[-1]
    theirs = services.notify(other.pk, "new_booking", PAYLOAD)[-1]
    r = call("post", f"{FEED}/read", h, {"ids": [mine.pk, theirs.pk, 99999]})
    assert r.json()["updated"] == 1
    theirs.refresh_from_db()
    assert theirs.read_at is None and feed_items(other_h)["unread_count"] == 1


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"ids": [1], "all": True},
        {"ids": None},
        {"ids": list(range(201))},
        {"ids": ["x"]},
        {"all": "later"},
    ],
)
def test_mark_read_validation(body: dict[str, Any]) -> None:
    _, h = owner_with_contacts()
    assert call("post", f"{FEED}/read", h, body).status_code == 422


def test_feed_validation_and_query_bound(django_assert_max_num_queries) -> None:  # type: ignore[no-untyped-def]
    user, h = owner_with_contacts()
    for _ in range(12):
        services.notify(user.pk, "new_booking", PAYLOAD)
    assert call("get", f"{FEED}?limit=0", h).status_code == 422
    assert call("get", f"{FEED}?limit=101", h).status_code == 422
    assert call("get", f"{FEED}?cursor=junk", h).status_code == 400
    with django_assert_max_num_queries(8):
        r = call("get", f"{FEED}?limit=50", h)
    assert r.status_code == 200 and len(r.json()["items"]) == 12


def test_feed_survives_a_template_that_cannot_render() -> None:
    user, h = owner_with_contacts()
    Notification.objects.create(user=user, channel="in_app", template_key="new_booking", payload={})
    item = feed_items(h)["items"][0]
    assert item["title"] == "New booking" and item["body"] == ""


# ------------------------------------------------------------ right template to the right party


def booking(owner: User, status: str = "confirmed", customer: User | None = None) -> Any:
    business = BusinessFactory(owner=owner, lead_time_minutes=0)
    open_all_week(business)
    svc = ServiceFactory(business=business, duration_minutes=60)
    a = AppointmentFactory(
        business=business,
        customer=customer or make_customer(name="Sana Khan", phone="+923331234567"),
        status=status,
        scheduled_start=local(TUESDAY, 12),
        scheduled_end=local(TUESDAY, 13),
        total_price=svc.price,
    )
    AppointmentService.objects.create(
        appointment=a,
        service=svc,
        name=svc.name,
        price_at_booking=svc.price,
        duration_at_booking=60,
    )
    return a


def parties(a: Any) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {"customer": [], "owner": []}
    rows = Notification.objects.filter(channel=Channel.IN_APP).order_by("id")
    for n in rows:
        if n.user_id == a.customer_id:
            out["customer"].append(n.template_key)
        elif n.user_id == a.business.owner_id:
            out["owner"].append(n.template_key)
    return out


@pytest.mark.parametrize(
    "event,status,kwargs,expected",
    [
        ("booking_created", "confirmed", {}, {"customer": [], "owner": ["new_booking"]}),
        (
            "booking_created",
            "pending",
            {},
            {"customer": ["booking_received"], "owner": ["new_booking"]},
        ),
        ("booking_created", "confirmed", {"source": "manual"}, {"customer": [], "owner": []}),
        ("booking_confirmed", "confirmed", {}, {"customer": ["booking_confirmed"], "owner": []}),
        (
            "booking_cancelled",
            "cancelled_by_business",
            {"cancelled_by": "business"},
            {"customer": ["booking_cancelled_by_business"], "owner": []},
        ),
        (
            "booking_cancelled",
            "cancelled_by_customer",
            {"cancelled_by": "customer"},
            {"customer": [], "owner": ["booking_cancelled_by_customer"]},
        ),
        (
            "booking_rescheduled",
            "confirmed",
            {},
            {"customer": ["booking_rescheduled"], "owner": ["provider_booking_rescheduled"]},
        ),
        (
            "booking_rescheduled",
            "confirmed",
            {"by": "business"},
            {"customer": ["booking_rescheduled_by_business"], "owner": []},
        ),
        (
            "booking_rescheduled",
            "confirmed",
            {"by": "business", "accepted_request": True},
            {"customer": ["reschedule_request_accepted"], "owner": []},
        ),
        (
            "booking_reschedule_requested",
            "confirmed",
            {},
            {"customer": [], "owner": ["provider_reschedule_requested"]},
        ),
        (
            "booking_reschedule_declined",
            "confirmed",
            {},
            {"customer": ["reschedule_request_declined"], "owner": []},
        ),
        (
            "booking_reassigned",
            "confirmed",
            {"old_staff_id": 1},
            {"customer": ["booking_reassigned"], "owner": []},
        ),
        ("booking_completed", "completed", {}, {"customer": ["review_prompt"], "owner": []}),
        ("booking_no_show", "no_show", {}, {"customer": [], "owner": []}),
    ],
)
def test_each_event_sends_exactly_the_right_template_to_the_right_party(
    event: str, status: str, kwargs: dict[str, Any], expected: dict[str, list[str]], run_commit
) -> None:  # type: ignore[no-untyped-def]
    owner = UserFactory(role=Role.BUSINESS_OWNER, phone="+923001112222", email="owner@example.com")
    a = booking(owner, status=status)
    with run_commit():
        events.emit(getattr(events, event), a.pk, **kwargs)
    assert parties(a) == expected


def test_business_cancellation_text_includes_reason(run_commit) -> None:  # type: ignore[no-untyped-def]
    owner = UserFactory(role=Role.BUSINESS_OWNER)
    a = booking(owner, status="confirmed")
    with run_commit():
        booking_services.cancel_by_business(
            a.pk, booking_services.Actor(owner.pk, "business_owner"), "Power outage"
        )
    body = Notification.objects.get(
        user=a.customer, template_key="booking_cancelled_by_business", channel="in_app"
    )
    _title, text = feed.title_and_body(body)
    assert "Reason: Power outage." in text
    assert any("Reason: Power outage." in m.body for m in FakeSMS.outbox)


def test_customer_cancellation_notifies_owner_only(run_commit) -> None:  # type: ignore[no-untyped-def]
    owner = UserFactory(role=Role.BUSINESS_OWNER, phone="+923001112222", email="o@example.com")
    a = booking(owner, status="confirmed")
    with run_commit():
        booking_services.cancel_by_customer(
            a.customer_id, str(a.public_id), "price", now=NOW - timedelta(days=3)
        )
    assert parties(a) == {"customer": [], "owner": ["booking_cancelled_by_customer"]}


def test_new_review_notifies_the_owner_with_rating(run_commit) -> None:  # type: ignore[no-untyped-def]
    owner = UserFactory(role=Role.BUSINESS_OWNER, phone="+923001112222", email="o@example.com")
    business = BusinessFactory(owner=owner, name="Glow Studio")
    appt, customer, h = completed_appointment(business=business)
    with run_commit():
        assert submit(appt, h, {"rating": 4, "comment": "Nice"}).status_code == 201
    n = Notification.objects.get(user=owner, template_key="new_review", channel="in_app")
    title, body = feed.title_and_body(n)
    assert title == "New review" and "4-star" in body and "Glow Studio" in body
    assert not Notification.objects.filter(user=customer, template_key="new_review").exists()


def test_no_notifications_when_the_transaction_rolls_back(run_commit) -> None:  # type: ignore[no-untyped-def]
    owner = UserFactory(role=Role.BUSINESS_OWNER)
    a = booking(owner, status="confirmed")
    with run_commit(), pytest.raises(RuntimeError), transaction.atomic():
        booking_services.cancel_by_business(
            a.pk, booking_services.Actor(owner.pk, "business_owner"), "x"
        )
        raise RuntimeError("abort")
    assert not Notification.objects.exists() and FakeSMS.outbox == []


# ------------------------------------------------------------ retries, fallback, admin flag


def queued_owner_notification(
    user: User, key: str = "pending_escalation", channel: str = "sms"
) -> Notification:
    return Notification.objects.create(
        user=user,
        recipient=user.phone or user.email or "",
        channel=channel,
        template_key=key,
        payload=PAYLOAD,
    )


def test_owner_sms_failure_retries_then_falls_back_to_email() -> None:
    user = UserFactory(role=Role.BUSINESS_OWNER, phone="+923001112222", email="o@example.com")
    n = queued_owner_notification(user)
    FakeSMS.fail_with = SMSDeliveryError("gateway down")
    outcomes = [services.attempt_delivery(n.pk) for _ in range(services.MAX_ATTEMPTS)]
    assert outcomes == [services.RETRY] * (services.MAX_ATTEMPTS - 1) + [services.FALLBACK]
    n.refresh_from_db()
    assert (
        n.status == "failed" and "fell back to email" in n.last_error and n.admin_flagged is False
    )
    fallback = Notification.objects.get(fallback_of=n)
    assert fallback.channel == "email" and fallback.recipient == "o@example.com"
    assert services.attempt_delivery(fallback.pk) == services.SENT


def test_failure_without_fallback_is_flagged_for_admin() -> None:
    user = UserFactory(role=Role.BUSINESS_OWNER, phone="+923001112222", email=None)
    n = queued_owner_notification(user)
    FakeSMS.fail_with = SMSDeliveryError("rejected", permanent=True)
    assert services.attempt_delivery(n.pk) == services.FAILED
    n.refresh_from_db()
    assert n.status == "failed" and n.admin_flagged is True
    assert list(feed.failed_for_admin()) == [n]
    ok = queued_owner_notification(user, channel="in_app")
    services.attempt_delivery(ok.pk)
    assert list(feed.failed_for_admin()) == [n]


def test_templates_are_data_editable_without_deploy() -> None:
    from apps.notifications.models import NotificationTemplate

    NotificationTemplate.objects.filter(key="new_review", channel="in_app").update(
        subject="Fresh feedback", body="{customer_name} rated you {rating}."
    )
    user, h = owner_with_contacts()
    services.notify(user.pk, "new_review", PAYLOAD)
    item = feed_items(h)["items"][0]
    assert item["title"] == "Fresh feedback" and item["body"] == "Sana rated you 5."


def test_unused_imports_guard() -> None:
    assert login_client and bearer and Client


# ------------------------------------------------------------ data migration


def test_template_migration_renames_old_keys_and_adds_new_ones() -> None:
    import importlib

    from django.apps import apps as django_apps

    from apps.notifications.models import NotificationTemplate

    migration = importlib.import_module("apps.notifications.migrations.0004_provider_templates")
    renames = migration.RENAMES
    NotificationTemplate.objects.filter(key__in=[*renames.values(), "new_review"]).delete()
    for old in renames:
        NotificationTemplate.objects.create(key=old, channel="sms", body="old {business_name}")
    Notification.objects.create(channel="sms", template_key="provider_booking_created")
    migration.forward(django_apps, None)
    keys = set(NotificationTemplate.objects.values_list("key", flat=True))
    assert set(renames.values()) <= keys and not set(renames) & keys
    assert Notification.objects.filter(template_key="new_booking").count() == 1
    assert NotificationTemplate.objects.filter(key="new_review", channel="in_app").exists()
    assert (
        "{note_suffix}"
        in NotificationTemplate.objects.get(key="booking_cancelled_by_business", channel="sms").body
    )
    migration.forward(django_apps, None)  # running it again changes nothing
    assert NotificationTemplate.objects.filter(key="new_review", channel="email").count() == 1
    migration.backward(django_apps, None)
    assert NotificationTemplate.objects.filter(key="provider_booking_created").exists()
    assert Notification.objects.filter(template_key="provider_booking_created").count() == 1
