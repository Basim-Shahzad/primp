from datetime import datetime
from typing import Annotated

from ninja import Field, Schema
from pydantic import StringConstraints


class TemplateOut(Schema):
    id: int
    key: str
    channel: str
    language: str
    subject: str
    body: str
    is_active: bool
    version: int
    placeholders: list[str]


class TemplatePatchIn(Schema):
    version: int = Field(ge=1)
    subject: Annotated[str, StringConstraints(max_length=200)] | None = None
    body: Annotated[str, StringConstraints(max_length=4000)] | None = None
    is_active: bool | None = None


class TemplatePreviewIn(Schema):
    subject: Annotated[str, StringConstraints(max_length=200)] = ""
    body: Annotated[str, StringConstraints(min_length=1, max_length=4000)]


class TemplatePreviewOut(Schema):
    subject: str
    body: str


class RuleOut(Schema):
    id: int
    action: str
    kind: str
    points: int
    unit_amount: int | None
    description: str
    is_active: bool
    valid_from: datetime | None
    valid_to: datetime | None
    version: int


class RulePatchIn(Schema):
    version: int = Field(ge=1)
    points: int | None = Field(None, ge=0, le=100_000)
    unit_amount: int | None = Field(None, ge=1, le=1_000_000)
    description: Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)] | None = (
        None
    )
    is_active: bool | None = None
    valid_from: datetime | None = None
    valid_to: datetime | None = None
