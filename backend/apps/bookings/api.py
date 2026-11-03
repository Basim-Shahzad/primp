import uuid
from datetime import date as date_type

from django.http import HttpRequest
from ninja import Query, Router

from apps.businesses import selectors as biz
from apps.core.errors import AppError, not_found

from . import availability
from .schemas import AvailabilityOut, SlotOut

businesses_router = Router(tags=["availability"])


def parse_service_ids(raw: str) -> list[int]:
    try:
        ids = [int(part) for part in raw.split(",") if part.strip()]
    except ValueError:
        ids = []
    if not ids:
        raise AppError(
            "invalid_services",
            "Provide service_ids as comma separated integers.",
            422,
            {"service_ids": ["Invalid value."]},
        )
    return ids


@businesses_router.get("/{business_id}/availability", auth=None, response=AvailabilityOut)
def get_availability(
    request: HttpRequest,
    business_id: uuid.UUID,
    date: date_type,
    service_ids: Query[str],
    staff_id: int | None = None,
) -> AvailabilityOut:
    config = biz.get_booking_config(business_id)
    if config is None:
        raise not_found()
    ids = parse_service_ids(service_ids)
    slots = availability.get_available_slots(config, date, ids, staff_id)
    services = availability.load_services(config, ids)
    return AvailabilityOut(
        date=date,
        timezone=config.timezone,
        duration_minutes=sum(s.duration_minutes for s in services),
        slots=[SlotOut(start=s.start, end=s.end, staff_ids=list(s.staff_ids)) for s in slots],
    )
