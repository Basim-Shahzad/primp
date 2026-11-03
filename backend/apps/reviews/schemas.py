import uuid
from datetime import datetime

from ninja import Field, Schema


class ReplyOut(Schema):
    body: str
    created_at: datetime


class ReviewOut(Schema):
    id: uuid.UUID
    rating: int
    comment: str
    photos: list[str]
    author_name: str
    created_at: datetime
    edited_at: datetime | None
    reply: ReplyOut | None


class ReviewPageOut(Schema):
    items: list[ReviewOut]
    next_cursor: str | None


class MyReviewOut(Schema):
    id: uuid.UUID
    appointment_id: uuid.UUID
    business_id: uuid.UUID
    rating: int
    comment: str
    photos: list[str]
    status: str
    created_at: datetime
    edited_at: datetime | None
    can_edit_until: datetime


class ReviewCreateIn(Schema):
    rating: int = Field(ge=1, le=5)
    comment: str = Field("", max_length=1000)


class ReviewEditIn(Schema):
    rating: int | None = Field(None, ge=1, le=5)
    comment: str | None = Field(None, max_length=1000)


class FlagIn(Schema):
    reason: str = Field(min_length=1, max_length=20)


class FlagOut(Schema):
    id: int
    reason: str
