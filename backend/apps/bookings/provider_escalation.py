"""Pending (manual-accept) bookings: alert the owner, then auto-cancel when nobody answers."""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from apps.core.errors import AppError
from apps.notifications import services as notifications

from . import selectors, services
from .models import Appointment, AppointmentStatus
from .services import Actor

logger = logging.getLogger(__name__)

ESCALATION_KIND = "pending_escalation"
AUTO_CANCEL_NOTE = "No response from the business"
SYSTEM = Actor(None, "system")


@dataclass(frozen=True)
class EscalationResult:
    escalated: int
    cancelled: int


def escalate_pending(now: datetime | None = None) -> EscalationResult:
    """Idempotent: each pending booking is escalated at most once and cancelled at most once.

    - After PENDING_ESCALATION_MINUTES without an answer the owner gets a `pending_escalation`
      alert (claim row guarantees a single alert).
    - At PENDING_AUTO_CANCEL_MINUTES after creation, or at its start time if that comes first,
      the booking is cancelled by the system and the customer is told.
    """
    now = now or timezone.now()
    escalate_after = timedelta(minutes=int(settings.PENDING_ESCALATION_MINUTES))
    cancel_after = timedelta(minutes=int(settings.PENDING_AUTO_CANCEL_MINUTES))
    pending = Appointment.objects.filter(status=AppointmentStatus.PENDING)

    cancelled = 0
    due_cancel = pending.filter(Q(created_at__lte=now - cancel_after) | Q(scheduled_start__lte=now))
    for appointment_id in list(due_cancel.values_list("pk", flat=True)):
        try:
            services.cancel_by_business(appointment_id, SYSTEM, AUTO_CANCEL_NOTE)
            cancelled += 1
        except AppError:  # answered or cancelled in the meantime: nothing to do
            continue

    escalated = 0
    due_alert = pending.filter(created_at__lte=now - escalate_after, scheduled_start__gt=now)
    for appointment_id in list(due_alert.values_list("pk", flat=True)):
        if not notifications.claim_reminder(appointment_id, ESCALATION_KIND):
            continue
        brief = selectors.appointment_brief(appointment_id)
        if brief is None or brief.status != AppointmentStatus.PENDING:
            continue
        notifications.notify(brief.owner_id, ESCALATION_KIND, brief.payload())
        escalated += 1
    return EscalationResult(escalated, cancelled)
