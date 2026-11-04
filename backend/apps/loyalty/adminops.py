"""Loyalty reads for the backoffice."""

from typing import Any

from .models import PointTransaction
from .services import summary


def points_overview(customer_id: int, limit: int = 10) -> dict[str, Any]:
    balance, lifetime = summary(customer_id)
    recent = [
        {
            "id": t.pk,
            "delta": t.delta,
            "reason": t.reason,
            "reference_type": t.reference_type,
            "created_at": t.created_at,
        }
        for t in PointTransaction.objects.filter(customer_id=customer_id).order_by(
            "-created_at", "-id"
        )[:limit]
    ]
    return {"balance": balance, "lifetime_earned": lifetime, "recent": recent}
