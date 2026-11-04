"""Public taxonomy."""

from typing import Any

from django.http import HttpRequest
from ninja import Router, Schema

from . import cache
from .selectors import category_tree

router = Router(tags=["catalog"])


class CategoryOut(Schema):
    id: int
    name: str
    slug: str
    icon: str
    description: str
    children: list["CategoryOut"] = []


def build_tree() -> list[dict[str, Any]]:
    return [
        {
            "id": c.pk,
            "name": c.name,
            "slug": c.slug,
            "icon": c.icon,
            "description": c.description,
            "children": [
                {
                    "id": k.pk,
                    "name": k.name,
                    "slug": k.slug,
                    "icon": k.icon,
                    "description": k.description,
                    "children": [],
                }
                for k in sorted(
                    (k for k in c.children.all() if k.is_active),
                    key=lambda k: (k.sort_order, k.name),
                )
            ],
        }
        for c in category_tree()
    ]


@router.get("", auth=None, response=list[CategoryOut])
def list_categories(request: HttpRequest) -> list[dict[str, Any]]:
    tree = cache.get()
    if tree is None:
        tree = build_tree()
        cache.put(tree)
    return tree
