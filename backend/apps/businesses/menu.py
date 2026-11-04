"""Service menu management (create, edit, order, delete-or-deactivate)."""

from typing import Any

from django.db import transaction
from django.utils.translation import gettext_lazy as _

from apps.catalog import selectors as catalog
from apps.core.errors import conflict, validation_error

from . import selectors
from .invalidation import touch
from .models import Business, Service

MAX_SERVICES = 200


def _category(category_id: int | None) -> Any:
    if category_id is None:
        return None
    category = catalog.get_active_category(category_id)
    if category is None:
        raise validation_error({"category_id": [str(_("Choose an active category."))]})
    return category


def list_services(owner_id: int, business_ref: str) -> list[Service]:
    business = selectors.owned_business(owner_id, business_ref)
    return list(business.services.select_related("category"))


def create_service(owner_id: int, business_ref: str, data: dict[str, Any]) -> Service:
    category = _category(data.pop("category_id", None))
    with transaction.atomic():
        business = selectors.owned_business(owner_id, business_ref, lock=True)
        if business.services.count() >= MAX_SERVICES:
            raise conflict("too_many_services", _("Too many services."))
        last = (
            business.services.order_by("-sort_order").values_list("sort_order", flat=True).first()
        )
        service = Service.objects.create(
            business=business, category=category, sort_order=(last or 0) + 1, **data
        )
    touch(business.pk)
    return service


def update_service(owner_id: int, service_id: int, changes: dict[str, Any]) -> Service:
    if "category_id" in changes:
        changes["category"] = _category(changes.pop("category_id"))
    with transaction.atomic():
        selectors.owned_service(owner_id, service_id)  # 404 for foreign ids
        service = Service.objects.select_for_update().get(pk=service_id)
        for key, value in changes.items():
            setattr(service, key, value)
        service.save()
    touch(service.business_id)
    return service


def delete_service(owner_id: int, service_id: int) -> bool:
    """Delete a service, or deactivate it when any appointment references it.

    Returns True when it was only deactivated (existing bookings stay valid).
    """
    from apps.bookings import selectors as bookings

    with transaction.atomic():
        selectors.owned_service(owner_id, service_id)
        service = Service.objects.select_for_update().get(pk=service_id)
        business_id = service.business_id
        if bookings.service_has_appointments(service.pk):
            service.is_active = False
            service.save(update_fields=["is_active", "updated_at"])
            deactivated = True
        else:
            service.delete()
            deactivated = False
    touch(business_id)
    return deactivated


def reorder(owner_id: int, business_ref: str, ids: list[int]) -> list[Service]:
    with transaction.atomic():
        business: Business = selectors.owned_business(owner_id, business_ref, lock=True)
        current = set(business.services.values_list("pk", flat=True))
        if len(ids) != len(set(ids)) or set(ids) != current:
            raise validation_error({"service_ids": [str(_("Send every service id exactly once."))]})
        for position, service_id in enumerate(ids):
            Service.objects.filter(pk=service_id).update(sort_order=position)
        services = list(business.services.select_related("category"))
    touch(business.pk)
    return services
