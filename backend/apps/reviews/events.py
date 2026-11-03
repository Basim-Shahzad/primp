"""Domain events for reviews (sent after commit)."""

from django.db import transaction
from django.dispatch import Signal

review_created = Signal()  # sends review_id, customer_id, business_id, appointment_id, has_photos
review_flagged = Signal()  # sends review_id, reporter_id, reason


def emit(signal: Signal, **kwargs: object) -> None:
    transaction.on_commit(lambda: signal.send(sender=None, **kwargs))
