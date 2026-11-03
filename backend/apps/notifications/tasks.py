import logging

from celery import shared_task
from django.core.mail import send_mail

from .adapters import get_sms_adapter

logger = logging.getLogger(__name__)


@shared_task(queue="critical")
def send_sms_task(to: str, body: str) -> str:
    return get_sms_adapter().send(to, body)


@shared_task(queue="default")
def send_email_task(to: str, subject: str, body: str) -> int:
    return send_mail(subject, body, None, [to])
