from datetime import datetime

from ninja import Field, Schema

from apps.core.pagination import DEFAULT_LIMIT, MAX_LIMIT


class PreferenceOut(Schema):
    event: str
    label: str
    dashboard: bool
    sms: bool
    email: bool


class PreferenceUpdateIn(Schema):
    event: str = Field(min_length=1, max_length=60)
    sms: bool | None = None
    email: bool | None = None


class PreferencePatchIn(Schema):
    preferences: list[PreferenceUpdateIn] = Field(min_length=1, max_length=20)


class FeedQuery(Schema):
    cursor: str | None = Field(None, max_length=2000)
    limit: int = Field(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT)
    unread_only: bool = False


class FeedItemOut(Schema):
    id: int
    event: str
    title: str
    body: str
    created_at: datetime
    read_at: datetime | None
    status: str


class FeedOut(Schema):
    items: list[FeedItemOut]
    next_cursor: str | None
    unread_count: int


class ReadIn(Schema):
    ids: list[int] | None = Field(None, max_length=200)
    all: bool = False


class ReadOut(Schema):
    updated: int
    unread_count: int
