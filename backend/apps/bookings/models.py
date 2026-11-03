from django.contrib.postgres.constraints import ExclusionConstraint
from django.contrib.postgres.fields import DateTimeRangeField, RangeOperators
from django.db import models
from django.db.models import F, Func, Q
from django.utils.translation import gettext_lazy as _

from apps.core.models import PublicIdMixin, TimeStampedModel


class AppointmentStatus(models.TextChoices):
    PENDING = "pending", _("Pending")
    CONFIRMED = "confirmed", _("Confirmed")
    COMPLETED = "completed", _("Completed")
    CANCELLED_BY_CUSTOMER = "cancelled_by_customer", _("Cancelled by customer")
    CANCELLED_BY_BUSINESS = "cancelled_by_business", _("Cancelled by business")
    NO_SHOW = "no_show", _("No show")


ACTIVE_STATUSES = (AppointmentStatus.PENDING, AppointmentStatus.CONFIRMED)


class PaymentMethod(models.TextChoices):
    PAY_AT_VENUE = "pay_at_venue", _("Pay at venue")
    ONLINE = "online", _("Online")


class PaymentStatus(models.TextChoices):
    UNPAID = "unpaid", _("Unpaid")
    PENDING = "pending", _("Pending")
    PAID = "paid", _("Paid")
    FAILED = "failed", _("Failed")
    REFUNDED = "refunded", _("Refunded")


class AppointmentSource(models.TextChoices):
    ONLINE = "online", _("Online")
    MANUAL = "manual", _("Manual")


class Appointment(PublicIdMixin, TimeStampedModel):
    customer = models.ForeignKey(
        "accounts.User", on_delete=models.PROTECT, related_name="appointments"
    )
    business = models.ForeignKey(
        "businesses.Business", on_delete=models.PROTECT, related_name="appointments"
    )
    staff = models.ForeignKey(
        "businesses.Staff",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="appointments",
    )
    status = models.CharField(
        max_length=25, choices=AppointmentStatus.choices, default=AppointmentStatus.PENDING
    )
    scheduled_start = models.DateTimeField(db_index=True)
    scheduled_end = models.DateTimeField()
    total_price = models.PositiveIntegerField(default=0)
    payment_method = models.CharField(
        max_length=15, choices=PaymentMethod.choices, default=PaymentMethod.PAY_AT_VENUE
    )
    payment_status = models.CharField(
        max_length=10, choices=PaymentStatus.choices, default=PaymentStatus.UNPAID
    )
    source = models.CharField(
        max_length=10, choices=AppointmentSource.choices, default=AppointmentSource.ONLINE
    )
    cancellation_reason = models.CharField(max_length=50, blank=True)
    cancellation_note = models.CharField(max_length=500, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    is_late_cancellation = models.BooleanField(default=False)
    idempotency_key = models.CharField(max_length=64, null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["business", "scheduled_start"]),
            models.Index(fields=["customer", "scheduled_start"]),
            models.Index(fields=["status", "scheduled_start"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(scheduled_end__gt=F("scheduled_start")), name="appt_end_after_start"
            ),
            models.UniqueConstraint(
                fields=["customer", "idempotency_key"],
                condition=Q(idempotency_key__isnull=False),
                name="appt_idempotency_unique_per_customer",
            ),
            ExclusionConstraint(
                name="appt_no_staff_overlap",
                expressions=[
                    (F("staff"), RangeOperators.EQUAL),
                    (
                        Func(
                            F("scheduled_start"),
                            F("scheduled_end"),
                            function="tstzrange",
                            output_field=DateTimeRangeField(),
                        ),
                        RangeOperators.OVERLAPS,
                    ),
                ],
                condition=Q(status__in=["pending", "confirmed"], staff__isnull=False),
            ),
        ]

    def __str__(self) -> str:
        return f"{self.public_id} {self.status}"

    @property
    def is_active(self) -> bool:
        return self.status in ACTIVE_STATUSES


class AppointmentService(models.Model):
    appointment = models.ForeignKey(Appointment, on_delete=models.CASCADE, related_name="items")
    service = models.ForeignKey("businesses.Service", on_delete=models.PROTECT, related_name="+")
    name = models.CharField(max_length=150)
    price_at_booking = models.PositiveIntegerField()
    duration_at_booking = models.PositiveIntegerField()

    def __str__(self) -> str:
        return f"{self.appointment_id}:{self.name}"


class AppointmentEvent(models.Model):
    """Status history timeline."""

    appointment = models.ForeignKey(Appointment, on_delete=models.CASCADE, related_name="events")
    from_status = models.CharField(max_length=25, blank=True)
    to_status = models.CharField(max_length=25)
    actor_id = models.BigIntegerField(null=True, blank=True)
    actor_role = models.CharField(max_length=20, blank=True)
    note = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]

    def __str__(self) -> str:
        return f"{self.appointment_id}: {self.from_status}->{self.to_status}"
