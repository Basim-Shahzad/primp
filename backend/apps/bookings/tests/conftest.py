import pytest
import time_machine

from apps.bookings.tests.helpers import NOW


@pytest.fixture
def frozen():  # type: ignore[no-untyped-def]
    with time_machine.travel(NOW, tick=False) as t:
        yield t
