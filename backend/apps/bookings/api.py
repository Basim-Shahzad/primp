import re
import uuid
from datetime import date as date_type
from typing import Any

from django.http import HttpRequest
from ninja import Header, Query, Router, Status

from apps.accounts.auth import jwt_auth
from apps.accounts.roles import Role
from apps.businesses import selectors as biz
from apps.core.errors import AppError, not_found
from apps.core.permissions import require_role

from . import availability, selectors, services
from .models import Appointment
from .schemas import AppointmentOut, AvailabilityOut, BookIn, HoldIn, HoldOut, SlotOut

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


# ------------------------------------------------------------------ appointments

appointments_router = Router(tags=["appointments"])
IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")


def _customer(request: HttpRequest) -> Any:
    return require_role(Role.CUSTOMER)(request)


@appointments_router.post("/hold", auth=jwt_auth, response={201: HoldOut})
def create_hold(request: HttpRequest, payload: HoldIn) -> Status[HoldOut]:
    user = _customer(request)
    result = services.create_hold(
        user_id=user.pk,
        business_ref=str(payload.business_id),
        service_ids=payload.service_ids,
        staff_id=payload.staff_id,
        start=payload.start,
    )
    return Status(
        201,
        HoldOut(
            token=result.token,
            expires_at=result.expires_at,
            business_id=payload.business_id,
            staff_id=result.staff_id,
            start=result.start,
            end=result.end,
        ),
    )


@appointments_router.delete("/hold/{token}", auth=jwt_auth, response={204: None})
def release_hold(request: HttpRequest, token: str) -> Status[None]:
    user = _customer(request)
    services.release_hold(token, user.pk)
    return Status(204, None)


@appointments_router.post("", auth=jwt_auth, response={200: AppointmentOut, 201: AppointmentOut})
def book_appointment(
    request: HttpRequest,
    payload: BookIn,
    idempotency_key: Header[str | None] = None,
) -> Status[AppointmentOut]:
    user = _customer(request)
    if idempotency_key is not None and not IDEMPOTENCY_KEY_RE.match(idempotency_key):
        raise AppError(
            "validation_error",
            "Invalid input.",
            422,
            {"Idempotency-Key": ["Use 1-64 letters, digits or _ . : -"]},
        )
    appointment, created = services.create_appointment(
        user_id=user.pk, hold_token=payload.hold_token, idempotency_key=idempotency_key
    )
    appointment = (
        Appointment.objects.select_related("business", "staff")
        .prefetch_related("items")
        .get(pk=appointment.pk)
    )
    return Status(201 if created else 200, selectors.appointment_out(appointment))
