from typing import Any

from django.http import HttpRequest
from ninja import Query, Router

from apps.accounts.auth import optional_user
from apps.core.errors import not_found

from . import page as page_service
from . import search as search_service
from . import selectors
from .schemas import BusinessPageOut, SearchItemOut, SearchOut, SuggestionOut

search_router = Router(tags=["search"])
businesses_router = Router(tags=["businesses"])


def item_out(item: search_service.SearchItem) -> SearchItemOut:
    b = item.business
    return SearchItemOut(
        id=b.public_id,
        slug=b.slug,
        name=b.name,
        area=b.area.name if b.area else None,
        distance_km=item.distance_km,
        rating_avg=float(b.rating_avg),
        rating_count=b.rating_count,
        starting_price=item.starting_price,
        is_verified=b.is_verified,
        cover_photo_url=item.cover_url,
        next_available_slot=item.next_slot,
        is_wishlisted=item.is_wishlisted,
    )


@search_router.get("/businesses", auth=None, response=SearchOut)
def search_businesses(
    request: HttpRequest, params: Query[search_service.SearchParams]
) -> SearchOut:
    user = optional_user(request)
    result = search_service.search(params, user.pk if user else None)
    return SearchOut(
        items=[item_out(i) for i in result.items],
        next_cursor=result.next_cursor,
        suggestions=[SuggestionOut(**s) for s in result.suggestions],
    )


@businesses_router.get("/{ref}", auth=None, response=BusinessPageOut)
def get_business(request: HttpRequest, ref: str, city: str | None = None) -> dict[str, Any]:
    business = selectors.get_public_business(ref, city)
    if business is None:
        raise not_found()
    return page_service.render_page(business)
