"""Assembly of the public business page (Step 9)."""

from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from django.core.files.storage import default_storage
from django.utils import timezone

from apps.reviews import selectors as reviews

from . import pagecache
from .models import Business, BusinessPhoto, Closure, OpeningHours, Service, Staff, StaffService

CLOSURE_LOOKAHEAD_DAYS = 90


def _url(path: str) -> str | None:
    return str(default_storage.url(path)) if path else None


def _build_static(b: Business, now: datetime) -> dict[str, Any]:
    photos = [
        {"id": p.pk, "url": str(default_storage.url(p.image)), "is_cover": p.is_cover}
        for p in BusinessPhoto.objects.filter(business=b)  # cover first via model ordering
    ]
    services = list(Service.objects.filter(business=b, is_active=True).select_related("category"))
    groups: dict[int | None, dict[str, Any]] = {}
    for s in services:
        key = s.category_id
        group = groups.setdefault(
            key,
            {
                "category": {"id": s.category_id, "name": s.category.name} if s.category else None,
                "sort": (s.category.sort_order, s.category.name) if s.category else (10**6, ""),
                "services": [],
            },
        )
        group["services"].append(
            {
                "id": s.pk,
                "name": s.name,
                "description": s.description,
                "price": s.price,
                "is_from_price": s.is_from_price,
                "duration_minutes": s.duration_minutes,
            }
        )
    service_groups = [
        {"category": g["category"], "services": g["services"]}
        for g in sorted(groups.values(), key=lambda g: g["sort"])
    ]

    staff_payload: list[dict[str, Any]] = []
    if b.staff_level_booking:
        active_service_ids = {s.pk for s in services}
        links: dict[int, list[int]] = {}
        for staff_id, service_id in StaffService.objects.filter(
            staff__business=b, staff__is_active=True
        ).values_list("staff_id", "service_id"):
            if service_id in active_service_ids:
                links.setdefault(staff_id, []).append(service_id)
        for m in Staff.objects.filter(business=b, is_active=True):
            staff_payload.append(
                {
                    "id": m.pk,
                    "name": m.name,
                    "photo_url": _url(m.photo),
                    "bio": m.bio,
                    "service_ids": sorted(links.get(m.pk, [])),
                }
            )

    shifts: dict[int, list[dict[str, str]]] = {d: [] for d in range(7)}
    for h in OpeningHours.objects.filter(business=b, staff__isnull=True):
        shifts[h.weekday].append(
            {"opens_at": h.opens_at.strftime("%H:%M"), "closes_at": h.closes_at.strftime("%H:%M")}
        )

    closures = [
        {"starts_at": c.starts_at, "ends_at": c.ends_at, "reason": c.reason, "kind": c.kind}
        for c in Closure.objects.filter(
            business=b,
            staff__isnull=True,
            ends_at__gt=now,
            starts_at__lt=now + timedelta(days=CLOSURE_LOOKAHEAD_DAYS),
        )[:50]
    ]
    summary = reviews.rating_summary(b.pk)
    return {
        "id": b.public_id,
        "slug": b.slug,
        "name": b.name,
        "category": {"id": b.category_id, "name": b.category.name},
        "description": b.description,
        "address": b.address,
        "phone": b.phone,
        "whatsapp": b.whatsapp,
        "gender_type": b.gender_type,
        "timezone": b.timezone,
        "location": {"lat": b.location.y, "lng": b.location.x},
        "area": {"id": b.area_id, "name": b.area.name} if b.area else None,
        "city": {"id": b.city_id, "name": b.city.name, "slug": b.city.slug},
        "is_verified": b.is_verified,
        "photos": photos,
        "services": service_groups,
        "staff": staff_payload,
        "hours": [{"weekday": d, "shifts": shifts[d]} for d in range(7)],
        "closures": closures,
        "booking": {
            "confirm_mode": b.confirm_mode,
            "cancellation_notice_hours": b.cancellation_notice_hours,
            "lead_time_minutes": b.lead_time_minutes,
            "max_advance_days": b.max_advance_days,
            "slot_interval_minutes": b.slot_interval_minutes,
            "staff_level_booking": b.staff_level_booking,
        },
        "rating": {
            "avg": float(summary.avg),
            "count": summary.count,
            "distribution": {str(k): v for k, v in summary.distribution.items()},
        },
    }


def is_open_now(payload: dict[str, Any], now: datetime) -> bool:
    tz = ZoneInfo(payload["timezone"])
    local = now.astimezone(tz)
    if any(c["starts_at"] <= now < c["ends_at"] for c in payload["closures"]):
        return False
    stamp = local.strftime("%H:%M")
    day = next(d for d in payload["hours"] if d["weekday"] == local.weekday())
    return any(s["opens_at"] <= stamp < s["closes_at"] for s in day["shifts"])


def render_page(b: Business, now: datetime | None = None) -> dict[str, Any]:
    """Cached static payload plus the time-dependent fields (open now, upcoming closures)."""
    now = now or timezone.now()
    payload = pagecache.get(b.pk)
    if payload is None:
        payload = _build_static(b, now)
        pagecache.put(b.pk, payload)
    return {
        **payload,
        "closures": [c for c in payload["closures"] if c["ends_at"] > now],
        "is_open_now": is_open_now(payload, now),
    }
