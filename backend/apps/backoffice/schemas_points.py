import uuid
from datetime import datetime
from typing import Annotated, Literal

from ninja import Field, Schema
from pydantic import StringConstraints

from .pagination import PageParams

Category = Literal["goodwill", "correction", "fraud_reversal", "other"]


class PreviewIn(Schema):
    customer_id: uuid.UUID
    amount: int
    category: Category = "goodwill"


class PreviewOut(Schema):
    current_balance: int
    resulting_balance: int
    allowed: bool
    problems: list[str]
    daily_limit: int
    daily_used: int


class AdjustIn(Schema):
    customer_id: uuid.UUID
    amount: int
    category: Category
    note: Annotated[str, StringConstraints(strip_whitespace=True, max_length=600)]


class AdjustOut(Schema):
    id: int
    customer_id: uuid.UUID
    delta: int
    balance_after: int
    category: str
    replayed: bool


class AdjustmentsQuery(PageParams):
    admin_id: uuid.UUID | None = None
    customer_id: uuid.UUID | None = None
    created_from: datetime | None = None
    created_to: datetime | None = None
    sort: str = Field("-created_at", max_length=20)


class AdjustmentRow(Schema):
    id: int
    customer_id: uuid.UUID
    admin_id: uuid.UUID | None
    delta: int
    category: str
    note: str
    created_at: datetime


class AdjustmentPage(Schema):
    items: list[AdjustmentRow]
    total: int
    limit: int
    offset: int
