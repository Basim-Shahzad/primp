"""Availability engine.

`free_starts` is a pure function (no DB, no clock) so it can be property-tested. The
`get_available_slots` service gathers inputs and applies business rules around it.
All wall-clock logic runs in the business timezone; results carry that offset.
"""

import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.core.cache import cache
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.businesses import selectors as biz
from apps.core.errors import AppError

from . import holds as hold_store
from .cache_keys import next_slot_key
from .models import ACTIVE_STATUSES, Appointment

logger = logging.getLogger(__name__)

MAX_SERVICES_PER_BOOKING = 10
NEXT_SLOT_CACHE_SECONDS = 60
NEXT_SLOT_LOOKAHEAD_DAYS = 14

Interval = tuple[datetime, datetime]


@dataclass(frozen=True)
class Slot:
    start: datetime
    end: datetime
    staff_ids: tuple[int, ...] = ()


def _combine(day: date, t: time, tz: ZoneInfo) -> datetime:
    return datetime.combine(day, t, tzinfo=tz)


def _ceil_to_grid(moment: datetime, interval_minutes: int) -> datetime:
    """Round up to the next clock-aligned multiple of the interval (minutes since midnight)."""
    minutes = moment.hour * 60 + moment.minute + (1 if moment.second or moment.microsecond else 0)
    aligned = -(-minutes // interval_minutes) * interval_minutes
    midnight = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight + timedelta(minutes=aligned)


def free_starts(
    *,
    day: date,
    tz: ZoneInfo,
    windows: list[tuple[time, time]],
    duration: timedelta,
    interval_minutes: int,
    buffer: timedelta,
    blocks: list[Interval],
    busy: list[Interval],
    capacity: int,
    earliest: datetime,
) -> list[datetime]:
    """Start times on `day` where `duration` fits inside a window.

    - blocks (closures) forbid any overlap with the appointment itself.
    - busy intervals (bookings/holds) also reserve `buffer` after themselves and after the new
      appointment; a start is free while fewer than `capacity` busy intervals conflict.
    """
    starts: set[datetime] = set()
    for opens, closes in windows:
        window_open = _combine(day, opens, tz)
        window_close = _combine(day, closes, tz)
        start = _ceil_to_grid(window_open, interval_minutes)
        while start + duration <= window_close:
            end = start + duration
            if start >= earliest and not any(
                start < b_end and b_start < end for b_start, b_end in blocks
            ):
                conflicts = sum(
                    1
                    for b_start, b_end in busy
                    if start < b_end + buffer and b_start < end + buffer
                )
                if conflicts < capacity:
                    starts.add(start)
            start += timedelta(minutes=interval_minutes)
    return sorted(starts)


def load_services(config: biz.BookingConfig, service_ids: list[int]) -> list[biz.ServiceInfo]:
    unique = list(dict.fromkeys(service_ids))
    if not unique or len(unique) > MAX_SERVICES_PER_BOOKING:
        raise AppError(
            "invalid_services",
            _("Choose between 1 and %(max)s services.") % {"max": MAX_SERVICES_PER_BOOKING},
            422,
            {"service_ids": [str(_("Invalid selection."))]},
        )
    services = biz.get_services(config.id, unique)
    if len(services) != len(unique):
        raise AppError(
            "invalid_services",
            _("One or more services are unavailable."),
            422,
            {"service_ids": [str(_("Unknown or inactive service."))]},
        )
    return services


def _day_bounds(day: date, tz: ZoneInfo) -> Interval:
    return _combine(day, time.min, tz), _combine(day + timedelta(days=1), time.min, tz)


def _resource_windows(
    windows_by_staff: dict[int | None, list[tuple[time, time]]], staff_id: int
) -> list[tuple[time, time]]:
    """Staff shifts override business shifts for a weekday when the staff has any (ADR-004)."""
    return windows_by_staff.get(staff_id) or windows_by_staff.get(None, [])


def get_available_slots(
    config: biz.BookingConfig,
    day: date,
    service_ids: list[int],
    staff_id: int | None = None,
    *,
    now: datetime | None = None,
) -> list[Slot]:
    now = now or timezone.now()
    tz = ZoneInfo(config.timezone)
    today = now.astimezone(tz).date()
    if day < today:
        raise AppError(
            "date_in_past", _("The date is in the past."), 422, {"date": [str(_("Past date."))]}
        )
    if day > today + timedelta(days=config.max_advance_days):
        return []

    services = load_services(config, service_ids)
    duration = timedelta(minutes=sum(s.duration_minutes for s in services))
    earliest = now + timedelta(minutes=config.lead_time_minutes)
    buffer = timedelta(minutes=config.buffer_minutes)
    day_start, day_end = _day_bounds(day, tz)
    lo, hi = day_start - buffer, day_end + buffer

    windows = biz.opening_windows(config.id, day.weekday())
    closures = biz.closures_overlapping(config.id, day_start, day_end)
    appointments = Appointment.objects.filter(
        business_id=config.id,
        status__in=ACTIVE_STATUSES,
        scheduled_start__lt=hi,
        scheduled_end__gt=lo,
    ).values_list("staff_id", "scheduled_start", "scheduled_end")
    holds = hold_store.active_holds(config.id, lo, hi)

    common = {
        "day": day,
        "tz": tz,
        "duration": duration,
        "interval_minutes": config.slot_interval_minutes,
        "buffer": buffer,
        "earliest": earliest,
    }

    if not config.staff_level_booking:
        starts = free_starts(
            windows=windows.get(None, []),
            blocks=[(s, e) for sid, s, e in closures if sid is None],
            busy=[(s, e) for _sid, s, e in appointments] + [(h.start, h.end) for h in holds],
            capacity=config.parallel_capacity,
            **common,  # type: ignore[arg-type]
        )
        return [Slot(s, s + duration) for s in starts]

    able = biz.capable_staff_ids(config.id, [s.id for s in services])
    if staff_id is not None:
        if staff_id not in able:
            raise AppError(
                "staff_cannot_perform",
                _("This staff member cannot perform all selected services."),
                422,
                {"staff_id": [str(_("Not available for these services."))]},
            )
        able = [staff_id]

    busy_by_staff: dict[int | None, list[Interval]] = defaultdict(list)
    for sid, s, e in appointments:
        busy_by_staff[sid].append((s, e))
    for h in holds:
        busy_by_staff[h.staff_id].append((h.start, h.end))
    business_blocks = [(s, e) for sid, s, e in closures if sid is None]

    available: dict[datetime, list[int]] = defaultdict(list)
    for sid in able:
        starts = free_starts(
            windows=_resource_windows(windows, sid),
            blocks=business_blocks + [(s, e) for csid, s, e in closures if csid == sid],
            busy=busy_by_staff.get(sid, []),
            capacity=1,
            **common,  # type: ignore[arg-type]
        )
        for s in starts:
            available[s].append(sid)
    return [Slot(s, s + duration, tuple(ids)) for s, ids in sorted(available.items())]


def find_slot(
    config: biz.BookingConfig,
    start: datetime,
    service_ids: list[int],
    staff_id: int | None = None,
    *,
    now: datetime | None = None,
) -> Slot | None:
    """The slot beginning exactly at `start`, if it is currently bookable."""
    tz = ZoneInfo(config.timezone)
    day = start.astimezone(tz).date()
    for slot in get_available_slots(config, day, service_ids, staff_id, now=now):
        if slot.start == start:
            return slot
    return None


def next_available_slot(
    config: biz.BookingConfig, *, now: datetime | None = None
) -> datetime | None:
    """Earliest slot for the shortest active service (any staff). Cached for 60 s."""
    key = next_slot_key(config.id)
    cached = cache.get(key)
    if cached is not None:
        return datetime.fromisoformat(cached) if cached else None
    now = now or timezone.now()
    result = _compute_next_slot(config, now)
    cache.set(key, result.isoformat() if result else "", timeout=NEXT_SLOT_CACHE_SECONDS)
    return result


def _compute_next_slot(config: biz.BookingConfig, now: datetime) -> datetime | None:
    shortest = biz.min_service_duration(config.id)
    if shortest is None:
        return None
    service = biz.cheapest_shortest_service_id(config.id)
    if service is None:  # pragma: no cover - shortest exists so a service exists
        return None
    tz = ZoneInfo(config.timezone)
    today = now.astimezone(tz).date()
    for offset in range(min(config.max_advance_days, NEXT_SLOT_LOOKAHEAD_DAYS) + 1):
        slots = get_available_slots(config, today + timedelta(days=offset), [service], now=now)
        if slots:
            return slots[0].start
    return None
