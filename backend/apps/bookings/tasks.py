from celery import shared_task
from django.utils import timezone


@shared_task(queue="default")
def escalate_pending_bookings() -> dict[str, int]:
    """Beat task (every 5 min): alert on and auto-cancel unanswered pending bookings."""
    from .provider_escalation import escalate_pending

    result = escalate_pending(timezone.now())
    return {"escalated": result.escalated, "cancelled": result.cancelled}


@shared_task(queue="bulk")
def rebuild_daily_stats(
    from_date: str | None = None, to_date: str | None = None, business_id: int | None = None
) -> int:
    """Beat task (nightly) and manual re-run: rebuild daily aggregates. Safe to repeat.

    Without dates: yesterday and today of every business. Dates are ISO (YYYY-MM-DD).
    """
    from datetime import date

    from . import analytics

    start = date.fromisoformat(from_date) if from_date else None
    end = date.fromisoformat(to_date) if to_date else None
    if business_id is not None:
        if start is None or end is None:
            raise ValueError("from_date and to_date are required with business_id")
        return analytics.recompute_range(business_id, start, end)
    return analytics.recompute_all(start, end)
