"""Admin business approval queue endpoints."""

import uuid
from typing import Any

from django.http import HttpRequest
from django.utils import timezone
from ninja import Query, Router, Status

from .models import ReviewQueueItem
from .pagination import check_sort, slice_page
from .permissions import admin_auth, require_permission
from .schemas import ReasonIn
from .schemas_listings import (
    ApproveIn,
    ClaimIn,
    DecisionOut,
    DuplicateOut,
    InfoIn,
    NoteIn,
    NoteOut,
    QueueBusiness,
    QueueDetail,
    QueueItemOut,
    QueuePage,
    QueueQuery,
    RejectIn,
)
from .services import listings as service

router = Router(tags=["admin"], auth=admin_auth)


def item_out(item: ReviewQueueItem) -> QueueItemOut:
    b = item.business
    return QueueItemOut(
        id=item.public_id,
        kind=item.kind,
        status=item.status,
        business=QueueBusiness(
            id=b.public_id,
            name=b.name,
            city=b.city.name,
            category=b.category.name,
            status=b.verification_status,
        ),
        assigned_to=item.assigned_to.public_id if item.assigned_to_id else None,  # type: ignore[union-attr]
        submitted_at=item.submitted_at,
        waiting_seconds=int(
            ((item.closed_at or timezone.now()) - item.submitted_at).total_seconds()
        ),
        version=item.version,
        outcome=item.outcome,
    )


@router.get("/review-queue", response=QueuePage)
def list_queue(request: HttpRequest, params: Query[QueueQuery]) -> QueuePage:
    require_permission("businesses.review")(request)
    sort = check_sort(params.sort, service.SORTS)
    qs = service.queue(
        city_id=params.city_id,
        category_id=params.category_id,
        kind=params.kind,
        status=params.status,
        sort=sort,
    )
    rows, total = slice_page(qs, params)
    return QueuePage(
        items=[item_out(i) for i in rows], total=total, limit=params.limit, offset=params.offset
    )


@router.get("/review-queue/{item_id}", response=QueueDetail)
def get_item(request: HttpRequest, item_id: uuid.UUID) -> QueueDetail:
    actor = require_permission("businesses.review")(request)
    data = service.detail(actor, str(item_id))
    return QueueDetail(
        **item_out(data["item"]).model_dump(),
        preview=_plain(data["preview"]),
        private=_plain(data["private"]),
        documents=data["documents"],
        duplicates=[DuplicateOut(**d) for d in data["duplicates"]],
        decisions=[
            DecisionOut(
                action=d.action,
                admin_id=d.admin.public_id if d.admin_id else None,
                checklist=d.checklist,
                message=d.message,
                created_at=d.created_at,
            )
            for d in data["decisions"]
        ],
        notes=[
            NoteOut(
                id=n.pk,
                author_id=n.author.public_id if n.author_id else None,
                body=n.body,
                created_at=n.created_at,
            )
            for n in data["notes"]
        ],
        previous_items=[item_out(i) for i in data["previous_items"]],
    )


def _plain(value: Any) -> Any:
    """JSON-friendly copy (uuids, datetimes) of the preview payloads."""
    import json

    from django.core.serializers.json import DjangoJSONEncoder

    return json.loads(json.dumps(value, cls=DjangoJSONEncoder))


@router.post("/review-queue/{item_id}/claim", response=QueueItemOut)
def claim(request: HttpRequest, item_id: uuid.UUID, payload: ClaimIn) -> QueueItemOut:
    actor = require_permission("businesses.review")(request)
    return item_out(service.claim(actor, str(item_id), payload.version))


@router.post("/review-queue/{item_id}/approve", response=QueueItemOut)
def approve(request: HttpRequest, item_id: uuid.UUID, payload: ApproveIn) -> QueueItemOut:
    actor = require_permission("businesses.review")(request)
    return item_out(
        service.approve(actor, str(item_id), payload.version, payload.checklist.model_dump())
    )


@router.post("/review-queue/{item_id}/reject", response=QueueItemOut)
def reject(request: HttpRequest, item_id: uuid.UUID, payload: RejectIn) -> QueueItemOut:
    actor = require_permission("businesses.review")(request)
    checklist = payload.checklist.model_dump() if payload.checklist else None
    return item_out(service.reject(actor, str(item_id), payload.version, payload.reason, checklist))


@router.post("/review-queue/{item_id}/request-info", response=QueueItemOut)
def request_info(request: HttpRequest, item_id: uuid.UUID, payload: InfoIn) -> QueueItemOut:
    actor = require_permission("businesses.review")(request)
    checklist = payload.checklist.model_dump() if payload.checklist else None
    return item_out(
        service.request_info(actor, str(item_id), payload.version, payload.message, checklist)
    )


@router.post("/review-queue/{item_id}/approve-change", response=QueueItemOut)
def approve_change(request: HttpRequest, item_id: uuid.UUID, payload: ClaimIn) -> QueueItemOut:
    actor = require_permission("businesses.review")(request)
    return item_out(service.approve_change(actor, str(item_id), payload.version))


@router.post("/review-queue/{item_id}/reject-change", response=QueueItemOut)
def reject_change(request: HttpRequest, item_id: uuid.UUID, payload: RejectIn) -> QueueItemOut:
    actor = require_permission("businesses.review")(request)
    return item_out(service.reject_change(actor, str(item_id), payload.version, payload.reason))


@router.post("/review-queue/{item_id}/notes", response={201: NoteOut})
def add_note(request: HttpRequest, item_id: uuid.UUID, payload: NoteIn) -> Status[NoteOut]:
    actor = require_permission("businesses.review")(request)
    note = service.add_note(actor, str(item_id), payload.body)
    return Status(
        201,
        NoteOut(
            id=note.pk, author_id=actor.user.public_id, body=note.body, created_at=note.created_at
        ),
    )


@router.post("/businesses/{business_id}/revoke-verification", response={200: dict[str, str]})
def revoke_verification(
    request: HttpRequest, business_id: uuid.UUID, payload: ReasonIn
) -> dict[str, str]:
    actor = require_permission("businesses.revoke")(request)
    business = service.revoke(actor, str(business_id), payload.reason)
    return {"status": business.verification_status}
