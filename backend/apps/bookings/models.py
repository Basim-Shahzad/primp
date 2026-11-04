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
        "accounts.User",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="appointments",
    )
    walk_in_name = models.CharField(max_length=100, blank=True)
    walk_in_phone = models.CharField(max_length=20, blank=True)
    business_note = models.CharField(max_length=1000, blank=True)
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
            models.Index(fields=["business", "updated_at"]),
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


class RescheduleStatus(models.TextChoices):
    PENDING = "pending", _("Pending")
    ACCEPTED = "accepted", _("Accepted")
    DECLINED = "declined", _("Declined")
    CANCELLED = "cancelled", _("Cancelled")


class RescheduleRequest(TimeStampedModel):
    """Manual-confirmation businesses must accept a customer's new time before it applies."""

    appointment = models.ForeignKey(
        Appointment, on_delete=models.CASCADE, related_name="reschedule_requests"
    )
    new_start = models.DateTimeField()
    new_end = models.DateTimeField()
    new_staff = models.ForeignKey(
        "businesses.Staff", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    status = models.CharField(
        max_length=10, choices=RescheduleStatus.choices, default=RescheduleStatus.PENDING
    )
    decided_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["appointment"],
                condition=Q(status="pending"),
                name="one_pending_reschedule_per_appointment",
            ),
            models.CheckConstraint(
                condition=Q(new_end__gt=F("new_start")), name="reschedule_end_after_start"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.appointment_id} -> {self.new_start:%Y-%m-%d %H:%M} ({self.status})"


class BusinessDailyStats(models.Model):
    """Per business and local day (of the appointment start): counts and completed revenue."""

    business = models.ForeignKey(
        "businesses.Business", on_delete=models.CASCADE, related_name="daily_stats"
    )
    date = models.DateField()
    bookings_total = models.PositiveIntegerField(default=0)
    completed = models.PositiveIntegerField(default=0)
    cancelled_by_customer = models.PositiveIntegerField(default=0)
    cancelled_by_business = models.PositiveIntegerField(default=0)
    no_shows = models.PositiveIntegerField(default=0)
    revenue_completed = models.PositiveBigIntegerField(default=0, help_text="PKR")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name_plural = "business daily stats"
        constraints = [
            models.UniqueConstraint(fields=["business", "date"], name="daily_stats_unique")
        ]
        indexes = [models.Index(fields=["business", "date"])]

    def __str__(self) -> str:
        return f"{self.business_id} {self.date}"


class ServiceDailyStats(models.Model):
    """Per service and local day: bookings (not cancelled) and completed revenue."""

    business = models.ForeignKey("businesses.Business", on_delete=models.CASCADE, related_name="+")
    service = models.ForeignKey(
        "businesses.Service", on_delete=models.CASCADE, related_name="daily_stats"
    )
    date = models.DateField()
    bookings = models.PositiveIntegerField(default=0)
    revenue = models.PositiveBigIntegerField(default=0, help_text="PKR")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name_plural = "service daily stats"
        constraints = [
            models.UniqueConstraint(fields=["service", "date"], name="service_daily_stats_unique")
        ]
        indexes = [models.Index(fields=["business", "date"])]

    def __str__(self) -> str:
        return f"{self.service_id} {self.date}"
