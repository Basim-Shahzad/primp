"""Request/response schemas for the provider (business owner) API."""

import uuid
from datetime import datetime
from typing import Annotated, Any

from ninja import Field, Schema
from pydantic import StringConstraints

from .models import Business, BusinessDocument
from .provider_validators import Description, Text150

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
