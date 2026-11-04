"""Loyalty reads for the backoffice."""

import uuid
from datetime import datetime
from typing import Any

from django.db.models import QuerySet, Sum

from .models import PointTransaction
from .services import ADJUSTMENT, ADJUSTMENT_CATEGORIES, adjust, find_adjustment, summary

__all__ = ["ADJUSTMENT_CATEGORIES", "adjust", "find_adjustment", "summary"]


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


ADJUSTMENT_SORTS = {"created_at": "created_at", "-created_at": "-created_at"}


def balance(customer_id: int) -> int:
    return summary(customer_id)[0]


def adjusted_between(admin_id: int, start: datetime, end: datetime) -> int:
    """Sum of |points| this admin adjusted in [start, end) (for the daily limit)."""
    rows = PointTransaction.objects.filter(
        admin_id=admin_id, reason=ADJUSTMENT, created_at__gte=start, created_at__lt=end
    ).values_list("delta", flat=True)
    return sum(abs(d) for d in rows)


def list_adjustments(
    *,
    admin_ref: str | None,
    customer_ref: str | None,
    created_from: datetime | None,
    created_to: datetime | None,
    sort: str,
) -> QuerySet[PointTransaction]:
    qs = PointTransaction.objects.filter(reason=ADJUSTMENT).select_related(
        "customer", "customer__profile", "admin", "admin__profile"
    )
    if admin_ref:
        qs = qs.filter(admin__public_id=uuid.UUID(admin_ref))
    if customer_ref:
        qs = qs.filter(customer__public_id=uuid.UUID(customer_ref))
    if created_from:
        qs = qs.filter(created_at__gte=created_from)
    if created_to:
        qs = qs.filter(created_at__lte=created_to)
    return qs.order_by(ADJUSTMENT_SORTS[sort], "-id" if sort.startswith("-") else "id")


def total_adjusted(customer_id: int) -> int:
    value = PointTransaction.objects.filter(customer_id=customer_id, reason=ADJUSTMENT).aggregate(
        t=Sum("delta")
    )["t"]
    return int(value or 0)


# ---- point rule editing -----------------------------------------------------------------

from django.db import transaction  # noqa: E402

from .models import PointRule  # noqa: E402


class StaleRule(Exception):
    pass


class RuleError(Exception):
    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field
        self.message = message


RULE_FIELDS = ("points", "unit_amount", "description", "is_active", "valid_from", "valid_to")


def rule_snapshot(r: PointRule) -> dict[str, Any]:
    return {
        "id": r.pk,
        "action": r.action,
        "kind": r.kind,
        "points": r.points,
        "unit_amount": r.unit_amount,
        "description": r.description,
        "is_active": r.is_active,
        "valid_from": r.valid_from.isoformat() if r.valid_from else None,
        "valid_to": r.valid_to.isoformat() if r.valid_to else None,
        "version": r.version,
    }


def list_rules() -> list[PointRule]:
    return list(PointRule.objects.order_by("action", "id"))


def update_rule(
    rule_id: int, version: int, changes: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    with transaction.atomic():
        r = PointRule.objects.select_for_update().filter(pk=rule_id).first()
        if r is None:
            return None
        if r.version != version:
            raise StaleRule
        before = rule_snapshot(r)
        for field in RULE_FIELDS:
            if field in changes:
                setattr(r, field, changes[field])
        if r.kind == "per_amount" and not r.unit_amount:
            raise RuleError("unit_amount", "A per-amount rule needs a unit amount.")
        if r.valid_from and r.valid_to and r.valid_to <= r.valid_from:
            raise RuleError("valid_to", "The end must be after the start.")
        r.version += 1
        r.save(update_fields=[*RULE_FIELDS, "version", "updated_at"])
        return before, rule_snapshot(r)
