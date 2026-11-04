from django.http import HttpRequest
from ninja import Router, Schema

from apps.core.errors import AppError, not_found

from . import events, services
from .gateways import InvalidPayload, InvalidSignature

router = Router(tags=["payments"])


class WebhookOut(Schema):
    status: str


@router.post("/webhooks/{provider}", auth=None, response=WebhookOut)
def webhook(request: HttpRequest, provider: str) -> WebhookOut:
    if services.get_gateway(provider) is None:
        raise not_found()
    try:
        outcome = services.process_webhook(provider, dict(request.headers), request.body)
    except InvalidSignature as exc:
        events.webhook_failed.send(sender=None, provider=provider, reason="invalid_signature")
        raise AppError("invalid_signature", "Invalid webhook signature.", 401) from exc
    except InvalidPayload as exc:
        events.webhook_failed.send(sender=None, provider=provider, reason="invalid_payload")
        raise AppError("invalid_payload", "Malformed webhook payload.", 400) from exc
    return WebhookOut(status=outcome)
