import uuid
from typing import Any

from django.http import HttpRequest
from ninja import File, Form, Query, Router, Schema, Status, UploadedFile
from pydantic import Field

from apps.accounts import selectors as accounts
from apps.accounts.auth import jwt_auth
from apps.accounts.roles import Role
from apps.businesses import selectors as businesses
from apps.core.errors import not_found
from apps.core.pagination import DEFAULT_LIMIT, MAX_LIMIT, paginate_cursor
from apps.core.permissions import require_role

from . import selectors, services
from .models import Review
from .schemas import (
    FlagIn,
    FlagOut,
    MyReviewOut,
    ReplyOut,
    ReviewCreateIn,
    ReviewEditIn,
    ReviewOut,
    ReviewPageOut,
)

businesses_router = Router(tags=["reviews"])


class ReviewListParams(Schema):
    rating: int | None = Field(None, ge=1, le=5)
    cursor: str | None = None
    limit: int = Field(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT)


def review_out(review: Review, names: dict[int, str]) -> ReviewOut:
    reply = getattr(review, "reply", None)
    return ReviewOut(
        id=review.public_id,
        rating=review.rating,
        comment=review.comment,
        photos=selectors.photo_urls(review),
        author_name=names.get(review.customer_id, "Customer"),
        created_at=review.created_at,
        edited_at=review.edited_at,
        reply=ReplyOut(body=reply.body, created_at=reply.created_at) if reply else None,
    )


@businesses_router.get("/{business_id}/reviews", auth=None, response=ReviewPageOut)
def list_reviews(
    request: HttpRequest, business_id: uuid.UUID, params: Query[ReviewListParams]
) -> ReviewPageOut:
    config = businesses.get_booking_config(business_id)
    if config is None:
        raise not_found()
    qs = selectors.visible_reviews(config.id).select_related("reply")
    if params.rating is not None:
        qs = qs.filter(rating=params.rating)
    page = paginate_cursor(
        qs, ordering=["-created_at", "-id"], cursor=params.cursor, limit=params.limit
    )
    names = accounts.display_names([r.customer_id for r in page.items])
    return ReviewPageOut(
        items=[review_out(r, names) for r in page.items], next_cursor=page.next_cursor
    )


appointments_router = Router(tags=["reviews"])
reviews_router = Router(tags=["reviews"])


def my_review_out(review: Review) -> MyReviewOut:
    return MyReviewOut(
        id=review.public_id,
        appointment_id=review.appointment.public_id,
        business_id=review.business.public_id,
        rating=review.rating,
        comment=review.comment,
        photos=selectors.photo_urls(review),
        status=review.status,
        created_at=review.created_at,
        edited_at=review.edited_at,
        can_edit_until=review.created_at + services.EDIT_WINDOW,
    )


def _load(review_id: int) -> Review:
    return Review.objects.select_related("appointment", "business").get(pk=review_id)


@appointments_router.post("/{appointment_id}/review", auth=jwt_auth, response={201: MyReviewOut})
def create_review(
    request: HttpRequest,
    appointment_id: uuid.UUID,
    payload: Form[ReviewCreateIn],
    photos: File[list[UploadedFile]] = None,  # type: ignore[assignment]
) -> Any:
    user = require_role(Role.CUSTOMER)(request)
    review = services.create_review(
        user_id=user.pk,
        appointment_public_id=str(appointment_id),
        rating=payload.rating,
        comment=payload.comment,
        photos=[f.read() for f in photos or []],
    )
    return Status(201, my_review_out(_load(review.pk)))


@reviews_router.patch("/{review_id}", auth=jwt_auth, response=MyReviewOut)
def edit_review(request: HttpRequest, review_id: uuid.UUID, payload: ReviewEditIn) -> MyReviewOut:
    user = require_role(Role.CUSTOMER)(request)
    review = services.edit_review(
        user_id=user.pk,
        review_public_id=str(review_id),
        rating=payload.rating,
        comment=payload.comment,
    )
    return my_review_out(_load(review.pk))


@reviews_router.post("/{review_id}/flag", auth=jwt_auth, response={201: FlagOut})
def flag_review(request: HttpRequest, review_id: uuid.UUID, payload: FlagIn) -> Any:
    flag = services.flag_review(
        user_id=request.auth.pk,  # type: ignore[attr-defined]
        review_public_id=str(review_id),
        reason=payload.reason,
    )
    return Status(201, FlagOut(id=flag.pk, reason=flag.reason))
