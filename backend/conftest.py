import pytest
from django.core.cache import cache


@pytest.fixture(autouse=True)
def _flush_redis() -> None:
    cache.clear()
