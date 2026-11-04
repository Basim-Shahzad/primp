"""Earning-only loyalty: rules, an idempotent ledger and an account summary."""

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from django.db import IntegrityError, transaction
from django.db.models import Q, Sum
from django.utils import timezone

from apps.core.errors import AppError, conflict

from .models import LoyaltyAccount, PointRule, PointTransaction, RuleKind

logger = logging.getLogger(__name__)

EXPIRY = timedelta(days=365)
BOOKING = "booking"
REVIEW = "review"
FIRST_BOOKING = "first_booking"
FIFTH_BOOKING = "fifth_booking"
TENTH_BOOKING = "tenth_booking"
PROFILE_COMPLETION = "profile_completion"
MILESTONES = ((1, FIRST_BOOKING), (5, FIFTH_BOOKING), (10, TENTH_BOOKING))
REVERSAL_SUFFIX = "_reversal"


def active_rule(action: str, now: datetime | None = None) -> PointRule | None:
    """The newest active rule for the action whose validity window contains `now`."""
    now = now or timezone.now()
    return (
        PointRule.objects.filter(action=action, is_active=True)
        .filter(Q(valid_from__isnull=True) | Q(valid_from__lte=now))
        .filter(Q(valid_to__isnull=True) | Q(valid_to__gt=now))
        .order_by("-id")
        .first()
    )


def points_for(rule: PointRule, amount: int = 0) -> int:
    """floor(amount * points / unit) for per-amount rules, else the flat points."""
    if rule.kind == RuleKind.PER_AMOUNT:
        assert rule.unit_amount
        return max(amount, 0) * rule.points // rule.unit_amount
    return rule.points


@dataclass(frozen=True)
class Credit:
    transaction: PointTransaction
    created: bool


def _account(customer_id: int) -> LoyaltyAccount:
    account, _created = LoyaltyAccount.objects.select_for_update().get_or_create(
        customer_id=customer_id
    )
    return account


def credit(
    *,
    customer_id: int,
    action: str,
    reference_type: str,
    reference_id: str | int,
    amount: int = 0,
    now: datetime | None = None,
) -> PointTransaction | None:
    """Credit points for an action. Returns None when no rule applies, the rule yields 0 points
    or this (action, reference) was already credited (so duplicate events never double-credit)."""
    now = now or timezone.now()
    rule = active_rule(action, now)
    if rule is None:
        return None
    points = points_for(rule, amount)
    if points <= 0:
        return None
    ref = str(reference_id)
    with transaction.atomic():
        account = _account(customer_id)
        try:
            with transaction.atomic():
                entry = PointTransaction.objects.create(
                    customer_id=customer_id,
                    delta=points,
                    reason=action,
                    reference_type=reference_type,
                    reference_id=ref,
                    expires_at=now + EXPIRY,
                    remaining=points,
                    created_at=now,
                )
        except IntegrityError:
            return None
        account.balance += points
        account.lifetime_earned += points
        account.save(update_fields=["balance", "lifetime_earned", "updated_at"])
    return entry


def reverse_points(reference_type: str, reference_id: str | int) -> list[PointTransaction]:
    """Reverse every credit tied to a reference (refund/dispute). Idempotent."""
    ref = str(reference_id)
    reversals: list[PointTransaction] = []
    with transaction.atomic():
        credits = list(
            PointTransaction.objects.filter(
                reference_type=reference_type, reference_id=ref, delta__gt=0
            ).order_by("id")
        )
        for original in credits:
            account = _account(original.customer_id)
            try:
                with transaction.atomic():
                    entry = PointTransaction.objects.create(
                        customer_id=original.customer_id,
                        delta=-original.delta,
                        reason=original.reason + REVERSAL_SUFFIX,
                        reference_type=reference_type,
                        reference_id=ref,
                        reverses=original,
                    )
            except IntegrityError:
                continue  # already reversed
            account.balance -= original.delta
            account.lifetime_earned -= original.delta
            account.save(update_fields=["balance", "lifetime_earned", "updated_at"])
            reversals.append(entry)
    return reversals


def credit_booking_completed(
    *, customer_id: int, appointment_id: int, amount: int, completed_count: int
) -> list[PointTransaction]:
    """Points for a completed booking plus any milestone bonuses reached (each awarded once)."""
    awarded = []
    entry = credit(
        customer_id=customer_id,
        action=BOOKING,
        reference_type="appointment",
        reference_id=appointment_id,
        amount=amount,
    )
    if entry:
        awarded.append(entry)
    for threshold, action in MILESTONES:
        if completed_count >= threshold:
            bonus = credit(
                customer_id=customer_id,
                action=action,
                reference_type="user",
                reference_id=customer_id,
            )
            if bonus:
                awarded.append(bonus)
    return awarded


def summary(customer_id: int) -> tuple[int, int]:
    account = LoyaltyAccount.objects.filter(customer_id=customer_id).first()
    return (account.balance, account.lifetime_earned) if account else (0, 0)


@dataclass(frozen=True)
class Mismatch:
    customer_id: int
    balance: int
    ledger_balance: int
    lifetime: int
    ledger_lifetime: int


def reconcile(*, repair: bool = False) -> list[Mismatch]:
    """Compare every account summary to its ledger. Logs each mismatch; optionally repairs."""
    ledger = {
        row["customer_id"]: row["total"]
        for row in PointTransaction.objects.values("customer_id").annotate(total=Sum("delta"))
    }
    mismatches: list[Mismatch] = []
    seen: set[int] = set()
    for account in LoyaltyAccount.objects.all():
        seen.add(account.customer_id)
        total = int(ledger.get(account.customer_id, 0))
        if account.balance != total or account.lifetime_earned != total:
            mismatches.append(
                Mismatch(
                    account.customer_id, account.balance, total, account.lifetime_earned, total
                )
            )
    for customer_id, total in ledger.items():
        if customer_id not in seen and total != 0:
            mismatches.append(Mismatch(customer_id, 0, int(total), 0, int(total)))
    for m in mismatches:
        logger.error(
            "Loyalty summary does not match ledger",
            extra={
                "customer_id": m.customer_id,
                "balance": m.balance,
                "ledger_balance": m.ledger_balance,
            },
        )
        if repair:
            with transaction.atomic():
                account = _account(m.customer_id)
                account.balance = m.ledger_balance
                account.lifetime_earned = m.ledger_lifetime
                account.save(update_fields=["balance", "lifetime_earned", "updated_at"])
    return mismatches


ADJUSTMENT = "adjustment"
ADJUSTMENT_CATEGORIES = ("goodwill", "correction", "fraud_reversal", "other")


def adjustment_reference(admin_id: int, key: str) -> str:
    """Ledger reference for (admin, idempotency key); hashed so any key fits the column."""
    return f"{admin_id}:{hashlib.sha256(key.encode()).hexdigest()[:40]}"


def find_adjustment(admin_id: int, key: str) -> PointTransaction | None:
    return PointTransaction.objects.filter(
        reason=ADJUSTMENT,
        reference_type=ADJUSTMENT,
        reference_id=adjustment_reference(admin_id, key),
    ).first()


def adjust(
    *,
    customer_id: int,
    delta: int,
    category: str,
    note: str,
    admin_id: int,
    key: str,
    now: datetime | None = None,
) -> tuple[PointTransaction, bool]:
    """Add or remove points by hand. Returns (ledger row, created); a repeat of the same
    (admin, key) returns the original row. The balance may only go below zero for
    `fraud_reversal`. The ledger row and the summary change in one transaction under the
    account's row lock, so parallel adjustments stay consistent."""
    now = now or timezone.now()
    with transaction.atomic():
        account = _account(customer_id)
        existing = find_adjustment(admin_id, key)
        if existing is not None:
            same = (existing.customer_id, existing.delta, existing.category, existing.note) == (
                customer_id,
                delta,
                category,
                note,
            )
            if not same:
                raise conflict(
                    "idempotency_conflict", "This Idempotency-Key was used for a different request."
                )
            return existing, False
        new_balance = account.balance + delta
        if new_balance < 0 and category != "fraud_reversal":
            raise AppError(
                "insufficient_points",
                "The customer does not have enough points for this adjustment.",
                422,
                {"amount": [f"Balance would become {new_balance}."]},
            )
        entry = PointTransaction.objects.create(
            customer_id=customer_id,
            delta=delta,
            reason=ADJUSTMENT,
            reference_type=ADJUSTMENT,
            reference_id=adjustment_reference(admin_id, key),
            expires_at=now + EXPIRY if delta > 0 else None,
            remaining=delta if delta > 0 else 0,
            admin_id=admin_id,
            category=category,
            note=note,
            created_at=now,
        )
        account.balance = new_balance
        account.lifetime_earned += delta
        account.save(update_fields=["balance", "lifetime_earned", "updated_at"])
    return entry, True
