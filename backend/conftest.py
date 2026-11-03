import pytest
from django.core.cache import cache


@pytest.fixture(autouse=True)
def _flush_redis() -> None:
    cache.clear()


@pytest.fixture(autouse=True)
def _reset_fake_sms() -> None:
    from apps.notifications.adapters import FakeSMS

    FakeSMS.reset()


@pytest.fixture
def run_commit(django_capture_on_commit_callbacks):  # type: ignore[no-untyped-def]
    """Context manager that runs transaction.on_commit callbacks when the block exits."""
    return lambda: django_capture_on_commit_callbacks(execute=True)
