"""Booking read/maintenance operations for the backoffice."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from django.db.models import Count
from django.utils import timezone

from .models import ACTIVE_STATUSES, Appointment
from .selectors import AppointmentBrief, appointment_brief


@dataclass(frozen=True)
class CustomerBookings:
    by_status: dict[str, int]
    recent: list[dict[str, Any]]


def customer_bookings(user_id: int, limit: int = 10) -> CustomerBookings:
    counts = dict(
        Appointment.objects.filter(customer_id=user_id)
        .values_list("status")
        .annotate(n=Count("id"))
        .order_by()
    )
    recent = [
        {
            "id": a.public_id,
            "status": a.status,
            "business": a.business.name,
            "start": a.scheduled_start,
            "total_price": a.total_price,
        }
        for a in Appointment.objects.filter(customer_id=user_id)
        .select_related("business")
        .order_by("-scheduled_start", "-id")[:limit]
    ]
    return CustomerBookings({k: int(v) for k, v in counts.items()}, recent)


def _briefs(ids: list[int]) -> list[AppointmentBrief]:
    return [b for b in (appointment_brief(i) for i in ids) if b is not None]


def future_active_for_customer(user_id: int, now: datetime | None = None) -> list[AppointmentBrief]:
    ids = Appointment.objects.filter(
        customer_id=user_id,
        status__in=ACTIVE_STATUSES,
        scheduled_start__gt=now or timezone.now(),
    ).values_list("pk", flat=True)
    return _briefs(list(ids))


def future_active_for_owner(owner_id: int, now: datetime | None = None) -> list[AppointmentBrief]:
    ids = Appointment.objects.filter(
        business__owner_id=owner_id,
        status__in=ACTIVE_STATUSES,
        scheduled_start__gt=now or timezone.now(),
    ).values_list("pk", flat=True)
    return _briefs(list(ids))


def scrub_customer_contact(user_id: int) -> int:
    """Blank the free-text contact copied onto a customer's appointments (anonymisation)."""
    return int(
        Appointment.objects.filter(customer_id=user_id).update(
            walk_in_name="", walk_in_phone="", business_note=""
        )
    )
