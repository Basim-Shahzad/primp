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
