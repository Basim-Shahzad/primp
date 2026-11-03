from datetime import datetime

from ninja import Schema


class PointsOut(Schema):
    balance: int
    lifetime_earned: int


class TransactionOut(Schema):
    id: int
    delta: int
    reason: str
    reference_type: str
    reference_id: str
    expires_at: datetime | None
    created_at: datetime


class TransactionPageOut(Schema):
    items: list[TransactionOut]
    next_cursor: str | None
