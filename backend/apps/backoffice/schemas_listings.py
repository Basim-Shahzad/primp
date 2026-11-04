import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from ninja import Field, Schema
from pydantic import StringConstraints

from .pagination import PageParams

Text500 = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]


class QueueQuery(PageParams):
    city_id: int | None = None
    category_id: int | None = None
    kind: Literal["new_listing", "change_request"] | None = None
    status: Literal["open", "in_review", "closed"] | None = None
    sort: str = "submitted_at"


class QueueBusiness(Schema):
    id: uuid.UUID
    name: str
    city: str
    category: str
    status: str


class QueueItemOut(Schema):
    id: uuid.UUID
    kind: str
    status: str
    business: QueueBusiness
    assigned_to: uuid.UUID | None
    submitted_at: datetime
    waiting_seconds: int
    version: int
    outcome: str


class QueuePage(Schema):
    items: list[QueueItemOut]
    total: int
    limit: int
    offset: int


class DecisionOut(Schema):
    action: str
    admin_id: uuid.UUID | None
    checklist: dict[str, bool] | None
    message: str
    created_at: datetime


class NoteOut(Schema):
    id: int
    author_id: uuid.UUID | None
    body: str
    created_at: datetime


class DuplicateOut(Schema):
    business_id: uuid.UUID
    name: str
    status: str
    reasons: list[str]


class QueueDetail(QueueItemOut):
    preview: dict[str, Any]
    private: dict[str, Any]
    documents: list[dict[str, Any]] | None
    duplicates: list[DuplicateOut]
    decisions: list[DecisionOut]
    notes: list[NoteOut]
    previous_items: list[QueueItemOut]


class Checklist(Schema):
    identity_matches: bool
    address_plausible: bool
    photos_genuine: bool
    category_correct: bool
    no_duplicate: bool


class ClaimIn(Schema):
    version: int = Field(ge=1)


class ApproveIn(ClaimIn):
    checklist: Checklist


class RejectIn(ClaimIn):
    reason: Text500
    checklist: Checklist | None = None


class InfoIn(ClaimIn):
    message: Text500
    checklist: Checklist | None = None


class NoteIn(Schema):
    body: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]
