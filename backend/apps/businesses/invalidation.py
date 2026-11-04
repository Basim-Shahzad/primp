"""One call that refreshes everything derived from a business's public listing."""

from . import pagecache


def touch(business_id: int) -> None:
    """Drop the cached public page, the cached next-slot value and ping frontend revalidation."""
    from apps.bookings.cache_keys import invalidate_next_slot

    pagecache.invalidate(business_id)
    invalidate_next_slot(business_id)
