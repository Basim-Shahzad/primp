import uuid
from datetime import datetime

from ninja import Schema


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
