"""Owner-side scheduling: manual (walk-in/phone) bookings, conflicts and the calendar view."""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.accounts import selectors as accounts
from apps.businesses import hours as hours_mod
from apps.businesses import selectors as biz
from apps.core.errors import AppError, conflict, validation_error

from . import availability, events, holds
from .models import (
    ACTIVE_STATUSES,
    Appointment,
    AppointmentService,
    AppointmentSource,
    AppointmentStatus,
    PaymentMethod,
    PaymentStatus,
)
from .services import Actor, _record

MAX_SCHEDULE_DAYS = 62
MAX_PAST_HOURS = 12  # a walk-in can be logged shortly after it started
MAX_CONFLICTS = 500
MAX_SCHEDULE_ROWS = 2000


def _slot_unavailable() -> AppError:
    return conflict("slot_unavailable", _("That time is no longer available."))


def create_manual_booking(
    owner_id: int,
    business_ref: str,
    *,
    customer_name: str,
    customer_phone: str,
    service_ids: list[int],
    staff_id: int | None,
    start: datetime,
    now: datetime | None = None,
) -> Appointment:
    """Record a walk-in or phone booking. Same safety rules as an online booking."""
    now = now or timezone.now()
    with transaction.atomic():
        business = biz.owned_business(owner_id, business_ref)
        config = biz.get_booking_config_by_id(business.pk, lock=True)  # serialise like online
        assert config is not None
        if start < now - timedelta(hours=MAX_PAST_HOURS):
            raise validation_error({"start": [str(_("The start time is too far in the past."))]})
        if start > now + timedelta(days=config.max_advance_days + 1):
            raise validation_error({"start": [str(_("The start time is too far ahead."))]})
        services = availability.load_services(config, service_ids)
        chosen: int | None = None
        if config.staff_level_booking:
            if staff_id is None:
                raise validation_error({"staff_id": [str(_("Choose a staff member."))]})
            if staff_id not in biz.capable_staff_ids(config.id, [s.id for s in services]):
                raise AppError(
                    "staff_cannot_perform",
                    _("This staff member cannot perform all selected services."),
                    422,
                    {"staff_id": [str(_("Not available for these services."))]},
                )
            if not biz.lock_staff(staff_id):
                raise validation_error({"staff_id": [str(_("Unknown staff member."))]})
            chosen = staff_id
        end = start + timedelta(minutes=sum(s.duration_minutes for s in services))
        problems = availability.window_problems(config, start, end, chosen)
        if availability.OUTSIDE_HOURS in problems:
            raise AppError("outside_working_hours", _("That time is outside working hours."), 422)
        if availability.BLOCKED in problems:
            raise conflict("time_blocked", _("That time is blocked."))
        if availability.CONFLICT in problems:
            raise _slot_unavailable()
        linked = accounts.verified_user_id_by_phone(customer_phone)
        try:
            with transaction.atomic():
                appointment = Appointment.objects.create(
                    customer_id=linked,
                    walk_in_name=customer_name,
                    walk_in_phone=customer_phone,
                    business_id=config.id,
                    staff_id=chosen,
                    status=AppointmentStatus.CONFIRMED,
                    scheduled_start=start,
                    scheduled_end=end,
                    total_price=sum(s.price for s in services),
                    payment_method=PaymentMethod.PAY_AT_VENUE,
                    payment_status=PaymentStatus.UNPAID,
                    source=AppointmentSource.MANUAL,
                )
        except IntegrityError as exc:  # exclusion constraint: lost a race for the staff slot
            raise _slot_unavailable() from exc
        AppointmentService.objects.bulk_create(
            [
                AppointmentService(
                    appointment=appointment,
                    service_id=s.id,
                    name=s.name,
                    price_at_booking=s.price,
                    duration_at_booking=s.duration_minutes,
                )
                for s in services
            ]
        )
        _record(
            appointment,
            "",
            AppointmentStatus.CONFIRMED,
            Actor(owner_id, "business_owner"),
            "manual booking",
        )
        events.emit(events.booking_created, appointment.pk, source="manual")
        events.emit(events.booking_confirmed, appointment.pk)
    return appointment


@dataclass(frozen=True)
class Conflict:
    appointment: Appointment
    reasons: list[str]


def conflicts(owner_id: int, business_ref: str, now: datetime | None = None) -> list[Conflict]:
    """Future active appointments that now fall outside working hours or inside a block."""
    now = now or timezone.now()
    business = biz.owned_business(owner_id, business_ref)
    config = biz.to_booking_config(business)
    tz = ZoneInfo(config.timezone)
    rows = list(
        Appointment.objects.filter(
            business_id=config.id, status__in=ACTIVE_STATUSES, scheduled_start__gt=now
        )
        .select_related("business", "staff", "customer")
        .prefetch_related("items")
        .order_by("scheduled_start", "id")[:MAX_CONFLICTS]
    )
    if not rows:
        return []
    windows_all = biz.opening_windows_all(config.id)
    closures = biz.closures_overlapping(
        config.id, min(a.scheduled_start for a in rows), max(a.scheduled_end for a in rows)
    )
    found: list[Conflict] = []
    for a in rows:
        resource = a.staff_id if config.staff_level_booking else None
        reasons: list[str] = []
        if availability.hours_problem(
            windows_all, tz, a.scheduled_start, a.scheduled_end, resource
        ):
            reasons.append(availability.OUTSIDE_HOURS)
        if availability.block_problem(closures, a.scheduled_start, a.scheduled_end, resource):
            reasons.append(availability.BLOCKED)
        if reasons:
            found.append(Conflict(a, reasons))
    return found


@dataclass(frozen=True)
class Schedule:
    timezone: str
    from_date: date
    to_date: date
    hours: list[hours_mod.DayShifts]
    staff_hours: dict[int, list[hours_mod.DayShifts]]
    blocks: list[tuple[int, str, int | None, datetime, datetime, str]]
    appointments: list[Appointment]
    holds_count: int


def schedule(
    owner_id: int,
    business_ref: str,
    from_date: date,
    to_date: date,
    staff_id: int | None = None,
) -> Schedule:
    """Everything a calendar needs for an inclusive local date range of at most 62 days."""
    if to_date < from_date:
        raise validation_error({"to": [str(_("The end date must not be before the start."))]})
    if (to_date - from_date).days + 1 > MAX_SCHEDULE_DAYS:
        raise validation_error({"to": [str(_("The range may span at most 62 days."))]})
    business = biz.owned_business(owner_id, business_ref)
    config = biz.to_booking_config(business)
    if staff_id is not None and biz.staff_in_business(config.id, staff_id) is None:
        from apps.core.errors import not_found

        raise not_found()
    tz = ZoneInfo(config.timezone)
    lo = datetime.combine(from_date, datetime.min.time(), tzinfo=tz)
    hi = datetime.combine(to_date + timedelta(days=1), datetime.min.time(), tzinfo=tz)

    appts = Appointment.objects.filter(
        business_id=config.id, scheduled_start__lt=hi, scheduled_end__gt=lo
    )
    if staff_id is not None:
        appts = appts.filter(staff_id=staff_id)
    rows = list(
        appts.select_related("business", "staff", "customer")
        .prefetch_related("items")
        .order_by("scheduled_start", "id")[:MAX_SCHEDULE_ROWS]
    )
    blocks = [
        (c.pk, c.kind, c.staff_id, c.starts_at, c.ends_at, c.reason)
        for c in business.closures.filter(starts_at__lt=hi, ends_at__gt=lo).order_by("starts_at")
        if staff_id is None or c.staff_id in (None, staff_id)
    ]
    staff_ids = (
        [staff_id]
        if staff_id is not None
        else list(business.staff.filter(is_active=True).values_list("pk", flat=True))
    )
    staff_hours: dict[int, list[hours_mod.DayShifts]] = {}
    for sid in staff_ids:
        week = hours_mod.week_of(config.id, sid)
        if any(d.shifts for d in week):
            staff_hours[sid] = week
    held = [
        h
        for h in holds.active_holds(config.id, lo, hi)
        if staff_id is None or h.staff_id in (None, staff_id)
    ]
    return Schedule(
        timezone=config.timezone,
        from_date=from_date,
        to_date=to_date,
        hours=hours_mod.week_of(config.id, None),
        staff_hours=staff_hours,
        blocks=blocks,
        appointments=rows,
        holds_count=len(held),
    )
