"""Cached public taxonomy (invalidated by every admin change)."""

from typing import Any

from django.core.cache import cache
from django.db import transaction

KEY = "catalog:tree:v1"
TTL = 300


def get() -> list[dict[str, Any]] | None:
    value: list[dict[str, Any]] | None = cache.get(KEY)
    return value


def put(tree: list[dict[str, Any]]) -> None:
    cache.set(KEY, tree, timeout=TTL)


def invalidate() -> None:
    """Drop now and again after commit (a reader may repopulate before the change is visible)."""
    cache.delete(KEY)
    transaction.on_commit(lambda: cache.delete(KEY))
