from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


class Channel(models.TextChoices):
    SMS = "sms", _("SMS")
    EMAIL = "email", _("Email")
    PUSH = "push", _("Push")
    IN_APP = "in_app", _("In-app")


class NotificationStatus(models.TextChoices):
    QUEUED = "queued", _("Queued")
    SENT = "sent", _("Sent")
    DELIVERED = "delivered", _("Delivered")
    FAILED = "failed", _("Failed")


class NotificationTemplate(models.Model):
    key = models.CharField(max_length=60)
    channel = models.CharField(max_length=10, choices=Channel.choices)
    language = models.CharField(max_length=8, default="en")
    subject = models.CharField(max_length=200, blank=True)
    body = models.TextField(help_text="Use {placeholders}")
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["key", "channel", "language"], name="notification_template_unique"
            )
        ]

    def __str__(self) -> str:
        return f"{self.key}/{self.channel}/{self.language}"


class Notification(models.Model):
    user = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    recipient = models.CharField(max_length=255, blank=True)
    channel = models.CharField(max_length=10, choices=Channel.choices)
    template_key = models.CharField(max_length=60)
    language = models.CharField(max_length=8, default="en")
    payload = models.JSONField(default=dict, blank=True)
    status = models.CharField(
        max_length=10, choices=NotificationStatus.choices, default=NotificationStatus.QUEUED
    )
    attempts = models.PositiveSmallIntegerField(default=0)
    provider_message_id = models.CharField(max_length=100, blank=True)
    last_error = models.TextField(blank=True)
    fallback_of = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="fallbacks"
    )
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=["status", "created_at"])]

    def __str__(self) -> str:
        return f"{self.template_key}/{self.channel} {self.status}"


class Reminder(models.Model):
    """Claim row that guarantees a reminder is sent at most once per (appointment, kind)."""

    appointment_id = models.BigIntegerField()
    kind = models.CharField(max_length=30)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["appointment_id", "kind"], name="reminder_unique")
        ]

    def __str__(self) -> str:
        return f"{self.appointment_id}:{self.kind}"
