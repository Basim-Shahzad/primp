"""Admin user management endpoints."""

import uuid
from typing import Any

from django.core.exceptions import ObjectDoesNotExist
from django.http import HttpRequest
from ninja import Query, Router, Status

from apps.accounts import adminops as accounts
from apps.accounts import selectors as account_selectors
from apps.accounts.profile import photo_url
from apps.bookings import adminops as bookings
from apps.loyalty import adminops as loyalty
from apps.reviews import selectors as reviews

from .pagination import check_sort, slice_page
from .permissions import admin_auth, require_permission
from .schemas import ReasonIn
from .schemas_users import (
    AdminCreateIn,
    AdminGroupIn,
    AdminOut,
    CountOut,
    DeletionItem,
    DeletionPage,
    DeletionQuery,
    DeletionRow,
    LoginRow,
    SuspensionOut,
    UserDetail,
    UserListItem,
    UserPage,
    UserQuery,
)
from .services import users as service

router = Router(tags=["admin"], auth=admin_auth)


def _name(user: Any) -> str:
    try:
        return str(user.profile.name)  # loaded with select_related in the list queries
    except ObjectDoesNotExist:
        return ""


def item(user: Any) -> UserListItem:
    return UserListItem(
        id=user.public_id,
        email=user.email,
        phone=user.phone,
        name=_name(user),
        role=user.role,
        status=user.status,
        is_email_verified=user.is_email_verified,
        is_phone_verified=user.is_phone_verified,
        created_at=user.created_at,
    )


@router.get("/users", response=UserPage)
def list_users(request: HttpRequest, params: Query[UserQuery]) -> UserPage:
    require_permission("users.view")(request)
    sort = check_sort(params.sort, accounts.USER_SORTS)
    qs = accounts.search_users(
        q=params.q,
        role=params.role,
        status=params.status,
        phone_verified=params.phone_verified,
        email_verified=params.email_verified,
        created_from=params.created_from,
        created_to=params.created_to,
        sort=sort,
    )
    rows, total = slice_page(qs, params)
    return UserPage(
        items=[item(u) for u in rows], total=total, limit=params.limit, offset=params.offset
    )


def detail_of(user: Any) -> UserDetail:
    profile = account_selectors.profile_of(user)
    booking = bookings.customer_bookings(user.pk)
    suspension = service.current_suspension(user.pk)
    return UserDetail(
        **item(user).model_dump(),
        photo_url=photo_url(profile),
        no_show_count=profile.no_show_count if profile else 0,
        bookings_by_status=booking.by_status,
        recent_bookings=booking.recent,
        reviews=reviews.customer_reviews(user.pk),
        points=loyalty.points_overview(user.pk),
        tickets=[],
        login_history=[
            LoginRow(at=r.created_at, ip=r.ip_address, user_agent=r.user_agent)
            for r in accounts.login_history(user.pk)
        ],
        deletion_requests=[
            DeletionRow(id=d.pk, status=d.status, reason=d.reason, requested_at=d.created_at)
            for d in accounts.user_deletion_requests(user.pk)
        ],
        suspension=SuspensionOut(
            reason=suspension.reason,
            suspended_at=suspension.created_at,
            suspended_by=None,
        )
        if suspension
        else None,
    )


@router.get("/users/{user_id}", response=UserDetail)
def get_user(request: HttpRequest, user_id: uuid.UUID) -> UserDetail:
    require_permission("users.view")(request)
    return detail_of(accounts.get_user(user_id))


@router.post("/users/{user_id}/suspend", response=UserListItem)
def suspend_user(request: HttpRequest, user_id: uuid.UUID, payload: ReasonIn) -> UserListItem:
    actor = require_permission("users.suspend")(request)
    return item(service.suspend(actor, str(user_id), payload.reason))


@router.post("/users/{user_id}/reactivate", response=UserListItem)
def reactivate_user(request: HttpRequest, user_id: uuid.UUID, payload: ReasonIn) -> UserListItem:
    actor = require_permission("users.suspend")(request)
    return item(service.reactivate(actor, str(user_id), payload.reason))


@router.post("/users/{user_id}/force-logout", response=CountOut)
def force_logout(request: HttpRequest, user_id: uuid.UUID) -> CountOut:
    actor = require_permission("users.force_logout")(request)
    return CountOut(revoked_sessions=service.force_logout(actor, str(user_id)))


@router.post("/users/{user_id}/password-reset", response={202: dict[str, str]})
def password_reset(request: HttpRequest, user_id: uuid.UUID) -> Status[dict[str, str]]:
    actor = require_permission("users.reset_password")(request)
    service.send_password_reset(actor, str(user_id))
    return Status(202, {"detail": "Reset link sent."})


# ---- deletion requests


@router.get("/deletion-requests", response=DeletionPage)
def list_deletion_requests(request: HttpRequest, params: Query[DeletionQuery]) -> DeletionPage:
    require_permission("users.view")(request)
    rows, total = slice_page(accounts.deletion_requests(params.status), params)
    return DeletionPage(
        items=[
            DeletionItem(
                id=r.pk,
                user_id=r.user.public_id,
                user_name=_name(r.user),
                status=r.status,
                reason=r.reason,
                requested_at=r.created_at,
            )
            for r in rows
        ],
        total=total,
        limit=params.limit,
        offset=params.offset,
    )


@router.post("/deletion-requests/{request_id}/process", response={200: dict[str, str]})
def process_deletion(request: HttpRequest, request_id: int) -> dict[str, str]:
    actor = require_permission("users.suspend")(request)
    service.process_deletion(actor, request_id)
    return {"detail": "Account anonymised."}


# ---- admin accounts


def admin_out(user: Any) -> AdminOut:
    return AdminOut(
        id=user.public_id,
        email=user.email,
        name=_name(user),
        role=user.role,
        groups=accounts.group_names(user),
        status=user.status,
    )


@router.get("/admins", response=list[AdminOut])
def list_admins(request: HttpRequest) -> list[AdminOut]:
    require_permission("admins.manage")(request)
    return [admin_out(u) for u in accounts.list_admins()]


@router.post("/admins", response={201: AdminOut})
def create_admin(request: HttpRequest, payload: AdminCreateIn) -> Status[AdminOut]:
    actor = require_permission("admins.manage")(request)
    user = service.create_admin(actor, email=payload.email, name=payload.name, group=payload.group)
    return Status(201, admin_out(user))


@router.patch("/admins/{user_id}/groups", response=AdminOut)
def set_admin_group(request: HttpRequest, user_id: uuid.UUID, payload: AdminGroupIn) -> AdminOut:
    actor = require_permission("admins.manage")(request)
    return admin_out(service.set_admin_group(actor, str(user_id), payload.group))
