import logging
from collections.abc import Mapping

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.bookings import services as bookings

from .gateways import PaymentGateway, gateway_registry
from .models import PaymentWebhookEvent

logger = logging.getLogger(__name__)

EVENT_TO_STATUS = {
    "payment.succeeded": bookings.PaymentStatus.PAID,
    "payment.failed": bookings.PaymentStatus.FAILED,
    "refund.succeeded": bookings.PaymentStatus.REFUNDED,
}


def get_gateway(provider: str) -> PaymentGateway | None:
    if provider not in gateway_registry.names():
        return None
    return gateway_registry.get(provider)


def process_webhook(provider: str, headers: Mapping[str, str], body: bytes) -> str:
    """Verify, de-duplicate and apply a webhook. Returns processed | duplicate | ignored.

    Raises InvalidSignature / InvalidPayload (from the gateway) for bad requests.
    """
    gateway = get_gateway(provider)
    assert gateway is not None
    event = gateway.verify_webhook(headers, body)
    try:
        with transaction.atomic():
            row = PaymentWebhookEvent.objects.create(
                provider=provider,
                event_id=event.event_id,
                event_type=event.type,
                payload={
                    "appointment_id": event.appointment_id,
                    "provider_ref": event.provider_ref,
                },
            )
    except IntegrityError:
        return "duplicate"
    status = EVENT_TO_STATUS.get(event.type)
    outcome = "ignored"
    if status is not None and bookings.set_payment_status(event.appointment_id, status):
        outcome = "processed"
    row.processed_at = timezone.now()
    row.outcome = outcome
    row.save(update_fields=["processed_at", "outcome"])
    return outcome
