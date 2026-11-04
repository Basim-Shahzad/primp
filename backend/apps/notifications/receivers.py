"""Turn booking domain events into notifications."""

import logging
from collections.abc import Callable
from typing import Any

from django.conf import settings

from apps.accounts import selectors as accounts
from apps.bookings import events
from apps.bookings import selectors as bookings
from apps.businesses import selectors as biz
from apps.reviews import events as review_events
from apps.reviews import selectors as reviews

from . import services

logger = logging.getLogger(__name__)


def _safe(handler: Callable[..., None]) -> Callable[..., None]:
    """A notification problem must never break a booking."""

    def wrapper(sender: Any, appointment_id: int, **kwargs: Any) -> None:
        try:
            handler(appointment_id, **kwargs)
        except Exception:
            logger.exception(
                "Notification handler failed", extra={"appointment_id": appointment_id}
            )
            if settings.NOTIFICATIONS_RAISE_ON_RENDER_ERROR:
                raise

    return wrapper


def _safe_kwargs(handler: Callable[..., None]) -> Callable[..., None]:
    """Like `_safe` for events that do not carry an appointment id (reviews)."""

    def wrapper(sender: Any, **kwargs: Any) -> None:
        kwargs.pop("signal", None)
        try:
            handler(**kwargs)
        except Exception:
            logger.exception("Notification handler failed", extra={"handler": handler.__name__})
            if settings.NOTIFICATIONS_RAISE_ON_RENDER_ERROR:
                raise

    return wrapper


def _to_customer(brief: bookings.AppointmentBrief, key: str) -> None:
    """Notify the customer; walk-in bookings without an account have nobody to tell."""
    if brief.customer_id is not None:
        services.notify(brief.customer_id, key, brief.payload())


def _brief(appointment_id: int) -> bookings.AppointmentBrief | None:
    return bookings.appointment_brief(appointment_id)


def on_created(appointment_id: int, source: str = "online", **_: Any) -> None:
    brief = _brief(appointment_id)
    if brief is None:
        return
    if brief.status == "pending":
        _to_customer(brief, "booking_received")
    if source == "manual":
        return  # the owner entered it themselves; no alert needed
    services.notify(brief.owner_id, "new_booking", brief.payload(), include_sms=False)


def on_confirmed(appointment_id: int, **_: Any) -> None:
    brief = _brief(appointment_id)
    if brief:
        _to_customer(brief, "booking_confirmed")


def on_cancelled(appointment_id: int, cancelled_by: str = "", **_: Any) -> None:
    brief = _brief(appointment_id)
    if brief is None:
        return
    if cancelled_by == "customer":
        services.notify(
            brief.owner_id, "booking_cancelled_by_customer", brief.payload(), include_sms=False
        )
    else:
        _to_customer(brief, "booking_cancelled_by_business")


def on_rescheduled(
    appointment_id: int, by: str = "customer", accepted_request: bool = False, **_: Any
) -> None:
    brief = _brief(appointment_id)
    if brief is None:
        return
    if by == "business":  # the owner already knows about their own moves
        key = (
            "reschedule_request_accepted" if accepted_request else "booking_rescheduled_by_business"
        )
        _to_customer(brief, key)
        return
    _to_customer(brief, "booking_rescheduled")
    services.notify(
        brief.owner_id, "provider_booking_rescheduled", brief.payload(), include_sms=False
    )


def on_review_created(review_id: int, business_id: int, customer_id: int, **_: Any) -> None:
    review = reviews.review_brief(review_id)
    owner = biz.owner_and_name(business_id)
    if review is None or owner is None:
        return
    names = accounts.display_names([customer_id])
    services.notify(
        owner.owner_id,
        "new_review",
        {
            "customer_name": names.get(customer_id, "A customer"),
            "rating": str(review.rating),
            "business_name": owner.name,
        },
        include_sms=False,
    )


def on_reschedule_requested(appointment_id: int, **_: Any) -> None:
    brief = _brief(appointment_id)
    if brief:
        services.notify(
            brief.owner_id, "provider_reschedule_requested", brief.payload(), include_sms=False
        )


def on_reschedule_declined(appointment_id: int, **_: Any) -> None:
    brief = _brief(appointment_id)
    if brief:
        _to_customer(brief, "reschedule_request_declined")


def on_reassigned(appointment_id: int, **_: Any) -> None:
    brief = _brief(appointment_id)
    if brief:
        _to_customer(brief, "booking_reassigned")


def on_completed(appointment_id: int, **_: Any) -> None:
    brief = _brief(appointment_id)
    if brief:
        _to_customer(brief, "review_prompt")


def connect() -> None:
    wiring = [
        (events.booking_created, on_created),
        (events.booking_confirmed, on_confirmed),
        (events.booking_cancelled, on_cancelled),
        (events.booking_rescheduled, on_rescheduled),
        (events.booking_reschedule_requested, on_reschedule_requested),
        (events.booking_reschedule_declined, on_reschedule_declined),
        (events.booking_completed, on_completed),
        (events.booking_reassigned, on_reassigned),
    ]
    for signal, handler in wiring:
        signal.connect(_safe(handler), weak=False, dispatch_uid=f"notifications-{handler.__name__}")
    review_events.review_created.connect(
        _safe_kwargs(on_review_created),
        weak=False,
        dispatch_uid="notifications-on_review_created",
    )
