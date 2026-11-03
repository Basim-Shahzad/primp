"""Customer wishlist: idempotent add/remove and a card list of still-visible businesses."""

import uuid
from datetime import datetime

from django.db import IntegrityError, transaction

from apps.businesses import search as business_search
from apps.businesses import selectors as businesses
from apps.core.errors import not_found
from apps.core.pagination import CursorPage, paginate_cursor

from .models import Wishlist


def add(user_id: int, business_public_id: uuid.UUID) -> bool:
    """Add a business. Returns True when newly added, False when it was already there."""
    business_id = businesses.public_business_id(business_public_id)
    if business_id is None:
        raise not_found()  # unknown, unlisted and hidden-city businesses cannot be saved
    try:
        with transaction.atomic():
            _, created = Wishlist.objects.get_or_create(user_id=user_id, business_id=business_id)
    except IntegrityError:  # lost a race with an identical request
        return False
    return created


def remove(user_id: int, business_public_id: uuid.UUID) -> None:
    business_id = businesses.any_business_id(business_public_id)
    if business_id is not None:
        Wishlist.objects.filter(user_id=user_id, business_id=business_id).delete()


def page(
    user_id: int, cursor: str | None, limit: int, now: datetime | None = None
) -> tuple[list[business_search.SearchItem], CursorPage[Wishlist]]:
    """Newest first. Businesses that are no longer public are skipped (the row is kept)."""
    qs = Wishlist.objects.filter(user_id=user_id)
    result = paginate_cursor(qs, ordering=["-created_at", "-id"], cursor=cursor, limit=limit)
    cards = business_search.cards_for([w.business_id for w in result.items], user_id, now)
    return cards, result
