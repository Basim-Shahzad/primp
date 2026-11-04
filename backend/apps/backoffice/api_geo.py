"""Admin city and area endpoints."""

import json
from typing import Any

from django.http import HttpRequest
from ninja import File, Router, Status, UploadedFile

from apps.businesses import adminops as businesses
from apps.core import ratelimit
from apps.core.errors import validation_error
from apps.geo import adminops as geo

from .permissions import admin_auth, require_permission
from .schemas_geo import (
    AreaIn,
    AreaOut,
    AreaPatchIn,
    CityIn,
    CityOut,
    CityPatchIn,
    ImportOut,
)
from .services import geo as service

router = Router(tags=["admin"], auth=admin_auth)
IMPORT_LIMIT = (10, 3600)
NULLABLE = {"polygon"}


def city_out(c: Any) -> CityOut:
    return CityOut(
        id=c.pk,
        name=c.name,
        slug=c.slug,
        lat=c.centre.y,
        lng=c.centre.x,
        visibility=c.visibility,
        is_active=c.is_active,
        version=c.version,
        area_count=int(c.area_count) if hasattr(c, "area_count") else len(geo.areas_of(c.pk)),
        listed_businesses=businesses.count_listed(city_id=c.pk),
    )


def area_out(a: Any) -> AreaOut:
    return AreaOut(
        id=a.pk,
        city_id=a.city_id,
        name=a.name,
        slug=a.slug,
        lat=a.centre.y,
        lng=a.centre.x,
        polygon=json.loads(a.polygon.geojson) if a.polygon else None,
        is_pilot=a.is_pilot,
        is_active=a.is_active,
        version=a.version,
        listed_businesses=businesses.count_listed(area_id=a.pk),
    )


def _changes(payload: Any) -> tuple[int, bool, dict[str, Any]]:
    changes = payload.model_dump(exclude_unset=True)
    version = changes.pop("version")
    force = bool(changes.pop("force", False))
    nulls = [k for k, v in changes.items() if v is None and k not in NULLABLE]
    if nulls:
        raise validation_error({k: ["This field may not be null."] for k in nulls})
    return version, force, changes


@router.get("/cities", response=list[CityOut])
def list_cities(request: HttpRequest) -> list[CityOut]:
    require_permission("geo.manage")(request)
    return [city_out(c) for c in geo.list_cities()]


@router.post("/cities", response={201: CityOut})
def create_city(request: HttpRequest, payload: CityIn) -> Status[CityOut]:
    actor = require_permission("geo.manage")(request)
    return Status(201, city_out(service.create_city(actor, **payload.model_dump())))


@router.patch("/cities/{city_id}", response=CityOut)
def patch_city(request: HttpRequest, city_id: int, payload: CityPatchIn) -> CityOut:
    actor = require_permission("geo.manage")(request)
    version, force, changes = _changes(payload)
    return city_out(service.update_city(actor, city_id, version, changes, force))


@router.get("/cities/{city_id}/areas", response=list[AreaOut])
def list_areas(request: HttpRequest, city_id: int) -> list[AreaOut]:
    require_permission("geo.manage")(request)
    geo.get_city(city_id)
    return [area_out(a) for a in geo.areas_of(city_id)]


@router.post("/cities/{city_id}/areas", response={201: AreaOut})
def create_area(request: HttpRequest, city_id: int, payload: AreaIn) -> Status[AreaOut]:
    actor = require_permission("geo.manage")(request)
    return Status(201, area_out(service.create_area(actor, city_id, **payload.model_dump())))


@router.post("/cities/{city_id}/areas/import", response=ImportOut)
def import_areas(request: HttpRequest, city_id: int, file: File[UploadedFile]) -> ImportOut:
    actor = require_permission("geo.manage")(request)
    ratelimit.enforce(
        f"area-import:{actor.id}", limit=IMPORT_LIMIT[0], window_seconds=IMPORT_LIMIT[1]
    )
    return ImportOut(imported=service.import_areas(actor, city_id, file.read()))


@router.patch("/cities/{city_id}/areas/{area_id}", response=AreaOut)
def patch_area(request: HttpRequest, city_id: int, area_id: int, payload: AreaPatchIn) -> AreaOut:
    actor = require_permission("geo.manage")(request)
    version, force, changes = _changes(payload)
    return area_out(service.update_area(actor, city_id, area_id, version, changes, force))
