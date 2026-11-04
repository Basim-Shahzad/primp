"""Read-only audit log viewer and CSV export (Plan 6.3, Step 10)."""

import csv
import uuid
from collections.abc import Iterator
from datetime import datetime
from typing import Any

from django.db.models import QuerySet

from apps.accounts import adminops as accounts
from apps.core.models import AuditLog

from ..pagination import check_sort

SORTS = {
    "-created_at": ("-created_at", "-id"),
    "created_at": ("created_at", "id"),
}
DANGEROUS = ("=", "+", "-", "@", "\t", "\r")
CSV_COLUMNS = [
    "id",
    "created_at",
    "actor",
    "action",
    "target_type",
    "target_id",
    "ip_address",
    "request_id",
    "before",
    "after",
]
CHUNK = 500


def safe_cell(value: Any) -> Any:
    """Neutralise spreadsheet formulas: text starting with = + - @ gets a leading quote."""
    if isinstance(value, str) and value.startswith(DANGEROUS):
        return "'" + value
    return value


def search(
    *,
    actor: uuid.UUID | None = None,
    action: str | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    sort: str = "-created_at",
) -> QuerySet[AuditLog]:
    qs = AuditLog.objects.all()
    if actor is not None:
        actor_pk = accounts.internal_id(actor)
        qs = qs.filter(actor_id=actor_pk) if actor_pk is not None else qs.none()
    if action:
        qs = (
            qs.filter(action__startswith=action)
            if action.endswith(".")
            else qs.filter(action=action)
        )
    if target_type:
        qs = qs.filter(target_type=target_type)
    if target_id:
        qs = qs.filter(target_id=target_id)
    if created_from:
        qs = qs.filter(created_at__gte=created_from)
    if created_to:
        qs = qs.filter(created_at__lt=created_to)
    return qs.order_by(*SORTS[check_sort(sort, SORTS)])


def actors_of(rows: list[AuditLog]) -> dict[int, uuid.UUID]:
    return accounts.public_ids({r.actor_id for r in rows if r.actor_id is not None})


def render(row: AuditLog, actors: dict[int, uuid.UUID]) -> dict[str, Any]:
    return {
        "id": row.pk,
        "created_at": row.created_at,
        "actor": actors.get(row.actor_id) if row.actor_id is not None else None,
        "action": row.action,
        "target_type": row.target_type,
        "target_id": row.target_id,
        "ip_address": row.ip_address,
        "request_id": row.request_id,
        "before": row.before,
        "after": row.after,
    }


class _Echo:
    def write(self, value: str) -> str:
        return value


def export_lines(qs: QuerySet[AuditLog]) -> Iterator[str]:
    writer = csv.writer(_Echo())
    yield writer.writerow(CSV_COLUMNS)
    chunk: list[AuditLog] = []

    def flush() -> Iterator[str]:
        actors = actors_of(chunk)
        for row in chunk:
            r = render(row, actors)
            yield writer.writerow(
                [
                    safe_cell(
                        ""
                        if r[c] is None
                        else (r[c].isoformat() if c == "created_at" else str(r[c]))
                    )
                    for c in CSV_COLUMNS
                ]
            )

    for row in qs.iterator(chunk_size=CHUNK):
        chunk.append(row)
        if len(chunk) >= CHUNK:
            yield from flush()
            chunk = []
    yield from flush()
