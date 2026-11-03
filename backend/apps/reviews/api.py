import uuid

from django.http import HttpRequest
from ninja import Query, Router, Schema
from pydantic import Field

from apps.accounts import selectors as accounts
from apps.businesses import selectors as businesses
from apps.core.errors import not_found
from apps.core.pagination import DEFAULT_LIMIT, MAX_LIMIT, paginate_cursor

from . import selectors
from .models import Review
from .schemas import ReplyOut, ReviewOut, ReviewPageOut

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
