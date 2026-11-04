from typing import Annotated

from ninja import Field, Schema
from pydantic import StringConstraints

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
Slug = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=50)]


class CategoryIn(Schema):
    name: Name
    slug: Slug | None = None
    icon: Annotated[str, StringConstraints(strip_whitespace=True, max_length=100)] = ""
    description: Annotated[str, StringConstraints(strip_whitespace=True, max_length=300)] = ""
    parent_id: int | None = None


class CategoryPatchIn(Schema):
    version: int = Field(ge=1)
    name: Name | None = None
    slug: Slug | None = None
    icon: Annotated[str, StringConstraints(strip_whitespace=True, max_length=100)] | None = None
    description: Annotated[str, StringConstraints(strip_whitespace=True, max_length=300)] | None = (
        None
    )
    parent_id: int | None = None


class VersionIn(Schema):
    version: int = Field(ge=1)


class ReorderIn(Schema):
    parent_id: int | None = None
    ids: list[int] = Field(max_length=500)


class CategoryOut(Schema):
    id: int
    name: str
    slug: str
    icon: str
    description: str
    parent_id: int | None
    sort_order: int
    is_active: bool
    version: int
    services_count: int
    businesses_count: int
    children: list["CategoryOut"] = []
