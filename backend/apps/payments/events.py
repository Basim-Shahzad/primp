"""Payment domain events."""

from django.dispatch import Signal

webhook_failed = Signal()  # sends provider and reason ("invalid_signature" | "invalid_payload")
