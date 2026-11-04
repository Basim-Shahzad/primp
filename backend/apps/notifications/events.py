"""Notification domain events (sent after commit)."""

from django.db import transaction
from django.dispatch import Signal

notification_failed = Signal()  # sends notification_id: failed for good, flagged for admins


def emit_failed(notification_id: int) -> None:
    transaction.on_commit(
        lambda: notification_failed.send(sender=None, notification_id=notification_id)
    )
