from celery import shared_task


@shared_task(queue="default")
def sweep_review_flags() -> int:
    """Beat task (every 5 min): safety net for flags whose event was missed. Idempotent."""
    from .services import tickets

    return tickets.process_review_flags()
