"""Turn business events from Plan 6.2 into review-queue items (idempotent)."""

import logging
from typing import Any

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.businesses import events as business_events

from .models import ReviewKind, ReviewQueueItem

logger = logging.getLogger(__name__)


def enqueue(business_id: int, kind: str) -> ReviewQueueItem | None:
    """Create the open item for (business, kind); a duplicate delivery changes nothing."""
    try:
        with transaction.atomic():
            return ReviewQueueItem.objects.create(
                business_id=business_id, kind=kind, submitted_at=timezone.now()
            )
    except IntegrityError:
        return None  # an open item already exists


def on_submitted(sender: Any, business_id: int, **kwargs: Any) -> None:
    enqueue(business_id, ReviewKind.NEW_LISTING)


def on_change_requested(sender: Any, business_id: int, **kwargs: Any) -> None:
    enqueue(business_id, ReviewKind.CHANGE_REQUEST)


def connect() -> None:
    business_events.business_submitted.connect(
        on_submitted, weak=False, dispatch_uid="backoffice-on-submitted"
    )
    business_events.business_change_review_requested.connect(
        on_change_requested, weak=False, dispatch_uid="backoffice-on-change-requested"
    )
