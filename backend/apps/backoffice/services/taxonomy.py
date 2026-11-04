"""Category taxonomy management (Plan 6.3, Step 4)."""

from typing import Any

from django.db import transaction

from apps.catalog import adminops as catalog
from apps.core import audit

from ..permissions import AdminActor

PERMISSION = "taxonomy.manage"


def _audit(actor: AdminActor, action: str, category: Any, before: Any, after: Any) -> None:
    audit.record(
        action,
        actor_id=actor.id,
        target_type="category",
        target_id=category.pk,
        before=before,
        after=after,
        ip=actor.ip,
    )


def create(actor: AdminActor, **data: Any) -> Any:
    actor.require(PERMISSION)
    with transaction.atomic():
        category = catalog.create(**data)
        _audit(actor, "admin.category_created", category, None, catalog.snapshot(category))
    return category


def update(actor: AdminActor, category_id: int, version: int, changes: dict[str, Any]) -> Any:
    actor.require(PERMISSION)
    with transaction.atomic():
        category = catalog.get(category_id, lock=True)
        before = catalog.snapshot(category)
        category = catalog.update(category, version, changes)
        _audit(actor, "admin.category_updated", category, before, catalog.snapshot(category))
    return category


def reorder(actor: AdminActor, parent_id: int | None, ids: list[int]) -> list[Any]:
    actor.require(PERMISSION)
    with transaction.atomic():
        before = [c.pk for c in catalog.siblings(parent_id)]
        ordered = catalog.reorder(parent_id, ids)
        audit.record(
            "admin.categories_reordered",
            actor_id=actor.id,
            target_type="category",
            target_id=parent_id if parent_id is not None else "root",
            before={"ids": before},
            after={"ids": [c.pk for c in ordered]},
            ip=actor.ip,
        )
    return ordered


def set_active(actor: AdminActor, category_id: int, version: int, active: bool) -> Any:
    actor.require(PERMISSION)
    with transaction.atomic():
        category = catalog.get(category_id, lock=True)
        before = catalog.snapshot(category)
        category = catalog.set_active(category, version, active)
        _audit(
            actor,
            "admin.category_activated" if active else "admin.category_deactivated",
            category,
            before,
            catalog.snapshot(category),
        )
    return category
