"""Domain events other apps subscribe to. They are sent after the transaction commits."""

from django.db import transaction
from django.dispatch import Signal

# every signal sends `appointment_id` (database id)
booking_created = Signal()
booking_confirmed = Signal()
booking_completed = Signal()
booking_cancelled = Signal()  # also sends `cancelled_by` ("customer"|"business")
booking_no_show = Signal()
booking_rescheduled = Signal()
booking_payment_changed = Signal()


def emit(signal: Signal, appointment_id: int, **kwargs: object) -> None:
    transaction.on_commit(lambda: signal.send(sender=None, appointment_id=appointment_id, **kwargs))
