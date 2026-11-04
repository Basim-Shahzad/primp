from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.models import PublicIdMixin, TimeStampedModel

from .permissions import PERMISSION_CHOICES


class AdminPermission(models.Model):
    """Anchor model: it only exists to hold the admin permissions (no table)."""

    class Meta:
        managed = False
        default_permissions = ()
        permissions = PERMISSION_CHOICES

    def __str__(self) -> str:
        return "admin permission"


class SuspensionRecord(models.Model):
    """One suspension of an account: why, by whom and the listing state to restore."""

    user = models.ForeignKey("accounts.User", on_delete=models.CASCADE, related_name="+")
    suspended_by = models.ForeignKey(
        "accounts.User", null=True, on_delete=models.SET_NULL, related_name="+"
    )
    reason = models.CharField(max_length=500)
    previous_listings = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    reactivated_at = models.DateTimeField(null=True, blank=True)
    reactivated_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    reactivation_reason = models.CharField(max_length=500, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user"],
                condition=models.Q(reactivated_at__isnull=True),
                name="one_open_suspension_per_user",
            )
        ]

    def __str__(self) -> str:
        return f"{self.user_id} suspended"


class ReviewKind(models.TextChoices):
    NEW_LISTING = "new_listing", _("New listing")
    CHANGE_REQUEST = "change_request", _("Change request")


class QueueStatus(models.TextChoices):
    OPEN = "open", _("Open")
    IN_REVIEW = "in_review", _("In review")
    CLOSED = "closed", _("Closed")


class ReviewQueueItem(PublicIdMixin, TimeStampedModel):
    """A business waiting for an admin decision (one open item per business and kind)."""

    business = models.ForeignKey(
        "businesses.Business", on_delete=models.CASCADE, related_name="review_items"
    )
    kind = models.CharField(max_length=15, choices=ReviewKind.choices)
    status = models.CharField(max_length=10, choices=QueueStatus.choices, default=QueueStatus.OPEN)
    assigned_to = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    submitted_at = models.DateTimeField()
    closed_at = models.DateTimeField(null=True, blank=True)
    outcome = models.CharField(max_length=20, blank=True)
    version = models.PositiveIntegerField(default=1)

    class Meta:
        ordering = ["submitted_at", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["business", "kind"],
                condition=~models.Q(status="closed"),
                name="one_open_review_item_per_business_and_kind",
            )
        ]
        indexes = [models.Index(fields=["status", "submitted_at"])]

    def __str__(self) -> str:
        return f"{self.kind} {self.business_id} ({self.status})"


class ReviewDecision(models.Model):
    item = models.ForeignKey(ReviewQueueItem, on_delete=models.CASCADE, related_name="decisions")
    admin = models.ForeignKey(
        "accounts.User", null=True, on_delete=models.SET_NULL, related_name="+"
    )
    action = models.CharField(max_length=20)
    checklist = models.JSONField(null=True, blank=True)
    message = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]

    def __str__(self) -> str:
        return f"{self.item_id}: {self.action}"


class ReviewNote(models.Model):
    """Internal note on a queue item (never shown to the owner)."""

    item = models.ForeignKey(ReviewQueueItem, on_delete=models.CASCADE, related_name="notes")
    author = models.ForeignKey(
        "accounts.User", null=True, on_delete=models.SET_NULL, related_name="+"
    )
    body = models.TextField(max_length=2000)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]

    def __str__(self) -> str:
        return f"note on {self.item_id}"
