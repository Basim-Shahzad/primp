"""Taxonomy changes for the backoffice. Keeps the two-level tree and slugs consistent."""

import re
from typing import Any

from django.db import transaction
from django.db.models import Max
from django.utils.text import slugify
from django.utils.translation import gettext_lazy as _

from apps.core.errors import AppError, conflict, not_found, validation_error

from . import cache
from .models import Category

SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def snapshot(c: Category) -> dict[str, Any]:
    return {
        "name": c.name,
        "slug": c.slug,
        "icon": c.icon,
        "description": c.description,
        "parent_id": c.parent_id,
        "sort_order": c.sort_order,
        "is_active": c.is_active,
        "version": c.version,
    }


def tree() -> list[Category]:
    """Every top-level category (active or not) with its children prefetched, in display order."""
    return list(
        Category.objects.filter(parent__isnull=True)
        .prefetch_related("children")
        .order_by("sort_order", "name", "id")
    )


def get(category_id: int, *, lock: bool = False) -> Category:
    qs = Category.objects.select_for_update(of=("self",)) if lock else Category.objects
    category = qs.filter(pk=category_id).first()
    if category is None:
        raise not_found()
    return category


def _unique_slug(base: str, exclude_pk: int | None = None) -> str:
    base = (slugify(base) or "category")[:40]
    slug, n = base, 1
    others = Category.objects.exclude(pk=exclude_pk) if exclude_pk else Category.objects.all()
    while others.filter(slug=slug).exists():
        n += 1
        slug = f"{base}-{n}"
    return slug


def _check_slug(slug: str, exclude_pk: int | None) -> str:
    if not SLUG_RE.match(slug) or len(slug) > 50:
        raise validation_error({"slug": [str(_("Use lowercase letters, digits and hyphens."))]})
    clash = Category.objects.filter(slug=slug)
    if exclude_pk:
        clash = clash.exclude(pk=exclude_pk)
    if clash.exists():
        raise AppError(
            "slug_taken",
            _("This slug is already used."),
            409,
            {"slug": [str(_("Already in use."))]},
        )
    return slug


def _parent(parent_id: int | None, category: Category | None) -> Category | None:
    if parent_id is None:
        return None
    parent = Category.objects.filter(pk=parent_id).first()
    bad = validation_error  # shorthand
    if parent is None:
        raise bad({"parent_id": [str(_("Unknown parent category."))]})
    if category is not None:
        if parent.pk == category.pk:
            raise bad({"parent_id": [str(_("A category cannot be its own parent."))]})
        if parent.parent_id == category.pk:
            raise bad({"parent_id": [str(_("A category cannot move under its own child."))]})
        if category.children.exists():
            raise bad({"parent_id": [str(_("A category with children cannot be nested."))]})
    if parent.parent_id is not None:
        raise bad({"parent_id": [str(_("Categories can be nested two levels deep."))]})
    return parent


def create(
    *, name: str, slug: str | None, icon: str, description: str, parent_id: int | None
) -> Category:
    parent = _parent(parent_id, None)
    final_slug = _check_slug(slug, None) if slug else _unique_slug(name)
    last = Category.objects.filter(parent=parent).aggregate(m=Max("sort_order"))["m"]
    category = Category.objects.create(
        name=name,
        slug=final_slug,
        icon=icon,
        description=description,
        parent=parent,
        sort_order=(last if last is not None else -1) + 1,
    )
    cache.invalidate()
    return category


def _stale(category: Category, version: int) -> None:
    if category.version != version:
        raise conflict("stale_version", _("The category changed; reload it and try again."))


def update(category: Category, version: int, changes: dict[str, Any]) -> Category:
    """`category` must be the row locked by the caller."""
    _stale(category, version)
    if "slug" in changes:
        category.slug = _check_slug(changes["slug"], category.pk)
    for field in ("name", "icon", "description"):
        if field in changes:
            setattr(category, field, changes[field])
    if "parent_id" in changes:
        category.parent = _parent(changes["parent_id"], category)
        last = (
            Category.objects.filter(parent=category.parent)
            .exclude(pk=category.pk)
            .aggregate(m=Max("sort_order"))["m"]
        )
        category.sort_order = (last if last is not None else -1) + 1
    category.version += 1
    category.save()  # Category.clean() re-validates depth and cycles
    cache.invalidate()
    return category


def reorder(parent_id: int | None, ids: list[int]) -> list[Category]:
    if parent_id is not None:
        get(parent_id)
    with transaction.atomic():
        siblings = list(
            Category.objects.select_for_update(of=("self",)).filter(parent_id=parent_id)
        )
        if len(ids) != len(set(ids)) or set(ids) != {c.pk for c in siblings}:
            raise validation_error(
                {"ids": [str(_("Send every sibling category id exactly once."))]}
            )
        by_id = {c.pk: c for c in siblings}
        for position, category_id in enumerate(ids):
            c = by_id[category_id]
            if c.sort_order != position:
                c.sort_order = position
                c.version += 1
                c.save(update_fields=["sort_order", "version", "updated_at"])
    cache.invalidate()
    return [by_id[i] for i in ids]


def set_active(category: Category, version: int, active: bool) -> Category:
    _stale(category, version)
    if category.is_active == active:
        raise conflict(
            "invalid_state",
            _("The category is already active.")
            if active
            else _("The category is already inactive."),
        )
    if active and category.parent_id and not category.parent.is_active:  # type: ignore[union-attr]
        raise conflict("parent_inactive", _("Activate the parent category first."))
    category.is_active = active
    category.version += 1
    category.save(update_fields=["is_active", "version", "updated_at"])
    cache.invalidate()
    return category


def siblings(parent_id: int | None) -> list[Category]:
    return list(Category.objects.filter(parent_id=parent_id).order_by("sort_order", "name", "id"))
