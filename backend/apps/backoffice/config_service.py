"""Runtime reads of platform settings: cache -> database -> code default.

Other apps call `config_service.get("key")`. A database or cache outage never breaks a read:
the code default is used (and a warning is logged).
"""

import logging
from typing import Any

from django.core.cache import cache
from django.db import DatabaseError, transaction

from .config_registry import definition

logger = logging.getLogger(__name__)
CACHE_PREFIX = "platform-config:v1:"
CACHE_TTL = 300


def _cache_key(key: str) -> str:
    return CACHE_PREFIX + key


def get(key: str) -> Any:
    """The effective value of a setting (validated; falls back to the default if invalid)."""
    spec = definition(key)  # unknown keys raise UnknownSetting: a programming error
    try:
        hit = cache.get(_cache_key(key))
    except Exception:
        hit = None
    if hit is not None:
        return hit["v"]
    from .models import PlatformSetting

    try:
        row = PlatformSetting.objects.filter(key=key).first()
    except DatabaseError:
        logger.warning("Platform settings unavailable; using the default", extra={"setting": key})
        return spec.default()
    value = spec.default()
    if row is not None:
        try:
            value = spec.adapter.dump_python(spec.adapter.validate_python(row.value), mode="json")
        except ValueError:
            logger.error("Stored setting is invalid; using the default", extra={"setting": key})
    try:
        cache.set(_cache_key(key), {"v": value}, timeout=CACHE_TTL)
    except Exception:
        logger.warning("Could not cache a platform setting", extra={"setting": key})
    return value


def version_of(key: str) -> int:
    """Stored version (0 while the key still uses its default)."""
    from .models import PlatformSetting

    return int(
        PlatformSetting.objects.filter(key=key).values_list("version", flat=True).first() or 0
    )


def invalidate(key: str) -> None:
    """Drop the cached value now and again after commit."""
    try:
        cache.delete(_cache_key(key))
        transaction.on_commit(lambda: cache.delete(_cache_key(key)))
    except Exception:
        logger.warning("Could not invalidate a platform setting", extra={"setting": key})
