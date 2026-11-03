from celery import shared_task

from . import services


@shared_task(queue="bulk")
def reconcile_accounts() -> int:
    """Nightly: compare account summaries to the ledger; mismatches are logged at ERROR."""
    return len(services.reconcile())
