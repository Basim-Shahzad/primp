"""Cache for the public business page plus optional frontend revalidation webhook."""

from typing import Any

from django.core.cache import cache
from django.db import transaction

PAGE_TTL_SECONDS = 600


def page_key(business_id: int) -> str:
    return f"bizpage:{business_id}"


def get(business_id: int) -> dict[str, Any] | None:
    value: dict[str, Any] | None = cache.get(page_key(business_id))
    return value


def put(business_id: int, payload: dict[str, Any]) -> None:
    cache.set(page_key(business_id), payload, timeout=PAGE_TTL_SECONDS)


def invalidate(business_id: int) -> None:
    """Drop the cached page now and tell the frontend to revalidate once the change commits."""
    cache.delete(page_key(business_id))
    from .tasks import revalidate_frontend

    transaction.on_commit(lambda: revalidate_frontend.delay(business_id))
