from django.db import transaction

from .tasks import send_email_task, send_sms_task


def queue_sms(to: str, body: str) -> None:
    transaction.on_commit(lambda: send_sms_task.delay(to, body))


def queue_email(to: str, subject: str, body: str) -> None:
    transaction.on_commit(lambda: send_email_task.delay(to, subject, body))
