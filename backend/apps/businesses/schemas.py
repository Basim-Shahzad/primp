import uuid
from datetime import datetime
from typing import Any

from ninja import Schema


class SearchItemOut(Schema):
    id: uuid.UUID
    slug: str
    name: str
    area: str | None
    distance_km: float | None
    rating_avg: float
    rating_count: int
    starting_price: int | None
    is_verified: bool
    cover_photo_url: str | None
    next_available_slot: datetime | None
    is_wishlisted: bool | None


class SuggestionOut(Schema):
    type: str
    label: str
    params: dict[str, Any]
    count: int | None = None


class SearchOut(Schema):
    items: list[SearchItemOut]
    next_cursor: str | None
    suggestions: list[SuggestionOut]


class PhotoOut(Schema):
    id: int
    url: str
    is_cover: bool


class CategoryRefOut(Schema):
    id: int
    name: str


class ServiceOut(Schema):
    id: int
    name: str
    description: str
    price: int
    is_from_price: bool
    duration_minutes: int


class ServiceGroupOut(Schema):
    category: CategoryRefOut | None
    services: list[ServiceOut]


class StaffOut(Schema):
    id: int
    name: str
    photo_url: str | None
    bio: str
    service_ids: list[int]


class ShiftOut(Schema):
    opens_at: str
    closes_at: str


class DayHoursOut(Schema):
    weekday: int
    shifts: list[ShiftOut]


class ClosureOut(Schema):
    starts_at: datetime
    ends_at: datetime
    reason: str
    kind: str


class LocationOut(Schema):
    lat: float
    lng: float


class AreaRefOut(Schema):
    id: int
    name: str


class CityRefOut(Schema):
    id: int
    name: str
    slug: str


class BookingRulesOut(Schema):
    confirm_mode: str
    cancellation_notice_hours: int
    lead_time_minutes: int
    max_advance_days: int
    slot_interval_minutes: int
    staff_level_booking: bool


class RatingOut(Schema):
    avg: float
    count: int
    distribution: dict[str, int]


class BusinessPageOut(Schema):
    id: uuid.UUID
    slug: str
    name: str
    category: CategoryRefOut
    description: str
    address: str
    phone: str
    whatsapp: str
    gender_type: str
    timezone: str
    location: LocationOut
    area: AreaRefOut | None
    city: CityRefOut
    is_verified: bool
    photos: list[PhotoOut]
    services: list[ServiceGroupOut]
    staff: list[StaffOut]
    hours: list[DayHoursOut]
    closures: list[ClosureOut]
    is_open_now: bool
    booking: BookingRulesOut
    rating: RatingOut
