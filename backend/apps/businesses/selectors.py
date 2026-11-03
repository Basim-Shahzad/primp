import uuid
from dataclasses import dataclass
from datetime import datetime, time

from django.db.models import Count, Min, QuerySet

from apps.geo.selectors import public_city_lookups

from .models import Business, Closure, OpeningHours, Service, Staff, StaffService


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


# ---- plain-data accessors for other apps (they may not import this app's models) ----


@dataclass(frozen=True)
class BookingConfig:
    id: int
    public_id: uuid.UUID
    name: str
    owner_id: int
    timezone: str
    confirm_mode: str
    cancellation_notice_hours: int
    lead_time_minutes: int
    max_advance_days: int
    slot_interval_minutes: int
    buffer_minutes: int
    staff_level_booking: bool
    parallel_capacity: int


@dataclass(frozen=True)
class ServiceInfo:
    id: int
    name: str
    price: int
    duration_minutes: int


def to_booking_config(b: Business) -> BookingConfig:
    return BookingConfig(
        id=b.pk,
        public_id=b.public_id,
        name=b.name,
        owner_id=b.owner_id,
        timezone=b.timezone,
        confirm_mode=b.confirm_mode,
        cancellation_notice_hours=b.cancellation_notice_hours,
        lead_time_minutes=b.lead_time_minutes,
        max_advance_days=b.max_advance_days,
        slot_interval_minutes=b.slot_interval_minutes,
        buffer_minutes=b.buffer_minutes,
        staff_level_booking=b.staff_level_booking,
        parallel_capacity=b.parallel_capacity,
    )


def get_booking_config(ref: str | uuid.UUID) -> BookingConfig | None:
    """Config of a publicly visible business, by public UUID."""
    try:
        public_id = ref if isinstance(ref, uuid.UUID) else uuid.UUID(str(ref))
    except ValueError:
        return None
    business = public_businesses().filter(public_id=public_id).first()
    return to_booking_config(business) if business else None


def get_booking_config_by_id(business_id: int, *, lock: bool = False) -> BookingConfig | None:
    qs = Business.objects.filter(pk=business_id)
    if lock:
        qs = qs.select_for_update()
    business = qs.first()
    return to_booking_config(business) if business else None


def get_services(
    business_id: int, ids: list[int], *, only_active: bool = True
) -> list[ServiceInfo]:
    qs = Service.objects.filter(business_id=business_id, pk__in=ids)
    if only_active:
        qs = qs.filter(is_active=True)
    return [
        ServiceInfo(s.pk, s.name, s.price, s.duration_minutes)
        for s in qs.order_by("sort_order", "id")
    ]


def capable_staff_ids(business_id: int, service_ids: list[int]) -> list[int]:
    """Active staff who can perform every one of the given services."""
    wanted = set(service_ids)
    rows = (
        StaffService.objects.filter(
            staff__business_id=business_id, staff__is_active=True, service_id__in=wanted
        )
        .values("staff_id")
        .annotate(n=Count("service_id", distinct=True))
        .filter(n=len(wanted))
        .order_by("staff_id")
    )
    return [r["staff_id"] for r in rows]


def opening_windows(business_id: int, weekday: int) -> dict[int | None, list[tuple[time, time]]]:
    """Shifts for a weekday keyed by staff id (None = business-wide)."""
    result: dict[int | None, list[tuple[time, time]]] = {}
    rows = OpeningHours.objects.filter(business_id=business_id, weekday=weekday).order_by(
        "opens_at"
    )
    for row in rows:
        result.setdefault(row.staff_id, []).append((row.opens_at, row.closes_at))
    return result


def closures_overlapping(
    business_id: int, start: datetime, end: datetime
) -> list[tuple[int | None, datetime, datetime]]:
    rows = Closure.objects.filter(business_id=business_id, starts_at__lt=end, ends_at__gt=start)
    return [(c.staff_id, c.starts_at, c.ends_at) for c in rows]


def lock_staff(staff_id: int) -> bool:
    """Row-lock a staff member (serialises booking for that staff). Needs an open transaction."""
    return Staff.objects.select_for_update().filter(pk=staff_id).exists()


def min_service_duration(business_id: int) -> int | None:
    value = Service.objects.filter(business_id=business_id, is_active=True).aggregate(
        m=Min("duration_minutes")
    )["m"]
    return int(value) if value is not None else None


def cheapest_shortest_service_id(business_id: int) -> int | None:
    """The active service with the shortest duration (ties: lowest id)."""
    row = (
        Service.objects.filter(business_id=business_id, is_active=True)
        .order_by("duration_minutes", "id")
        .values_list("pk", flat=True)
        .first()
    )
    return int(row) if row is not None else None


def opening_windows_all(business_id: int) -> dict[int, dict[int | None, list[tuple[time, time]]]]:
    """All shifts keyed by weekday then staff id (None = business-wide)."""
    result: dict[int, dict[int | None, list[tuple[time, time]]]] = {}
    rows = OpeningHours.objects.filter(business_id=business_id).order_by("opens_at")
    for row in rows:
        result.setdefault(row.weekday, {}).setdefault(row.staff_id, []).append(
            (row.opens_at, row.closes_at)
        )
    return result


def shortest_service(business_id: int) -> ServiceInfo | None:
    """The active service with the shortest duration (ties: lowest id)."""
    s = (
        Service.objects.filter(business_id=business_id, is_active=True)
        .order_by("duration_minutes", "id")
        .first()
    )
    return ServiceInfo(s.pk, s.name, s.price, s.duration_minutes) if s else None
