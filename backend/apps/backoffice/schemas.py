import uuid
from typing import Annotated

from ninja import Schema
from pydantic import StringConstraints

from apps.accounts.schemas import Email, Password


class AdminLoginIn(Schema):
    email: Email
    password: Password


class ReauthIn(Schema):
    password: Annotated[str, StringConstraints(min_length=1, max_length=128)]


class ReauthOut(Schema):
    valid_for_seconds: int


class AdminMeOut(Schema):
    id: uuid.UUID
    email: str | None
    name: str
    role: str
    groups: list[str]
    permissions: list[str]
    reauth_valid: bool


class ReasonIn(Schema):
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
