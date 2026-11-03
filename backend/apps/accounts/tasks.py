from celery import shared_task

from . import services


@shared_task(queue="critical")
def password_reset_task(email: str) -> None:
    services.request_password_reset(email)
