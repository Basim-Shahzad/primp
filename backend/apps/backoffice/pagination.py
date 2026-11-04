"""Limit/offset pagination with a total count and allow-listed sorting for admin lists."""

from typing import Any, Generic, TypeVar

from django.db.models import QuerySet
from ninja import Field, Schema

from apps.core.errors import validation_error

T = TypeVar("T")
MAX_LIMIT = 100


class PageParams(Schema):
    limit: int = Field(20, ge=1, le=MAX_LIMIT)
    offset: int = Field(0, ge=0, le=100_000)


class Page(Schema, Generic[T]):
    items: list[T]
    total: int
    limit: int
    offset: int


def check_sort(sort: str, allowed: dict[str, Any] | set[str]) -> str:
    """Never hand raw input to order_by: only keys of the allow-list pass."""
    if sort not in allowed:
        raise validation_error({"sort": [f"Use one of: {', '.join(sorted(allowed))}."]})
    return sort


def slice_page(qs: QuerySet[Any], params: PageParams) -> tuple[list[Any], int]:
    return list(qs[params.offset : params.offset + params.limit]), qs.count()
