import uuid
from datetime import datetime
from typing import Any, Literal

from ninja import Field, Schema

from apps.accounts.schemas import Email
from apps.backoffice.pagination import PageParams


class UserQuery(PageParams):
    q: str | None = Field(None, min_length=1, max_length=100)
    role: Literal["customer", "business_owner", "staff", "admin", "super_admin"] | None = None
    status: Literal["active", "suspended", "deleted"] | None = None
    phone_verified: bool | None = None
    email_verified: bool | None = None
    created_from: datetime | None = None
    created_to: datetime | None = None
    sort: str = "-created_at"


class UserListItem(Schema):
    id: uuid.UUID
    email: str | None
    phone: str | None
    name: str
    role: str
    status: str
    is_email_verified: bool
    is_phone_verified: bool
    created_at: datetime


class UserPage(Schema):
    items: list[UserListItem]
    total: int
    limit: int
    offset: int


class LoginRow(Schema):
    at: datetime
    ip: str | None
    user_agent: str


class DeletionRow(Schema):
    id: int
    status: str
    reason: str
    requested_at: datetime


class SuspensionOut(Schema):
    reason: str
    suspended_at: datetime
    suspended_by: uuid.UUID | None


class UserDetail(UserListItem):
    photo_url: str | None
    no_show_count: int
    bookings_by_status: dict[str, int]
    recent_bookings: list[dict[str, Any]]
    reviews: list[dict[str, Any]]
    points: dict[str, Any]
    tickets: list[dict[str, Any]]
    login_history: list[LoginRow]
    deletion_requests: list[DeletionRow]
    suspension: SuspensionOut | None


class DeletionQuery(PageParams):
    status: Literal["pending", "processed", "cancelled"] | None = None


class DeletionItem(Schema):
    id: int
    user_id: uuid.UUID
    user_name: str
    status: str
    reason: str
    requested_at: datetime


class DeletionPage(Schema):
    items: list[DeletionItem]
    total: int
    limit: int
    offset: int


class AdminCreateIn(Schema):
    email: Email
    name: str = Field(min_length=1, max_length=100)
    group: Literal["support_agent", "ops_admin", "super_admin"]


class AdminGroupIn(Schema):
    group: Literal["support_agent", "ops_admin", "super_admin"]


class AdminOut(Schema):
    id: uuid.UUID
    email: str | None
    name: str
    role: str
    groups: list[str]
    status: str


class CountOut(Schema):
    revoked_sessions: int
