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
    """Context manager that runs transaction.on_commit callbacks when the block exits.

    Callbacks that register further callbacks (events -> notifications -> tasks) are drained
    until nothing is pending, like a real commit would.
    """
    from contextlib import contextmanager

    @contextmanager
    def runner():  # type: ignore[no-untyped-def]
        with django_capture_on_commit_callbacks(execute=False) as callbacks:
            yield
        pending = list(callbacks)
        while pending:
            following: list = []
            for callback in pending:
                with django_capture_on_commit_callbacks(execute=False) as more:
                    callback()
                following.extend(more)
            pending = following

    return runner


@pytest.fixture(autouse=True)
def _reset_fake_gateway() -> None:
    from apps.payments.gateways import FakeGateway

    FakeGateway.reset()


@pytest.fixture
def captured_events():  # type: ignore[no-untyped-def]
    """Collects (signal name, appointment_id, extra kwargs) for every booking event."""
    from apps.bookings import events

    received: list[tuple[str, int, dict[str, object]]] = []
    handlers = []
    for name in (
        "booking_created",
        "booking_confirmed",
        "booking_completed",
        "booking_cancelled",
        "booking_no_show",
        "booking_rescheduled",
        "booking_reschedule_requested",
        "booking_reschedule_declined",
        "booking_payment_changed",
    ):
        signal = getattr(events, name)

        def handler(sender, appointment_id, _name=name, **kwargs):  # type: ignore[no-untyped-def]
            received.append((_name, appointment_id, kwargs))

        signal.connect(handler, weak=False)
        handlers.append((signal, handler))
    yield received
    for signal, handler in handlers:
        signal.disconnect(handler)


@pytest.fixture
def frozen():  # type: ignore[no-untyped-def]
    """Freeze the clock at Monday 2026-11-02 09:00 Asia/Karachi."""
    import time_machine

    from apps.bookings.tests.helpers import NOW

    with time_machine.travel(NOW, tick=False) as t:
        yield t


@pytest.fixture
def long_tokens(settings):  # type: ignore[no-untyped-def]
    """Tests that travel through time keep their access tokens valid across the jumps."""
    from datetime import timedelta

    settings.ACCESS_TOKEN_LIFETIME = timedelta(days=3)
    settings.ADMIN_ACCESS_TOKEN_LIFETIME = timedelta(days=3)
