import re
import uuid
from datetime import date as date_type
from typing import Any
from zoneinfo import ZoneInfo

from django.http import HttpRequest
from ninja import Header, Query, Router, Status

from apps.accounts.auth import jwt_auth
from apps.accounts.roles import Role
from apps.businesses import selectors as biz
from apps.core.errors import AppError, not_found
from apps.core.permissions import require_role

from . import availability, selectors, services
from .models import Appointment
from .schemas import (
    AppointmentOut,
    AvailabilityOut,
    BookIn,
    CancelIn,
    HoldIn,
    HoldOut,
    PolicyOut,
    RescheduleIn,
    RescheduleOut,
    RescheduleRequestOut,
    SlotOut,
)

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
        appointment_id=str(payload.appointment_id) if payload.appointment_id else None,
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


def _load_out(appointment_id: int) -> AppointmentOut:
    appointment = (
        Appointment.objects.select_related("business", "staff")
        .prefetch_related("items")
        .get(pk=appointment_id)
    )
    return selectors.appointment_out(appointment)


@appointments_router.patch("/{appointment_id}/cancel", auth=jwt_auth, response=AppointmentOut)
def cancel_appointment(
    request: HttpRequest, appointment_id: uuid.UUID, payload: CancelIn
) -> AppointmentOut:
    user = _customer(request)
    appointment = services.cancel_by_customer(
        user.pk, str(appointment_id), payload.reason, payload.note
    )
    return _load_out(appointment.pk)


@appointments_router.get("/{appointment_id}/policy", auth=jwt_auth, response=PolicyOut)
def appointment_policy(request: HttpRequest, appointment_id: uuid.UUID) -> PolicyOut:
    user = _customer(request)
    policy = services.get_policy(user.pk, str(appointment_id))
    if not policy.can_cancel:
        message = "This appointment can no longer be cancelled."
    elif policy.is_late_now:
        message = (
            f"Cancellations within {policy.notice_hours} hours of the appointment are marked "
            "as late and may affect your booking history."
        )
    else:
        message = f"Free cancellation until {policy.free_until.isoformat()}."
    return PolicyOut(
        cancellation_notice_hours=policy.notice_hours,
        free_cancellation_until=policy.free_until,
        is_late_now=policy.is_late_now,
        can_cancel=policy.can_cancel,
        message=message,
    )


@appointments_router.patch(
    "/{appointment_id}/reschedule", auth=jwt_auth, response={200: RescheduleOut, 202: RescheduleOut}
)
def reschedule_appointment(
    request: HttpRequest, appointment_id: uuid.UUID, payload: RescheduleIn
) -> Status[RescheduleOut]:
    user = _customer(request)
    result = services.reschedule_by_customer(user.pk, str(appointment_id), payload.hold_token)
    req = result.request
    tz = ZoneInfo(result.appointment.business.timezone)
    return Status(
        200 if result.applied else 202,
        RescheduleOut(
            applied=result.applied,
            appointment=_load_out(result.appointment.pk),
            request=RescheduleRequestOut(
                id=req.pk,
                status=req.status,
                new_start=req.new_start.astimezone(tz),
                new_end=req.new_end.astimezone(tz),
            )
            if req
            else None,
        ),
    )
