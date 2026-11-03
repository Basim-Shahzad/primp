import uuid

from django.db.models import QuerySet

from apps.geo.selectors import public_city_lookups

from .models import Business


def public_businesses() -> QuerySet[Business]:
    """Businesses visible to customers: listed, in an active public city (ADR-003)."""
    return Business.objects.filter(is_listed=True, **public_city_lookups())


def get_public_business(ref: str, city_slug: str | None = None) -> Business | None:
    """Lookup by public UUID or by slug (slugs are unique per city, so `city_slug` narrows)."""
    qs = public_businesses().select_related("city", "area", "category")
    try:
        return qs.filter(public_id=uuid.UUID(ref)).first()
    except ValueError:
        pass
    qs = qs.filter(slug=ref)
    if city_slug:
        qs = qs.filter(city__slug=city_slug)
    return qs.order_by("id").first()
