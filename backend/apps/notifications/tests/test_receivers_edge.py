"""Failure paths of the event receivers: a notification problem must never break a booking."""

from unittest import mock

import pytest

from apps.accounts import receivers as account_receivers
from apps.accounts.factories import make_customer
from apps.core.private_storage import PrivateFileSystemStorage
from apps.notifications import feed, receivers
from apps.notifications.models import Notification

pytestmark = pytest.mark.django_db
MISSING = 987654321


@pytest.mark.parametrize(
    "handler",
    [
        receivers.on_created,
        receivers.on_confirmed,
        receivers.on_cancelled,
        receivers.on_rescheduled,
        receivers.on_reschedule_requested,
        receivers.on_reschedule_declined,
        receivers.on_reassigned,
        receivers.on_completed,
    ],
)
def test_handlers_ignore_a_vanished_appointment(handler) -> None:  # type: ignore[no-untyped-def]
    handler(MISSING)
    assert not Notification.objects.exists()


def test_review_handler_ignores_vanished_review_or_business() -> None:
    receivers.on_review_created(review_id=MISSING, business_id=MISSING, customer_id=MISSING)
    assert not Notification.objects.exists()


def test_safe_wrappers_swallow_or_raise_by_setting(settings) -> None:  # type: ignore[no-untyped-def]
    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("template exploded")

    for wrap, call_args in (
        (receivers._safe, (None, 1)),
        (receivers._safe_kwargs, (None,)),
    ):
        settings.NOTIFICATIONS_RAISE_ON_RENDER_ERROR = False
        wrap(boom)(*call_args)  # logged, not raised
        settings.NOTIFICATIONS_RAISE_ON_RENDER_ERROR = True
        with pytest.raises(RuntimeError):
            wrap(boom)(*call_args)


def test_no_show_counter_ignores_missing_and_logs_failures(settings) -> None:  # type: ignore[no-untyped-def]
    account_receivers.on_no_show(None, MISSING)  # unknown appointment: nothing happens
    user = make_customer()
    settings.NOTIFICATIONS_RAISE_ON_RENDER_ERROR = False
    with mock.patch(
        "apps.accounts.receivers.bookings.appointment_brief", side_effect=RuntimeError("db")
    ):
        account_receivers.on_no_show(None, 1)
        settings.NOTIFICATIONS_RAISE_ON_RENDER_ERROR = True
        with pytest.raises(RuntimeError):
            account_receivers.on_no_show(None, 1)
    assert user.profile.no_show_count == 0


def test_private_filesystem_storage_never_exposes_a_url(tmp_path) -> None:  # type: ignore[no-untyped-def]
    storage = PrivateFileSystemStorage(location=str(tmp_path), base_url="https://cdn.example/")
    with pytest.raises(ValueError, match="signed download link"):
        storage.url("documents/a.png")


def test_feed_title_falls_back_when_the_template_is_gone() -> None:
    from apps.accounts.factories import UserFactory
    from apps.accounts.roles import Role

    user = UserFactory(role=Role.BUSINESS_OWNER)
    n = Notification.objects.create(
        user=user, channel="in_app", template_key="new_booking", payload={}
    )
    assert feed.title_and_body(n) == ("New booking", "")
    n.template_key = "no_such_template"
    assert feed.title_and_body(n) == ("No such template", "")
    assert feed.render_items([n]) == [("No such template", "")]
