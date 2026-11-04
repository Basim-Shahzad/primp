from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from django.contrib.gis.db.models.functions import Distance
from django.contrib.gis.geos import Point

from .models import Area, City, CityVisibility


def get_active_city(city_id: int) -> City | None:
    return City.objects.filter(pk=city_id, is_active=True).first()


def get_active_area(area_id: int) -> Area | None:
    return Area.objects.filter(pk=area_id, is_active=True).select_related("city").first()


def visible_cities() -> list[City]:
    return list(City.objects.filter(is_active=True, visibility=CityVisibility.PUBLIC))


_preview: ContextVar[bool] = ContextVar("preview_cities_visible", default=False)


@contextmanager
def preview_cities(enabled: bool) -> Iterator[None]:
    """Within the block, `preview` cities count as public (set only for authenticated admins)."""
    token = _preview.set(enabled)
    try:
        yield
    finally:
        _preview.reset(token)


def public_city_lookups(prefix: str = "city") -> dict[str, object]:
    """ORM lookups restricting a queryset to businesses in active, public cities."""
    if _preview.get():
        return {
            f"{prefix}__is_active": True,
            f"{prefix}__visibility__in": [CityVisibility.PUBLIC, CityVisibility.PREVIEW],
        }
    return {f"{prefix}__is_active": True, f"{prefix}__visibility": CityVisibility.PUBLIC}


def nearest_city_id(point: Point) -> int | None:
    """City of the active area whose centre is closest to the point."""
    area = (
        Area.objects.filter(is_active=True, city__is_active=True)
        .annotate(d=Distance("centre", point))
        .order_by("d")
        .first()
    )
    return area.city_id if area else None


def areas_in_city(city_id: int, *, exclude_id: int | None = None) -> list[Area]:
    qs = Area.objects.filter(is_active=True, city_id=city_id)
    if exclude_id is not None:
        qs = qs.exclude(pk=exclude_id)
    return list(qs.order_by("name")[:20])


def get_onboardable_city(city_id: int) -> City | None:
    """A city a new business may be placed in: active and not hidden."""
    return (
        City.objects.filter(pk=city_id, is_active=True)
        .exclude(visibility=CityVisibility.HIDDEN)
        .first()
    )


def get_onboardable_area(area_id: int, city_id: int) -> Area | None:
    return (
        Area.objects.filter(pk=area_id, city_id=city_id, is_active=True, city__is_active=True)
        .exclude(city__visibility=CityVisibility.HIDDEN)
        .select_related("city")
        .first()
    )


def distance_km(a_lat: float, a_lng: float, b_lat: float, b_lng: float) -> float:
    """Great-circle distance in kilometres (haversine)."""
    from math import asin, cos, radians, sin, sqrt

    dlat, dlng = radians(b_lat - a_lat), radians(b_lng - a_lng)
    h = sin(dlat / 2) ** 2 + cos(radians(a_lat)) * cos(radians(b_lat)) * sin(dlng / 2) ** 2
    return 2 * 6371.0088 * asin(sqrt(h))


def within_city(city: City, lat: float, lng: float, max_km: float) -> bool:
    return distance_km(city.centre.y, city.centre.x, lat, lng) <= max_km
