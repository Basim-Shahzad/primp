from datetime import date, datetime

from ninja import Schema


class SlotOut(Schema):
    start: datetime
    end: datetime
    staff_ids: list[int]


class AvailabilityOut(Schema):
    date: date
    timezone: str
    duration_minutes: int
    slots: list[SlotOut]
