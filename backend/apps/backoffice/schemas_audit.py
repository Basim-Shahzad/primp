import uuid
from datetime import datetime
from typing import Any

from ninja import Field, Schema

from .pagination import PageParams


class AuditQuery(PageParams):
    actor: uuid.UUID | None = None
    action: str | None = Field(None, min_length=1, max_length=100)
    target_type: str | None = Field(None, min_length=1, max_length=100)
    target_id: str | None = Field(None, min_length=1, max_length=64)
    created_from: datetime | None = None
    created_to: datetime | None = None
    sort: str = "-created_at"


class ExportQuery(Schema):
    actor: uuid.UUID | None = None
    action: str | None = Field(None, min_length=1, max_length=100)
    target_type: str | None = Field(None, min_length=1, max_length=100)
    target_id: str | None = Field(None, min_length=1, max_length=64)
    created_from: datetime | None = None
    created_to: datetime | None = None


class AuditRow(Schema):
    id: int
    created_at: datetime
    actor: uuid.UUID | None
    action: str
    target_type: str
    target_id: str
    ip_address: str | None
    request_id: str
    before: Any
    after: Any


class AuditPage(Schema):
    items: list[AuditRow]
    total: int
    limit: int
    offset: int
