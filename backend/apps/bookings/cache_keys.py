from django.core.cache import cache


def next_slot_key(business_id: int) -> str:
    return f"nextslot:{business_id}"


def invalidate_next_slot(business_id: int) -> None:
    cache.delete(next_slot_key(business_id))
