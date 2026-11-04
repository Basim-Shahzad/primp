from celery import shared_task


@shared_task(queue="default")
def sweep_review_flags() -> int:
    """Beat task (every 5 min): safety net for flags whose event was missed. Idempotent."""
    from .services import tickets

    return tickets.process_review_flags()


@shared_task(queue="bulk")
def flush_banner_counters() -> int:
    """Beat task (every minute): move banner counters from Redis to the database."""
    from . import banners

    return banners.flush_counters()
