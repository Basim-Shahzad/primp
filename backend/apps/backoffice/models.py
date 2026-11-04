from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.models import ImmutableRecordError, PublicIdMixin, TimeStampedModel

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


class TicketType(models.TextChoices):
    BOOKING_DISPUTE = "booking_dispute", _("Booking dispute")
    REFUND_REQUEST = "refund_request", _("Refund request")
    FLAGGED_REVIEW = "flagged_review", _("Flagged review")
    PROVIDER_COMPLAINT = "provider_complaint", _("Provider complaint")
    ACCOUNT_ISSUE = "account_issue", _("Account issue")
    FAILED_NOTIFICATION = "failed_notification", _("Failed notification")
    OTHER = "other", _("Other")


class TicketPriority(models.TextChoices):
    LOW = "low", _("Low")
    NORMAL = "normal", _("Normal")
    HIGH = "high", _("High")
    URGENT = "urgent", _("Urgent")


class TicketStatus(models.TextChoices):
    OPEN = "open", _("Open")
    IN_PROGRESS = "in_progress", _("In progress")
    RESOLVED = "resolved", _("Resolved")
    CLOSED = "closed", _("Closed")


class Ticket(PublicIdMixin, TimeStampedModel):
    type = models.CharField(max_length=25, choices=TicketType.choices)
    priority = models.CharField(
        max_length=8, choices=TicketPriority.choices, default=TicketPriority.NORMAL
    )
    status = models.CharField(
        max_length=12, choices=TicketStatus.choices, default=TicketStatus.OPEN
    )
    subject = models.CharField(max_length=200)
    requester = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    assigned_to = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    appointment = models.ForeignKey(
        "bookings.Appointment", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    business = models.ForeignKey(
        "businesses.Business", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    review = models.ForeignKey(
        "reviews.Review", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    related_user = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    source_key = models.CharField(max_length=120, null=True, blank=True, unique=True)
    first_response_at = models.DateTimeField(null=True, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    version = models.PositiveIntegerField(default=1)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["status", "priority", "created_at"]),
            models.Index(fields=["requester", "-created_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.type} {self.status}"


class TicketMessage(models.Model):
    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name="messages")
    author = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    body = models.TextField(max_length=4000)
    is_internal = models.BooleanField(default=False)
    attachments = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]

    def __str__(self) -> str:
        return f"message on {self.ticket_id}"


class TicketEvent(models.Model):
    """Append-only history of what happened to a ticket."""

    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name="events")
    actor = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    kind = models.CharField(max_length=20)
    from_value = models.CharField(max_length=40, blank=True)
    to_value = models.CharField(max_length=40, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]

    def __str__(self) -> str:
        return f"{self.ticket_id}: {self.kind}"


class Banner(TimeStampedModel):
    """A scheduled promotional banner (shown when starts_at <= now < ends_at)."""

    title = models.CharField(max_length=120)
    subtitle = models.CharField(max_length=200, blank=True)
    desktop_image = models.CharField(max_length=255, blank=True)
    mobile_image = models.CharField(max_length=255, blank=True)
    link_url = models.CharField(max_length=500)
    city = models.ForeignKey(
        "geo.City", null=True, blank=True, on_delete=models.CASCADE, related_name="+"
    )
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField()
    is_enabled = models.BooleanField(default=False)
    sort_order = models.PositiveIntegerField(default=0)
    impressions = models.PositiveBigIntegerField(default=0)
    clicks = models.PositiveBigIntegerField(default=0)
    version = models.PositiveIntegerField(default=1)

    class Meta:
        ordering = ["sort_order", "id"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(ends_at__gt=models.F("starts_at")),
                name="banner_ends_after_start",
            )
        ]
        indexes = [models.Index(fields=["is_enabled", "starts_at", "ends_at"])]

    def __str__(self) -> str:
        return self.title


class PlatformSetting(models.Model):
    """A runtime override of a code default (see config_registry)."""

    key = models.CharField(max_length=80, unique=True)
    value = models.JSONField()
    schema_id = models.CharField(max_length=80)
    version = models.PositiveIntegerField(default=1)
    updated_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"{self.key} v{self.version}"


class HistoryQuerySet(models.QuerySet["PlatformSettingHistory"]):
    def update(self, **kwargs: object) -> int:
        raise ImmutableRecordError("Setting history cannot be updated.")

    def delete(self) -> tuple[int, dict[str, int]]:
        raise ImmutableRecordError("Setting history cannot be deleted.")


class PlatformSettingHistory(models.Model):
    """Append-only record of every change."""

    key = models.CharField(max_length=80, db_index=True)
    old_value = models.JSONField(null=True, blank=True)
    new_value = models.JSONField()
    version = models.PositiveIntegerField()
    changed_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    changed_at = models.DateTimeField(auto_now_add=True)

    objects = HistoryQuerySet.as_manager()

    class Meta:
        ordering = ["-changed_at", "-id"]
        verbose_name_plural = "platform setting history"

    def __str__(self) -> str:
        return f"{self.key} -> v{self.version}"

    def save(self, *args: object, **kwargs: object) -> None:
        if not self._state.adding:
            raise ImmutableRecordError("Setting history cannot be modified.")
        super().save(*args, **kwargs)  # type: ignore[arg-type]

    def delete(self, *args: object, **kwargs: object) -> tuple[int, dict[str, int]]:
        raise ImmutableRecordError("Setting history cannot be deleted.")
