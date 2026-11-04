"""Schemas for the provider-side booking endpoints."""

import uuid
from datetime import datetime
from typing import Annotated, Literal

from ninja import Field, Schema
from pydantic import StringConstraints


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
