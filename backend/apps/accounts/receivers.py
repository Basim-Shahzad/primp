"""React to booking events that concern a customer's account."""

import logging
from typing import Any

from django.conf import settings
from django.db.models import F

from apps.bookings import events as booking_events
from apps.bookings import selectors as bookings

from .models import CustomerProfile

logger = logging.getLogger(__name__)


def on_no_show(sender: Any, appointment_id: int, **kwargs: Any) -> None:
    """Count the no-show on the customer's profile (walk-ins without an account have none)."""
    try:
        brief = bookings.appointment_brief(appointment_id)
        if brief is None or brief.customer_id is None:
            return
        CustomerProfile.objects.filter(user_id=brief.customer_id).update(
            no_show_count=F("no_show_count") + 1
        )
    except Exception:
        logger.exception("No-show counter failed", extra={"appointment_id": appointment_id})
        if settings.NOTIFICATIONS_RAISE_ON_RENDER_ERROR:
            raise


def connect() -> None:
    booking_events.booking_no_show.connect(
        on_no_show, weak=False, dispatch_uid="accounts-on-no-show"
    )
