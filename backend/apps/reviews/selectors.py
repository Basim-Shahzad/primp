from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from django.core.files.storage import default_storage
from django.db.models import Count, QuerySet

from .models import Review, ReviewStatus


@dataclass(frozen=True)
class RatingSummary:
    avg: Decimal
    count: int
    distribution: dict[int, int]


def visible_reviews(business_id: int) -> QuerySet[Review]:
    return Review.objects.filter(business_id=business_id, status=ReviewStatus.VISIBLE)


def rating_summary(business_id: int) -> RatingSummary:
    rows = visible_reviews(business_id).values("rating").annotate(n=Count("id"))
    distribution = dict.fromkeys(range(1, 6), 0)
    for row in rows:
        distribution[row["rating"]] = row["n"]
    count = sum(distribution.values())
    total = sum(star * n for star, n in distribution.items())
    avg = (Decimal(total) / count).quantize(Decimal("0.01"), ROUND_HALF_UP) if count else Decimal(0)
    return RatingSummary(avg=avg, count=count, distribution=distribution)


def photo_urls(review: Review) -> list[str]:
    return [str(default_storage.url(p)) for p in review.photos]


def reviewed_appointment_ids(appointment_ids: list[int]) -> set[int]:
    return set(
        Review.objects.filter(appointment_id__in=appointment_ids).values_list(
            "appointment_id", flat=True
        )
    )


@dataclass(frozen=True)
class ReviewBrief:
    business_id: int
    customer_id: int
    rating: int


def review_brief(review_id: int) -> ReviewBrief | None:
    row = Review.objects.filter(pk=review_id).values("business_id", "customer_id", "rating").first()
    return ReviewBrief(**row) if row else None
