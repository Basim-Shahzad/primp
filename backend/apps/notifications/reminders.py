import logging
from datetime import datetime

from apps.bookings import selectors as bookings

from . import services

logger = logging.getLogger(__name__)


def scan(now: datetime) -> int:
    """Send every reminder that is due. Safe to run repeatedly and concurrently."""
    sent = 0
    for kind, offset in services.reminder_offsets().items():
        for appointment_id in bookings.due_for_reminder(offset, now, services.reminder_grace()):
            if not services.claim_reminder(appointment_id, kind):
                continue
            brief = bookings.appointment_brief(appointment_id)
            if brief is None or brief.status != "confirmed":
                continue  # cancelled between the scan query and the claim
            services.notify(brief.customer_id, kind, brief.payload())
            sent += 1
    return sent
