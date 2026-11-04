from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from django.db.models import F, Q, QuerySet

from apps.accounts import selectors as accounts
from apps.businesses import selectors as business_selectors
from apps.reviews import selectors as reviews

from .models import Appointment, AppointmentStatus, RescheduleRequest, RescheduleStatus
from .schemas import (
    ActionsOut,
    AppointmentDetailOut,
    AppointmentOut,
    AppointmentServiceOut,
    BusinessRefOut,
    ContactOut,
    RebookDraftOut,
    RebookServiceOut,
    RescheduleRequestOut,
    StaffRefOut,
    TimelineEventOut,
)


def appointment_out(a: Appointment) -> AppointmentOut:
    tz = ZoneInfo(a.business.timezone)  # times are shown in the business timezone
    return AppointmentOut(
        id=a.public_id,
        status=a.status,
        business=BusinessRefOut(
            id=a.business.public_id, name=a.business.name, slug=a.business.slug
        ),
        staff=StaffRefOut(id=a.staff.pk, name=a.staff.name) if a.staff else None,
        start=a.scheduled_start.astimezone(tz),
        end=a.scheduled_end.astimezone(tz),
        total_price=a.total_price,
        payment_method=a.payment_method,
        payment_status=a.payment_status,
        services=[
            AppointmentServiceOut(
                id=i.service_id,
                name=i.name,
                price=i.price_at_booking,
                duration_minutes=i.duration_at_booking,
            )
            for i in a.items.all()
        ],
        created_at=a.created_at,
        cancelled_at=a.cancelled_at,
        cancellation_reason=a.cancellation_reason,
        is_late_cancellation=a.is_late_cancellation,
    )


@dataclass(frozen=True)
class AppointmentBrief:
    id: int
    public_id: str
    status: str
    customer_id: int | None
    customer_name: str
    owner_id: int
    business_name: str
    staff_name: str
    start: datetime
    services: str
    total_price: int
    is_late_cancellation: bool
    cancellation_reason: str
    payment_status: str = ""
    cancellation_note: str = ""

    def payload(self) -> dict[str, Any]:
        """Values for notification templates, in the business timezone."""
        return {
            "customer_name": self.customer_name or "there",
            "business_name": self.business_name,
            "staff_name": self.staff_name,
            "services": self.services,
            "date": self.start.strftime("%a %d %b"),
            "time": self.start.strftime("%H:%M"),
            "total_price": f"PKR {self.total_price:,}",
            "appointment_id": self.public_id,
            "late_note": " (late cancellation)" if self.is_late_cancellation else "",
            "reason": self.cancellation_reason.replace("_", " "),
            "note_suffix": (
                f" Reason: {self.cancellation_note}."
                if self.cancellation_reason == "business" and self.cancellation_note
                else ""
            ),
        }


def appointment_brief(appointment_id: int) -> AppointmentBrief | None:
    a = (
        Appointment.objects.filter(pk=appointment_id)
        .select_related("business", "staff")
        .prefetch_related("items")
        .first()
    )
    if a is None:
        return None
    contact = accounts.contact_for(a.customer_id) if a.customer_id else None
    tz = ZoneInfo(a.business.timezone)
    return AppointmentBrief(
        id=a.pk,
        public_id=str(a.public_id),
        status=a.status,
        customer_id=a.customer_id,
        customer_name=contact.name if contact else a.walk_in_name,
        owner_id=a.business.owner_id,
        business_name=a.business.name,
        staff_name=a.staff.name if a.staff else "",
        start=a.scheduled_start.astimezone(tz),
        services=", ".join(i.name for i in a.items.all()),
        total_price=a.total_price,
        is_late_cancellation=a.is_late_cancellation,
        cancellation_reason=a.cancellation_reason,
        payment_status=a.payment_status,
        cancellation_note=a.cancellation_note,
    )


def completed_count(customer_id: int) -> int:
    return Appointment.objects.filter(
        customer_id=customer_id, status=AppointmentStatus.COMPLETED
    ).count()


def due_for_reminder(offset_seconds: int, now: datetime, grace_seconds: int = 1800) -> list[int]:
    """Confirmed appointments whose reminder window is open right now.

    The window is `start - offset <= now < start - offset + grace`: a reminder is neither sent
    early nor "stale" after a scanner outage, and a booking made after the window opened (a
    booking made 1 h ahead must not get a "24 h" reminder) is skipped.
    """
    upper = now + timedelta(seconds=offset_seconds)
    lower = upper - timedelta(seconds=grace_seconds)
    return list(
        Appointment.objects.filter(
            status=AppointmentStatus.CONFIRMED,
            scheduled_start__gt=lower,
            scheduled_start__lte=upper,
        )
        .filter(created_at__lte=F("scheduled_start") - timedelta(seconds=offset_seconds))
        .values_list("pk", flat=True)
    )


@dataclass(frozen=True)
class AppointmentRef:
    id: int
    business_id: int
    customer_id: int | None
    status: str
    scheduled_start: datetime


def customer_appointment(user_id: int, public_id: str) -> AppointmentRef | None:
    """The caller's own appointment, or None (callers answer 404 so existence is not leaked)."""
    a = Appointment.objects.filter(public_id=public_id, customer_id=user_id).first()
    if a is None:
        return None
    return AppointmentRef(a.pk, a.business_id, a.customer_id, a.status, a.scheduled_start)


# ---------------------------------------------------------------- history dashboard

SCOPES = ("upcoming", "past", "cancelled")
CANCELLED = (AppointmentStatus.CANCELLED_BY_CUSTOMER, AppointmentStatus.CANCELLED_BY_BUSINESS)
ACTIVE = (AppointmentStatus.PENDING, AppointmentStatus.CONFIRMED)


def customer_appointments(
    user_id: int, scope: str, now: datetime
) -> tuple[QuerySet[Appointment], list[str]]:
    """Queryset and cursor ordering for a history tab.

    Partition at `now`: cancelled; upcoming = pending/confirmed starting now or later;
    past = everything else (completed, no-show, and bookings whose time has passed).
    """
    qs = (
        Appointment.objects.filter(customer_id=user_id)
        .select_related("business", "staff")
        .prefetch_related("items")
    )
    if scope == "cancelled":
        return qs.filter(status__in=CANCELLED), ["-scheduled_start", "-id"]
    if scope == "upcoming":
        return qs.filter(status__in=ACTIVE, scheduled_start__gte=now), ["scheduled_start", "id"]
    past = Q(status__in=(AppointmentStatus.COMPLETED, AppointmentStatus.NO_SHOW)) | Q(
        status__in=ACTIVE, scheduled_start__lt=now
    )
    return qs.filter(past), ["-scheduled_start", "-id"]


def next_appointment(user_id: int, now: datetime) -> Appointment | None:
    qs, ordering = customer_appointments(user_id, "upcoming", now)
    return qs.order_by(*ordering).first()


def allowed_actions(
    appointment: Appointment, now: datetime, *, has_review: bool, pending_request: bool
) -> ActionsOut:
    bookable_future = appointment.status in ACTIVE and now < appointment.scheduled_start
    return ActionsOut(
        can_cancel=bookable_future,
        can_reschedule=bookable_future and not pending_request,
        can_review=appointment.status == AppointmentStatus.COMPLETED and not has_review,
    )


def appointment_detail(user_id: int, public_id: str, now: datetime) -> AppointmentDetailOut | None:
    a = (
        Appointment.objects.filter(public_id=public_id, customer_id=user_id)
        .select_related("business", "staff")
        .prefetch_related("items", "events")
        .first()
    )
    if a is None:
        return None
    pending = (
        RescheduleRequest.objects.filter(appointment=a, status=RescheduleStatus.PENDING)
        .order_by("-id")
        .first()
    )
    has_review = a.pk in reviews.reviewed_appointment_ids([a.pk])
    tz = ZoneInfo(a.business.timezone)
    base = appointment_out(a)
    return AppointmentDetailOut(
        **base.model_dump(),
        timeline=[
            TimelineEventOut(
                status=e.to_status,
                from_status=e.from_status,
                at=e.created_at,
                actor_role=e.actor_role,
                note=e.note,
            )
            for e in a.events.all()
        ],
        contact=ContactOut(
            phone=a.business.phone,
            whatsapp=a.business.whatsapp,
            address=a.business.address,
            lat=a.business.location.y,
            lng=a.business.location.x,
        ),
        actions=allowed_actions(a, now, has_review=has_review, pending_request=pending is not None),
        has_review=has_review,
        pending_reschedule=RescheduleRequestOut(
            id=pending.pk,
            status=pending.status,
            new_start=pending.new_start.astimezone(tz),
            new_end=pending.new_end.astimezone(tz),
        )
        if pending
        else None,
    )


def rebook_draft(user_id: int, public_id: str) -> RebookDraftOut | None:
    a = (
        Appointment.objects.filter(public_id=public_id, customer_id=user_id)
        .select_related("business", "staff")
        .prefetch_related("items__service")
        .first()
    )
    if a is None:
        return None
    items = list(a.items.all())
    service_ids = [i.service_id for i in items]
    services = [
        RebookServiceOut(
            id=i.service_id,
            name=i.service.name,
            price=i.service.price,
            duration_minutes=i.service.duration_minutes,
            is_active=i.service.is_active,
        )
        for i in items
    ]
    unavailable = [s.id for s in services if not s.is_active]
    staff_available = a.staff_id is None or a.staff_id in business_selectors.capable_staff_ids(
        a.business_id, service_ids
    )
    listed = business_selectors.get_booking_config(a.business.public_id) is not None
    return RebookDraftOut(
        business_id=a.business.public_id,
        business_slug=a.business.slug,
        business_name=a.business.name,
        service_ids=service_ids,
        services=services,
        staff_id=a.staff_id if staff_available else None,
        staff_available=staff_available,
        unavailable_service_ids=unavailable,
        can_book=listed and not unavailable,
    )


def service_has_appointments(service_id: int) -> bool:
    """True when any appointment (past or future, any status) references the service."""
    from .models import AppointmentService

    return AppointmentService.objects.filter(service_id=service_id).exists()
