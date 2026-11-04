"""Manual points adjustments by admins (Plan 6.3, Step 8)."""

from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.accounts import adminops as accounts
from apps.accounts.roles import Role
from apps.core import audit
from apps.core.errors import AppError, validation_error
from apps.loyalty import adminops as loyalty

from ..permissions import AdminActor

PERMISSION = "points.adjust"
MIN_NOTE = 10
MAX_NOTE = 500


def _day_bounds(now: datetime) -> tuple[datetime, datetime]:
    tz = ZoneInfo(settings.BUSINESS_TIME_ZONE)
    local = now.astimezone(tz).date()
    start = datetime.combine(local, time.min, tzinfo=tz)
    return start, start + timedelta(days=1)


def max_amount() -> int:
    return int(settings.POINTS_ADJUST_MAX)


def daily_limit() -> int:
    return int(settings.POINTS_ADJUST_DAILY_LIMIT)


def _check_amount(amount: int) -> None:
    limit = max_amount()
    if amount == 0 or abs(amount) > limit:
        raise validation_error(
            {
                "amount": [
                    str(_("Enter a non-zero amount of at most %(max)d points.") % {"max": limit})
                ]
            }
        )


def _customer(ref: str, *, lock: bool = False) -> Any:
    user = accounts.get_user(ref, lock=lock)
    if user.role not in (Role.CUSTOMER, Role.BUSINESS_OWNER) or user.status == "deleted":
        raise validation_error(
            {"customer_id": [str(_("Points can only go to customer accounts."))]}
        )
    return user


def preview(actor: AdminActor, customer_ref: str, amount: int, category: str) -> dict[str, Any]:
    """What an adjustment would do. Writes nothing."""
    actor.require(PERMISSION, reauth=False)
    _check_amount(amount)
    user = _customer(customer_ref)
    current = loyalty.balance(user.pk)
    resulting = current + amount
    start, end = _day_bounds(timezone.now())
    used = loyalty.adjusted_between(actor.id, start, end)
    problems: list[str] = []
    if resulting < 0 and category != "fraud_reversal":
        problems.append("insufficient_points")
    if used + abs(amount) > daily_limit():
        problems.append("daily_limit_exceeded")
    return {
        "current_balance": current,
        "resulting_balance": resulting,
        "allowed": not problems,
        "problems": problems,
        "daily_limit": daily_limit(),
        "daily_used": used,
    }


def adjust(
    actor: AdminActor,
    *,
    customer_ref: str,
    amount: int,
    category: str,
    note: str,
    key: str,
) -> tuple[Any, bool, int]:
    """Returns (ledger row, created, balance after). Needs a recent re-authentication."""
    actor.require(PERMISSION)
    _check_amount(amount)
    note = note.strip()
    if not MIN_NOTE <= len(note) <= MAX_NOTE:
        raise validation_error(
            {
                "note": [
                    str(
                        _("Explain the reason in %(a)d to %(b)d characters.")
                        % {"a": MIN_NOTE, "b": MAX_NOTE}
                    )
                ]
            }
        )
    if category not in loyalty.ADJUSTMENT_CATEGORIES:
        raise validation_error({"category": [str(_("Unknown category."))]})
    if not 1 <= len(key) <= 64:
        raise validation_error(
            {"Idempotency-Key": [str(_("Send an Idempotency-Key of 1-64 characters."))]}
        )
    with transaction.atomic():
        accounts.get_user(
            actor.user.public_id, lock=True
        )  # serialises this admin's own adjustments
        user = _customer(customer_ref)
        replay = loyalty.find_adjustment(actor.id, key)
        if replay is None:
            start, end = _day_bounds(timezone.now())
            used = loyalty.adjusted_between(actor.id, start, end)
            if used + abs(amount) > daily_limit():
                raise AppError(
                    "daily_limit_exceeded",
                    _("This would exceed your daily adjustment limit (%(used)d of %(limit)d used).")
                    % {"used": used, "limit": daily_limit()},
                    403,
                )
        before = loyalty.balance(user.pk)
        entry, created = loyalty.adjust(
            customer_id=user.pk,
            delta=amount,
            category=category,
            note=note,
            admin_id=actor.id,
            key=key,
        )
        after = loyalty.balance(user.pk)
        if created:
            audit.record(
                "admin.points_adjusted",
                actor_id=actor.id,
                target_type="user",
                target_id=user.public_id,
                before={"balance": before},
                after={
                    "balance": after,
                    "delta": amount,
                    "category": category,
                    "note": note,
                    "transaction_id": entry.pk,
                },
                ip=actor.ip,
            )
    return entry, created, after
