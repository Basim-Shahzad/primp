"""Request/response schemas for the provider (business owner) API."""

import uuid
from datetime import date, datetime, time
from typing import Annotated, Any, Literal

from ninja import Field, Schema
from pydantic import StringConstraints

from .models import Business, BusinessDocument
from .provider_validators import E164, Address, Description, Text150

# ---- business ----


class BusinessCreateIn(Schema):
    name: Text150
    category_id: int
    gender_type: str = Field(pattern="^(women|men|unisex)$")
    description: Description = ""
    city_id: int


class OwnerBusinessOut(Schema):
    id: uuid.UUID
    name: str
    slug: str
    category_id: int
    city_id: int
    area_id: int | None
    description: str
    address: str
    phone: str
    whatsapp: str
    gender_type: str
    lat: float
    lng: float
    logo_url: str | None
    status: str
    status_message: str
    is_listed: bool
    is_verified: bool
    verified_at: datetime | None
    revoked_reason: str
    pending_review_changes: dict[str, Any] | None
    created_at: datetime

    @staticmethod
    def from_business(b: Business) -> "OwnerBusinessOut":
        from django.core.files.storage import default_storage

        return OwnerBusinessOut(
            id=b.public_id,
            name=b.name,
            slug=b.slug,
            category_id=b.category_id,
            city_id=b.city_id,
            area_id=b.area_id,
            description=b.description,
            address=b.address,
            phone=b.phone,
            whatsapp=b.whatsapp,
            gender_type=b.gender_type,
            lat=b.location.y,
            lng=b.location.x,
            logo_url=str(default_storage.url(b.logo)) if b.logo else None,
            status=b.verification_status,
            status_message=b.status_message,
            is_listed=b.is_listed,
            is_verified=b.is_verified,
            verified_at=b.verified_at,
            revoked_reason=b.revoked_reason,
            pending_review_changes=b.pending_review_changes,
            created_at=b.created_at,
        )


class ChecklistItemOut(Schema):
    key: str
    required: bool
    done: bool
    missing: list[str]


class OnboardingOut(Schema):
    status: str
    status_message: str
    completed_steps: list[str]
    last_step: str
    is_complete: bool
    items: list[ChecklistItemOut]


class SubmitIn(Schema):
    accept_provider_agreement: bool
    accept_commission_terms: bool
    terms_version: Annotated[str, StringConstraints(max_length=20)]


class StarterItemOut(Schema):
    id: int
    category_id: int
    name: str
    description: str
    price: int
    duration_minutes: int


class DocumentOut(Schema):
    id: int
    kind: str
    status: str
    reviewer_note: str
    content_type: str
    size: int
    url: str
    created_at: datetime

    @staticmethod
    def from_document(d: BusinessDocument) -> "DocumentOut":
        from . import files

        return DocumentOut(
            id=d.pk,
            kind=d.kind,
            status=d.status,
            reviewer_note=d.reviewer_note,
            content_type=d.content_type,
            size=d.size,
            url=files.signed_url(d.pk),
            created_at=d.created_at,
        )


class PhotoOutOwner(Schema):
    id: int
    url: str
    medium: str
    thumb: str
    is_cover: bool
    sort_order: int


# ---- profile ----


class ProfilePatchIn(Schema):
    name: Text150 | None = None
    category_id: int | None = None
    description: Description | None = None
    phone: E164 | None = None
    whatsapp: E164 | None = None
    address: Address | None = None
    gender_type: Literal["women", "men", "unisex"] | None = None
    city_id: int | None = None
    area_id: int | None = None
    lat: float | None = Field(None, ge=-90, le=90)
    lng: float | None = Field(None, ge=-180, le=180)


class PhotoOrderIn(Schema):
    photo_ids: list[int] = Field(max_length=20)


class ServiceOrderIn(Schema):
    service_ids: list[int] = Field(max_length=200)


Duration = Annotated[int, Field(ge=5, le=600, multiple_of=5)]
Price = Annotated[int, Field(ge=0, le=10_000_000)]
GenderLit = Literal["women", "men", "unisex"]


class ServiceIn(Schema):
    name: Text150
    description: Annotated[str, StringConstraints(strip_whitespace=True, max_length=1000)] = ""
    category_id: int | None = None
    price: Price
    is_from_price: bool = False
    duration_minutes: Duration
    gender: GenderLit = "unisex"


class ServicePatchIn(Schema):
    name: Text150 | None = None
    description: (
        Annotated[str, StringConstraints(strip_whitespace=True, max_length=1000)] | None
    ) = None
    category_id: int | None = None
    price: Price | None = None
    is_from_price: bool | None = None
    duration_minutes: Duration | None = None
    gender: GenderLit | None = None
    is_active: bool | None = None


class ServiceOut(Schema):
    id: int
    name: str
    description: str
    category_id: int | None
    price: int
    is_from_price: bool
    duration_minutes: int
    gender: str
    is_active: bool
    sort_order: int


class ServiceDeleteOut(Schema):
    deactivated: bool


class ShiftSchema(Schema):
    opens_at: time
    closes_at: time


class DaySchema(Schema):
    weekday: int = Field(ge=0, le=6)
    shifts: list[ShiftSchema] = Field(default_factory=list, max_length=10)


class HoursIn(Schema):
    days: list[DaySchema] = Field(max_length=7)


class HoursOut(Schema):
    days: list[DaySchema]


class ClosureIn(Schema):
    starts_on: date
    ends_on: date
    message: Annotated[str, StringConstraints(strip_whitespace=True, max_length=255)] = ""


class ClosureOut(Schema):
    id: int
    kind: str
    starts_at: datetime
    ends_at: datetime
    message: str


class BookingSettingsIn(Schema):
    confirm_mode: Literal["instant", "manual"] | None = None
    cancellation_notice_hours: int | None = Field(None, ge=0, le=72)
    lead_time_minutes: int | None = Field(None, ge=0, le=1440)
    max_advance_days: int | None = Field(None, ge=1, le=180)
    slot_interval_minutes: Literal[15, 30, 60] | None = None
    buffer_minutes: int | None = Field(None, ge=0, le=60)
    staff_level_booking: bool | None = None
    parallel_capacity: int | None = Field(None, ge=1, le=50)


class BookingSettingsOut(Schema):
    confirm_mode: str
    cancellation_notice_hours: int
    lead_time_minutes: int
    max_advance_days: int
    slot_interval_minutes: int
    buffer_minutes: int
    staff_level_booking: bool
    parallel_capacity: int


# ---- staff ----


class StaffIn(Schema):
    name: Text150
    bio: Annotated[str, StringConstraints(strip_whitespace=True, max_length=1000)] = ""
    service_ids: list[int] = Field(default_factory=list, max_length=200)


class StaffPatchIn(Schema):
    name: Text150 | None = None
    bio: Annotated[str, StringConstraints(strip_whitespace=True, max_length=1000)] | None = None
    is_active: bool | None = None


class StaffOut(Schema):
    id: int
    name: str
    bio: str
    photo_url: str | None
    is_active: bool
    service_ids: list[int]
    has_hours_override: bool
