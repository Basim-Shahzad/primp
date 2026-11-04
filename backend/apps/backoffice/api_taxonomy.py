"""Admin category taxonomy endpoints."""

from typing import Any

from django.http import HttpRequest
from ninja import Router, Status

from apps.businesses import adminops as businesses
from apps.catalog import adminops as catalog

from .permissions import admin_auth, require_permission
from .schemas_taxonomy import (
    CategoryIn,
    CategoryOut,
    CategoryPatchIn,
    ReorderIn,
    VersionIn,
)
from .services import taxonomy as service

router = Router(tags=["admin"], auth=admin_auth)


def out(
    c: Any, usage: dict[int, tuple[int, int]], children: list[Any] | None = None
) -> CategoryOut:
    services, bizs = usage.get(c.pk, (0, 0))
    return CategoryOut(
        id=c.pk,
        name=c.name,
        slug=c.slug,
        icon=c.icon,
        description=c.description,
        parent_id=c.parent_id,
        sort_order=c.sort_order,
        is_active=c.is_active,
        version=c.version,
        services_count=services,
        businesses_count=bizs,
        children=[out(k, usage) for k in (children or [])],
    )


@router.get("/categories", response=list[CategoryOut])
def list_categories(request: HttpRequest) -> list[CategoryOut]:
    require_permission("taxonomy.manage")(request)
    usage = businesses.category_usage()
    return [
        out(c, usage, sorted(c.children.all(), key=lambda k: (k.sort_order, k.name, k.pk)))
        for c in catalog.tree()
    ]


@router.post("/categories", response={201: CategoryOut})
def create_category(request: HttpRequest, payload: CategoryIn) -> Status[CategoryOut]:
    actor = require_permission("taxonomy.manage")(request)
    category = service.create(actor, **payload.model_dump())
    return Status(201, out(category, {}))


@router.post("/categories/reorder", response=list[CategoryOut])
def reorder_categories(request: HttpRequest, payload: ReorderIn) -> list[CategoryOut]:
    actor = require_permission("taxonomy.manage")(request)
    ordered = service.reorder(actor, payload.parent_id, payload.ids)
    usage = businesses.category_usage()
    return [out(c, usage) for c in ordered]


@router.patch("/categories/{category_id}", response=CategoryOut)
def patch_category(request: HttpRequest, category_id: int, payload: CategoryPatchIn) -> CategoryOut:
    actor = require_permission("taxonomy.manage")(request)
    changes = payload.model_dump(exclude_unset=True)
    version = changes.pop("version")
    null_fields = [
        k for k in ("name", "slug", "icon", "description") if k in changes and changes[k] is None
    ]
    if null_fields:
        from apps.core.errors import validation_error

        raise validation_error({k: ["This field may not be null."] for k in null_fields})
    category = service.update(actor, category_id, version, changes)
    return out(category, businesses.category_usage())


@router.post("/categories/{category_id}/deactivate", response=CategoryOut)
def deactivate_category(request: HttpRequest, category_id: int, payload: VersionIn) -> CategoryOut:
    actor = require_permission("taxonomy.manage")(request)
    category = service.set_active(actor, category_id, payload.version, False)
    return out(category, businesses.category_usage())


@router.post("/categories/{category_id}/activate", response=CategoryOut)
def activate_category(request: HttpRequest, category_id: int, payload: VersionIn) -> CategoryOut:
    actor = require_permission("taxonomy.manage")(request)
    category = service.set_active(actor, category_id, payload.version, True)
    return out(category, businesses.category_usage())
