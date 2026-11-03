from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from django.db.models import F

from apps.accounts import selectors as accounts

from .models import Appointment, AppointmentStatus
from .schemas import (
    AppointmentOut,
    AppointmentServiceOut,
    BusinessRefOut,
    StaffRefOut,
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
    customer_id: int
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
    contact = accounts.contact_for(a.customer_id)
    tz = ZoneInfo(a.business.timezone)
    return AppointmentBrief(
        id=a.pk,
        public_id=str(a.public_id),
        status=a.status,
        customer_id=a.customer_id,
        customer_name=contact.name if contact else "",
        owner_id=a.business.owner_id,
        business_name=a.business.name,
        staff_name=a.staff.name if a.staff else "",
        start=a.scheduled_start.astimezone(tz),
        services=", ".join(i.name for i in a.items.all()),
        total_price=a.total_price,
        is_late_cancellation=a.is_late_cancellation,
        cancellation_reason=a.cancellation_reason,
        payment_status=a.payment_status,
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
    customer_id: int
    status: str
    scheduled_start: datetime


def customer_appointment(user_id: int, public_id: str) -> AppointmentRef | None:
    """The caller's own appointment, or None (callers answer 404 so existence is not leaked)."""
    a = Appointment.objects.filter(public_id=public_id, customer_id=user_id).first()
    if a is None:
        return None
    return AppointmentRef(a.pk, a.business_id, a.customer_id, a.status, a.scheduled_start)
