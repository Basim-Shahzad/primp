import uuid
from datetime import date, datetime
from typing import Annotated, Any, Literal

from ninja import Field, Schema
from pydantic import StringConstraints

from .pagination import PageParams

Subject = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Body = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]
UserType = Literal[
    "booking_dispute", "refund_request", "provider_complaint", "account_issue", "other"
]


class TicketCreateIn(Schema):
    type: UserType
    subject: Subject
    body: Body
    appointment_id: uuid.UUID | None = None
    business_id: uuid.UUID | None = None
    review_id: uuid.UUID | None = None


class MessageIn(Schema):
    body: Body


class OwnMessageOut(Schema):
    id: int
    from_support: bool
    body: str
    created_at: datetime


class OwnTicketOut(Schema):
    id: uuid.UUID
    type: str
    status: str
    priority: str
    subject: str
    created_at: datetime
    updated_at: datetime


class OwnTicketDetail(OwnTicketOut):
    messages: list[OwnMessageOut]


class OwnQuery(PageParams):
    status: Literal["open", "in_progress", "resolved", "closed"] | None = None


class OwnTicketPage(Schema):
    items: list[OwnTicketOut]
    total: int
    limit: int
    offset: int


# ---- admin


class AdminTicketQuery(PageParams):
    status: Literal["open", "in_progress", "resolved", "closed"] | None = None
    priority: Literal["low", "normal", "high", "urgent"] | None = None
    type: str | None = Field(None, max_length=25)
    assignee: str | None = Field(None, max_length=40)
    older_than_hours: int | None = Field(None, ge=0, le=24 * 365)
    sort: str = "priority"


class PersonRef(Schema):
    id: uuid.UUID
    name: str


class AdminTicketItem(OwnTicketOut):
    requester: PersonRef | None
    assigned_to: PersonRef | None
    version: int
    first_response_at: datetime | None
    resolved_at: datetime | None


class AdminTicketPage(Schema):
    items: list[AdminTicketItem]
    total: int
    limit: int
    offset: int


class AdminMessageOut(Schema):
    id: int
    author: PersonRef | None
    is_internal: bool
    body: str
    attachments: list[dict[str, str]]
    created_at: datetime


class TicketEventOut(Schema):
    kind: str
    actor_id: uuid.UUID | None
    from_value: str
    to_value: str
    created_at: datetime


class AdminTicketDetail(AdminTicketItem):
    linked: dict[str, Any]
    messages: list[AdminMessageOut]
    history: list[TicketEventOut]


class AssignIn(Schema):
    version: int = Field(ge=1)
    assignee_id: uuid.UUID | None = None


class StatusIn(Schema):
    version: int = Field(ge=1)
    status: Literal["open", "in_progress", "resolved", "closed"]


class AdminMessageIn(Schema):
    body: Body
    internal: bool = False


class StatsQuery(Schema):
    from_date: date | None = Field(None, alias="from")
    to_date: date | None = Field(None, alias="to")


class StatsOut(Schema):
    from_date: date
    to_date: date
    total: int
    by_status: dict[str, int]
    median_first_response_seconds: int | None
    median_resolution_seconds: int | None
    first_response_count: int
    resolved_count: int


class ModerateIn(Schema):
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
