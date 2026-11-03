from django.db import models
from django.utils import timezone


class PaymentWebhookEvent(models.Model):
    """One row per provider event id; the unique constraint makes processing idempotent."""

    provider = models.CharField(max_length=30)
    event_id = models.CharField(max_length=100)
    event_type = models.CharField(max_length=50)
    payload = models.JSONField(default=dict)
    received_at = models.DateTimeField(default=timezone.now)
    processed_at = models.DateTimeField(null=True, blank=True)
    outcome = models.CharField(max_length=30, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["provider", "event_id"], name="webhook_event_unique")
        ]

    def __str__(self) -> str:
        return f"{self.provider}:{self.event_id}"
