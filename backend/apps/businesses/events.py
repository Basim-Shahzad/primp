"""Business-level domain events (sent after commit). Plan 6.3 consumes them."""

from django.db import transaction
from django.dispatch import Signal

# every signal sends `business_id` (database id)
business_submitted = Signal()
business_change_review_requested = Signal()  # also sends `changes` (field -> new value)
business_verification_changed = Signal()  # also sends `status`


def emit(signal: Signal, business_id: int, **kwargs: object) -> None:
    transaction.on_commit(lambda: signal.send(sender=None, business_id=business_id, **kwargs))
