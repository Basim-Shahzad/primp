import threading
from datetime import timedelta
from unittest import mock

import pytest
import time_machine
from django.db import connection
from django.test import Client
from redis.exceptions import ConnectionError as RedisConnectionError

from apps.accounts.factories import UserFactory
from apps.accounts.models import Role
from apps.accounts.tests.helpers import bearer, login_client
from apps.bookings import holds
from apps.bookings.factories import AppointmentFactory
from apps.bookings.models import (
    Appointment,
    AppointmentEvent,
    AppointmentService,
    AppointmentStatus,
    PaymentMethod,
    PaymentStatus,
)
from apps.bookings.tests.api_helpers import (
    BOOK,
    book,
    capacity_business,
    customer_session,
    place_hold,
    post,
    staff_business,
)
from apps.bookings.tests.helpers import NOW, TUESDAY, local
from apps.businesses.factories import ServiceFactory

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("frozen")]


def hold_token(headers, b, services, hour=12, **extra):  # type: ignore[no-untyped-def]
    r = place_hold(headers, b, services, hour, **extra)
    assert r.status_code == 201, r.content
    return r.json()["token"]


# ---------------- success paths ----------------


def test_instant_booking_confirmed_with_full_db_state(run_commit, captured_events) -> None:  # type: ignore[no-untyped-def]
    b, svc = capacity_business(confirm_mode="instant")
    second = ServiceFactory(business=b, duration_minutes=30, price=500, name="Add-on")
    user, h = customer_session()
    token = hold_token(h, b, [svc, second])
    with run_commit():
        r = book(h, token, "key-1")
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "confirmed" and body["total_price"] == 2500
    assert (
        body["start"] == "2026-11-03T12:00:00+05:00" and body["end"] == "2026-11-03T13:30:00+05:00"
    )
    assert body["payment_method"] == "pay_at_venue" and body["payment_status"] == "unpaid"
    assert body["business"]["slug"] == b.slug and body["staff"] is None
    assert [s["name"] for s in body["services"]] == [svc.name, "Add-on"]
    appt = Appointment.objects.get()
    assert appt.customer_id == user.pk and appt.status == AppointmentStatus.CONFIRMED
    assert appt.source == "online" and appt.idempotency_key == "key-1" and appt.public_id
    assert (
        appt.payment_method == PaymentMethod.PAY_AT_VENUE
        and appt.payment_status == PaymentStatus.UNPAID
    )
    assert AppointmentService.objects.filter(appointment=appt).count() == 2
    ev = AppointmentEvent.objects.get()
    assert (ev.from_status, ev.to_status, ev.actor_id, ev.actor_role) == (
        "",
        "confirmed",
        user.pk,
        "customer",
    )
    assert holds.get_hold(token) is None  # hold released
    assert [(n, a) for n, a, _ in captured_events] == [
        ("booking_created", appt.pk),
        ("booking_confirmed", appt.pk),
    ]


def test_manual_confirm_mode_creates_pending(run_commit, captured_events) -> None:  # type: ignore[no-untyped-def]
    b, svc = capacity_business(confirm_mode="manual")
    _, h = customer_session()
    with run_commit():
        r = book(h, hold_token(h, b, [svc]))
    assert r.json()["status"] == "pending"
    assert [n for n, _, _ in captured_events] == ["booking_created"]
    assert AppointmentEvent.objects.get().to_status == "pending"


def test_events_fire_only_after_commit(django_capture_on_commit_callbacks, captured_events) -> None:  # type: ignore[no-untyped-def]
    b, svc = capacity_business()
    _, h = customer_session()
    token = hold_token(h, b, [svc])
    with django_capture_on_commit_callbacks(execute=False) as callbacks:
        book(h, token)
        assert captured_events == []  # nothing yet: still inside the transaction
    assert callbacks
    for cb in callbacks:
        cb()
    assert [n for n, _, _ in captured_events] == ["booking_created", "booking_confirmed"]


def test_staff_mode_booking_assigns_staff_and_blocks_only_that_staff() -> None:
    b, svc, (alice, bob) = staff_business()
    _, h1 = customer_session()
    _, h2 = customer_session()
    first = book(h1, hold_token(h1, b, [svc], staff_id=alice.pk))
    assert first.json()["staff"] == {"id": alice.pk, "name": alice.name}
    second = book(h2, hold_token(h2, b, [svc]))
    assert second.json()["staff"]["id"] == bob.pk
    assert Appointment.objects.count() == 2


def test_price_snapshot_unaffected_by_later_price_change() -> None:
    b, svc = capacity_business()
    _, h = customer_session()
    r = book(h, hold_token(h, b, [svc]))
    svc.price = 9999
    svc.duration_minutes = 15
    svc.save()
    item = AppointmentService.objects.get()
    assert (item.price_at_booking, item.duration_at_booking, item.name) == (2000, 60, svc.name)
    appt = Appointment.objects.get()
    assert appt.total_price == 2000 and appt.scheduled_end - appt.scheduled_start == timedelta(
        hours=1
    )
    assert r.json()["total_price"] == 2000


def test_booked_slot_disappears_from_availability_and_next_slot_cache_cleared() -> None:
    from apps.bookings.availability import find_slot
    from apps.businesses import selectors as biz

    b, svc = capacity_business()
    _, h = customer_session()
    cfg = biz.get_booking_config_by_id(b.pk)
    assert cfg and find_slot(cfg, local(TUESDAY, 12), [svc.pk], now=NOW)
    book(h, hold_token(h, b, [svc]))
    assert find_slot(cfg, local(TUESDAY, 12), [svc.pk], now=NOW) is None


# ---------------- idempotency ----------------


def test_idempotent_retry_returns_same_booking() -> None:
    b, svc = capacity_business()
    _, h = customer_session()
    token = hold_token(h, b, [svc])
    first = book(h, token, "retry-key")
    second = book(h, token, "retry-key")  # hold is gone, key still resolves
    assert (first.status_code, second.status_code) == (201, 200)
    assert first.json()["id"] == second.json()["id"]
    assert Appointment.objects.count() == 1
    assert AppointmentEvent.objects.count() == 1


def test_same_key_different_customers_are_independent() -> None:
    b, svc = capacity_business()
    _, h1 = customer_session()
    _, h2 = customer_session()
    r1 = book(h1, hold_token(h1, b, [svc], hour=10), "same")
    r2 = book(h2, hold_token(h2, b, [svc], hour=14), "same")
    assert r1.status_code == r2.status_code == 201 and r1.json()["id"] != r2.json()["id"]


@pytest.mark.parametrize("key", ["", "a" * 65, "has space", "bad/slash", "emoji🙂"])
def test_invalid_idempotency_key_422(key: str) -> None:
    b, svc = capacity_business()
    _, h = customer_session()
    token = hold_token(h, b, [svc])
    r = post(BOOK, {"hold_token": token}, h, HTTP_IDEMPOTENCY_KEY=key)
    assert r.status_code == 422 and Appointment.objects.count() == 0


def test_idempotency_key_boundary_64_chars() -> None:
    b, svc = capacity_business()
    _, h = customer_session()
    assert book(h, hold_token(h, b, [svc]), "k" * 64).status_code == 201


def test_booking_without_key_still_works_and_cannot_be_replayed() -> None:
    b, svc = capacity_business()
    _, h = customer_session()
    token = hold_token(h, b, [svc])
    assert book(h, token).status_code == 201
    r = book(h, token)
    assert r.status_code == 409 and r.json()["code"] == "hold_expired"
    assert Appointment.objects.count() == 1


@pytest.mark.django_db(transaction=True)
def test_concurrent_requests_with_same_key_create_one_booking() -> None:
    b, svc = capacity_business()
    _, h = customer_session()
    token = hold_token(h, b, [svc])
    results: list[tuple[int, str]] = []
    barrier = threading.Barrier(5)

    def worker() -> None:
        try:
            barrier.wait()
            r = book(h, token, "race")
            results.append((r.status_code, r.json().get("id", r.json().get("code"))))
        finally:
            connection.close()

    with time_machine.travel(NOW, tick=False):
        threads = [threading.Thread(target=worker) for _ in range(5)]
        [t.start() for t in threads]
        [t.join() for t in threads]
    assert Appointment.objects.count() == 1
    ids = {r[1] for r in results if r[0] in (200, 201)}
    assert len(ids) == 1 and any(r[0] == 201 for r in results)


# ---------------- hold validation at booking time ----------------


def test_book_with_unknown_or_garbage_token_409() -> None:
    _, h = customer_session()
    for token in ("nope", "soft.garbage", "soft."):
        r = book(h, token)
        assert r.status_code == 409 and r.json()["code"] == "hold_expired"


def test_book_hold_owned_by_another_user_404() -> None:
    b, svc = capacity_business()
    _, owner = customer_session()
    _, thief = customer_session()
    token = hold_token(owner, b, [svc])
    r = book(thief, token)
    assert r.status_code == 404
    assert Appointment.objects.count() == 0 and holds.get_hold(token) is not None


def test_expired_hold_rejected() -> None:
    b, svc = capacity_business()
    _, h = customer_session()
    token = hold_token(h, b, [svc])
    with time_machine.travel(NOW + timedelta(seconds=301), tick=False):
        r = book(h, token)
    assert r.status_code == 409 and r.json()["code"] == "hold_expired"


def test_hold_valid_until_just_before_expiry() -> None:
    b, svc = capacity_business()
    _, h = customer_session()
    token = hold_token(h, b, [svc])
    with time_machine.travel(NOW + timedelta(seconds=299), tick=False):
        assert book(h, token).status_code == 201


def test_hold_released_by_user_cannot_be_booked() -> None:
    b, svc = capacity_business()
    _, h = customer_session()
    token = hold_token(h, b, [svc])
    Client().delete(f"/api/v1/appointments/hold/{token}", **h)
    assert book(h, token).status_code == 409


def test_slot_taken_between_hold_and_booking_409() -> None:
    b, svc = capacity_business()
    _, h = customer_session()
    token = hold_token(h, b, [svc])
    AppointmentFactory(
        business=b, scheduled_start=local(TUESDAY, 12), scheduled_end=local(TUESDAY, 13)
    )
    r = book(h, token)
    assert r.status_code == 409 and r.json()["code"] == "slot_unavailable"
    assert Appointment.objects.count() == 1


def test_service_deactivated_or_business_unlisted_after_hold() -> None:
    b, svc = capacity_business()
    _, h = customer_session()
    token = hold_token(h, b, [svc])
    svc.is_active = False
    svc.save()
    r = book(h, token)
    assert r.status_code == 422 and r.json()["code"] == "invalid_services"
    svc.is_active = True
    svc.save()
    b.is_listed = False
    b.save()
    assert book(h, token).status_code == 404


def test_closure_added_after_hold_blocks_booking() -> None:
    from apps.businesses.factories import ClosureFactory

    b, svc = capacity_business()
    _, h = customer_session()
    token = hold_token(h, b, [svc])
    ClosureFactory(business=b, starts_at=local(TUESDAY, 11), ends_at=local(TUESDAY, 14))
    assert book(h, token).status_code == 409


def test_book_validation_and_auth() -> None:
    _, h = customer_session()
    assert post(BOOK, {}, h).status_code == 422
    assert post(BOOK, {"hold_token": ""}, h).status_code == 422
    assert post(BOOK, {"hold_token": "x"}, {}).status_code == 401
    owner = UserFactory(role=Role.ADMIN)
    _, session = login_client(owner)
    assert post(BOOK, {"hold_token": "x"}, bearer(session)).status_code == 403


# ---------------- Redis outage ----------------


def test_redis_outage_uses_soft_holds_and_db_constraint(
    monkeypatch: pytest.MonkeyPatch, caplog
) -> None:  # type: ignore[no-untyped-def]
    b, svc, (alice,) = staff_business(staff_count=1)
    _, h1 = customer_session()
    _, h2 = customer_session()

    def boom() -> None:
        raise RedisConnectionError("down")

    monkeypatch.setattr(holds, "_conn", boom)
    t1 = hold_token(h1, b, [svc])
    t2 = hold_token(h2, b, [svc])  # no Redis -> both get soft holds for the same slot
    assert t1.startswith("soft.") and t2.startswith("soft.")
    assert book(h1, t1).status_code == 201
    r = book(h2, t2)
    assert r.status_code == 409 and r.json()["code"] == "slot_unavailable"  # DB constraint wins
    assert Appointment.objects.filter(staff=alice).count() == 1
    assert "soft hold" in caplog.text


def test_soft_hold_belongs_to_its_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    b, svc = capacity_business()
    _, h1 = customer_session()
    _, h2 = customer_session()

    def boom() -> None:
        raise RedisConnectionError("down")

    monkeypatch.setattr(holds, "_conn", boom)
    token = hold_token(h1, b, [svc])
    assert book(h2, token).status_code == 404


def test_soft_hold_expires_with_signature_age() -> None:
    b, svc = capacity_business()
    _, h = customer_session()
    with mock.patch.object(holds, "_conn", side_effect=RedisConnectionError("down")):
        token = hold_token(h, b, [svc])
        with time_machine.travel(NOW + timedelta(seconds=400), tick=False):
            assert book(h, token).status_code == 409


def test_real_token_with_redis_down_is_503(monkeypatch: pytest.MonkeyPatch) -> None:
    b, svc = capacity_business()
    _, h = customer_session()
    token = hold_token(h, b, [svc])

    def boom() -> None:
        raise RedisConnectionError("down")

    monkeypatch.setattr(holds, "_conn", boom)
    r = book(h, token)
    assert r.status_code == 503 and r.json()["code"] == "holds_unavailable"


def test_booking_succeeds_even_if_redis_dies_after_creation(
    monkeypatch: pytest.MonkeyPatch, caplog
) -> None:  # type: ignore[no-untyped-def]
    b, svc = capacity_business()
    _, h = customer_session()
    token = hold_token(h, b, [svc])
    real = holds.release_hold

    def failing_release(t: str) -> bool:
        raise RedisConnectionError("gone")

    monkeypatch.setattr(holds, "release_hold", failing_release)
    assert book(h, token).status_code == 201
    assert "Redis unavailable while releasing" in caplog.text
    monkeypatch.setattr(holds, "release_hold", real)


# ---------------- concurrency: real parallel transactions ----------------


def _race(n: int, run) -> list[int]:  # type: ignore[no-untyped-def]
    results: list[int] = []
    barrier = threading.Barrier(n)

    def worker(i: int) -> None:
        try:
            barrier.wait()
            results.append(run(i))
        finally:
            connection.close()

    with time_machine.travel(NOW, tick=False):
        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
        [t.start() for t in threads]
        [t.join() for t in threads]
    return results


@pytest.mark.django_db(transaction=True)
def test_parallel_hold_race_only_one_hold_wins() -> None:
    b, svc = capacity_business()
    sessions = [customer_session()[1] for _ in range(8)]
    codes = _race(8, lambda i: place_hold(sessions[i], b, [svc]).status_code)
    assert sorted(codes) == [201] + [409] * 7


@pytest.mark.django_db(transaction=True)
def test_parallel_double_booking_staff_mode_exactly_one_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Redis is 'down' so every user holds the same slot; only the DB can stop double booking."""
    b, svc, (alice,) = staff_business(staff_count=1)
    sessions = [customer_session()[1] for _ in range(8)]

    def boom() -> None:
        raise RedisConnectionError("down")

    monkeypatch.setattr(holds, "_conn", boom)
    tokens = [hold_token(h, b, [svc]) for h in sessions]
    codes = _race(8, lambda i: book(sessions[i], tokens[i]).status_code)
    assert sorted(codes) == [201] + [409] * 7
    assert Appointment.objects.filter(staff=alice, status="confirmed").count() == 1


@pytest.mark.django_db(transaction=True)
def test_parallel_double_booking_any_staff_each_gets_distinct_staff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    b, svc, staff = staff_business(staff_count=3)
    sessions = [customer_session()[1] for _ in range(6)]

    def boom() -> None:
        raise RedisConnectionError("down")

    monkeypatch.setattr(holds, "_conn", boom)
    # soft holds all pick the same candidate (no Redis state), so bookings contend on one staff
    tokens = [hold_token(h, b, [svc]) for h in sessions]
    codes = _race(6, lambda i: book(sessions[i], tokens[i]).status_code)
    assert codes.count(201) == 1 and codes.count(409) == 5
    assert Appointment.objects.filter(staff__in=staff).count() == 1


@pytest.mark.django_db(transaction=True)
def test_parallel_booking_capacity_mode_respects_parallel_capacity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    b, svc = capacity_business(parallel_capacity=2)
    sessions = [customer_session()[1] for _ in range(6)]

    def boom() -> None:
        raise RedisConnectionError("down")

    monkeypatch.setattr(holds, "_conn", boom)
    tokens = [hold_token(h, b, [svc]) for h in sessions]
    codes = _race(6, lambda i: book(sessions[i], tokens[i]).status_code)
    assert codes.count(201) == 2 and codes.count(409) == 4
    assert Appointment.objects.filter(business=b).count() == 2


@pytest.mark.django_db(transaction=True)
def test_parallel_hold_then_book_full_flow_single_winner() -> None:
    b, svc, _ = staff_business(staff_count=1)
    sessions = [customer_session()[1] for _ in range(6)]

    def attempt(i: int) -> int:
        r = place_hold(sessions[i], b, [svc])
        if r.status_code != 201:
            return r.status_code
        return book(sessions[i], r.json()["token"]).status_code

    codes = _race(6, attempt)
    assert codes.count(201) == 1 and set(codes) <= {201, 409}
    assert Appointment.objects.count() == 1


def test_exclusion_constraint_blocks_overlap_at_database_level() -> None:
    from django.db import IntegrityError, transaction

    b, _, (alice,) = staff_business(staff_count=1)
    AppointmentFactory(
        business=b,
        staff=alice,
        scheduled_start=local(TUESDAY, 12),
        scheduled_end=local(TUESDAY, 13),
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        AppointmentFactory(
            business=b,
            staff=alice,
            scheduled_start=local(TUESDAY, 12, 30),
            scheduled_end=local(TUESDAY, 13, 30),
        )
    # adjacent, cancelled and other-staff bookings are fine
    AppointmentFactory(
        business=b,
        staff=alice,
        scheduled_start=local(TUESDAY, 13),
        scheduled_end=local(TUESDAY, 14),
    )
    AppointmentFactory(
        business=b,
        staff=alice,
        status="cancelled_by_customer",
        scheduled_start=local(TUESDAY, 12),
        scheduled_end=local(TUESDAY, 13),
    )
    AppointmentFactory(
        business=b, staff=None, scheduled_start=local(TUESDAY, 12), scheduled_end=local(TUESDAY, 13)
    )
