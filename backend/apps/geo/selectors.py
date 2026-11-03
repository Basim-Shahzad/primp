from .models import Area, City, CityVisibility


def get_active_city(city_id: int) -> City | None:
    return City.objects.filter(pk=city_id, is_active=True).first()


def get_active_area(area_id: int) -> Area | None:
    return Area.objects.filter(pk=area_id, is_active=True).select_related("city").first()


def visible_cities() -> list[City]:
    return list(City.objects.filter(is_active=True, visibility=CityVisibility.PUBLIC))


def public_city_lookups(prefix: str = "city") -> dict[str, object]:
    """ORM lookups restricting a queryset to businesses in active, public cities."""
    return {f"{prefix}__is_active": True, f"{prefix}__visibility": CityVisibility.PUBLIC}
