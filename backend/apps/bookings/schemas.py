import uuid
from datetime import date, datetime

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


class BookIn(Schema):
    hold_token: str = Field(min_length=1, max_length=2000)
