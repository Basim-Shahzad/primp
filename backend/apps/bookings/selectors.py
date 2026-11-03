from zoneinfo import ZoneInfo

from .models import Appointment
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
    )
