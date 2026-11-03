from django.db import transaction

from apps.businesses import services as business_services

from . import selectors


def recompute_business_rating(business_id: int) -> selectors.RatingSummary:
    """Recompute and store the aggregate rating from visible reviews (call inside a transaction)."""
    with transaction.atomic():
        summary = selectors.rating_summary(business_id)
        business_services.set_rating(business_id, summary.avg, summary.count)
    return summary
