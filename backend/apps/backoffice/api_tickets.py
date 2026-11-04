"""Support tickets: requester endpoints (`/tickets`) and admin endpoints (`/admin/tickets`)."""

import uuid
from typing import Any

from django.http import HttpRequest
from ninja import Query, Router, Status

from apps.accounts.auth import jwt_auth
from apps.accounts.roles import CUSTOMER_CAPABLE
from apps.core.permissions import require_role
from apps.reviews import adminops as reviews

from .pagination import check_sort, slice_page
from .permissions import admin_auth, require_permission
from .schemas_tickets import (
    AdminMessageIn,
    AdminMessageOut,
    AdminTicketDetail,
    AdminTicketItem,
    AdminTicketPage,
    AdminTicketQuery,
    AssignIn,
    MessageIn,
    ModerateIn,
    OwnMessageOut,
    OwnQuery,
    OwnTicketDetail,
    OwnTicketOut,
    OwnTicketPage,
    PersonRef,
    StatsOut,
    StatsQuery,
    StatusIn,
    TicketCreateIn,
    TicketEventOut,
)
from .services import moderation
from .services import tickets as service

user_router = Router(tags=["support"], auth=jwt_auth)
router = Router(tags=["admin"], auth=admin_auth)
requester_only = require_role(*CUSTOMER_CAPABLE)


def _own(t: Any) -> OwnTicketOut:
    return OwnTicketOut(
        id=t.public_id,
        type=t.type,
        status=t.status,
        priority=t.priority,
        subject=t.subject,
        created_at=t.created_at,
        updated_at=t.updated_at,
    )


def _own_detail(t: Any) -> OwnTicketDetail:
    return OwnTicketDetail(
        **_own(t).model_dump(),
        messages=[
            OwnMessageOut(
                id=m.pk,
                from_support=m.author_id is not None and m.author_id != t.requester_id,
                body=m.body,
                created_at=m.created_at,
            )
            for m in t.messages.filter(is_internal=False)
        ],
    )


@user_router.post("", response={201: OwnTicketDetail})
def create_ticket(request: HttpRequest, payload: TicketCreateIn) -> Status[OwnTicketDetail]:
    user = requester_only(request)
    ticket = service.create_ticket(
        user,
        type=payload.type,
        subject=payload.subject,
        body=payload.body,
        appointment_id=str(payload.appointment_id) if payload.appointment_id else None,
        business_id=str(payload.business_id) if payload.business_id else None,
        review_id=str(payload.review_id) if payload.review_id else None,
    )
    return Status(201, _own_detail(ticket))


@user_router.get("", response=OwnTicketPage)
def list_own(request: HttpRequest, params: Query[OwnQuery]) -> OwnTicketPage:
    user = requester_only(request)
    rows, total = slice_page(service.own_tickets(user.pk, params.status), params)
    return OwnTicketPage(
        items=[_own(t) for t in rows], total=total, limit=params.limit, offset=params.offset
    )


@user_router.get("/{ticket_id}", response=OwnTicketDetail)
def get_own(request: HttpRequest, ticket_id: uuid.UUID) -> OwnTicketDetail:
    user = requester_only(request)
    return _own_detail(service.get_own(user.pk, str(ticket_id)))


@user_router.post("/{ticket_id}/messages", response={201: OwnMessageOut})
def add_own_message(
    request: HttpRequest, ticket_id: uuid.UUID, payload: MessageIn
) -> Status[OwnMessageOut]:
    user = requester_only(request)
    m = service.user_message(user, str(ticket_id), payload.body)
    return Status(
        201, OwnMessageOut(id=m.pk, from_support=False, body=m.body, created_at=m.created_at)
    )


# ---------------------------------------------------------------- admin


def _person(user: Any) -> PersonRef | None:
    if user is None:
        return None
    try:
        name = str(user.profile.name) or str(user.email or "")
    except Exception:
        name = str(user.email or "")
    return PersonRef(id=user.public_id, name=name)


def _item(t: Any) -> AdminTicketItem:
    return AdminTicketItem(
        **_own(t).model_dump(),
        requester=_person(t.requester),
        assigned_to=_person(t.assigned_to),
        version=t.version,
        first_response_at=t.first_response_at,
        resolved_at=t.resolved_at,
    )


def _detail(t: Any) -> AdminTicketDetail:
    messages = [
        AdminMessageOut(
            id=m.pk,
            author=_person(m.author),
            is_internal=m.is_internal,
            body=m.body,
            attachments=[{"name": str(a.get("name", ""))} for a in m.attachments],  # never paths
            created_at=m.created_at,
        )
        for m in t.messages.select_related("author", "author__profile")
    ]
    history = [
        TicketEventOut(
            kind=e.kind,
            actor_id=e.actor.public_id if e.actor_id else None,
            from_value=e.from_value,
            to_value=e.to_value,
            created_at=e.created_at,
        )
        for e in t.events.select_related("actor")
    ]
    return AdminTicketDetail(
        **_item(t).model_dump(),
        linked=service.linked_summaries(t),
        messages=messages,
        history=history,
    )


@router.get("/tickets", response=AdminTicketPage)
def admin_list(request: HttpRequest, params: Query[AdminTicketQuery]) -> AdminTicketPage:
    actor = require_permission("tickets.view")(request)
    sort = check_sort(params.sort, service.SORTS)
    qs = service.admin_queryset(
        status=params.status,
        priority=params.priority,
        ticket_type=params.type,
        assignee=params.assignee,
        older_than_hours=params.older_than_hours,
        admin_id=actor.id,
        sort=sort,
    )
    rows, total = slice_page(qs, params)
    return AdminTicketPage(
        items=[_item(t) for t in rows], total=total, limit=params.limit, offset=params.offset
    )


@router.get("/tickets/stats", response=StatsOut)
def admin_stats(request: HttpRequest, params: Query[StatsQuery]) -> StatsOut:
    actor = require_permission("tickets.view")(request)
    return StatsOut(**service.stats(actor, params.from_date, params.to_date))


@router.get("/tickets/{ticket_id}", response=AdminTicketDetail)
def admin_get(request: HttpRequest, ticket_id: uuid.UUID) -> AdminTicketDetail:
    require_permission("tickets.view")(request)
    return _detail(service.get_ticket(str(ticket_id)))


@router.post("/tickets/{ticket_id}/assign", response=AdminTicketDetail)
def admin_assign(
    request: HttpRequest, ticket_id: uuid.UUID, payload: AssignIn
) -> AdminTicketDetail:
    actor = require_permission("tickets.handle")(request)
    assignee = str(payload.assignee_id) if payload.assignee_id else None
    return _detail(service.assign(actor, str(ticket_id), assignee, payload.version))


@router.post("/tickets/{ticket_id}/status", response=AdminTicketDetail)
def admin_status(
    request: HttpRequest, ticket_id: uuid.UUID, payload: StatusIn
) -> AdminTicketDetail:
    actor = require_permission("tickets.handle")(request)
    return _detail(service.change_status(actor, str(ticket_id), payload.status, payload.version))


@router.post("/tickets/{ticket_id}/messages", response={201: AdminMessageOut})
def admin_message(
    request: HttpRequest, ticket_id: uuid.UUID, payload: AdminMessageIn
) -> Status[AdminMessageOut]:
    actor = require_permission("tickets.handle")(request)
    m = service.admin_message(actor, str(ticket_id), payload.body, payload.internal)
    return Status(
        201,
        AdminMessageOut(
            id=m.pk,
            author=_person(actor.user),
            is_internal=m.is_internal,
            body=m.body,
            attachments=[],
            created_at=m.created_at,
        ),
    )


# ---- review moderation


def _moderate(
    request: HttpRequest, review_id: uuid.UUID, action: str, reason: str
) -> dict[str, Any]:
    actor = require_permission("reviews.moderate")(request)
    review = moderation.moderate(actor, str(review_id), action, reason)
    return {
        "id": str(review.public_id),
        "status": review.status,
        **reviews.review_summary(review.pk),
    }


@router.post("/reviews/{review_id}/hide", response=dict[str, Any])
def hide_review(request: HttpRequest, review_id: uuid.UUID, payload: ModerateIn) -> dict[str, Any]:
    return _moderate(request, review_id, "hide", payload.reason)


@router.post("/reviews/{review_id}/restore", response=dict[str, Any])
def restore_review(
    request: HttpRequest, review_id: uuid.UUID, payload: ModerateIn
) -> dict[str, Any]:
    return _moderate(request, review_id, "restore", payload.reason)


@router.post("/reviews/{review_id}/remove", response=dict[str, Any])
def remove_review(
    request: HttpRequest, review_id: uuid.UUID, payload: ModerateIn
) -> dict[str, Any]:
    return _moderate(request, review_id, "remove", payload.reason)
