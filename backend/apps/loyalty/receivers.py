"""Credit points from domain events. Points are only ever earned on completion, never creation."""

import logging
from collections.abc import Callable
from typing import Any

from django.conf import settings
from django.dispatch import Signal

from apps.accounts import events as account_events
from apps.accounts import selectors as accounts
from apps.bookings import events as booking_events
from apps.bookings import selectors as bookings
from apps.reviews import events as review_events

from . import services

logger = logging.getLogger(__name__)


def _safe(handler: Callable[..., None]) -> Callable[..., None]:
    def wrapper(sender: Any, **kwargs: Any) -> None:
        try:
            handler(**{k: v for k, v in kwargs.items() if k != "signal"})
        except Exception:
            logger.exception("Loyalty handler failed")
            if settings.LOYALTY_RAISE_ON_ERROR:
                raise

    return wrapper


def on_booking_completed(appointment_id: int, **_: Any) -> None:
    brief = bookings.appointment_brief(appointment_id)
    if brief is None or brief.customer_id is None:
        return  # walk-in without an account earns no points
    services.credit_booking_completed(
        customer_id=brief.customer_id,
        appointment_id=appointment_id,
        amount=brief.total_price,
        completed_count=bookings.completed_count(brief.customer_id),
    )


def on_booking_payment_changed(appointment_id: int, **_: Any) -> None:
    brief = bookings.appointment_brief(appointment_id)
    if brief is not None and brief.payment_status == "refunded":
        services.reverse_points("appointment", appointment_id)


def on_review_created(review_id: int, customer_id: int, **_: Any) -> None:
    services.credit(
        customer_id=customer_id,
        action=services.REVIEW,
        reference_type="review",
        reference_id=review_id,
    )


def on_profile_updated(user_id: int, **_: Any) -> None:
    if accounts.profile_is_complete(user_id):
        services.credit(
            customer_id=user_id,
            action=services.PROFILE_COMPLETION,
            reference_type="user",
            reference_id=user_id,
        )


def connect() -> None:
    wiring: list[tuple[Signal, Callable[..., None]]] = [
        (booking_events.booking_completed, on_booking_completed),
        (booking_events.booking_payment_changed, on_booking_payment_changed),
        (review_events.review_created, on_review_created),
        (account_events.profile_updated, on_profile_updated),
    ]
    for signal, handler in wiring:
        signal.connect(_safe(handler), weak=False, dispatch_uid=f"loyalty-{handler.__name__}")
