from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from apps.bookings.availability import Slot

PKT = ZoneInfo("Asia/Karachi")
# Monday 2026-11-02 09:00 Karachi
NOW = datetime(2026, 11, 2, 4, 0, tzinfo=UTC)
MONDAY = date(2026, 11, 2)
TUESDAY = date(2026, 11, 3)


def local(day: date, hh: int, mm: int = 0) -> datetime:
    return datetime.combine(day, time(hh, mm), tzinfo=PKT)


def hhmm(slots: list[Slot]) -> list[str]:
    return [s.start.astimezone(PKT).strftime("%H:%M") for s in slots]


def day_plus(n: int) -> date:
    return MONDAY + timedelta(days=n)
