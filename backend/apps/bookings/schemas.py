import uuid
from datetime import date, datetime
from typing import Literal

from ninja import Field, Schema
from pydantic import AwareDatetime


class SlotOut(Schema):
    start: datetime
    end: datetime
    staff_ids: list[int]


class AvailabilityOut(Schema):
    date: date
    timezone: str
    duration_minutes: int
    slots: list[SlotOut]


class HoldIn(Schema):
    business_id: uuid.UUID
    service_ids: list[int] = Field(min_length=1, max_length=10)
    staff_id: int | None = None
    start: AwareDatetime
    appointment_id: uuid.UUID | None = None  # set when holding a new time for a reschedule


class HoldOut(Schema):
    token: str
    expires_at: datetime
    business_id: uuid.UUID
    staff_id: int | None
    start: datetime
    end: datetime


class AppointmentServiceOut(Schema):
    id: int
    name: str
    price: int
    duration_minutes: int


class BusinessRefOut(Schema):
    id: uuid.UUID
    name: str
    slug: str


class StaffRefOut(Schema):
    id: int
    name: str


class AppointmentOut(Schema):
    id: uuid.UUID
    status: str
    business: BusinessRefOut
    staff: StaffRefOut | None
    start: datetime
    end: datetime
    total_price: int
    payment_method: str
    payment_status: str
    services: list[AppointmentServiceOut]
    created_at: datetime
    cancelled_at: datetime | None = None
    cancellation_reason: str = ""
    is_late_cancellation: bool = False


class BookIn(Schema):
    hold_token: str = Field(min_length=1, max_length=2000)


class CancelIn(Schema):
    reason: Literal[
        "change_of_plans", "found_alternative", "schedule_conflict", "price", "emergency", "other"
    ]
    note: str = Field("", max_length=500)


class PolicyOut(Schema):
    cancellation_notice_hours: int
    free_cancellation_until: datetime
    is_late_now: bool
    can_cancel: bool
    message: str


class RescheduleIn(Schema):
    hold_token: str = Field(min_length=1, max_length=2000)


class RescheduleRequestOut(Schema):
    id: int
    status: str
    new_start: datetime
    new_end: datetime


class RescheduleOut(Schema):
    applied: bool
    appointment: AppointmentOut
    request: RescheduleRequestOut | None
