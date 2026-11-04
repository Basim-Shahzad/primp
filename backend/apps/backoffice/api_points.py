"""Admin manual points adjustment endpoints."""

from django.http import HttpRequest
from ninja import Header, Query, Router, Status

from apps.loyalty import adminops as loyalty

from .pagination import check_sort, slice_page
from .permissions import admin_auth, require_permission
from .schemas_points import (
    AdjustIn,
    AdjustmentPage,
    AdjustmentRow,
    AdjustmentsQuery,
    AdjustOut,
    PreviewIn,
    PreviewOut,
)
from .services import points as service

router = Router(tags=["admin"], auth=admin_auth)


@router.post("/points/adjust/preview", response=PreviewOut)
def preview(request: HttpRequest, payload: PreviewIn) -> PreviewOut:
    actor = require_permission("points.adjust", reauth=False)(request)  # reads and previews
    return PreviewOut(
        **service.preview(actor, str(payload.customer_id), payload.amount, payload.category)
    )


@router.post("/points/adjust", response={200: AdjustOut, 201: AdjustOut})
def adjust(
    request: HttpRequest,
    payload: AdjustIn,
    idempotency_key: Header[str | None] = None,
) -> Status[AdjustOut]:
    actor = require_permission("points.adjust")(request)
    entry, created, balance_after = service.adjust(
        actor,
        customer_ref=str(payload.customer_id),
        amount=payload.amount,
        category=payload.category,
        note=payload.note,
        key=(idempotency_key or "").strip(),
    )
    return Status(
        201 if created else 200,
        AdjustOut(
            id=entry.pk,
            customer_id=payload.customer_id,
            delta=entry.delta,
            balance_after=balance_after,
            category=entry.category,
            replayed=not created,
        ),
    )


@router.get("/points/adjustments", response=AdjustmentPage)
def list_adjustments(request: HttpRequest, params: Query[AdjustmentsQuery]) -> AdjustmentPage:
    require_permission("points.adjust", reauth=False)(request)
    sort = check_sort(params.sort, loyalty.ADJUSTMENT_SORTS)
    qs = loyalty.list_adjustments(
        admin_ref=str(params.admin_id) if params.admin_id else None,
        customer_ref=str(params.customer_id) if params.customer_id else None,
        created_from=params.created_from,
        created_to=params.created_to,
        sort=sort,
    )
    rows, total = slice_page(qs, params)
    return AdjustmentPage(
        items=[
            AdjustmentRow(
                id=t.pk,
                customer_id=t.customer.public_id,
                admin_id=t.admin.public_id if t.admin_id else None,
                delta=t.delta,
                category=t.category,
                note=t.note,
                created_at=t.created_at,
            )
            for t in rows
        ],
        total=total,
        limit=params.limit,
        offset=params.offset,
    )
