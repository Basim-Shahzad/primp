import logging

from celery import shared_task
from django.core.mail import send_mail
from django.utils import timezone

from . import services
from .models import Notification

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=services.MAX_ATTEMPTS - 1)
def deliver_notification(self, notification_id: int) -> str:  # type: ignore[no-untyped-def]
    outcome = services.attempt_delivery(notification_id)
    if outcome == services.RETRY:
        key = (
            Notification.objects.filter(pk=notification_id)
            .values_list("template_key", flat=True)
            .first()
            or ""
        )
        raise self.retry(
            countdown=services.backoff_seconds(self.request.retries), queue=services.queue_for(key)
        )
    return outcome


@shared_task(queue="default")
def scan_reminders() -> int:
    """Beat task (every 5 min): send due 24 h / 2 h reminders exactly once each."""
    from .reminders import scan

    return scan(timezone.now())


@shared_task(queue="critical")
def send_sms_task(to: str, body: str) -> str:
    from .adapters import get_sms_adapter

    return get_sms_adapter().send(to, body)


@shared_task(queue="default")
def send_email_task(to: str, subject: str, body: str) -> int:
    return send_mail(subject, body, None, [to])
