"""City and area management (Plan 6.3, Step 5)."""

from typing import Any

from django.db import transaction
from django.utils.translation import gettext_lazy as _

from apps.businesses import adminops as businesses
from apps.core import audit
from apps.core.errors import AppError
from apps.geo import adminops as geo

from ..permissions import AdminActor

PERMISSION = "geo.manage"


def _guard_deactivation(
    actor: AdminActor, changes: dict[str, Any], was_active: bool, listed: int, force: bool
) -> bool:
    """True when this update switches the place off. Listings need `force` and re-authentication."""
    switching_off = changes.get("is_active") is False and was_active
    if not switching_off or listed == 0:
        return switching_off and listed > 0
    if not force:
        raise AppError(
            "has_active_listings",
            _("%(n)d listed businesses would disappear; pass force=true to confirm.")
            % {"n": listed},
            409,
            {"listed_businesses": [str(listed)]},
        )
    actor.require(PERMISSION, reauth=True)
    return True


def create_city(actor: AdminActor, **data: Any) -> Any:
    actor.require(PERMISSION)
    with transaction.atomic():
        city = geo.create_city(**data)
        audit.record(
            "admin.city_created",
            actor_id=actor.id,
            target_type="city",
            target_id=city.pk,
            after=geo.city_snapshot(city),
            ip=actor.ip,
        )
    return city


def update_city(
    actor: AdminActor, city_id: int, version: int, changes: dict[str, Any], force: bool
) -> Any:
    actor.require(PERMISSION)
    with transaction.atomic():
        city = geo.get_city(city_id, lock=True)
        before = geo.city_snapshot(city)
        listed = businesses.count_listed(city_id=city.pk)
        hides = _guard_deactivation(actor, changes, city.is_active, listed, force)
        city = geo.update_city(city, version, changes)
        after = geo.city_snapshot(city)
        if hides:
            notified = businesses.notify_owners_unavailable(city_id=city.pk, place=city.name)
            after = {**after, "hidden_listings": notified, "forced": True}
        audit.record(
            "admin.city_updated",
            actor_id=actor.id,
            target_type="city",
            target_id=city.pk,
            before=before,
            after=after,
            ip=actor.ip,
        )
    return city


def create_area(actor: AdminActor, city_id: int, **data: Any) -> Any:
    actor.require(PERMISSION)
    with transaction.atomic():
        city = geo.get_city(city_id, lock=True)
        area = geo.create_area(city, **data)
        audit.record(
            "admin.area_created",
            actor_id=actor.id,
            target_type="area",
            target_id=area.pk,
            after=geo.area_snapshot(area),
            ip=actor.ip,
        )
    return area


def update_area(
    actor: AdminActor,
    city_id: int,
    area_id: int,
    version: int,
    changes: dict[str, Any],
    force: bool,
) -> Any:
    actor.require(PERMISSION)
    with transaction.atomic():
        area = geo.get_area(city_id, area_id, lock=True)
        before = geo.area_snapshot(area)
        listed = businesses.count_listed(area_id=area.pk)
        hides = _guard_deactivation(actor, changes, area.is_active, listed, force)
        area = geo.update_area(area, version, changes)
        after = geo.area_snapshot(area)
        if hides:
            notified = businesses.notify_owners_unavailable(area_id=area.pk, place=area.name)
            after = {**after, "hidden_listings": notified, "forced": True}
        audit.record(
            "admin.area_updated",
            actor_id=actor.id,
            target_type="area",
            target_id=area.pk,
            before=before,
            after=after,
            ip=actor.ip,
        )
    return area


def import_areas(actor: AdminActor, city_id: int, data: bytes) -> int:
    """All-or-nothing CSV import: any bad row means nothing is created."""
    actor.require(PERMISSION)
    with transaction.atomic():
        city = geo.get_city(city_id, lock=True)
        rows, errors = geo.validate_csv(city, data)
        if errors:
            fields: dict[str, list[str]] = {}
            for e in errors:
                fields.setdefault(f"row {e.row}", []).append(e.message)
            raise AppError(
                "csv_invalid",
                _("The file has %(n)d invalid rows; nothing was imported.") % {"n": len(fields)},
                422,
                fields,
            )
        created = geo.import_areas(city, rows)
        audit.record(
            "admin.areas_imported",
            actor_id=actor.id,
            target_type="city",
            target_id=city.pk,
            before={"areas": len(geo.areas_of(city.pk)) - len(created)},
            after={"areas": len(geo.areas_of(city.pk)), "imported": len(created)},
            ip=actor.ip,
        )
    return len(created)
