"""Provider-side booking endpoints (staff impact workflow now; dashboard actions follow)."""

import uuid
from datetime import date, datetime
from typing import Literal

from django.http import HttpRequest
from ninja import Field, Query, Router, Schema, Status

from apps.accounts.auth import jwt_auth
from apps.accounts.phone import normalize_pk_mobile
from apps.accounts.roles import Role
from apps.core.errors import validation_error
from apps.core.pagination import DEFAULT_LIMIT, MAX_LIMIT
from apps.core.permissions import require_role

from . import (
    provider_actions,
    provider_dashboard,
    provider_schedule,
    provider_selectors,
    staff_impact,
)
from .provider_schemas import (
    ApplyOut,
    BlockOut,
    CancelIn,
    ChangesOut,
    ConflictOut,
    DayHoursOut,
    DeactivateIn,
    ImpactItemOut,
    ImpactOut,
    ManualBookingIn,
    NeedsActionOut,
    NeedsActionRescheduleOut,
    NoteIn,
    OwnerRescheduleIn,
    ProviderAppointmentDetailOut,
    ProviderAppointmentOut,
    ProviderAppointmentPageOut,
    RejectIn,
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


# ---- dashboard ----


class AppointmentListQuery(Schema):
    from_date: date | None = Field(None, alias="from")
    to_date: date | None = Field(None, alias="to")
    staff_id: int | None = None
    status: str | None = Field(None, max_length=200)
    service_id: int | None = None
    q: str | None = Field(None, max_length=200)
    order: Literal["asc", "desc"] = "asc"
    cursor: str | None = Field(None, max_length=2000)
    limit: int = Field(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT)


def _outs(rows: list) -> list[ProviderAppointmentOut]:  # type: ignore[type-arg]
    return provider_selectors.appointment_outs(rows)


@provider_router.get("/businesses/{business_id}/appointments", response=ProviderAppointmentPageOut)
def list_appointments(
    request: HttpRequest, business_id: uuid.UUID, params: Query[AppointmentListQuery]
) -> ProviderAppointmentPageOut:
    user = owner_only(request)
    page = provider_dashboard.list_appointments(
        user.pk,
        str(business_id),
        provider_dashboard.ListParams(
            from_date=params.from_date,
            to_date=params.to_date,
            staff_id=params.staff_id,
            status=params.status,
            service_id=params.service_id,
            q=params.q,
            order=params.order,
            cursor=params.cursor,
            limit=params.limit,
        ),
    )
    return ProviderAppointmentPageOut(items=_outs(page.items), next_cursor=page.next_cursor)


@provider_router.get(
    "/businesses/{business_id}/appointments/today", response=list[ProviderAppointmentOut]
)
def appointments_today(
    request: HttpRequest, business_id: uuid.UUID
) -> list[ProviderAppointmentOut]:
    user = owner_only(request)
    return _outs(provider_dashboard.today(user.pk, str(business_id)))


@provider_router.get("/businesses/{business_id}/appointments/needs-action", response=NeedsActionOut)
def appointments_needing_action(request: HttpRequest, business_id: uuid.UUID) -> NeedsActionOut:
    user = owner_only(request)
    found = provider_dashboard.needs_action(user.pk, str(business_id))
    request_rows = [a for _r, a in found.requests]
    outs = _outs(found.pending + request_rows)
    pending_outs, request_outs = outs[: len(found.pending)], outs[len(found.pending) :]
    return NeedsActionOut(
        unread_count=found.unread_count,
        pending=pending_outs,
        reschedule_requests=[
            NeedsActionRescheduleOut(request=provider_dashboard.request_out(r), appointment=out)
            for (r, _a), out in zip(found.requests, request_outs, strict=True)
        ],
    )


@provider_router.get("/businesses/{business_id}/appointments/changes", response=ChangesOut)
def appointment_changes(
    request: HttpRequest, business_id: uuid.UUID, since: datetime
) -> ChangesOut:
    user = owner_only(request)
    server_time, rows = provider_dashboard.changes_since(user.pk, str(business_id), since)
    return ChangesOut(server_time=server_time, appointments=_outs(rows))


@provider_router.get("/appointments/{appointment_id}", response=ProviderAppointmentDetailOut)
def appointment_detail(
    request: HttpRequest, appointment_id: uuid.UUID
) -> ProviderAppointmentDetailOut:
    user = owner_only(request)
    return provider_dashboard.detail(user.pk, str(appointment_id))


# ---- actions ----


def _one(appointment: object) -> ProviderAppointmentOut:
    fresh = provider_selectors.base_queryset().get(pk=appointment.pk)  # type: ignore[attr-defined]
    return provider_selectors.appointment_outs([fresh])[0]


@provider_router.post("/appointments/{appointment_id}/accept", response=ProviderAppointmentOut)
def accept_appointment(request: HttpRequest, appointment_id: uuid.UUID) -> ProviderAppointmentOut:
    user = owner_only(request)
    return _one(provider_actions.accept(user.pk, str(appointment_id)))


@provider_router.post("/appointments/{appointment_id}/decline", response=ProviderAppointmentOut)
def decline_appointment(
    request: HttpRequest, appointment_id: uuid.UUID, payload: RejectIn
) -> ProviderAppointmentOut:
    user = owner_only(request)
    return _one(provider_actions.decline(user.pk, str(appointment_id), payload.reason))


@provider_router.post("/appointments/{appointment_id}/cancel", response=ProviderAppointmentOut)
def cancel_appointment(
    request: HttpRequest, appointment_id: uuid.UUID, payload: CancelIn
) -> ProviderAppointmentOut:
    user = owner_only(request)
    return _one(provider_actions.cancel(user.pk, str(appointment_id), payload.reason))


@provider_router.post("/appointments/{appointment_id}/reschedule", response=ProviderAppointmentOut)
def reschedule_appointment(
    request: HttpRequest, appointment_id: uuid.UUID, payload: OwnerRescheduleIn
) -> ProviderAppointmentOut:
    user = owner_only(request)
    return _one(
        provider_actions.reschedule(
            user.pk, str(appointment_id), start=payload.start, staff_id=payload.staff_id
        )
    )


@provider_router.post("/appointments/{appointment_id}/complete", response=ProviderAppointmentOut)
def complete_appointment(request: HttpRequest, appointment_id: uuid.UUID) -> ProviderAppointmentOut:
    user = owner_only(request)
    return _one(provider_actions.complete(user.pk, str(appointment_id)))


@provider_router.post("/appointments/{appointment_id}/no-show", response=ProviderAppointmentOut)
def no_show_appointment(request: HttpRequest, appointment_id: uuid.UUID) -> ProviderAppointmentOut:
    user = owner_only(request)
    return _one(provider_actions.no_show(user.pk, str(appointment_id)))


@provider_router.put("/appointments/{appointment_id}/note", response=ProviderAppointmentOut)
def note_appointment(
    request: HttpRequest, appointment_id: uuid.UUID, payload: NoteIn
) -> ProviderAppointmentOut:
    user = owner_only(request)
    return _one(provider_actions.set_note(user.pk, str(appointment_id), payload.note))


@provider_router.post(
    "/appointments/{appointment_id}/reschedule-request/accept", response=ProviderAppointmentOut
)
def accept_request(request: HttpRequest, appointment_id: uuid.UUID) -> ProviderAppointmentOut:
    user = owner_only(request)
    return _one(provider_actions.accept_reschedule_request(user.pk, str(appointment_id)))


@provider_router.post(
    "/appointments/{appointment_id}/reschedule-request/decline", response=ProviderAppointmentOut
)
def decline_request(request: HttpRequest, appointment_id: uuid.UUID) -> ProviderAppointmentOut:
    user = owner_only(request)
    return _one(provider_actions.decline_reschedule_request(user.pk, str(appointment_id)))
