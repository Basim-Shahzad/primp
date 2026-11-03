"""Location-aware business search (Step 8)."""

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

from django.conf import settings
from django.contrib.gis.db.models.functions import Distance
from django.contrib.gis.geos import Point
from django.contrib.gis.measure import D
from django.contrib.postgres.search import TrigramSimilarity
from django.core import signing
from django.core.files.storage import default_storage
from django.db.models import (
    Exists,
    ExpressionWrapper,
    F,
    FloatField,
    IntegerField,
    Min,
    OuterRef,
    Q,
    QuerySet,
    Subquery,
)
from django.db.models.functions import Coalesce
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from ninja import Field, Schema

from apps.accounts import selectors as accounts
from apps.bookings import availability
from apps.catalog import selectors as catalog
from apps.core.errors import AppError
from apps.core.pagination import DEFAULT_LIMIT, CursorPage, encode_cursor, paginate_cursor
from apps.geo import selectors as geo

from . import selectors
from .models import Business, BusinessPhoto, GenderType, OpeningHours, Service, VerificationStatus

logger = logging.getLogger(__name__)

RADII = (1, 3, 5, 10)
DEFAULT_RADIUS_KM = 5
SOONEST_CANDIDATE_CAP = 300
FAR_FUTURE = datetime(9999, 1, 1, tzinfo=ZoneInfo("UTC"))
PRICE_SORT_NULL = 10**9


_Date = date


class SearchParams(Schema):
    lat: float | None = Field(None, ge=-90, le=90)
    lng: float | None = Field(None, ge=-180, le=180)
    area_id: int | None = None
    city_id: int | None = None
    q: str | None = Field(None, max_length=100)
    category_id: int | None = None
    subcategory_id: int | None = None
    price_min: int | None = Field(None, ge=0)
    price_max: int | None = Field(None, ge=0)
    rating_min: float | None = Field(None, ge=0, le=5)
    available: Literal["today", "tomorrow", "date"] | None = None
    date: _Date | None = None
    time_from: time | None = None
    time_to: time | None = None
    radius_km: int | None = None
    verified_only: bool = False
    gender_type: Literal["women", "men", "unisex"] | None = None
    sort: Literal["distance", "rating", "price", "soonest"] | None = None
    cursor: str | None = None
    limit: int = Field(DEFAULT_LIMIT, ge=1, le=50)


def _bad(field_name: str, message: object) -> AppError:
    return AppError("validation_error", _("Invalid input."), 422, {field_name: [str(message)]})


def validate_params(p: SearchParams) -> None:
    if (p.lat is None) != (p.lng is None):
        raise _bad("lat" if p.lat is None else "lng", _("lat and lng must be provided together."))
    if p.radius_km is not None:
        if p.radius_km not in RADII:
            raise _bad("radius_km", _("radius_km must be one of 1, 3, 5, 10."))
        if p.lat is None:
            raise _bad("radius_km", _("radius_km requires lat and lng."))
    if p.sort == "distance" and p.lat is None:
        raise _bad("sort", _("Sorting by distance requires lat and lng."))
    if p.price_min is not None and p.price_max is not None and p.price_min > p.price_max:
        raise _bad("price_min", _("price_min cannot exceed price_max."))
    if p.available == "date" and p.date is None:
        raise _bad("date", _("date is required when available=date."))
    if p.available != "date" and p.date is not None:
        raise _bad("date", _("date is only valid with available=date."))
    if (p.time_from or p.time_to) and p.available is None:
        raise _bad("available", _("time_from/time_to require the available filter."))
    if p.time_from and p.time_to and p.time_to <= p.time_from:
        raise _bad("time_to", _("time_to must be after time_from."))
    if p.available == "date" and p.date and p.date < _local_today():
        raise _bad("date", _("The date is in the past."))


def _local_today() -> date:
    return timezone.now().astimezone(ZoneInfo(settings.BUSINESS_TIME_ZONE)).date()


def effective_sort(p: SearchParams) -> str:
    return p.sort or ("distance" if p.lat is not None else "rating")


def _origin(p: SearchParams) -> Point | None:
    return Point(p.lng, p.lat, srid=4326) if p.lat is not None and p.lng is not None else None


def build_queryset(
    p: SearchParams, *, radius_km: int | None = None, apply_radius: bool = True
) -> QuerySet[Business]:
    qs = selectors.public_businesses().select_related("area", "city")
    active_services = Service.objects.filter(business=OuterRef("pk"), is_active=True)

    qs = qs.annotate(
        starting_price=Subquery(
            active_services.values("business").annotate(m=Min("price")).values("m")[:1],
            output_field=IntegerField(),
        )
    ).annotate(sort_price=Coalesce(F("starting_price"), PRICE_SORT_NULL))

    origin = _origin(p)
    if origin is not None:
        qs = qs.annotate(
            dist=ExpressionWrapper(Distance("location", origin), output_field=FloatField())
        )
        if apply_radius:
            radius = radius_km or p.radius_km or DEFAULT_RADIUS_KM
            qs = qs.filter(location__dwithin=(origin, D(km=radius)))

    if p.area_id is not None:
        qs = qs.filter(area_id=p.area_id)
    if p.city_id is not None:
        qs = qs.filter(city_id=p.city_id)
    if p.verified_only:
        qs = qs.filter(verification_status=VerificationStatus.VERIFIED)
    if p.gender_type == "unisex":
        qs = qs.filter(gender_type=GenderType.UNISEX)
    elif p.gender_type:
        qs = qs.filter(gender_type__in=[p.gender_type, GenderType.UNISEX])
    if p.rating_min is not None:
        qs = qs.filter(rating_avg__gte=p.rating_min)

    if p.q:
        text = p.q.strip()
        qs = qs.annotate(name_sim=TrigramSimilarity("name", text)).filter(
            Q(name__icontains=text)
            | Q(name_sim__gt=0.3)
            | Exists(active_services.filter(name__icontains=text))
        )
    if p.category_id is not None:
        ids = catalog.category_ids_with_descendants(p.category_id)
        qs = qs.filter(Q(category_id__in=ids) | Exists(active_services.filter(category_id__in=ids)))
    if p.subcategory_id is not None:
        qs = qs.filter(Exists(active_services.filter(category_id=p.subcategory_id)))
    if p.price_min is not None or p.price_max is not None:
        priced = active_services
        if p.price_min is not None:
            priced = priced.filter(price__gte=p.price_min)
        if p.price_max is not None:
            priced = priced.filter(price__lte=p.price_max)
        qs = qs.filter(Exists(priced))

    if p.available is not None:
        qs = qs.filter(Exists(active_services))
        hours = OpeningHours.objects.filter(
            business=OuterRef("pk"), weekday=_target_date(p).weekday()
        )
        if p.time_to is not None:
            hours = hours.filter(opens_at__lte=p.time_to)
        if p.time_from is not None:
            hours = hours.filter(closes_at__gt=p.time_from)
        qs = qs.filter(Exists(hours))
    return qs


def _target_date(p: SearchParams) -> date:
    today = _local_today()
    if p.available == "tomorrow":
        return today + timedelta(days=1)
    if p.available == "date" and p.date:
        return p.date
    return today


def _ordering(sort: str) -> list[str]:
    return {
        "distance": ["dist", "id"],
        "rating": ["-rating_avg", "-rating_count", "-id"],
        "price": ["sort_price", "id"],
    }[sort]


def _has_matching_slot(business: Business, p: SearchParams, now: datetime) -> bool:
    config = selectors.to_booking_config(business)
    service_id = selectors.cheapest_shortest_service_id(business.pk)
    if service_id is None:
        return False
    tz = ZoneInfo(config.timezone)
    local_today = now.astimezone(tz).date()
    day = (
        local_today
        if p.available == "today"
        else local_today + timedelta(days=1)
        if p.available == "tomorrow"
        else p.date
    )
    assert day is not None
    try:
        slots = availability.get_available_slots(config, day, [service_id], now=now)
    except AppError:
        return False
    for slot in slots:
        local_start = slot.start.astimezone(tz).time()
        if p.time_from and local_start < p.time_from:
            continue
        if p.time_to and local_start > p.time_to:
            continue
        return True
    return False


@dataclass
class SearchItem:
    business: Business
    distance_km: float | None
    starting_price: int | None
    cover_url: str | None
    next_slot: datetime | None
    is_wishlisted: bool | None


@dataclass
class SearchResult:
    items: list[SearchItem]
    next_cursor: str | None
    suggestions: list[dict[str, Any]] = field(default_factory=list)


def _cover_urls(ids: list[int]) -> dict[int, str]:
    covers: dict[int, str] = {}
    for photo in BusinessPhoto.objects.filter(business_id__in=ids).order_by(
        "business_id", "-is_cover", "sort_order", "id"
    ):
        covers.setdefault(photo.business_id, photo.image)
    return {bid: str(default_storage.url(path)) for bid, path in covers.items()}


def _assemble(rows: list[Business], user_id: int | None, now: datetime) -> list[SearchItem]:
    ids = [b.pk for b in rows]
    covers = _cover_urls(ids)
    wished = accounts.wishlisted_business_ids(user_id, ids) if user_id is not None else None
    items = []
    for b in rows:
        dist = getattr(b, "dist", None)
        items.append(
            SearchItem(
                business=b,
                distance_km=round(dist / 1000, 2) if dist is not None else None,
                starting_price=getattr(b, "starting_price", None),
                cover_url=covers.get(b.pk),
                next_slot=availability.next_available_slot(selectors.to_booking_config(b), now=now),
                is_wishlisted=(b.pk in wished) if wished is not None else None,
            )
        )
    return items


def search(p: SearchParams, user_id: int | None) -> SearchResult:
    validate_params(p)
    now = timezone.now()
    sort = effective_sort(p)
    qs = build_queryset(p)
    result = _search_soonest(qs, p, now) if sort == "soonest" else _search_keyset(qs, p, sort, now)
    items = _assemble(result[0], user_id, now)
    out = SearchResult(items, result[1])
    if not items and not p.cursor:
        out.suggestions = suggestions_for(p)
    return out


def _passes(business: Business, p: SearchParams, now: datetime) -> bool:
    return p.available is None or _has_matching_slot(business, p, now)


def _search_keyset(
    qs: QuerySet[Business], p: SearchParams, sort: str, now: datetime
) -> tuple[list[Business], str | None]:
    ordering = _ordering(sort)
    if p.available is None:
        page: CursorPage[Business] = paginate_cursor(
            qs, ordering=ordering, cursor=p.cursor, limit=p.limit
        )
        return page.items, page.next_cursor
    # Availability depends on live bookings, so it cannot be a SQL filter: walk candidates in
    # order and keep those with a matching slot until the page is full.
    results: list[Business] = []
    cursor = p.cursor
    batch = max(p.limit * 3, 30)
    next_cursor: str | None = None
    while True:
        page = paginate_cursor(qs, ordering=ordering, cursor=cursor, limit=batch)
        for index, candidate in enumerate(page.items):
            if _passes(candidate, p, now):
                results.append(candidate)
                if len(results) == p.limit:
                    more = index < len(page.items) - 1 or page.next_cursor is not None
                    next_cursor = encode_cursor(ordering, candidate) if more else None
                    return results, next_cursor
        if page.next_cursor is None:
            return results, None
        cursor = page.next_cursor


def _search_soonest(
    qs: QuerySet[Business], p: SearchParams, now: datetime
) -> tuple[list[Business], str | None]:
    base_order = ["dist", "id"] if p.lat is not None else ["-rating_avg", "-id"]
    candidates = [b for b in qs.order_by(*base_order)[:SOONEST_CANDIDATE_CAP] if _passes(b, p, now)]
    keyed = []
    for b in candidates:
        slot = availability.next_available_slot(selectors.to_booking_config(b), now=now)
        keyed.append(((slot or FAR_FUTURE).isoformat(), b.pk, b))
    keyed.sort(key=lambda t: (t[0], t[1]))
    start = 0
    if p.cursor:
        try:
            data = signing.loads(p.cursor, salt="search.soonest")
            marker = (str(data["s"]), int(data["i"]))
        except (signing.BadSignature, KeyError, TypeError, ValueError) as exc:
            raise AppError("invalid_cursor", _("The pagination cursor is invalid."), 400) from exc
        start = next((i for i, (s, pk, _b) in enumerate(keyed) if (s, pk) > marker), len(keyed))
    window = keyed[start : start + p.limit]
    next_cursor = None
    if start + p.limit < len(keyed) and window:
        s, pk, _b = window[-1]
        next_cursor = signing.dumps({"s": s, "i": pk}, salt="search.soonest")
    return [b for _s, _pk, b in window], next_cursor


def suggestions_for(p: SearchParams) -> list[dict[str, Any]]:
    """Ideas for an empty result: wider radius, nearby areas, related categories."""
    out: list[dict[str, Any]] = []
    origin = _origin(p)
    if origin is not None:
        current = p.radius_km or DEFAULT_RADIUS_KM
        for radius in (r for r in RADII if r > current):
            count = build_queryset(p, radius_km=radius).count()
            if count:
                out.append(
                    {
                        "type": "wider_radius",
                        "label": f"{count} within {radius} km",
                        "params": {"radius_km": radius},
                        "count": count,
                    }
                )
                break
    relaxed = p.model_copy(update={"area_id": None, "city_id": None})
    city_id = p.city_id
    if city_id is None and p.area_id is not None:
        area = geo.get_active_area(p.area_id)
        city_id = area.city_id if area else None
    if origin is not None and city_id is None:
        city_id = geo.nearest_city_id(origin)
    areas = geo.areas_in_city(city_id, exclude_id=p.area_id) if city_id else []
    nearby: list[dict[str, Any]] = []
    for area in areas:
        count = build_queryset(
            relaxed.model_copy(update={"area_id": area.pk}), apply_radius=False
        ).count()
        if count:
            nearby.append(
                {
                    "type": "nearby_area",
                    "label": f"{area.name} ({count})",
                    "params": {"area_id": area.pk},
                    "count": count,
                }
            )
    out.extend(sorted(nearby, key=lambda s: -int(s["count"]))[:3])
    if p.category_id is not None or p.subcategory_id is not None:
        related: list[dict[str, Any]] = []
        for category in catalog.category_tree():
            if category.pk == p.category_id:
                continue
            probe = relaxed.model_copy(update={"category_id": category.pk, "subcategory_id": None})
            count = build_queryset(probe, apply_radius=False).count()
            if count:
                related.append(
                    {
                        "type": "related_category",
                        "label": f"{category.name} ({count})",
                        "params": {"category_id": category.pk, "subcategory_id": None},
                        "count": count,
                    }
                )
        out.extend(sorted(related, key=lambda s: -int(s["count"]))[:3])
    return out


def cards_for(
    business_ids: list[int], user_id: int | None, now: datetime | None = None
) -> list[SearchItem]:
    """Result cards for specific businesses, in the given order (only publicly visible ones)."""
    now = now or timezone.now()
    rows = {
        b.pk: b for b in build_queryset(SearchParams.model_validate({})).filter(pk__in=business_ids)
    }
    ordered = [rows[i] for i in business_ids if i in rows]
    return _assemble(ordered, user_id, now)
