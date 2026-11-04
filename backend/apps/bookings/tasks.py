from celery import shared_task
from django.utils import timezone


@shared_task(queue="default")
def escalate_pending_bookings() -> dict[str, int]:
    """Beat task (every 5 min): alert on and auto-cancel unanswered pending bookings."""
    from .provider_escalation import escalate_pending

    result = escalate_pending(timezone.now())
    return {"escalated": result.escalated, "cancelled": result.cancelled}
