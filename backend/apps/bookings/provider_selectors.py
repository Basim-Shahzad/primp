"""Read side of the provider booking dashboard: owner-facing appointment shapes."""

from collections.abc import Iterable
from zoneinfo import ZoneInfo

from django.db.models import QuerySet

from apps.accounts import selectors as accounts

from .models import ACTIVE_STATUSES, Appointment
from .provider_schemas import (
    ProviderAppointmentOut,
    ProviderCustomerOut,
    ProviderServiceOut,
    ProviderStaffOut,
)


def base_queryset() -> QuerySet[Appointment]:
    return Appointment.objects.select_related("business", "staff", "customer").prefetch_related(
        "items"
    )


def appointment_outs(
    appointments: Iterable[Appointment], *, hide_inactive_phone: bool = True
) -> list[ProviderAppointmentOut]:
    """Owner view of appointments. The customer's phone is only shown for active bookings."""
    rows = list(appointments)
    contacts = accounts.contacts_for([a.customer_id for a in rows if a.customer_id is not None])
    out: list[ProviderAppointmentOut] = []
    for a in rows:
        tz = ZoneInfo(a.business.timezone)
        contact = contacts.get(a.customer_id) if a.customer_id is not None else None
        name = (contact.name if contact and contact.name else a.walk_in_name) or "Customer"
        phone = (contact.phone if contact else None) or a.walk_in_phone or None
        if hide_inactive_phone and a.status not in ACTIVE_STATUSES:
            phone = None
        out.append(
            ProviderAppointmentOut(
                id=a.public_id,
                status=a.status,
                source=a.source,
                start=a.scheduled_start.astimezone(tz),
                end=a.scheduled_end.astimezone(tz),
                staff=ProviderStaffOut(id=a.staff.pk, name=a.staff.name) if a.staff else None,
                services=[
                    ProviderServiceOut(
                        id=i.service_id,
                        name=i.name,
                        price=i.price_at_booking,
                        duration_minutes=i.duration_at_booking,
                    )
                    for i in a.items.all()
                ],
                total_price=a.total_price,
                payment_method=a.payment_method,
                payment_status=a.payment_status,
                customer=ProviderCustomerOut(
                    user_id=a.customer.public_id if a.customer is not None else None,
                    name=name,
                    phone=phone,
                ),
                business_note=a.business_note,
                cancellation_reason=a.cancellation_reason,
                created_at=a.created_at,
            )
        )
    return out
