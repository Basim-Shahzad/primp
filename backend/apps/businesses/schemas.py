import uuid
from datetime import datetime
from typing import Any

from ninja import Schema


class SearchItemOut(Schema):
    id: uuid.UUID
    slug: str
    name: str
    area: str | None
    distance_km: float | None
    rating_avg: float
    rating_count: int
    starting_price: int | None
    is_verified: bool
    cover_photo_url: str | None
    next_available_slot: datetime | None
    is_wishlisted: bool | None


class SuggestionOut(Schema):
    type: str
    label: str
    params: dict[str, Any]
    count: int | None = None


class SearchOut(Schema):
    items: list[SearchItemOut]
    next_cursor: str | None
    suggestions: list[SuggestionOut]
