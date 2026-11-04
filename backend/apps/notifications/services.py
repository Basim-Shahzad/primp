"""Notification dispatcher: pick channels, render, send, retry and fall back."""

import logging
from collections.abc import Iterable
from datetime import datetime
from typing import Any

from django.conf import settings
from django.core.mail import send_mail
from django.db import transaction
from django.utils import timezone

from apps.accounts import selectors as accounts

from .adapters import SMSDeliveryError, get_sms_adapter
from .models import Channel, Notification, NotificationStatus, Reminder
from .templating import MissingPlaceholder, TemplateNotFound, active_channels, render, render_text

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 4  # first try + 3 retries
BACKOFF_BASE_SECONDS = 30

SENT = "sent"
RETRY = "retry"
FAILED = "failed"
FALLBACK = "fallback"
SKIPPED = "skipped"

CRITICAL_KEYS = {
    "otp",
    "booking_received",
    "booking_confirmed",
    "booking_cancelled",
    "booking_rescheduled",
    "booking_reschedule_declined",
    "booking_reassigned",
}
BULK_KEYS = {"review_prompt"}


def queue_for(key: str) -> str:
    if key in CRITICAL_KEYS:
        return "critical"
    return "bulk" if key in BULK_KEYS else "default"


def backoff_seconds(retries: int) -> int:
    """Exponential backoff: 30 s, 60 s, 120 s."""
    return int(BACKOFF_BASE_SECONDS * (2**retries))


def _enqueue(notification: Notification) -> None:
    from .tasks import deliver_notification

    transaction.on_commit(
        lambda: deliver_notification.apply_async(
            args=(notification.pk,), queue=queue_for(notification.template_key)
        )
    )


def render_sms_body(key: str, payload: dict[str, Any]) -> str:
    """Body for SMS sent outside the log (OTP codes are never stored in the notification table)."""
    return render(key, Channel.SMS, payload)[1]


def notify(
    user_id: int,
    key: str,
    payload: dict[str, Any],
    *,
    include_sms: bool = True,
) -> list[Notification]:
    """Queue notifications for a user: one primary channel (SMS, else email) plus in-app."""
    contact = accounts.contact_for(user_id)
    if contact is None:
        return []
    available = active_channels(key)
    wanted: list[tuple[str, str]] = []
    if include_sms and Channel.SMS in available and contact.phone:
        wanted.append((Channel.SMS, contact.phone))
    elif Channel.EMAIL in available and contact.email:
        wanted.append((Channel.EMAIL, contact.email))
    if Channel.IN_APP in available:
        wanted.append((Channel.IN_APP, str(user_id)))
    created = []
    for channel, recipient in wanted:
        notification = Notification.objects.create(
            user_id=user_id,
            recipient=recipient,
            channel=channel,
            template_key=key,
            payload=payload,
        )
        _enqueue(notification)
        created.append(notification)
    return created


def _fail(notification: Notification, error: str) -> str:
    notification.status = NotificationStatus.FAILED
    notification.last_error = error[:1000]
    notification.save(update_fields=["status", "last_error", "attempts"])
    return FAILED


def _fallback_to_email(notification: Notification, error: str) -> str:
    contact = accounts.contact_for(notification.user_id) if notification.user_id else None
    if contact is None or not contact.email:
        return _fail(notification, f"{error}; no email address to fall back to")
    fallback = Notification.objects.create(
        user_id=notification.user_id,
        recipient=contact.email,
        channel=Channel.EMAIL,
        template_key=notification.template_key,
        language=notification.language,
        payload=notification.payload,
        fallback_of=notification,
    )
    _enqueue(fallback)
    notification.status = NotificationStatus.FAILED
    notification.last_error = f"{error}; fell back to email"[:1000]
    notification.save(update_fields=["status", "last_error", "attempts"])
    return FALLBACK


def _send(n: Notification, subject: str, body: str) -> str:
    if n.channel == Channel.IN_APP:
        n.status = NotificationStatus.DELIVERED
        n.sent_at = timezone.now()
        n.save(update_fields=["status", "sent_at", "attempts"])
        return SENT
    try:
        if n.channel == Channel.SMS:
            n.provider_message_id = get_sms_adapter().send(n.recipient, body)
        elif n.channel == Channel.EMAIL:
            send_mail(subject, body, None, [n.recipient])
        else:
            return _fail(n, f"unsupported channel {n.channel}")
    except SMSDeliveryError as exc:
        if exc.permanent or n.attempts >= MAX_ATTEMPTS:
            return _fallback_to_email(n, str(exc))
        n.last_error = str(exc)[:1000]
        n.save(update_fields=["last_error", "attempts"])
        return RETRY
    except Exception as exc:
        if n.attempts >= MAX_ATTEMPTS:
            return _fail(n, f"gave up after {n.attempts} attempts: {exc}")
        n.last_error = str(exc)[:1000]
        n.save(update_fields=["last_error", "attempts"])
        return RETRY
    n.status = NotificationStatus.SENT
    n.sent_at = timezone.now()
    n.last_error = ""
    n.save(update_fields=["status", "sent_at", "provider_message_id", "last_error", "attempts"])
    return SENT


def attempt_delivery(notification_id: int) -> str:
    """One delivery attempt. Returns SENT | RETRY | FAILED | FALLBACK | SKIPPED."""
    render_error: Exception | None = None
    with transaction.atomic():
        n = Notification.objects.select_for_update().filter(pk=notification_id).first()
        if n is None or n.status != NotificationStatus.QUEUED:
            return SKIPPED  # already sent/failed: running twice has no double effect
        n.attempts += 1
        try:
            subject, body = render(n.template_key, n.channel, n.payload, n.language)
        except (MissingPlaceholder, TemplateNotFound) as exc:
            render_error = exc
            outcome = _fail(n, f"render error: {exc}")
        else:
            outcome = _send(n, subject, body)
    if render_error is not None:
        logger.error("Notification template error", extra={"notification_id": notification_id})
        if settings.NOTIFICATIONS_RAISE_ON_RENDER_ERROR:
            raise render_error  # fail loudly in tests; the failure row is already committed
    return outcome


def claim_reminder(appointment_id: int, kind: str) -> bool:
    """True exactly once per (appointment, kind), even with concurrent scanners."""
    from django.db import IntegrityError

    try:
        with transaction.atomic():
            Reminder.objects.create(appointment_id=appointment_id, kind=kind)
    except IntegrityError:
        return False
    return True


def render_check(key: str, channels: Iterable[str], payload: dict[str, Any]) -> None:
    """Raise if any active template for the key cannot render with this payload."""
    for channel in channels:
        render(key, channel, payload)
    render_text("", {})


def reminder_offsets() -> dict[str, int]:
    return dict(settings.REMINDER_OFFSETS)


def reminder_grace() -> int:
    return int(settings.REMINDER_GRACE_SECONDS)


def now_utc() -> datetime:
    return timezone.now()


def queue_sms(to: str, body: str) -> None:
    """Fire-and-forget SMS (no log row), e.g. OTP codes that must never be stored."""
    from .tasks import send_sms_task

    transaction.on_commit(lambda: send_sms_task.delay(to, body))


def queue_email(to: str, subject: str, body: str) -> None:
    from .tasks import send_email_task

    transaction.on_commit(lambda: send_email_task.delay(to, subject, body))
