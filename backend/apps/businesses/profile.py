"""Owner-side profile edits: details, review-gated fields, booking settings and closures."""

from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from django.conf import settings
from django.contrib.gis.geos import Point
from django.db import transaction
from django.utils.translation import gettext_lazy as _

from apps.catalog import selectors as catalog
from apps.core import audit
from apps.core.errors import conflict, not_found, validation_error
from apps.geo import selectors as geo

from . import events, onboarding, selectors
from .invalidation import touch
from .models import Business, Closure, ClosureKind, VerificationStatus

REVIEW_GATED = ("name", "category_id")
MAX_CLOSURES = 200
MAX_CLOSURE_DAYS = 366


def _bad(field: str, message: object) -> Any:
    return validation_error({field: [str(message)]})


def update_profile(owner_id: int, business_ref: str, changes: dict[str, Any]) -> Business:
    """Apply a partial update. Name/category of a verified business wait for admin review."""
    with transaction.atomic():
        business = selectors.owned_business(owner_id, business_ref, lock=True)
        needs_review = business.verification_status == VerificationStatus.VERIFIED
        gated: dict[str, Any] = {}
        before = {k: getattr(business, k) for k in ("name", "category_id", "description")}
        if "name" in changes:
            if needs_review and changes["name"] != business.name:
                gated["name"] = changes["name"]
            else:
                business.name = changes["name"]
                business.slug = onboarding.unique_slug(business.city_id, business.name, business.pk)
        if "category_id" in changes:
            category = catalog.get_active_category(changes["category_id"])
            if category is None:
                raise _bad("category_id", _("Choose an active category."))
            if needs_review and category.pk != business.category_id:
                gated["category_id"] = category.pk
            else:
                business.category = category
        for plain in ("description", "phone", "whatsapp", "address", "gender_type"):
            if plain in changes:
                setattr(business, plain, changes[plain])
        _apply_place(business, changes)
        if gated:
            pending = dict(business.pending_review_changes or {})
            pending.update(gated)
            business.pending_review_changes = pending
        business.save()
        if gated:
            audit.record(
                "business.change_review_requested",
                actor_id=owner_id,
                target_type="business",
                target_id=business.public_id,
                before={k: before[k] for k in before if k in gated or k + "_id" in gated},
                after=gated,
            )
            events.emit(events.business_change_review_requested, business.pk, changes=gated)
    touch(business.pk)
    return business


def _apply_place(business: Business, changes: dict[str, Any]) -> None:
    place_keys = {"city_id", "area_id", "lat", "lng"}
    if not place_keys & set(changes):
        return
    city = geo.get_onboardable_city(changes.get("city_id", business.city_id))
    if city is None:
        raise _bad("city_id", _("This city is not available."))
    area_id = changes.get("area_id", business.area_id if city.pk == business.city_id else None)
    area = None
    if area_id is not None:
        area = geo.get_onboardable_area(area_id, city.pk)
        if area is None:
            raise _bad("area_id", _("This area is not available in the selected city."))
    lat = changes.get("lat", business.location.y)
    lng = changes.get("lng", business.location.x)
    if not geo.within_city(city, lat, lng, float(settings.CITY_MAX_RADIUS_KM)):
        raise validation_error(
            {
                "lat": [str(_("The location is outside the selected city."))],
                "lng": [str(_("The location is outside the selected city."))],
            }
        )
    if city.pk != business.city_id:
        business.slug = onboarding.unique_slug(city.pk, business.name, business.pk)
    business.city = city
    business.area = area
    business.location = Point(lng, lat, srid=4326)


def update_booking_settings(owner_id: int, business_ref: str, changes: dict[str, Any]) -> Business:
    with transaction.atomic():
        business = selectors.owned_business(owner_id, business_ref, lock=True)
        before = {k: getattr(business, k) for k in changes}
        for key, value in changes.items():
            setattr(business, key, value)
        business.save(update_fields=[*changes, "updated_at"])
        audit.record(
            "business.booking_settings_changed",
            actor_id=owner_id,
            target_type="business",
            target_id=business.public_id,
            before=before,
            after=changes,
        )
    touch(business.pk)
    return business


# ---------------------------------------------------------------- closures


def _local_midnight(day: date, tz: ZoneInfo) -> datetime:
    return datetime.combine(day, time(0, 0), tzinfo=tz)


def create_closure(
    owner_id: int, business_ref: str, *, starts_on: date, ends_on: date, message: str
) -> Closure:
    """A whole-day closure from `starts_on` to `ends_on` inclusive, in the business timezone."""
    if ends_on < starts_on:
        raise _bad("ends_on", _("The end date must not be before the start date."))
    if (ends_on - starts_on).days >= MAX_CLOSURE_DAYS:
        raise _bad("ends_on", _("A closure can last at most 366 days."))
    with transaction.atomic():
        business = selectors.owned_business(owner_id, business_ref, lock=True)
        if business.closures.count() >= MAX_CLOSURES:
            raise conflict("too_many_closures", _("Too many closures. Remove old ones first."))
        tz = ZoneInfo(business.timezone)
        closure = Closure.objects.create(
            business=business,
            staff=None,
            starts_at=_local_midnight(starts_on, tz),
            ends_at=_local_midnight(ends_on + timedelta(days=1), tz),
            reason=message.strip(),
            kind=ClosureKind.HOLIDAY,
        )
    touch(business.pk)
    return closure


def list_closures(owner_id: int, business_ref: str) -> list[Closure]:
    business = selectors.owned_business(owner_id, business_ref)
    return list(business.closures.filter(staff__isnull=True))


def delete_closure(owner_id: int, closure_id: int) -> None:
    with transaction.atomic():
        closure = (
            Closure.objects.select_related("business")
            .filter(pk=closure_id, business__owner_id=owner_id)
            .first()
        )
        if closure is None:
            raise not_found()
        business_id = closure.business_id
        closure.delete()
    touch(business_id)
