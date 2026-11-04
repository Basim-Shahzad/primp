import uuid
from datetime import datetime
from typing import Any

from ninja import Field, Schema

from .pagination import PageParams


class SettingOut(Schema):
    key: str
    description: str
    value: Any
    default: Any
    is_default: bool
    version: int
    value_schema: dict[str, Any]
    updated_by: uuid.UUID | None
    updated_at: datetime | None


class SettingPatchIn(Schema):
    value: Any
    version: int = Field(ge=0)


class SettingChangedOut(Schema):
    key: str
    value: Any
    version: int


class HistoryRow(Schema):
    version: int
    old_value: Any
    new_value: Any
    changed_by: uuid.UUID | None
    changed_at: datetime


class HistoryPage(Schema):
    items: list[HistoryRow]
    total: int
    limit: int
    offset: int


class HistoryQuery(PageParams):
    pass
