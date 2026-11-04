"""Schemas for the provider-side booking endpoints."""

import uuid
from datetime import date, datetime
from typing import Annotated, Literal

from ninja import Field, Schema
from pydantic import AwareDatetime, StringConstraints


class ResolutionIn(Schema):
    appointment_id: uuid.UUID
    action: Literal["reassign", "cancel"]
    new_staff_id: int | None = None


class DeactivateIn(Schema):
    resolutions: list[ResolutionIn] = Field(default_factory=list, max_length=500)


class StaffServicesIn(Schema):
    service_ids: list[int] = Field(max_length=200)
    resolutions: list[ResolutionIn] = Field(default_factory=list, max_length=500)


class ImpactItemOut(Schema):
    appointment_id: uuid.UUID
    status: str
    start: datetime
    end: datetime
    customer_name: str
    services: list[str]


class ImpactOut(Schema):
    count: int
    appointments: list[ImpactItemOut]


class ApplyOut(Schema):
    reassigned: int
    cancelled: int


Reason = Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)]


# ---- appointments as the owner sees them ----


class ProviderServiceOut(Schema):
    id: int
    name: str
    price: int
    duration_minutes: int


class ProviderStaffOut(Schema):
    id: int
    name: str


class ProviderCustomerOut(Schema):
    user_id: uuid.UUID | None
    name: str
    phone: str | None


class ProviderAppointmentOut(Schema):
    id: uuid.UUID
    status: str
    source: str
    start: datetime
    end: datetime
    staff: ProviderStaffOut | None
    services: list[ProviderServiceOut]
    total_price: int
    payment_method: str
    payment_status: str
    customer: ProviderCustomerOut
    business_note: str
    cancellation_reason: str
    allowed_actions: list[str] = Field(default_factory=list)
    created_at: datetime


class ManualBookingIn(Schema):
    customer_name: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)
    ]
    customer_phone: str = Field(min_length=1, max_length=25)
    service_ids: list[int] = Field(min_length=1, max_length=10)
    staff_id: int | None = None
    start: AwareDatetime


class ConflictOut(Schema):
    appointment: ProviderAppointmentOut
    reasons: list[str]
    actions: list[str]


class BlockOut(Schema):
    id: int
    kind: str
    staff_id: int | None
    starts_at: datetime
    ends_at: datetime
    reason: str


class ShiftOut(Schema):
    opens_at: str
    closes_at: str


class DayHoursOut(Schema):
    weekday: int
    shifts: list[ShiftOut]


class ScheduleOut(Schema):
    timezone: str
    from_date: date
    to_date: date
    hours: list[DayHoursOut]
    staff_hours: dict[str, list[DayHoursOut]]
    blocks: list[BlockOut]
    appointments: list[ProviderAppointmentOut]
    holds_count: int


class TimelineOut(Schema):
    from_status: str
    to_status: str
    actor_role: str
    note: str
    created_at: datetime


class CustomerSummaryOut(Schema):
    user_id: uuid.UUID | None
    name: str
    phone: str | None
    previous_completed_visits: int
    no_show_count: int


class RescheduleRequestOwnerOut(Schema):
    id: int
    new_start: datetime
    new_end: datetime
    new_staff_id: int | None
    status: str
    created_at: datetime


class ProviderAppointmentDetailOut(ProviderAppointmentOut):
    timeline: list[TimelineOut]
    customer_summary: CustomerSummaryOut
    reschedule_request: RescheduleRequestOwnerOut | None


class ProviderAppointmentPageOut(Schema):
    items: list[ProviderAppointmentOut]
    next_cursor: str | None


class NeedsActionRescheduleOut(Schema):
    request: RescheduleRequestOwnerOut
    appointment: ProviderAppointmentOut


class NeedsActionOut(Schema):
    unread_count: int
    pending: list[ProviderAppointmentOut]
    reschedule_requests: list[NeedsActionRescheduleOut]


class ChangesOut(Schema):
    server_time: datetime
    appointments: list[ProviderAppointmentOut]


class RejectIn(Schema):
    reason: Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)] = ""


class CancelIn(Schema):
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]


class OwnerRescheduleIn(Schema):
    start: AwareDatetime
    staff_id: int | None = None


class NoteIn(Schema):
    note: Annotated[str, StringConstraints(strip_whitespace=True, max_length=1000)]
