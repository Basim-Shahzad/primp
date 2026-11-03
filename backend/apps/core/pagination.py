"""Opaque, signed keyset (cursor) pagination plus simple offset pagination."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

from django.core import signing
from django.db import models
from django.db.models import Q
from django.utils.translation import gettext_lazy as _
from ninja import Field, Schema

from .errors import AppError

T = TypeVar("T")
DEFAULT_LIMIT = 20
MAX_LIMIT = 100
_SALT = "primp.cursor"


class CursorParams(Schema):
    cursor: str | None = None
    limit: int = Field(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT)


class OffsetParams(Schema):
    limit: int = Field(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT)
    offset: int = Field(0, ge=0, le=100_000)


class CursorPageOut(Schema, Generic[T]):
    items: list[T]
    next_cursor: str | None = None


class OffsetPageOut(Schema, Generic[T]):
    items: list[T]
    total: int
    limit: int
    offset: int


@dataclass
class CursorPage(Generic[T]):
    items: list[T]
    next_cursor: str | None


def _invalid_cursor() -> AppError:
    return AppError("invalid_cursor", _("The pagination cursor is invalid."), 400)


def _direction(ordering: Sequence[str]) -> bool:
    descending = {o.startswith("-") for o in ordering}
    if len(descending) != 1:
        raise ValueError("All ordering fields must share one direction")
    return descending.pop()


def _decoder(qs: models.QuerySet[Any], name: str) -> Any:
    annotation = qs.query.annotations.get(name)
    field = annotation.output_field if annotation is not None else qs.model._meta.get_field(name)
    return field.to_python


def _encode(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _keyset_filter(names: list[str], values: list[Any], descending: bool) -> Q:
    op = "lt" if descending else "gt"
    result = Q()
    for i in range(len(names)):
        clause = Q(**{f"{names[i]}__{op}": values[i]})
        for j in range(i):
            clause &= Q(**{names[j]: values[j]})
        result |= clause
    return result


def encode_cursor(ordering: Sequence[str], obj: Any) -> str:
    """Cursor pointing just after `obj` for the given ordering."""
    names = [o.lstrip("-") for o in ordering]
    payload = {"o": ",".join(ordering), "v": [_encode(getattr(obj, n)) for n in names]}
    return str(signing.dumps(payload, salt=_SALT, compress=True))


def paginate_cursor(
    qs: models.QuerySet[Any],
    *,
    ordering: Sequence[str],
    cursor: str | None,
    limit: int = DEFAULT_LIMIT,
    max_limit: int = MAX_LIMIT,
) -> CursorPage[Any]:
    """Order must end with a unique field (e.g. id) so ordering is stable.

    Fields may be annotations. All fields share one direction.
    """
    limit = max(1, min(limit, max_limit))
    descending = _direction(ordering)
    names = [o.lstrip("-") for o in ordering]
    fingerprint = ",".join(ordering)
    qs = qs.order_by(*ordering)
    if cursor:
        try:
            data = signing.loads(cursor, salt=_SALT)
            if data["o"] != fingerprint or len(data["v"]) != len(names):
                raise ValueError
            values = [_decoder(qs, n)(v) for n, v in zip(names, data["v"], strict=True)]
        except (signing.BadSignature, KeyError, TypeError, ValueError, Exception) as exc:
            raise _invalid_cursor() from exc
        qs = qs.filter(_keyset_filter(names, values, descending))
    rows = list(qs[: limit + 1])
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = None
    if has_more and rows:
        next_cursor = encode_cursor(ordering, rows[-1])
    return CursorPage(rows, next_cursor)


def paginate_offset(qs: models.QuerySet[Any], *, limit: int, offset: int) -> dict[str, Any]:
    return {
        "items": list(qs[offset : offset + limit]),
        "total": qs.count(),
        "limit": limit,
        "offset": offset,
    }
