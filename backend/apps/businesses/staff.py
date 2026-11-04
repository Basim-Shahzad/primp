"""Staff members: create, edit, photo, services and per-staff hours."""

import uuid
from typing import Any

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import transaction
from django.utils.translation import gettext_lazy as _

from apps.core.errors import conflict, validation_error
from apps.core.images import process_image

from . import hours, selectors
from .invalidation import touch
from .models import Service, Staff, StaffService

MAX_STAFF = 50


def list_staff(owner_id: int, business_ref: str, *, include_inactive: bool = True) -> list[Staff]:
    business = selectors.owned_business(owner_id, business_ref)
    qs = business.staff.all()
    return list(qs if include_inactive else qs.filter(is_active=True))


def service_ids_by_staff(staff_ids: list[int]) -> dict[int, list[int]]:
    result: dict[int, list[int]] = {sid: [] for sid in staff_ids}
    for staff_id, service_id in StaffService.objects.filter(staff_id__in=staff_ids).values_list(
        "staff_id", "service_id"
    ):
        result[staff_id].append(service_id)
    return {k: sorted(v) for k, v in result.items()}


def staff_with_override(staff_ids: list[int]) -> set[int]:
    from .models import OpeningHours

    return set(
        OpeningHours.objects.filter(staff_id__in=staff_ids).values_list("staff_id", flat=True)
    )


def _valid_service_ids(business_id: int, ids: list[int]) -> list[int]:
    unique = list(dict.fromkeys(ids))
    found = set(
        Service.objects.filter(business_id=business_id, pk__in=unique).values_list("pk", flat=True)
    )
    if found != set(unique):
        raise validation_error({"service_ids": [str(_("Unknown service for this business."))]})
    return unique


def create_staff(
    owner_id: int, business_ref: str, *, name: str, bio: str, service_ids: list[int]
) -> Staff:
    with transaction.atomic():
        business = selectors.owned_business(owner_id, business_ref, lock=True)
        if business.staff.count() >= MAX_STAFF:
            raise conflict("too_many_staff", _("Too many staff members."))
        valid = _valid_service_ids(business.pk, service_ids)
        staff = Staff.objects.create(business=business, name=name, bio=bio)
        StaffService.objects.bulk_create([StaffService(staff=staff, service_id=i) for i in valid])
    touch(business.pk)
    return staff


def update_staff(owner_id: int, staff_id: int, changes: dict[str, Any]) -> Staff:
    with transaction.atomic():
        staff = selectors.owned_staff(owner_id, staff_id)
        staff = Staff.objects.select_for_update().get(pk=staff.pk)
        if changes.get("is_active") is False:
            raise conflict("use_deactivate", _("Deactivate staff through the deactivate action."))
        for key, value in changes.items():
            setattr(staff, key, value)
        staff.save()
    touch(staff.business_id)
    return staff


def set_photo(owner_id: int, staff_id: int, data: bytes) -> Staff:
    image = process_image(data, max_dimension=512)
    with transaction.atomic():
        staff = selectors.owned_staff(owner_id, staff_id)
        staff = Staff.objects.select_for_update().select_related("business").get(pk=staff.pk)
        old = staff.photo
        staff.photo = default_storage.save(
            f"staff/{staff.business.public_id}/{uuid.uuid4().hex}.{image.ext}",
            ContentFile(image.content),
        )
        staff.save(update_fields=["photo", "updated_at"])
        if old:
            transaction.on_commit(lambda: default_storage.delete(old))
    touch(staff.business_id)
    return staff


def set_hours(owner_id: int, staff_id: int, week: list[hours.DayShifts]) -> Staff:
    staff = selectors.owned_staff(owner_id, staff_id)
    hours.replace_week(staff.business_id, staff.pk, week)  # an empty week removes the override
    return staff


def get_hours(owner_id: int, staff_id: int) -> list[hours.DayShifts]:
    staff = selectors.owned_staff(owner_id, staff_id)
    return hours.week_of(staff.business_id, staff.pk)


def valid_service_ids_for(staff: Staff, ids: list[int]) -> list[int]:
    return _valid_service_ids(staff.business_id, ids)


def set_links(staff: Staff, service_ids: list[int]) -> None:
    """Make the staff member's services exactly `service_ids` (already validated)."""
    wanted = set(service_ids)
    current = set(StaffService.objects.filter(staff=staff).values_list("service_id", flat=True))
    StaffService.objects.filter(staff=staff, service_id__in=current - wanted).delete()
    StaffService.objects.bulk_create(
        [StaffService(staff=staff, service_id=i) for i in sorted(wanted - current)]
    )


def mark_inactive(staff: Staff) -> None:
    staff.is_active = False
    staff.save(update_fields=["is_active", "updated_at"])
