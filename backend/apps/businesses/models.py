from datetime import time
from typing import Any

from django.contrib.gis.db import models as gis_models
from django.contrib.postgres.constraints import ExclusionConstraint
from django.contrib.postgres.fields import IntegerRangeField, RangeOperators
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import F, Func, Q, Value
from django.db.models.functions import Coalesce
from django.utils.translation import gettext_lazy as _

from apps.core.models import PublicIdMixin, TimeStampedModel


class GenderType(models.TextChoices):
    WOMEN = "women", _("Women")
    MEN = "men", _("Men")
    UNISEX = "unisex", _("Unisex")


class VerificationStatus(models.TextChoices):
    DRAFT = "draft", _("Draft")
    PENDING = "pending", _("Pending")
    NEEDS_INFO = "needs_info", _("Needs info")
    VERIFIED = "verified", _("Verified")
    REJECTED = "rejected", _("Rejected")
    REVOKED = "revoked", _("Revoked")


class ConfirmMode(models.TextChoices):
    INSTANT = "instant", _("Instant")
    MANUAL = "manual", _("Manual")


class Business(PublicIdMixin, TimeStampedModel):
    owner = models.ForeignKey("accounts.User", on_delete=models.PROTECT, related_name="businesses")
    name = models.CharField(max_length=150)
    slug = models.SlugField(max_length=170)
    category = models.ForeignKey("catalog.Category", on_delete=models.PROTECT, related_name="+")
    description = models.TextField(blank=True)
    address = models.CharField(max_length=255, blank=True)
    location = gis_models.PointField(geography=True, srid=4326)
    city = models.ForeignKey("geo.City", on_delete=models.PROTECT, related_name="businesses")
    area = models.ForeignKey(
        "geo.Area", null=True, blank=True, on_delete=models.SET_NULL, related_name="businesses"
    )
    phone = models.CharField(max_length=20, blank=True)
    whatsapp = models.CharField(max_length=20, blank=True)
    gender_type = models.CharField(
        max_length=10, choices=GenderType.choices, default=GenderType.UNISEX
    )
    verification_status = models.CharField(
        max_length=12,
        choices=VerificationStatus.choices,
        default=VerificationStatus.DRAFT,
        db_index=True,
    )
    is_listed = models.BooleanField(default=False, db_index=True)

    confirm_mode = models.CharField(
        max_length=10, choices=ConfirmMode.choices, default=ConfirmMode.INSTANT
    )
    cancellation_notice_hours = models.PositiveIntegerField(default=24)
    lead_time_minutes = models.PositiveIntegerField(default=60)
    max_advance_days = models.PositiveIntegerField(default=60)
    slot_interval_minutes = models.PositiveSmallIntegerField(default=30)
    buffer_minutes = models.PositiveIntegerField(default=0)
    staff_level_booking = models.BooleanField(default=False)
    parallel_capacity = models.PositiveSmallIntegerField(default=1)

    rating_avg = models.DecimalField(max_digits=3, decimal_places=2, default=0)
    rating_count = models.PositiveIntegerField(default=0)
    timezone = models.CharField(max_length=64, default="Asia/Karachi")

    logo = models.CharField(max_length=255, blank=True)
    onboarding_progress = models.JSONField(default=dict, blank=True)
    status_message = models.CharField(max_length=500, blank=True)
    verified_at = models.DateTimeField(null=True, blank=True)
    verified_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    revoked_reason = models.CharField(max_length=500, blank=True)
    pending_review_changes = models.JSONField(null=True, blank=True)

    class Meta:
        verbose_name_plural = "businesses"
        constraints = [
            models.UniqueConstraint(fields=["city", "slug"], name="business_slug_unique_per_city"),
            models.CheckConstraint(
                condition=Q(slot_interval_minutes__in=[15, 30, 60]),
                name="business_slot_interval_valid",
            ),
            models.CheckConstraint(
                condition=Q(parallel_capacity__gte=1), name="business_parallel_capacity_min_1"
            ),
            models.CheckConstraint(
                condition=Q(max_advance_days__gte=1), name="business_max_advance_min_1"
            ),
            models.CheckConstraint(
                condition=Q(rating_avg__gte=0, rating_avg__lte=5), name="business_rating_range"
            ),
        ]
        indexes = [models.Index(fields=["city", "is_listed"])]

    def __str__(self) -> str:
        return self.name

    @property
    def is_verified(self) -> bool:
        return self.verification_status == VerificationStatus.VERIFIED


class BusinessPhoto(TimeStampedModel):
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="photos")
    image = models.CharField(max_length=255)
    sort_order = models.PositiveIntegerField(default=0)
    is_cover = models.BooleanField(default=False)

    class Meta:
        ordering = ["-is_cover", "sort_order", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["business"], condition=Q(is_cover=True), name="one_cover_photo_per_business"
            )
        ]

    def __str__(self) -> str:
        return f"{self.business_id}:{self.image}"


class Staff(TimeStampedModel):
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="staff")
    name = models.CharField(max_length=100)
    photo = models.CharField(max_length=255, blank=True)
    bio = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        verbose_name_plural = "staff"
        ordering = ["name", "id"]

    def __str__(self) -> str:
        return self.name


class Service(TimeStampedModel):
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="services")
    category = models.ForeignKey(
        "catalog.Category", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    name = models.CharField(max_length=150)
    description = models.TextField(blank=True)
    price = models.PositiveIntegerField(help_text="PKR")
    is_from_price = models.BooleanField(default=False)
    duration_minutes = models.PositiveIntegerField()
    is_active = models.BooleanField(default=True)
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "id"]
        constraints = [
            models.CheckConstraint(
                condition=Q(duration_minutes__gt=0), name="service_duration_positive"
            ),
        ]

    def __str__(self) -> str:
        return self.name


class StaffService(models.Model):
    staff = models.ForeignKey(Staff, on_delete=models.CASCADE, related_name="staff_services")
    service = models.ForeignKey(Service, on_delete=models.CASCADE, related_name="staff_services")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["staff", "service"], name="staff_service_unique_pair")
        ]

    def __str__(self) -> str:
        return f"{self.staff_id}-{self.service_id}"

    def save(self, *args: Any, **kwargs: Any) -> None:
        self.clean()
        super().save(*args, **kwargs)

    def clean(self) -> None:
        super().clean()
        if self.staff.business_id != self.service.business_id:
            raise ValidationError(_("Staff and service must belong to the same business."))


def _seconds(field: str) -> Func:
    return Func(
        F(field),
        template="EXTRACT(EPOCH FROM %(expressions)s)::int",
        output_field=models.IntegerField(),
    )


class OpeningHours(models.Model):
    """One shift. Several rows per weekday allow split shifts; staff null = business-wide."""

    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="opening_hours")
    staff = models.ForeignKey(
        Staff, null=True, blank=True, on_delete=models.CASCADE, related_name="opening_hours"
    )
    weekday = models.PositiveSmallIntegerField(help_text="0=Monday .. 6=Sunday")
    opens_at = models.TimeField()
    closes_at = models.TimeField()

    class Meta:
        verbose_name_plural = "opening hours"
        ordering = ["weekday", "opens_at"]
        constraints = [
            models.CheckConstraint(
                condition=Q(weekday__gte=0, weekday__lte=6), name="hours_weekday_valid"
            ),
            models.CheckConstraint(
                condition=Q(closes_at__gt=F("opens_at")), name="hours_close_after_open"
            ),
            ExclusionConstraint(
                name="hours_no_overlap",
                expressions=[
                    (F("business"), RangeOperators.EQUAL),
                    (Coalesce(F("staff"), Value(0)), RangeOperators.EQUAL),
                    (F("weekday"), RangeOperators.EQUAL),
                    (
                        Func(
                            _seconds("opens_at"),
                            _seconds("closes_at"),
                            function="int4range",
                            output_field=IntegerRangeField(),
                        ),
                        RangeOperators.OVERLAPS,
                    ),
                ],
            ),
        ]

    def __str__(self) -> str:
        return f"{self.weekday} {self.opens_at}-{self.closes_at}"

    def clean(self) -> None:
        super().clean()
        if self.staff is not None and self.staff.business_id != self.business_id:
            raise ValidationError(_("Staff must belong to the business."))
        opens: time = self.opens_at
        closes: time = self.closes_at
        if closes <= opens:
            raise ValidationError(_("Closing time must be after opening time."))
        clash = OpeningHours.objects.filter(
            business_id=self.business_id,
            staff_id=self.staff_id,  # type: ignore[misc]
            weekday=self.weekday,
            opens_at__lt=closes,
            closes_at__gt=opens,
        ).exclude(pk=self.pk)
        if clash.exists():
            raise ValidationError(_("Opening hours overlap an existing shift."))


class ClosureKind(models.TextChoices):
    HOLIDAY = "holiday", _("Holiday")
    LEAVE = "leave", _("Leave")
    BLOCK = "block", _("Block")


class Closure(TimeStampedModel):
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="closures")
    staff = models.ForeignKey(
        Staff, null=True, blank=True, on_delete=models.CASCADE, related_name="closures"
    )
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField()
    reason = models.CharField(max_length=255, blank=True)
    kind = models.CharField(max_length=10, choices=ClosureKind.choices, default=ClosureKind.HOLIDAY)

    class Meta:
        ordering = ["starts_at"]
        constraints = [
            models.CheckConstraint(
                condition=Q(ends_at__gt=F("starts_at")), name="closure_ends_after_start"
            ),
        ]
        indexes = [models.Index(fields=["business", "starts_at", "ends_at"])]

    def __str__(self) -> str:
        return f"{self.business_id} {self.kind}"


class DocumentKind(models.TextChoices):
    CNIC_FRONT = "cnic_front", _("CNIC front")
    CNIC_BACK = "cnic_back", _("CNIC back")
    LICENCE = "licence", _("Licence")
    SELFIE_WITH_CNIC = "selfie_with_cnic", _("Selfie with CNIC")
    STOREFRONT = "storefront", _("Storefront")


class DocumentStatus(models.TextChoices):
    PENDING = "pending", _("Pending")
    ACCEPTED = "accepted", _("Accepted")
    REJECTED = "rejected", _("Rejected")


class BusinessDocument(TimeStampedModel):
    """Verification document kept in private storage; never publicly addressable."""

    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="documents")
    kind = models.CharField(max_length=20, choices=DocumentKind.choices)
    file = models.CharField(max_length=255)
    content_type = models.CharField(max_length=50)
    size = models.PositiveIntegerField()
    status = models.CharField(
        max_length=10, choices=DocumentStatus.choices, default=DocumentStatus.PENDING
    )
    reviewer_note = models.CharField(max_length=500, blank=True)

    class Meta:
        ordering = ["id"]
        indexes = [models.Index(fields=["business", "kind"])]

    def __str__(self) -> str:
        return f"{self.business_id}:{self.kind}"


class BusinessSubmission(models.Model):
    """One row per submission for review, recording the accepted terms."""

    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="submissions")
    number = models.PositiveSmallIntegerField()
    terms_version = models.CharField(max_length=20)
    is_resubmission_after_rejection = models.BooleanField(default=False)
    accepted_at = models.DateTimeField()
    ip_address = models.GenericIPAddressField(null=True, blank=True)

    class Meta:
        ordering = ["number"]
        constraints = [
            models.UniqueConstraint(fields=["business", "number"], name="submission_unique_number")
        ]

    def __str__(self) -> str:
        return f"{self.business_id}#{self.number}"


class StarterMenuItem(models.Model):
    """Editable service templates suggested to new businesses of a category."""

    category = models.ForeignKey("catalog.Category", on_delete=models.CASCADE, related_name="+")
    name = models.CharField(max_length=150)
    description = models.CharField(max_length=300, blank=True)
    price = models.PositiveIntegerField(help_text="PKR")
    duration_minutes = models.PositiveIntegerField()
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "id"]

    def __str__(self) -> str:
        return self.name
