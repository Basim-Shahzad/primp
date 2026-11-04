"""Provider-side booking endpoints (staff impact workflow now; dashboard actions follow)."""

import uuid
from datetime import date
from typing import Literal

from django.http import HttpRequest
from ninja import Field, Query, Router, Schema, Status

from apps.accounts.auth import jwt_auth
from apps.accounts.phone import normalize_pk_mobile
from apps.accounts.roles import Role
from apps.core.errors import validation_error
from apps.core.permissions import require_role

from . import provider_schedule, provider_selectors, staff_impact
from .provider_schemas import (
    ApplyOut,
    BlockOut,
    ConflictOut,
    DayHoursOut,
    DeactivateIn,
    ImpactItemOut,
    ImpactOut,
    ManualBookingIn,
    ProviderAppointmentOut,
    ResolutionIn,
    ScheduleOut,
    ShiftOut,
    StaffServicesIn,
)

provider_router = Router(tags=["provider"], auth=jwt_auth)
owner_only = require_role(Role.BUSINESS_OWNER)


def to_resolutions(items: list[ResolutionIn]) -> list[staff_impact.Resolution]:
    return [staff_impact.Resolution(str(i.appointment_id), i.action, i.new_staff_id) for i in items]


def parse_ids(raw: str) -> list[int]:
    try:
        ids = [int(x) for x in raw.split(",") if x.strip()]
    except ValueError:
        raise validation_error({"service_ids": ["Use a comma-separated list of ids."]}) from None
    if len(ids) > 200:
        raise validation_error({"service_ids": ["Too many ids."]})
    return ids


@provider_router.get("/staff/{staff_id}/impact", response=ImpactOut)
def staff_impact_view(
    request: HttpRequest,
    staff_id: int,
    action: Literal["deactivate", "remove_services"],
    service_ids: str = "",
) -> ImpactOut:
    user = owner_only(request)
    items = staff_impact.impact(user.pk, staff_id, action, parse_ids(service_ids))
    return ImpactOut(
        count=len(items),
        appointments=[
            ImpactItemOut(
                appointment_id=uuid.UUID(i.appointment_id),
                status=i.status,
                start=i.start,
                end=i.end,
                customer_name=i.customer_name,
                services=i.services,
            )
            for i in items
        ],
    )


@provider_router.post("/staff/{staff_id}/deactivate", response=ApplyOut)
def deactivate_staff(request: HttpRequest, staff_id: int, payload: DeactivateIn) -> ApplyOut:
    user = owner_only(request)
    result = staff_impact.deactivate(user.pk, staff_id, to_resolutions(payload.resolutions))
    return ApplyOut(reassigned=result.reassigned, cancelled=result.cancelled)


@provider_router.put("/staff/{staff_id}/services", response=ApplyOut)
def put_staff_services(request: HttpRequest, staff_id: int, payload: StaffServicesIn) -> ApplyOut:
    user = owner_only(request)
    result = staff_impact.set_services(
        user.pk, staff_id, payload.service_ids, to_resolutions(payload.resolutions)
    )
    return ApplyOut(reassigned=result.reassigned, cancelled=result.cancelled)


# ---- schedule: manual bookings, conflicts, calendar ----


@provider_router.post(
    "/businesses/{business_id}/manual-bookings", response={201: ProviderAppointmentOut}
)
def create_manual_booking(
    request: HttpRequest, business_id: uuid.UUID, payload: ManualBookingIn
) -> Status[ProviderAppointmentOut]:
    user = owner_only(request)
    try:
        phone = normalize_pk_mobile(payload.customer_phone)
    except ValueError as exc:
        raise validation_error({"customer_phone": [str(exc)]}) from exc
    appointment = provider_schedule.create_manual_booking(
        user.pk,
        str(business_id),
        customer_name=payload.customer_name,
        customer_phone=phone,
        service_ids=payload.service_ids,
        staff_id=payload.staff_id,
        start=payload.start,
    )
    fresh = provider_selectors.base_queryset().get(pk=appointment.pk)
    return Status(201, provider_selectors.appointment_outs([fresh])[0])


@provider_router.get("/businesses/{business_id}/conflicts", response=list[ConflictOut])
def list_conflicts(request: HttpRequest, business_id: uuid.UUID) -> list[ConflictOut]:
    user = owner_only(request)
    found = provider_schedule.conflicts(user.pk, str(business_id))
    outs = provider_selectors.appointment_outs([c.appointment for c in found])
    return [
        ConflictOut(appointment=out, reasons=c.reasons, actions=["reschedule", "cancel"])
        for out, c in zip(outs, found, strict=True)
    ]


def _days(week: list) -> list[DayHoursOut]:  # type: ignore[type-arg]
    return [
        DayHoursOut(
            weekday=d.weekday,
            shifts=[
                ShiftOut(opens_at=o.strftime("%H:%M"), closes_at=c.strftime("%H:%M"))
                for o, c in d.shifts
            ],
        )
        for d in week
    ]


class ScheduleQuery(Schema):
    from_date: date = Field(alias="from")
    to_date: date = Field(alias="to")
    staff_id: int | None = None


@provider_router.get("/businesses/{business_id}/schedule", response=ScheduleOut)
def get_schedule(
    request: HttpRequest, business_id: uuid.UUID, params: Query[ScheduleQuery]
) -> ScheduleOut:
    user = owner_only(request)
    sched = provider_schedule.schedule(
        user.pk, str(business_id), params.from_date, params.to_date, params.staff_id
    )
    return ScheduleOut(
        timezone=sched.timezone,
        from_date=sched.from_date,
        to_date=sched.to_date,
        hours=_days(sched.hours),
        staff_hours={str(k): _days(v) for k, v in sched.staff_hours.items()},
        blocks=[
            BlockOut(id=i, kind=k, staff_id=sid, starts_at=a, ends_at=b, reason=r)
            for i, k, sid, a, b, r in sched.blocks
        ],
        appointments=provider_selectors.appointment_outs(sched.appointments),
        holds_count=sched.holds_count,
    )
