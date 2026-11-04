"""Owner dashboard queries: list, today's timeline, needs-action, detail and change polling."""

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from django.db.models import Q, QuerySet
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.accounts import selectors as accounts
from apps.businesses import selectors as biz
from apps.core.errors import validation_error
from apps.core.pagination import CursorPage, paginate_cursor

from .models import (
    ACTIVE_STATUSES,
    Appointment,
    AppointmentStatus,
    RescheduleRequest,
    RescheduleStatus,
)
from .provider_actions import owned_appointment
from .provider_schemas import (
    CustomerSummaryOut,
    ProviderAppointmentDetailOut,
    RescheduleRequestOwnerOut,
    TimelineOut,
)
from .provider_selectors import appointment_outs, base_queryset

STATUSES = {s.value for s in AppointmentStatus}
MAX_RANGE_DAYS = 366
MAX_CHANGES = 200


def _day_bounds(day: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    lo = datetime.combine(day, time.min, tzinfo=tz)
    return lo, datetime.combine(day + timedelta(days=1), time.min, tzinfo=tz)


def _search_filter(q: str) -> Q:
    text = q.strip()
    digits = re.sub(r"\D", "", text)
    clause = Q(walk_in_name__icontains=text) | Q(customer__profile__name__icontains=text)
    if len(digits) >= 4:
        tail = digits[-10:] if len(digits) > 10 else digits.lstrip("0") or digits
        clause |= Q(walk_in_phone__icontains=tail) | Q(customer__phone__icontains=tail)
    return clause


@dataclass(frozen=True)
class ListParams:
    from_date: date | None = None
    to_date: date | None = None
    staff_id: int | None = None
    status: str | None = None
    service_id: int | None = None
    q: str | None = None
    order: Literal["asc", "desc"] = "asc"
    cursor: str | None = None
    limit: int = 20


def list_appointments(
    owner_id: int, business_ref: str, params: ListParams
) -> CursorPage[Appointment]:
    business = biz.owned_business(owner_id, business_ref)
    tz = ZoneInfo(business.timezone)
    qs = base_queryset().filter(business_id=business.pk)
    errors: dict[str, list[str]] = {}
    if params.from_date and params.to_date:
        if params.to_date < params.from_date:
            errors["to"] = [str(_("The end date must not be before the start."))]
        elif (params.to_date - params.from_date).days >= MAX_RANGE_DAYS:
            errors["to"] = [str(_("The range may span at most 366 days."))]
    if params.status is not None:
        wanted = [s for s in params.status.split(",") if s]
        if not wanted or any(s not in STATUSES for s in wanted):
            errors["status"] = [str(_("Unknown status."))]
    if params.q is not None and not 2 <= len(params.q.strip()) <= 100:
        errors["q"] = [str(_("Search needs 2 to 100 characters."))]
    if errors:
        raise validation_error(errors)
    if params.from_date:
        qs = qs.filter(scheduled_start__gte=_day_bounds(params.from_date, tz)[0])
    if params.to_date:
        qs = qs.filter(scheduled_start__lt=_day_bounds(params.to_date, tz)[1])
    if params.staff_id is not None:
        qs = qs.filter(staff_id=params.staff_id)
    if params.status is not None:
        qs = qs.filter(status__in=params.status.split(","))
    if params.service_id is not None:
        qs = qs.filter(items__service_id=params.service_id)
    if params.q:
        qs = qs.filter(_search_filter(params.q))
    qs = qs.distinct()
    ordering = ("scheduled_start", "id") if params.order == "asc" else ("-scheduled_start", "-id")
    return paginate_cursor(qs, ordering=ordering, cursor=params.cursor, limit=params.limit)


def today(owner_id: int, business_ref: str, now: datetime | None = None) -> list[Appointment]:
    """Today's bookings (business timezone) in time order, cancelled ones left out."""
    now = now or timezone.now()
    business = biz.owned_business(owner_id, business_ref)
    lo, hi = _day_bounds(
        now.astimezone(ZoneInfo(business.timezone)).date(), ZoneInfo(business.timezone)
    )
    return list(
        base_queryset()
        .filter(business_id=business.pk, scheduled_start__gte=lo, scheduled_start__lt=hi)
        .exclude(
            status__in=[
                AppointmentStatus.CANCELLED_BY_CUSTOMER,
                AppointmentStatus.CANCELLED_BY_BUSINESS,
            ]
        )
        .order_by("scheduled_start", "id")
    )


@dataclass(frozen=True)
class NeedsAction:
    pending: list[Appointment]
    requests: list[tuple[RescheduleRequest, Appointment]]

    @property
    def unread_count(self) -> int:
        return len(self.pending) + len(self.requests)


def needs_action(owner_id: int, business_ref: str, now: datetime | None = None) -> NeedsAction:
    """Pending (manual-accept) bookings and pending customer reschedule requests."""
    now = now or timezone.now()
    business = biz.owned_business(owner_id, business_ref)
    pending = list(
        base_queryset()
        .filter(
            business_id=business.pk,
            status=AppointmentStatus.PENDING,
            scheduled_start__gt=now,
        )
        .order_by("scheduled_start", "id")
    )
    requests = list(
        RescheduleRequest.objects.filter(
            appointment__business_id=business.pk,
            appointment__status__in=ACTIVE_STATUSES,
            status=RescheduleStatus.PENDING,
        )
        .select_related("appointment__business", "appointment__staff", "appointment__customer")
        .prefetch_related("appointment__items")
        .order_by("created_at", "id")
    )
    return NeedsAction(pending, [(r, r.appointment) for r in requests])


def request_out(r: RescheduleRequest) -> RescheduleRequestOwnerOut:
    return RescheduleRequestOwnerOut(
        id=r.pk,
        new_start=r.new_start,
        new_end=r.new_end,
        new_staff_id=r.new_staff_id,
        status=r.status,
        created_at=r.created_at,
    )


def detail(
    owner_id: int, public_id: str, now: datetime | None = None
) -> ProviderAppointmentDetailOut:
    now = now or timezone.now()
    appointment = owned_appointment(owner_id, public_id)
    full = base_queryset().get(pk=appointment.pk)
    out = appointment_outs([full], now=now)[0]
    timeline = [
        TimelineOut(
            from_status=e.from_status,
            to_status=e.to_status,
            actor_role=e.actor_role,
            note=e.note,
            created_at=e.created_at,
        )
        for e in full.events.all()
    ]
    visits = Appointment.objects.filter(
        business_id=full.business_id, status=AppointmentStatus.COMPLETED
    ).exclude(pk=full.pk)
    no_shows = 0
    if full.customer_id is not None:
        visits = visits.filter(customer_id=full.customer_id)
        no_shows = accounts.no_show_count(full.customer_id)
    elif full.walk_in_phone:
        visits = visits.filter(customer__isnull=True, walk_in_phone=full.walk_in_phone)
    else:
        visits = visits.none()
    pending = (
        full.reschedule_requests.filter(status=RescheduleStatus.PENDING).order_by("-id").first()
    )
    return ProviderAppointmentDetailOut(
        **out.model_dump(),
        timeline=timeline,
        customer_summary=CustomerSummaryOut(
            user_id=out.customer.user_id,
            name=out.customer.name,
            phone=out.customer.phone,
            previous_completed_visits=visits.count(),
            no_show_count=no_shows,
        ),
        reschedule_request=request_out(pending) if pending else None,
    )


def changes_since(
    owner_id: int, business_ref: str, since: datetime, now: datetime | None = None
) -> tuple[datetime, list[Appointment]]:
    """Appointments (or their reschedule requests) touched after `since`: cheap polling feed."""
    now = now or timezone.now()
    business = biz.owned_business(owner_id, business_ref)
    qs: QuerySet[Appointment] = (
        base_queryset()
        .filter(business_id=business.pk)
        .filter(Q(updated_at__gt=since) | Q(reschedule_requests__updated_at__gt=since))
        .distinct()
        .order_by("updated_at", "id")
    )
    return now, list(qs[:MAX_CHANGES])
