"""Fixed-window rate limiting on the Redis-backed cache, usable from services."""

from django.core.cache import cache
from django.utils.translation import gettext_lazy as _

from .errors import AppError


def hit(key: str, *, limit: int, window_seconds: int) -> bool:
    """Count a hit. Returns False when the limit is already exceeded."""
    cache_key = f"rl:{key}"
    if cache.add(cache_key, 1, timeout=window_seconds):
        return True
    try:
        count = cache.incr(cache_key)
    except ValueError:  # expired between add and incr
        cache.add(cache_key, 1, timeout=window_seconds)
        return True
    return int(count) <= limit


def enforce(key: str, *, limit: int, window_seconds: int) -> None:
    if not hit(key, limit=limit, window_seconds=window_seconds):
        raise AppError("throttled", _("Too many requests. Please try again later."), 429)
