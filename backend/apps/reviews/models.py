from django.db import models
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.core.models import PublicIdMixin, TimeStampedModel


class ReviewStatus(models.TextChoices):
    VISIBLE = "visible", _("Visible")
    HIDDEN = "hidden", _("Hidden")
    REMOVED = "removed", _("Removed")


class Review(PublicIdMixin, TimeStampedModel):
    appointment = models.OneToOneField(
        "bookings.Appointment", on_delete=models.PROTECT, related_name="review"
    )
    customer = models.ForeignKey("accounts.User", on_delete=models.PROTECT, related_name="reviews")
    business = models.ForeignKey(
        "businesses.Business", on_delete=models.CASCADE, related_name="reviews"
    )
    rating = models.PositiveSmallIntegerField()
    comment = models.TextField(max_length=1000, blank=True)
    photos = models.JSONField(default=list, blank=True)
    status = models.CharField(
        max_length=10, choices=ReviewStatus.choices, default=ReviewStatus.VISIBLE
    )
    is_flagged = models.BooleanField(default=False)
    edited_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=["business", "status", "-created_at"])]
        constraints = [
            models.CheckConstraint(
                condition=Q(rating__gte=1, rating__lte=5), name="review_rating_1_to_5"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.business_id} {self.rating}★"


class ReviewReply(TimeStampedModel):
    review = models.OneToOneField(Review, on_delete=models.CASCADE, related_name="reply")
    author = models.ForeignKey("accounts.User", on_delete=models.PROTECT, related_name="+")
    body = models.TextField(max_length=1000)

    def __str__(self) -> str:
        return f"reply to {self.review_id}"


class FlagReason(models.TextChoices):
    OFFENSIVE = "offensive", _("Offensive")
    FAKE = "fake", _("Fake")
    UNRELATED = "unrelated", _("Unrelated")


class ReviewFlag(models.Model):
    review = models.ForeignKey(Review, on_delete=models.CASCADE, related_name="flags")
    reporter = models.ForeignKey("accounts.User", on_delete=models.CASCADE, related_name="+")
    reason = models.CharField(max_length=10, choices=FlagReason.choices)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["review", "reporter"], name="review_flag_unique_reporter"
            )
        ]

    def __str__(self) -> str:
        return f"{self.review_id}:{self.reason}"


class ReviewOutbox(models.Model):
    """Events other apps pick up later (Plan 6.3 turns `review_flagged` into a support ticket)."""

    event = models.CharField(max_length=50)
    review = models.ForeignKey(Review, on_delete=models.CASCADE, related_name="+")
    payload = models.JSONField(default=dict)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    processed_at = models.DateTimeField(null=True, blank=True)

    def __str__(self) -> str:
        return f"{self.event}:{self.review_id}"
