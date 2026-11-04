"""Keep today's numbers fresh: recompute the touched day when a booking event arrives."""

import logging
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from django.conf import settings
from django.dispatch import Signal

from apps.businesses import selectors as biz

from . import analytics, events
from .models import Appointment

logger = logging.getLogger(__name__)


def _refresh(sender: Any, appointment_id: int, old_start: datetime | None = None, **_: Any) -> None:
    try:
        row = (
            Appointment.objects.filter(pk=appointment_id)
            .values("business_id", "scheduled_start")
            .first()
        )
        if row is None:
            return
        tz = ZoneInfo(biz.timezone_of(row["business_id"]) or "Asia/Karachi")
        days = {row["scheduled_start"].astimezone(tz).date()}
        if old_start is not None:
            days.add(old_start.astimezone(tz).date())
        for day in sorted(days):
            analytics.recompute_range(row["business_id"], day, day)
    except Exception:
        logger.exception("Daily stats refresh failed", extra={"appointment_id": appointment_id})
        if settings.ANALYTICS_RAISE_ON_ERROR:
            raise


SIGNALS: list[Signal] = [
    events.booking_created,
    events.booking_confirmed,
    events.booking_completed,
    events.booking_cancelled,
    events.booking_no_show,
    events.booking_rescheduled,
]


def connect() -> None:
    for signal in SIGNALS:
        signal.connect(_refresh, weak=False, dispatch_uid=f"analytics-refresh-{id(signal)}")
