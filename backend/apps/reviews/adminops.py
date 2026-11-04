"""Review operations for the backoffice."""

import uuid
from dataclasses import dataclass
from typing import Any

from django.db.models import Q
from django.utils import timezone

from apps.core.errors import not_found

from .models import Review, ReviewOutbox, ReviewStatus
from .services import moderate_review


@dataclass(frozen=True)
class FlagEvent:
    outbox_id: int
    review_id: int
    business_id: int
    reporter_id: int
    flag_id: int
    reason: str


def claim_flag_events(limit: int = 200) -> list[FlagEvent]:
    """Lock and mark the unprocessed `review_flagged` rows (call inside a transaction).

    Rows are marked processed here, so a ticket is created once per flag even when the
    event and the periodic sweep both run.
    """
    rows = list(
        ReviewOutbox.objects.select_for_update(skip_locked=True, of=("self",))
        .select_related("review")
        .filter(event="review_flagged", processed_at__isnull=True)
        .order_by("id")[:limit]
    )
    now = timezone.now()
    events = []
    for row in rows:
        payload = row.payload or {}
        events.append(
            FlagEvent(
                outbox_id=row.pk,
                review_id=row.review_id,
                business_id=row.review.business_id,
                reporter_id=int(payload.get("reporter_id", 0)),
                flag_id=int(payload.get("flag_id", row.pk)),
                reason=str(payload.get("reason", "")),
            )
        )
        row.processed_at = now
        row.save(update_fields=["processed_at"])
    return events


def get_review(ref: str | uuid.UUID, *, lock: bool = False) -> Review:
    try:
        public_id = ref if isinstance(ref, uuid.UUID) else uuid.UUID(str(ref))
    except ValueError:
        raise not_found() from None
    qs = Review.objects.select_related("business", "customer")
    if lock:
        qs = qs.select_for_update(of=("self",))
    review = qs.filter(public_id=public_id).first()
    if review is None:
        raise not_found()
    return review


def review_for_requester(public_id: str, user_id: int) -> int | None:
    """Review pk if the user wrote it or owns the reviewed business (else None)."""
    try:
        ref = uuid.UUID(str(public_id))
    except ValueError:
        return None
    row = (
        Review.objects.filter(public_id=ref)
        .filter(Q(customer_id=user_id) | Q(business__owner_id=user_id))
        .values_list("pk", flat=True)
        .first()
    )
    return int(row) if row is not None else None


def review_summary(review_id: int) -> dict[str, Any]:
    r = Review.objects.select_related("business").filter(pk=review_id).first()
    if r is None:
        return {}
    return {
        "id": r.public_id,
        "rating": r.rating,
        "status": r.status,
        "business": r.business.name,
        "comment": r.comment[:200],
    }


ALLOWED = {
    "hide": ({ReviewStatus.VISIBLE}, ReviewStatus.HIDDEN),
    "remove": ({ReviewStatus.VISIBLE, ReviewStatus.HIDDEN}, ReviewStatus.REMOVED),
    "restore": ({ReviewStatus.HIDDEN, ReviewStatus.REMOVED}, ReviewStatus.VISIBLE),
}

moderate = moderate_review
