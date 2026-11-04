"""Resumable business onboarding: draft creation, step validation, checklist and submission."""

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import time
from typing import Any

from django.conf import settings
from django.contrib.gis.geos import Point
from django.db import transaction
from django.utils import timezone
from django.utils.text import slugify
from django.utils.translation import gettext_lazy as _
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from apps.backoffice import config_service
from apps.catalog import selectors as catalog
from apps.core import audit, ratelimit
from apps.core.errors import AppError, conflict, fields_from_pydantic, validation_error
from apps.geo import selectors as geo
from apps.notifications import services as notifications

from . import events, hours, selectors
from .invalidation import touch
from .models import (
    Business,
    BusinessDocument,
    BusinessPhoto,
    BusinessSubmission,
    DocumentKind,
    GenderType,
    Service,
    StarterMenuItem,
    VerificationStatus,
)
from .provider_validators import E164, Address, Description, Text150

logger = logging.getLogger(__name__)

STEPS = ("basics", "location", "services", "staff_hours", "media", "documents")
REQUIRED_DOCUMENTS = (
    DocumentKind.CNIC_FRONT,
    DocumentKind.CNIC_BACK,
    DocumentKind.SELFIE_WITH_CNIC,
)
MAX_PHOTOS = 20
EDITABLE = (VerificationStatus.DRAFT, VerificationStatus.NEEDS_INFO, VerificationStatus.REJECTED)
CREATE_LIMIT = (10, 3600)


class StrictIn(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BasicsStep(StrictIn):
    name: Text150
    category_id: int
    gender_type: GenderType
    description: Description = ""


class LocationStep(StrictIn):
    city_id: int | None = None
    area_id: int | None = None
    address: Address
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)
    phone: E164
    whatsapp: E164 = ""

    @field_validator("phone")
    @classmethod
    def _phone_required(cls, value: str) -> str:
        if not value:
            raise ValueError(str(_("A contact phone number is required.")))
        return value


class ServicesStep(StrictIn):
    starter_item_ids: list[int] = Field(default_factory=list, max_length=50)


class ShiftIn(StrictIn):
    opens_at: time
    closes_at: time


class DayIn(StrictIn):
    weekday: int
    shifts: list[ShiftIn] = Field(default_factory=list, max_length=10)


class StaffHoursStep(StrictIn):
    hours: list[DayIn] | None = None


class EmptyStep(StrictIn):
    pass


STEP_SCHEMAS: dict[str, type[BaseModel]] = {
    "basics": BasicsStep,
    "location": LocationStep,
    "services": ServicesStep,
    "staff_hours": StaffHoursStep,
    "media": EmptyStep,
    "documents": EmptyStep,
}


@dataclass
class ChecklistItem:
    key: str
    required: bool
    done: bool
    missing: list[str] = field(default_factory=list)


@dataclass
class Checklist:
    items: list[ChecklistItem]
    completed_steps: list[str]
    last_step: str

    @property
    def is_complete(self) -> bool:
        return all(i.done for i in self.items if i.required)


# ---------------------------------------------------------------- helpers


def unique_slug(city_id: int, name: str, exclude_pk: int | None = None) -> str:
    base = (slugify(name) or "business")[:140]
    slug, n = base, 1
    others = Business.objects.filter(city_id=city_id)
    if exclude_pk is not None:
        others = others.exclude(pk=exclude_pk)
    while others.filter(slug=slug).exists():
        n += 1
        slug = f"{base}-{n}"
    return slug


def _parse(step: str, payload: dict[str, Any]) -> Any:
    try:
        return STEP_SCHEMAS[step].model_validate(payload)
    except ValidationError as exc:
        errors = [{**e, "msg": str(e.get("ctx", {}).get("error", e["msg"]))} for e in exc.errors()]
        raise validation_error(fields_from_pydantic(errors)) from exc


def _progress(business: Business) -> dict[str, Any]:
    progress = business.onboarding_progress or {}
    return {
        "completed": list(progress.get("completed", [])),
        "last_step": progress.get("last_step", ""),
    }


def _mark(business: Business, step: str) -> None:
    progress = _progress(business)
    if step not in progress["completed"]:
        progress["completed"].append(step)
    progress["last_step"] = step
    business.onboarding_progress = progress


def _ensure_editable(business: Business) -> None:
    if business.verification_status not in EDITABLE:
        raise conflict(
            "business_locked",
            _("This business cannot be edited through onboarding in its current status."),
        )


def _bad_field(field_name: str, message: object) -> AppError:
    return validation_error({field_name: [str(message)]})


# ---------------------------------------------------------------- creation


def create_draft(
    owner_id: int,
    *,
    name: str,
    category_id: int,
    gender_type: str,
    description: str,
    city_id: int,
) -> Business:
    ratelimit.enforce(
        f"biz-create:{owner_id}", limit=CREATE_LIMIT[0], window_seconds=CREATE_LIMIT[1]
    )
    category = catalog.get_active_category(category_id)
    if category is None:
        raise _bad_field("category_id", _("Choose an active category."))
    city = geo.get_onboardable_city(city_id)
    if city is None:
        raise _bad_field("city_id", _("This city is not available."))
    with transaction.atomic():
        business = Business.objects.create(
            owner_id=owner_id,
            name=name.strip(),
            slug=unique_slug(city.pk, name),
            category=category,
            description=description.strip(),
            gender_type=gender_type,
            city=city,
            location=Point(city.centre.x, city.centre.y, srid=4326),  # placeholder until `location`
            verification_status=VerificationStatus.DRAFT,
            is_listed=False,
            onboarding_progress={"completed": ["basics"], "last_step": "basics"},
            cancellation_notice_hours=int(config_service.get("default_cancellation_notice_hours")),
        )
    return business


# ---------------------------------------------------------------- steps


def apply_step(owner_id: int, business_ref: str, step: str, payload: dict[str, Any]) -> Business:
    from apps.core.errors import not_found

    if step not in STEPS:
        raise not_found()
    with transaction.atomic():
        business = selectors.owned_business(owner_id, business_ref, lock=True)
        _ensure_editable(business)
        data = _parse(step, payload)
        handler = _HANDLERS[step]
        handler(business, data)
        _mark(business, step)
        business.save()
    touch(business.pk)
    return business


def _basics(business: Business, data: BasicsStep) -> None:
    category = catalog.get_active_category(data.category_id)
    if category is None:
        raise _bad_field("category_id", _("Choose an active category."))
    if data.name != business.name:
        business.slug = unique_slug(business.city_id, data.name, business.pk)
    business.name = data.name
    business.category = category
    business.gender_type = data.gender_type
    business.description = data.description


def _location(business: Business, data: LocationStep) -> None:
    city = geo.get_onboardable_city(business.city_id if data.city_id is None else data.city_id)
    if city is None:
        raise _bad_field("city_id", _("This city is not available."))
    area = None
    if data.area_id is not None:
        area = geo.get_onboardable_area(data.area_id, city.pk)
        if area is None:
            raise _bad_field("area_id", _("This area is not available in the selected city."))
    if not geo.within_city(city, data.lat, data.lng, float(settings.CITY_MAX_RADIUS_KM)):
        raise validation_error(
            {
                "lat": [str(_("The location is outside the selected city."))],
                "lng": [str(_("The location is outside the selected city."))],
            }
        )
    if city.pk != business.city_id:
        business.slug = unique_slug(city.pk, business.name, business.pk)
    business.city = city
    business.area = area
    business.address = data.address
    business.location = Point(data.lng, data.lat, srid=4326)
    business.phone = data.phone
    business.whatsapp = data.whatsapp


def _services(business: Business, data: ServicesStep) -> None:
    if data.starter_item_ids:
        existing = {n.lower() for n in business.services.values_list("name", flat=True)}
        items = StarterMenuItem.objects.filter(pk__in=data.starter_item_ids)
        order = business.services.count()
        for item in items:
            if item.name.lower() in existing:
                continue
            Service.objects.create(
                business=business,
                category_id=item.category_id,
                name=item.name,
                description=item.description,
                price=item.price,
                duration_minutes=item.duration_minutes,
                sort_order=order,
            )
            order += 1
    if not business.services.filter(is_active=True).exists():
        raise _bad_field("services", _("Add at least one active service."))


def _staff_hours(business: Business, data: StaffHoursStep) -> None:
    if data.hours is not None:
        week = [
            hours.DayShifts(d.weekday, [(s.opens_at, s.closes_at) for s in d.shifts])
            for d in data.hours
        ]
        hours.replace_week(business.pk, None, week)
    if not business.opening_hours.filter(staff__isnull=True).exists():
        raise _bad_field("hours", _("Set the weekly opening hours."))


def _media(business: Business, data: EmptyStep) -> None:
    errors: dict[str, list[str]] = {}
    if not business.logo:
        errors["logo"] = [str(_("Upload a logo."))]
    count = business.photos.count()
    if count < 1:
        errors["photos"] = [str(_("Upload at least one photo."))]
    elif count > MAX_PHOTOS:
        errors["photos"] = [str(_("At most 20 photos."))]
    if errors:
        raise validation_error(errors)


def _documents(business: Business, data: EmptyStep) -> None:
    missing = _missing_documents(business)
    if missing:
        raise validation_error({"documents": [f"{k}: " + str(_("required")) for k in missing]})


_HANDLERS: dict[str, Callable[[Business, Any], None]] = {
    "basics": _basics,
    "location": _location,
    "services": _services,
    "staff_hours": _staff_hours,
    "media": _media,
    "documents": _documents,
}


def _missing_documents(business: Business) -> list[str]:
    have = set(
        BusinessDocument.objects.filter(business=business)
        .exclude(status="rejected")
        .values_list("kind", flat=True)
    )
    return [k.value for k in REQUIRED_DOCUMENTS if k.value not in have]


# ---------------------------------------------------------------- checklist


def checklist(business: Business) -> Checklist:
    progress = _progress(business)
    done_steps = set(progress["completed"])

    def item(key: str, required: bool, missing: list[str]) -> ChecklistItem:
        return ChecklistItem(key, required, not missing, missing)

    basics_missing = []
    if not business.name:
        basics_missing.append("name")
    if not business.description.strip():
        basics_missing.append("description")
    location_missing = []
    if "location" not in done_steps:
        location_missing.append("location")
    if not business.address:
        location_missing.append("address")
    if not business.phone:
        location_missing.append("phone")
    services_missing = [] if business.services.filter(is_active=True).exists() else ["services"]
    hours_missing = [] if business.opening_hours.filter(staff__isnull=True).exists() else ["hours"]
    media_missing = []
    if not business.logo:
        media_missing.append("logo")
    n_photos = business.photos.count()
    if n_photos < 1:
        media_missing.append("photos")
    elif n_photos > MAX_PHOTOS:
        media_missing.append("photos_max")
    items = [
        item("basics", True, basics_missing),
        item("location", True, location_missing),
        item("services", True, services_missing),
        item("staff_hours", True, hours_missing),
        item("media", True, media_missing),
        item("documents", True, _missing_documents(business)),
    ]
    return Checklist(items, progress["completed"], progress["last_step"])


# ---------------------------------------------------------------- submission


def notify_status_changed(business: Business, status: str, message: str = "") -> None:
    notifications.notify(
        business.owner_id,
        "onboarding_status_changed",
        {"business_name": business.name, "status": status.replace("_", " "), "message": message},
    )


def submit(
    owner_id: int,
    business_ref: str,
    *,
    accept_provider_agreement: bool,
    accept_commission_terms: bool,
    terms_version: str,
    ip: str | None,
) -> Business:
    with transaction.atomic():
        business = selectors.owned_business(owner_id, business_ref, lock=True)
        status = business.verification_status
        if status == VerificationStatus.PENDING:
            raise conflict("already_submitted", _("This business is already awaiting review."))
        if status not in EDITABLE:
            raise conflict("invalid_status", _("This business cannot be submitted right now."))
        resubmission = status == VerificationStatus.REJECTED
        if (
            resubmission
            and business.submissions.filter(is_resubmission_after_rejection=True).exists()
        ):
            raise conflict(
                "resubmission_not_allowed",
                _("A rejected business can be resubmitted only once."),
            )
        errors: dict[str, list[str]] = {}
        if not accept_provider_agreement:
            errors["accept_provider_agreement"] = [
                str(_("You must accept the provider agreement."))
            ]
        if not accept_commission_terms:
            errors["accept_commission_terms"] = [str(_("You must accept the commission terms."))]
        if terms_version != settings.PROVIDER_TERMS_VERSION:
            errors["terms_version"] = [str(_("These terms are out of date. Please review again."))]
        for it in checklist(business).items:
            if it.required and not it.done:
                errors[it.key] = it.missing
        if errors:
            raise validation_error(errors, _("Complete every required step before submitting."))
        now = timezone.now()
        number = business.submissions.count() + 1
        BusinessSubmission.objects.create(
            business=business,
            number=number,
            terms_version=terms_version,
            is_resubmission_after_rejection=resubmission,
            accepted_at=now,
            ip_address=ip,
        )
        previous = business.verification_status
        business.verification_status = VerificationStatus.PENDING
        business.status_message = ""
        business.is_listed = False
        business.save(
            update_fields=["verification_status", "status_message", "is_listed", "updated_at"]
        )
        audit.record(
            "business.submitted",
            actor_id=owner_id,
            target_type="business",
            target_id=business.public_id,
            before={"status": previous},
            after={"status": business.verification_status, "submission": number},
            ip=ip,
        )
        events.emit(events.business_submitted, business.pk, submission=number)
        notify_status_changed(business, VerificationStatus.PENDING)
    touch(business.pk)
    return business


def photo_count(business: Business) -> int:
    return BusinessPhoto.objects.filter(business=business).count()
