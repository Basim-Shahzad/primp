from typing import Any, NoReturn

from django.db import models
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.core.models import ImmutableRecordError, TimeStampedModel


class RuleKind(models.TextChoices):
    PER_AMOUNT = "per_amount", _("Points per amount spent")
    FLAT = "flat", _("Flat points")


class PointRule(TimeStampedModel):
    """Earning rule, editable in the admin without a deployment.

    per_amount: floor(amount * points / unit_amount); flat: `points`.
    """

    action = models.CharField(max_length=40, db_index=True)
    kind = models.CharField(max_length=12, choices=RuleKind.choices, default=RuleKind.FLAT)
    points = models.PositiveIntegerField()
    unit_amount = models.PositiveIntegerField(
        null=True, blank=True, help_text="PKR, per_amount only"
    )
    description = models.CharField(max_length=200, blank=True)
    is_active = models.BooleanField(default=True)
    valid_from = models.DateTimeField(null=True, blank=True)
    valid_to = models.DateTimeField(null=True, blank=True)
    version = models.PositiveIntegerField(default=1)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(kind="flat") | Q(unit_amount__isnull=False, unit_amount__gt=0),
                name="rule_per_amount_needs_unit",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.action}: {self.points}"


class LedgerQuerySet(models.QuerySet["PointTransaction"]):
    def update(self, **kwargs: Any) -> NoReturn:
        raise ImmutableRecordError("Point transactions cannot be updated.")

    def delete(self) -> NoReturn:
        raise ImmutableRecordError("Point transactions cannot be deleted.")


class PointTransaction(models.Model):
    """Append-only ledger. (reason, reference) is unique so every event credits at most once."""

    customer = models.ForeignKey("accounts.User", on_delete=models.PROTECT, related_name="+")
    delta = models.IntegerField()
    reason = models.CharField(max_length=50)
    reference_type = models.CharField(max_length=30)
    reference_id = models.CharField(max_length=64)
    expires_at = models.DateTimeField(null=True, blank=True)
    remaining = models.IntegerField(default=0, help_text="For FIFO redemption (Phase 2)")
    reverses = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="reversals"
    )
    admin = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    category = models.CharField(max_length=20, blank=True)
    note = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    objects = LedgerQuerySet.as_manager()

    class Meta:
        indexes = [models.Index(fields=["customer", "-created_at", "-id"])]
        constraints = [
            models.UniqueConstraint(
                fields=["reason", "reference_type", "reference_id"], name="ledger_unique_event"
            ),
            models.CheckConstraint(condition=~Q(delta=0), name="ledger_delta_nonzero"),
        ]

    def __str__(self) -> str:
        return f"{self.customer_id} {self.delta:+d} {self.reason}"

    def save(self, *args: Any, **kwargs: Any) -> None:
        if not self._state.adding:
            raise ImmutableRecordError("Point transactions cannot be modified.")
        super().save(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> NoReturn:
        raise ImmutableRecordError("Point transactions cannot be deleted.")


class LoyaltyAccount(models.Model):
    """Summary kept in step with the ledger inside the same transaction."""

    customer = models.OneToOneField("accounts.User", on_delete=models.CASCADE, related_name="+")
    balance = models.IntegerField(default=0)
    lifetime_earned = models.IntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"{self.customer_id}: {self.balance}"
