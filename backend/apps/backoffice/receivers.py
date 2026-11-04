"""Turn business events from Plan 6.2 into review-queue items (idempotent)."""

import logging
from typing import Any

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.bookings import adminops as bookings
from apps.bookings import events as booking_events
from apps.businesses import events as business_events
from apps.notifications import adminops as notifications
from apps.notifications import events as notification_events
from apps.payments import events as payment_events
from apps.reviews import events as review_events

from . import config_service
from .models import ReviewKind, ReviewQueueItem, TicketPriority, TicketType
from .services import tickets

logger = logging.getLogger(__name__)


def enqueue(business_id: int, kind: str) -> ReviewQueueItem | None:
    """Create the open item for (business, kind); a duplicate delivery changes nothing."""
    try:
        with transaction.atomic():
            return ReviewQueueItem.objects.create(
                business_id=business_id, kind=kind, submitted_at=timezone.now()
            )
    except IntegrityError:
        return None  # an open item already exists


def on_submitted(sender: Any, business_id: int, **kwargs: Any) -> None:
    enqueue(business_id, ReviewKind.NEW_LISTING)


def on_change_requested(sender: Any, business_id: int, **kwargs: Any) -> None:
    enqueue(business_id, ReviewKind.CHANGE_REQUEST)


def on_review_flagged(sender: Any, **kwargs: Any) -> None:
    tickets.process_review_flags()


def on_booking_cancelled(
    sender: Any, appointment_id: int, cancelled_by: str = "", is_late: bool = False, **kw: Any
) -> None:
    """Open a dispute ticket for late customer cancellations when configured."""
    if not (
        cancelled_by == "customer"
        and is_late
        and config_service.get("auto_ticket_late_cancellations")
    ):
        return
    refs = bookings.appointment_refs(appointment_id)
    if refs is None:
        return
    business_id, customer_id = refs
    tickets.system_ticket(
        type=TicketType.BOOKING_DISPUTE,
        subject="Late cancellation",
        body="The customer cancelled inside the business's cancellation window.",
        source_key=f"late_cancel:{appointment_id}",
        related_user_id=customer_id,
        appointment_id=appointment_id,
        business_id=business_id,
    )


def on_notification_failed(sender: Any, notification_id: int, **kw: Any) -> None:
    info = notifications.failure_summary(notification_id)
    if not info:
        return
    tickets.system_ticket(
        type=TicketType.FAILED_NOTIFICATION,
        subject=f"Notification {info['template_key']} could not be delivered",
        body=f"Channel {info['channel']}, {info['attempts']} attempts. Last error: {info['error']}",
        source_key=f"notification_failed:{notification_id}",
        related_user_id=info["user_id"] if isinstance(info["user_id"], int) else None,
    )


def on_webhook_failed(sender: Any, provider: str, reason: str, **kw: Any) -> None:
    """One ticket per provider and reason per hour, however many requests fail."""
    hour = timezone.now().strftime("%Y%m%d%H")
    tickets.system_ticket(
        type=TicketType.OTHER,
        subject=f"Payment webhook failure ({provider})",
        body=f"A webhook from {provider} was rejected: {reason}.",
        source_key=f"payment_webhook:{provider}:{reason}:{hour}",
        priority=TicketPriority.HIGH,
    )


def connect() -> None:
    review_events.review_flagged.connect(
        on_review_flagged, weak=False, dispatch_uid="backoffice-on-review-flagged"
    )
    booking_events.booking_cancelled.connect(
        on_booking_cancelled, weak=False, dispatch_uid="backoffice-on-booking-cancelled"
    )
    notification_events.notification_failed.connect(
        on_notification_failed, weak=False, dispatch_uid="backoffice-on-notification-failed"
    )
    payment_events.webhook_failed.connect(
        on_webhook_failed, weak=False, dispatch_uid="backoffice-on-webhook-failed"
    )
    business_events.business_submitted.connect(
        on_submitted, weak=False, dispatch_uid="backoffice-on-submitted"
    )
    business_events.business_change_review_requested.connect(
        on_change_requested, weak=False, dispatch_uid="backoffice-on-change-requested"
    )
