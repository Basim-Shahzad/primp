from django.contrib import admin

from apps.core.admin_base import ReadOnlyAdmin

from .models import PaymentWebhookEvent


@admin.register(PaymentWebhookEvent)
class PaymentWebhookEventAdmin(ReadOnlyAdmin):
    """Provider payloads can hold personal data, so they are not shown."""

    exclude = ("payload",)
    list_display = ("provider", "event_type", "event_id", "outcome", "received_at")
    list_filter = ("provider", "outcome")
    search_fields = ("event_id",)
